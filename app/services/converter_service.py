"""Converter: 여러 형식의 파일 → PDF.

- 오피스 문서 (docx · xlsx · pptx · odt · rtf · csv …): LibreOffice (사용자 프로필을 작업마다 새로, 외부 링크 · 매크로 · OLE/DDE 차단)
- 한글 hwpx: 직접 해석해 HTML 로 그린 뒤 Chrome 으로 인쇄 / hwp(5.0): LibreOffice → 안 되면 본문 글자만 뽑아서
- 마크다운 · 주피터 노트북 · HTML · EPUB · 텍스트/코드 · SVG: HTML 로 그려 헤드리스 Chrome --print-to-pdf (배율 · 가로 방향 조절 가능)
  마크다운은 UnivDash 파일 뷰어와 같은 렌더러(marked · DOMPurify · KaTeX · highlight.js)와 같은 스타일을 쓴다.
- 그림 (png · jpg · webp · tiff · heic …): A4 에 맞춰 넣는다 (img2pdf, 무손실) / PDF: 그대로

보안 (여러 사용자가 올리는 믿을 수 없는 파일):
- 결과는 data/converter/<사용자 해시>/<토큰>/ 에만 둔다 (static 밖, 소유자만 접근). 원본 이름은 표시용으로만 쓴다.
- Chrome 페이지는 모든 스크립트 · 스타일을 안에 넣고 CSP(default-src 'none', 스크립트는 nonce)로 파일 · 네트워크 접근을 막는다.
  사용자 내용은 DOMPurify 로 정리하고, 그림은 data: 만 허용한다. 외부 네트워크 이름 풀이도 막는다.
- LibreOffice · Chrome 은 서버 비밀값(.env)이 없는 최소 환경 변수로, 시간 제한을 두고 실행한다.
- 크기 · 개수 · 보관 기간 제한, 압축 폭탄 · 그림 폭탄 방지.
"""

import base64
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import warnings
import zipfile
from pathlib import Path, PurePosixPath

import img2pdf
from PIL import Image, ImageOps, ImageSequence

from app.core.config import settings
from app.services.hwp5_text import Hwp5Error, hwp5_to_html
from app.services.hwpx_render import HwpxError, hwpx_to_html

ROOT = Path(settings.BASE_DIR) / "data" / "converter"
ASSETS = Path(__file__).resolve().parent / "converter_assets"

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_USER_JOBS = 200
MAX_USER_BYTES = 2 * 1024 * 1024 * 1024
KEEP_SECONDS = 7 * 24 * 3600
ZOOM_RANGE = (0.4, 2.0)
TIMEOUT = 150
MAX_IMAGE_PIXELS = 120_000_000
MAX_IMAGE_FRAMES = 300
MAX_ZIP_MEMBER = 40 * 1024 * 1024
MAX_EPUB_TOTAL = 150 * 1024 * 1024

_TOKEN = re.compile(r"^[0-9a-f]{24}$")
_slots = threading.BoundedSemaphore(
    2
)  # 동시에 도는 변환 (LibreOffice · Chrome 은 무겁다)

# ── 지원 형식 ────────────────────────────────────────────────────────────────

FORMATS = [
    (
        "문서",
        "office",
        "doc docx docm dot dotx dotm odt ott fodt rtf wpd wps lwp abw sxw pages",
    ),
    ("한글", "hangul", "hwpx hwp"),
    (
        "스프레드시트",
        "office",
        "xls xlsx xlsm xlsb xlt xltx ods ots fods csv tsv numbers sxc",
    ),
    (
        "프레젠테이션",
        "office",
        "ppt pptx pptm pps ppsx pot potx odp otp fodp key sxi",
    ),
    ("그리기", "office", "odg otg fodg vsd vsdx pub cdr wmf emf"),
    ("마크다운 · 노트북", "web", "md markdown mdx rmd ipynb"),
    ("웹 · 전자책", "web", "html htm xhtml epub"),
    (
        "텍스트 · 코드",
        "text",
        "txt text log json jsonl yaml yml toml ini cfg conf env xml tex bib rst org "
        "py js mjs cjs ts tsx jsx java kt c h cpp hpp cc cs go rs rb php swift lua r pl sh bash zsh "
        "sql css scss less vue svelte dart scala hs ex exs erl clj diff patch gradle properties srt vtt",
    ),
    (
        "그림",
        "image",
        "png jpg jpeg jfif gif webp bmp tif tiff ico heic heif avif tga ppm pgm pbm svg",
    ),
    ("PDF", "pdf", "pdf"),
]
OFFICE_EXT = {
    f".{e}" for _, group, exts in FORMATS if group == "office" for e in exts.split()
}
TEXT_EXT = {
    f".{e}"
    for label, _, exts in FORMATS
    if label == "텍스트 · 코드"
    for e in exts.split()
}
MD_EXT = {".md", ".markdown", ".mdx", ".rmd"}
HTML_EXT = {".html", ".htm", ".xhtml"}
IMAGE_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".ico",
    ".heic",
    ".heif",
    ".avif",
    ".tga",
    ".ppm",
    ".pgm",
    ".pbm",
}
# 글자 배율을 HTML(CSS zoom)로 다시 그리는 형식. 나머지는 만든 PDF(base.pdf)의 쪽 내용을 확대 · 축소한다
HTML_ZOOM = {"md", "ipynb", "html", "epub", "text"}
_HLJS_LANG = {
    ".txt": "",
    ".text": "",
    ".log": "",
    ".srt": "",
    ".vtt": "",
    ".env": "bash",
}


class ConverterError(RuntimeError):
    pass


def formats() -> list[dict]:
    return [{"label": label, "exts": exts.split()} for label, _, exts in FORMATS]


def kind_of(name: str) -> str | None:
    lower = name.lower()
    ext = Path(lower).suffix
    if lower in {"dockerfile", "makefile"}:
        return "text"
    if ext == ".hwpx":
        return "hwpx"
    if ext == ".hwp":
        return "hwp"
    if ext in MD_EXT:
        return "md"
    if ext == ".ipynb":
        return "ipynb"
    if ext in HTML_EXT:
        return "html"
    if ext == ".epub":
        return "epub"
    if ext == ".svg":
        return "svg"
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in OFFICE_EXT:
        return "office"
    if ext in TEXT_EXT:
        return "text"
    return None


# ── 저장소 ──────────────────────────────────────────────────────────────────


def _user_dir(username: str) -> Path:
    digest = hashlib.sha256(f"lecai-converter:{username}".encode()).hexdigest()[:32]
    path = ROOT / digest
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _job_dir(username: str, token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise ConverterError("잘못된 요청입니다.")
    job = _user_dir(username) / token
    if not (job / "meta.json").is_file():
        raise FileNotFoundError(token)
    return job


def _meta(job: Path) -> dict:
    return json.loads((job / "meta.json").read_text(encoding="utf-8"))


def _write_meta(job: Path, meta: dict) -> None:
    tmp = job / f".meta.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, job / "meta.json")


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _prune(user_dir: Path) -> None:
    now = time.time()
    for job in user_dir.iterdir():
        try:
            if not job.is_dir():
                continue
            if not (job / "meta.json").is_file():
                # 실패 · 중단된 작업 찌꺼기 (변환 중일 수 있으니 한 시간 뒤에)
                if now - job.stat().st_mtime > 3600:
                    shutil.rmtree(job, ignore_errors=True)
            elif now - job.stat().st_mtime > KEEP_SECONDS:
                shutil.rmtree(job, ignore_errors=True)
        except OSError:
            continue


def _public(meta: dict) -> dict:
    keys = (
        "token",
        "name",
        "pdf_name",
        "kind",
        "ext",
        "pages",
        "size",
        "source_size",
        "zoom",
        "landscape",
        "zoomable",
        "orientable",
        "created",
        "note",
    )
    return {k: meta.get(k) for k in keys}


def list_jobs(username: str) -> list[dict]:
    user_dir = _user_dir(username)
    _prune(user_dir)
    jobs = []
    for job in user_dir.iterdir():
        try:
            jobs.append(_public(_meta(job)))
        except OSError, ValueError:
            continue
    jobs.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return jobs


def _display_name(name: str) -> str:
    name = PurePosixPath((name or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "file"
    return name[:180]


def pdf_name_for(name: str) -> str:
    stem = Path(name).stem if Path(name).suffix else name
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", stem).strip(" .") or "document"
    return f"{stem[:150]}.pdf"


def start_job(username: str, filename: str) -> tuple[Path, dict]:
    """업로드를 받기 전에 작업 폴더를 만든다 (용량 · 개수 확인)."""
    name = _display_name(filename)
    kind = kind_of(name)
    if not kind:
        ext = Path(name).suffix.lower() or name
        raise ConverterError(f"지원하지 않는 형식이에요 ({ext}).")
    user_dir = _user_dir(username)
    _prune(user_dir)
    jobs = [j for j in user_dir.iterdir() if j.is_dir()]
    if len(jobs) >= MAX_USER_JOBS:
        raise ConverterError(
            f"변환 결과가 너무 많아요 (최대 {MAX_USER_JOBS}개). 목록에서 지운 뒤 다시 해 주세요."
        )
    if _dir_size(user_dir) >= MAX_USER_BYTES:
        raise ConverterError(
            "저장 공간이 가득 찼어요 (2GB). 목록에서 지운 뒤 다시 해 주세요."
        )
    token = secrets.token_hex(12)
    job = user_dir / token
    job.mkdir(mode=0o700)
    ext = Path(name).suffix.lower() or ".txt"
    meta = {
        "token": token,
        "name": name,
        "pdf_name": pdf_name_for(name),
        "kind": kind,
        "ext": ext.lstrip("."),
        "source": f"source{ext}",
        "zoomable": True,
        "orientable": kind in HTML_ZOOM or kind == "svg",
        "zoom": 1.0,
        "landscape": False,
        "created": time.time(),
    }
    return job, meta


def discard(job: Path) -> None:
    shutil.rmtree(job, ignore_errors=True)


def finish_job(job: Path, meta: dict, zoom: float, landscape: bool) -> dict:
    """원본(job/source.*)이 저장된 뒤 변환한다. 실패하면 작업 폴더를 지운다."""
    source = job / meta["source"]
    meta["source_size"] = source.stat().st_size
    try:
        _convert(job, meta, zoom, landscape)
    except BaseException:
        discard(job)
        raise
    _write_meta(job, meta)
    return _public(meta)


def rerender(username: str, token: str, zoom: float, landscape: bool) -> dict:
    job = _job_dir(username, token)
    meta = _meta(job)
    zoom = _clamp_zoom(zoom)
    landscape = bool(landscape) and bool(meta.get("orientable"))
    base = job / "base.pdf"
    if meta.get("mode") == "pdf" and not base.is_file() and meta.get("zoom", 1) == 1:
        shutil.copyfile(job / "out.pdf", base)  # 예전에 만든 결과
    if (
        meta.get("mode") == "pdf"
        and base.is_file()
        and landscape == bool(meta.get("landscape"))
    ):
        # 쪽 내용만 다시 확대 · 축소 (다시 변환하지 않아 빠르다)
        _scale(base, job / "out.new.pdf", zoom)
        os.replace(job / "out.new.pdf", job / "out.pdf")
        shutil.rmtree(job / "pages", ignore_errors=True)
        meta.update(
            {
                "size": (job / "out.pdf").stat().st_size,
                "zoom": zoom,
                "version": int(meta.get("version", 0)) + 1,
            }
        )
    else:
        _convert(job, meta, zoom, landscape)
    _write_meta(job, meta)
    return _public(meta)


def delete(username: str, token: str) -> None:
    shutil.rmtree(_job_dir(username, token), ignore_errors=True)


def pdf_path(username: str, token: str) -> tuple[Path, str]:
    job = _job_dir(username, token)
    return job / "out.pdf", _meta(job)["pdf_name"]


def page_png(username: str, token: str, number: int, width: int) -> Path:
    job = _job_dir(username, token)
    meta = _meta(job)
    if not 1 <= number <= int(meta.get("pages") or 1):
        raise FileNotFoundError(number)
    width = max(200, min(2000, int(width)))
    version = meta.get("version", 0)
    target = job / "pages" / f"v{version}-p{number}-{width}.png"
    if not target.is_file():
        target.parent.mkdir(exist_ok=True)
        prefix = target.with_suffix("")
        subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-singlefile",
                "-f",
                str(number),
                "-l",
                str(number),
                "-scale-to-x",
                str(width),
                "-scale-to-y",
                "-1",
                str(job / "out.pdf"),
                str(prefix),
            ],
            capture_output=True,
            timeout=60,
            check=False,
            env=_env(job),
        )
        if not target.is_file():
            raise ConverterError("페이지를 그리지 못했어요.")
    return target


def bundle(username: str, tokens: list[str], mode: str) -> tuple[Path, str]:
    """여러 결과를 하나의 PDF(merge) 또는 zip 으로. 돌려준 파일은 응답 뒤 지운다."""
    if not tokens or len(tokens) > 100:
        raise ConverterError("합칠 파일을 골라 주세요 (최대 100개).")
    items = []
    for token in dict.fromkeys(tokens):
        job = _job_dir(username, token)
        items.append((job / "out.pdf", _meta(job)["pdf_name"]))
    tmp_dir = _user_dir(username) / f".bundle-{secrets.token_hex(6)}"
    tmp_dir.mkdir(mode=0o700)
    if mode == "zip":
        target = tmp_dir / "converted.zip"
        used: set[str] = set()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, name in items:
                stem, index, unique = Path(name).stem, 2, name
                while unique in used:
                    unique, index = f"{stem} ({index}).pdf", index + 1
                used.add(unique)
                archive.write(path, unique)
        return target, "converted.zip"
    target = tmp_dir / "merged.pdf"
    if len(items) == 1:
        shutil.copyfile(items[0][0], target)
    else:
        result = subprocess.run(
            ["pdfunite", *[str(p) for p, _ in items], str(target)],
            capture_output=True,
            timeout=120,
            check=False,
            env=_env(tmp_dir),
        )
        if result.returncode != 0 or not target.is_file():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise ConverterError("PDF 를 합치지 못했어요.")
    return target, "merged.pdf"


# ── 실행 도우미 ──────────────────────────────────────────────────────────────


def _env(home: Path) -> dict:
    """서버 비밀값(.env 로 읽힌 환경 변수)을 넘기지 않는 최소 환경."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _run(command: list[str], cwd: Path, home: Path, timeout: int = TIMEOUT):
    """시간이 넘으면 자식 프로세스까지 통째로 끝낸다."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=_env(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.communicate()
        raise ConverterError(f"변환이 너무 오래 걸려요 ({timeout}초 초과).") from error
    finally:
        try:
            os.killpg(
                process.pid, signal.SIGKILL
            )  # 남은 손자 프로세스 정리 (soffice.bin 등)
        except OSError:
            pass
    return process.returncode, out, err


def chrome_path() -> str | None:
    configured = os.getenv("CHROME_PATH")
    if configured and os.access(configured, os.X_OK):
        return configured
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def _page_count(pdf: Path) -> int:
    info = subprocess.run(
        ["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30, check=False
    )
    match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
    if info.returncode != 0 or not match:
        raise ConverterError("만들어진 PDF 를 읽지 못했어요.")
    return int(match.group(1))


# ── 변환 ────────────────────────────────────────────────────────────────────


def _clamp_zoom(zoom) -> float:
    return round(min(ZOOM_RANGE[1], max(ZOOM_RANGE[0], float(zoom or 1))), 2)


def _scale(source: Path, target: Path, zoom: float) -> None:
    """PDF 각 쪽의 내용을 쪽 가운데를 기준으로 zoom 배 (종이 크기는 그대로)."""
    if abs(zoom - 1) < 0.001:
        shutil.copyfile(source, target)
        return
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(source))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ConverterError("암호가 걸린 PDF 는 배율을 바꿀 수 없어요.")
        writer = PdfWriter()
        for page in reader.pages:
            box = page.cropbox
            cx = float(box.left) + float(box.width) / 2
            cy = float(box.bottom) + float(box.height) / 2
            page.add_transformation(
                Transformation().translate(-cx, -cy).scale(zoom, zoom).translate(cx, cy)
            )
            writer.add_page(page)
        with open(target, "wb") as handle:
            writer.write(handle)
    except ConverterError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as error:
        raise ConverterError("이 PDF 는 배율을 바꾸지 못했어요.") from error


def _convert(job: Path, meta: dict, zoom: float, landscape: bool) -> None:
    zoom = _clamp_zoom(zoom)
    landscape = bool(landscape) and bool(
        meta.get("orientable", meta["kind"] in HTML_ZOOM or meta["kind"] == "svg")
    )
    source = job / meta["source"]
    work = job / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    out = job / "out.new.pdf"
    out.unlink(missing_ok=True)
    kind = meta["kind"]
    meta["note"] = None
    mode = "html" if kind in HTML_ZOOM else "pdf"
    if not _slots.acquire(timeout=600):
        raise ConverterError("변환 대기열이 붐벼요. 잠시 뒤 다시 해 주세요.")
    try:
        if kind == "office":
            _office(source, work, out, meta)
        elif kind == "hwp":
            with source.open("rb") as handle:
                ole = handle.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
            if not ole:  # HWP 97 이전 형식은 LibreOffice 가 읽는다
                _office(source, work, out, meta)
            else:  # HWP 5.0: 본문 글자만
                try:
                    body = hwp5_to_html(str(source))
                except Hwp5Error as error:
                    raise ConverterError(str(error)) from error
                meta["note"] = (
                    "hwp 는 글자만 옮겨요 (그림 · 서식 제외). 가능하면 hwpx 로 저장해 올려 주세요."
                )
                mode = "html"
                _chrome(work, out, "hwp5", body, "", zoom, landscape)
        elif kind == "hwpx":
            try:
                body = hwpx_to_html(str(source))
            except HwpxError as error:
                raise ConverterError(str(error)) from error
            _chrome(work, out, "hwpx", body, "", 1.0, False)
        elif kind == "pdf":
            shutil.copyfile(source, out)
        elif kind == "image":
            _image(source, work, out)
        elif kind == "svg":
            data = _read_limited(source, MAX_TEXT_BYTES)
            body = f'<img alt="" src="data:image/svg+xml;base64,{base64.b64encode(data).decode()}">'
            _chrome(work, out, "image", body, "", 1.0, landscape)
        elif kind == "md":
            _chrome(work, out, "md", _read_text(source), "", zoom, landscape)
        elif kind == "ipynb":
            _chrome(work, out, "md", _notebook_markdown(source), "", zoom, landscape)
        elif kind == "html":
            _chrome(work, out, "html", _read_text(source), "", zoom, landscape)
        elif kind == "epub":
            _chrome(work, out, "html", _epub_html(source), "", zoom, landscape)
        else:
            ext = Path(meta["source"]).suffix.lower()
            lang = _HLJS_LANG.get(ext, ext.lstrip("."))
            _chrome(work, out, "text", _read_text(source), lang, zoom, landscape)
        if not out.is_file() or out.stat().st_size == 0:
            raise ConverterError("PDF 를 만들지 못했어요.")
        pages = _page_count(out)
        if mode == "pdf":
            os.replace(out, job / "base.pdf")
            _scale(job / "base.pdf", out, zoom)
        else:
            (job / "base.pdf").unlink(missing_ok=True)
    finally:
        _slots.release()
        shutil.rmtree(work, ignore_errors=True)
    os.replace(out, job / "out.pdf")
    shutil.rmtree(job / "pages", ignore_errors=True)
    meta.update(
        {
            "pages": pages,
            "size": (job / "out.pdf").stat().st_size,
            "mode": mode,
            "zoomable": True,
            "orientable": mode == "html" or kind == "svg",
            "zoom": zoom,
            "landscape": landscape,
            "version": int(meta.get("version", 0)) + 1,
        }
    )


def _read_limited(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise ConverterError(
            f"파일이 너무 커요 (이 형식은 최대 {limit // 1024 // 1024}MB)."
        )
    return path.read_bytes()


def _decode(data: bytes) -> str:
    encodings = ["utf-8-sig"]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.insert(0, "utf-16")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp949")  # 한국어 윈도우에서 만든 텍스트
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _read_text(path: Path) -> str:
    data = _read_limited(path, MAX_TEXT_BYTES)
    if b"\x00" in data[:8192] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        raise ConverterError("텍스트 파일이 아니에요.")
    return _decode(data)


# LibreOffice: 외부 링크(그림 · 섹션 · 셀 참조) · OLE/DDE · 매크로 차단, 네트워크는 없는 프록시로
_LO_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetProxyType" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetNoProxy" oor:op="fuse"><value></value></prop></item>
</oor:items>
"""


# 확장자와 내용이 맞는지 (손상 · 위장 파일을 LibreOffice 가 글자로 읽어 엉뚱한 PDF 를 만들지 않게)
_ZIP_FORMATS = set(
    ".docx .docm .dotx .dotm .xlsx .xlsm .xlsb .xltx .pptx .pptm .ppsx .potx "
    ".odt .ott .ods .ots .odp .otp .odg .otg .pages .numbers .key .vsdx".split()
)
_OLE_FORMATS = set(".doc .dot .xls .xlt .ppt .pps .pot .vsd .pub".split())


def _check_signature(source: Path) -> None:
    ext = source.suffix.lower()
    with source.open("rb") as handle:
        head = handle.read(512)
    if ext in _ZIP_FORMATS:
        ok = head.startswith(b"PK\x03\x04")
    elif ext in _OLE_FORMATS:
        # 옛 오피스 형식. 이름만 .doc/.xls 인 RTF · HTML · XML 문서도 흔해서 받아 준다
        stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
        ok = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or stripped[
            :5
        ].lower() in (b"{\\rtf", b"<?xml", b"<html", b"<!doc", b"<tabl")
    else:
        return
    if not ok:
        raise ConverterError("파일이 손상됐거나 확장자와 내용이 달라요.")


def _office(source: Path, work: Path, out: Path, meta: dict) -> None:
    _check_signature(source)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ConverterError("서버에 LibreOffice 가 없어 이 형식을 변환할 수 없어요.")
    profile = work / "lo-profile"
    (profile / "user").mkdir(parents=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        _LO_REGISTRY, encoding="utf-8"
    )
    ext = source.suffix.lower()
    target = "pdf"
    infilter = []
    if ext in {".csv", ".tsv"}:
        # 한국어 윈도우 CSV(cp949)도 깨지지 않게 UTF-8 로 바꿔서 넘긴다
        text = _decode(_read_limited(source, MAX_UPLOAD_BYTES))
        converted = (
            work / f"{Path(meta['pdf_name']).stem}{ext}"
        )  # 시트 이름 = 원래 파일 이름
        converted.write_text(text, encoding="utf-8")
        source = converted
        infilter = [f"--infilter=CSV:{9 if ext == '.tsv' else 44},34,76"]
        target = "pdf:calc_pdf_Export"
    else:
        linked = work / f"input{ext}"
        shutil.copyfile(source, linked)
        source = linked
    outdir = work / "out"
    outdir.mkdir()
    code, _, err = _run(
        [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            *infilter,
            "--convert-to",
            target,
            "--outdir",
            str(outdir),
            str(source),
        ],
        cwd=work,
        home=work,
    )
    result = outdir / f"{source.stem}.pdf"
    if not result.is_file() or result.stat().st_size == 0:
        detail = (err or b"").decode(errors="replace").strip().splitlines()[-1:]
        raise ConverterError(
            "문서를 변환하지 못했어요. 손상됐거나 암호가 걸린 파일인지 확인해 주세요."
            + (f" ({detail[0][:160]})" if detail and code else "")
        )
    os.replace(result, out)


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


_inline_cache: dict[str, str] = {}


def _inline_css() -> str:
    """KaTeX · 본문 글꼴을 data: 로 넣은 CSS (한 번 만들어 둔다)."""
    if "css" in _inline_cache:
        return _inline_cache["css"]

    def font(path: Path) -> str:
        return "data:font/woff2;base64," + base64.b64encode(path.read_bytes()).decode()

    katex = _asset("katex.min.css")
    katex = re.sub(r',url\(fonts/[^)]+\.(?:woff|ttf)\) format\("[^"]+"\)', "", katex)
    katex = re.sub(
        r"url\(fonts/([\w-]+\.woff2)\)",
        lambda m: f"url({font(ASSETS / 'katex-fonts' / m.group(1))})",
        katex,
    )
    fonts = (
        "@font-face{font-family:'Inter';font-style:normal;font-weight:100 900;"
        f"src:url({font(ASSETS / 'fonts' / 'inter-latin-wght-normal.woff2')}) format('woff2-variations')}}"
        "@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:100 800;"
        f"src:url({font(ASSETS / 'fonts' / 'jetbrains-mono-latin-wght-normal.woff2')}) format('woff2-variations')}}"
    )
    _inline_cache["css"] = "\n".join(
        [fonts, katex, _asset("highlight-github-light.css"), _asset("document.css")]
    )
    return _inline_cache["css"]


def _inline_js() -> str:
    if "js" not in _inline_cache:
        _inline_cache["js"] = "\n;\n".join(
            _asset(name)
            for name in (
                "marked.min.js",
                "purify.min.js",
                "katex.min.js",
                "highlight.min.js",
            )
        )
    return _inline_cache["js"]


def _script_json(value: dict) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _page_html(kind: str, content: str, lang: str, zoom: float, landscape: bool) -> str:
    nonce = secrets.token_urlsafe(18)
    size = "A4 landscape" if landscape else "A4"
    if kind == "hwpx":
        page_css = """
  @page { size: A4; margin: 0; }
  .hwp-doc { display: block; padding: 0; gap: 0; }
  .hwp-page { box-shadow: none; border-radius: 0; margin: 0 auto; break-after: page; }
  .hwp-page:last-child { break-after: auto; }"""
    elif kind == "image":
        height = "190mm" if landscape else "277mm"
        page_css = f"""
  @page {{ size: {size}; margin: 10mm; }}
  #doc {{ height: {height}; display: flex; align-items: center; justify-content: center; }}
  #doc img {{ width: 100%; height: 100%; object-fit: contain; }}  /* 벡터라 쪽에 맞게 키운다 */"""
    else:
        page_css = f"""
  @page {{ size: {size}; margin: 14mm 13mm; }}
  #doc {{ zoom: {zoom}; }}
  #doc .exv-md {{ max-width: none; margin: 0; padding: 0; font-size: 15px; }}
  #doc .exv-md pre code {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  #doc .exv-md pre, #doc .exv-md-table {{ overflow: visible; }}
  #doc .exv-md-table table {{ width: auto; max-width: 100%; }}
  #doc .katex-display {{ overflow: visible; }}
  #doc pre, #doc .katex-display, #doc img, #doc tr, #doc blockquote {{ break-inside: avoid; }}
  #doc .cv-text {{ break-inside: auto; }}
  #doc h1, #doc h2, #doc h3, #doc h4 {{ break-after: avoid; }}
  #doc img {{ max-width: 100%; }}
  .hwp5-section {{ font-family: 'Noto Serif KR', 'Noto Serif CJK KR', 'Nanum Myeongjo', serif; font-size: 10.5pt; line-height: 1.6; }}
  .hwp5-section + .hwp5-section {{ break-before: page; }}
  .hwp5-section .hwp-p {{ margin: 0; min-height: 1em; white-space: pre-wrap; }}"""
    if kind == "image":
        body = f'<div id="doc">{content}</div>'
        scripts = ""
    elif kind == "hwp5":
        body = f'<div id="doc">{content}</div>'  # 서버에서 escape 한 글자뿐
        scripts = ""
    else:
        body = '<div id="doc"></div>'
        source = _script_json(
            {
                "kind": "hwpx" if kind == "hwpx" else kind,
                "content": content,
                "lang": lang,
            }
        )
        scripts = (
            f'<script type="application/json" id="cv-src">{source}</script>\n'
            f'<script nonce="{nonce}">{_inline_js()}</script>\n'
            f'<script nonce="{nonce}">{_asset("render.js")}</script>'
        )
    csp = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; img-src data:; font-src data:; "
        "base-uri 'none'; form-action 'none'"
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{html.escape(csp)}">
<title>document</title>
<style>{_inline_css()}</style>
<style>
  html, body {{ background: #fff; margin: 0; }}
  body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
{page_css}
</style>
</head><body>
{body}
{scripts}
</body></html>"""


def _chrome(
    work: Path,
    out: Path,
    kind: str,
    content: str,
    lang: str,
    zoom: float,
    landscape: bool,
) -> None:
    chrome = chrome_path()
    if not chrome:
        raise ConverterError(
            "서버에 Chrome/Chromium 이 없어 이 형식을 변환할 수 없어요."
        )
    page = work / "page.html"
    page.write_text(_page_html(kind, content, lang, zoom, landscape), encoding="utf-8")
    pdf = work / "out.pdf"
    # Chrome 은 TMPDIR 안에 소켓을 만드는데 경로 길이 제한(108자)이 있어 짧은 임시 폴더를 따로 쓴다
    scratch = Path(tempfile.mkdtemp(prefix="lecai-cv-"))
    try:
        _run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-features=Translate,OptimizationHints,MediaRouter",
                f"--user-data-dir={scratch / 'profile'}",
                "--host-resolver-rules=MAP * ~NOTFOUND",  # 외부 네트워크 차단
                "--proxy-server=127.0.0.1:9",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=20000",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf}",
                page.as_uri(),
            ],
            cwd=work,
            home=scratch,
            timeout=120,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise ConverterError("PDF 를 만들지 못했어요.")
    os.replace(pdf, out)


# ── 그림 ────────────────────────────────────────────────────────────────────


def _pil_open(path: Path) -> Image.Image | None:
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            return Image.open(path)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ConverterError("그림이 너무 커요.") from error
    except OSError, ValueError, SyntaxError:
        return None


def _open_image(source: Path, work: Path) -> Image.Image:
    image = _pil_open(source)
    if image is not None:
        return image
    # Pillow 가 못 여는 형식(HEIC 등)은 ffmpeg 로 PNG 로 바꿔서
    png = work / "decoded.png"
    if shutil.which("ffmpeg"):
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-y",
                str(png),
            ],
            cwd=work,
            home=work,
            timeout=90,
        )
    image = _pil_open(png) if png.is_file() else None
    if image is None:
        raise ConverterError("그림을 읽지 못했어요.")
    return image


def _image(source: Path, work: Path, out: Path) -> None:
    image = _open_image(source, work)
    frames: list[bytes] = []
    multi = (
        image.format or ""
    ).upper() == "TIFF"  # 여러 쪽 스캔(tiff)은 쪽마다, 움직이는 그림은 첫 장만
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            sequence = ImageSequence.Iterator(image) if multi else [image]
            for index, frame in enumerate(sequence):
                if index >= MAX_IMAGE_FRAMES:
                    break
                frame = ImageOps.exif_transpose(frame.copy())
                if frame.mode in ("RGBA", "LA", "P", "PA"):
                    frame = frame.convert("RGBA")
                    background = Image.new("RGB", frame.size, (255, 255, 255))
                    background.paste(frame, mask=frame.getchannel("A"))
                    frame = background
                elif frame.mode not in ("RGB", "L", "1", "CMYK"):
                    frame = frame.convert("RGB")
                buffer = io.BytesIO()
                if frame.mode == "CMYK":
                    frame.save(buffer, "JPEG", quality=95)
                else:
                    frame.save(buffer, "PNG", optimize=False)
                frames.append(buffer.getvalue())
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ConverterError("그림이 너무 커요.") from error
    except (OSError, ValueError) as error:
        raise ConverterError("그림을 읽지 못했어요.") from error
    finally:
        image.close()
    if not frames:
        raise ConverterError("그림을 읽지 못했어요.")
    a4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
    border = (img2pdf.mm_to_pt(10), img2pdf.mm_to_pt(10))
    layout = img2pdf.get_layout_fun(
        a4, border=border, fit=img2pdf.FitMode.into, auto_orient=True
    )
    try:
        out.write_bytes(
            img2pdf.convert(
                frames, layout_fun=layout, rotation=img2pdf.Rotation.ifvalid
            )
        )
    except (img2pdf.ImageOpenError, ValueError, OSError) as error:
        raise ConverterError("그림을 PDF 로 바꾸지 못했어요.") from error


# ── 주피터 노트북 → 마크다운 ─────────────────────────────────────────────────

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _joined(value) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def _fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def _notebook_markdown(source: Path) -> str:
    try:
        notebook = json.loads(_read_text(source))
        cells = notebook.get("cells") or []
        lang = (
            (notebook.get("metadata") or {}).get("kernelspec", {}).get("language")
            or (notebook.get("metadata") or {}).get("language_info", {}).get("name")
            or "python"
        )
    except (ValueError, AttributeError) as error:
        raise ConverterError("노트북(ipynb) 형식이 올바르지 않아요.") from error
    parts = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        text = _joined(cell.get("source"))
        kind = cell.get("cell_type")
        if kind == "markdown":
            parts.append(text)
        elif kind == "code":
            if text.strip():
                parts.append(_fence(text, lang))
            for output in cell.get("outputs") or []:
                if not isinstance(output, dict):
                    continue
                kind_out = output.get("output_type")
                if kind_out == "stream":
                    parts.append(
                        _fence(_ANSI.sub("", _joined(output.get("text")))[:20000])
                    )
                elif kind_out == "error":
                    trace = "\n".join(
                        _ANSI.sub("", line) for line in output.get("traceback") or []
                    )
                    parts.append(_fence(trace[:20000]))
                elif kind_out in ("execute_result", "display_data"):
                    data = output.get("data") or {}
                    image = next(
                        (m for m in ("image/png", "image/jpeg") if m in data), None
                    )
                    if image:
                        b64 = re.sub(r"\s", "", _joined(data[image]))
                        if re.fullmatch(r"[A-Za-z0-9+/=]+", b64):
                            parts.append(f"![](data:{image};base64,{b64})")
                    elif "text/markdown" in data:
                        parts.append(_joined(data["text/markdown"]))
                    elif "text/html" in data:
                        parts.append(_joined(data["text/html"]))
                    elif "text/plain" in data:
                        parts.append(
                            _fence(_ANSI.sub("", _joined(data["text/plain"]))[:20000])
                        )
        elif text.strip():
            parts.append(_fence(text))
    return "\n\n".join(parts) or "_(빈 노트북)_"


# ── EPUB → HTML ─────────────────────────────────────────────────────────────


def _zip_read(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER:
        raise ConverterError("전자책 안의 파일이 너무 커요.")
    budget[0] -= info.file_size
    if budget[0] < 0:
        raise ConverterError("전자책이 너무 커요.")
    return archive.read(name)


def _epub_html(source: Path) -> str:
    import posixpath
    import xml.etree.ElementTree as ET

    budget = [MAX_EPUB_TOTAL]
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            container = ET.fromstring(
                _zip_read(archive, "META-INF/container.xml", budget)
            )
            rootfile = next(
                (
                    e.get("full-path")
                    for e in container.iter()
                    if e.tag.endswith("rootfile")
                ),
                None,
            )
            if not rootfile or rootfile not in names:
                raise ConverterError("전자책(epub) 형식이 올바르지 않아요.")
            opf = ET.fromstring(_zip_read(archive, rootfile, budget))
            base = posixpath.dirname(rootfile)
            manifest = {}
            for item in opf.iter():
                if item.tag.endswith("item") and item.get("id") and item.get("href"):
                    href = posixpath.normpath(
                        posixpath.join(base, item.get("href").split("#")[0])
                    )
                    manifest[item.get("id")] = (href, item.get("media-type") or "")
            spine = [
                manifest[ref.get("idref")][0]
                for ref in opf.iter()
                if ref.tag.endswith("itemref") and ref.get("idref") in manifest
            ]
            styles = []
            for href, media in manifest.values():
                if media == "text/css" and href in names:
                    css = _zip_read(archive, href, budget).decode(
                        "utf-8", errors="replace"
                    )
                    css = re.sub(r"@import[^;]*;", "", css)
                    css = re.sub(
                        r"url\([^)]*\)", "none", css
                    )  # 외부 · 글꼴 파일 참조는 빼고
                    styles.append(css[:300_000])
            media_types = {href: media for href, media in manifest.values()}

            def inline(match: re.Match, chapter: str) -> str:
                attr, quote, value = match.group(1), match.group(2), match.group(3)
                target = posixpath.normpath(
                    posixpath.join(
                        posixpath.dirname(chapter), html.unescape(value).split("#")[0]
                    )
                )
                media = media_types.get(target, "")
                if not media.startswith("image/") or target not in names:
                    return match.group(0)
                data = base64.b64encode(_zip_read(archive, target, budget)).decode()
                return f"{attr}={quote}data:{media};base64,{data}{quote}"

            chapters = []
            for chapter in spine:
                if chapter not in names:
                    continue
                text = _zip_read(archive, chapter, budget).decode(
                    "utf-8", errors="replace"
                )
                match = re.search(
                    r"<body[^>]*>(.*)</body>", text, re.DOTALL | re.IGNORECASE
                )
                body = match.group(1) if match else text
                body = re.sub(
                    r'\b(src|xlink:href|href)=(["\'])([^"\']+\.(?:png|jpe?g|gif|webp|svg|bmp))\2',
                    lambda m, c=chapter: inline(m, c),
                    body,
                    flags=re.IGNORECASE,
                )
                chapters.append(f'<section class="cv-chapter">{body}</section>')
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise ConverterError("전자책(epub)을 읽지 못했어요.") from error
    if not chapters:
        raise ConverterError("전자책에서 본문을 찾지 못했어요.")
    style = (
        "<style>"
        + "\n".join(styles)
        + "\n.cv-chapter + .cv-chapter { break-before: page; }</style>"
    )
    return style + "".join(chapters)
