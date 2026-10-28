"""뷰어 내보내기 → PDF (모아찍기: 한 장에 2 · 4쪽).

한 장에 1쪽은 브라우저 인쇄 창으로 저장한다 (viewer.html). 여러 쪽을 한 장에 모으려면 쪽 나눔을 알아야 해서
서버가 뷰어가 만든 내보내기 문서(HTML)를 헤드리스 Chrome 으로 PDF 로 만든 뒤, 그 쪽들을 벡터 그대로 줄여 배치한다.

보안 (사용자가 보낸 HTML 을 그린다):
- <script> · <link> · <iframe> 등은 지우고, 맨 앞에 CSP(default-src 'none', 그림 · 글꼴은 data: 만, 스크립트 없음)를 넣는다.
  → 파일(file://) · 네트워크에 접근할 수 없다. 외부 네트워크 이름 풀이도 막는다 (converter 와 같은 실행 방식).
- 슬라이드 그림(/static/…)은 서버가 static 폴더 안의 그림 파일인지 확인한 뒤 data: 로 바꿔 넣는다.
- KaTeX 글꼴 · 스타일은 서버에 있는 것(converter_assets)을 넣는다.
- 결과는 data/export/<사용자 해시>/<토큰>/ 에 30분만 둔다.
"""

import base64
import hashlib
import html as htmllib
import mimetypes
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.config import settings
from app.services import converter_service as cv
from app.services.converter_service import ConverterError

ROOT = Path(settings.BASE_DIR) / "data" / "export"
STATIC = (Path(settings.BASE_DIR) / "static").resolve()
KEEP_SECONDS = 30 * 60
MAX_HTML = 8 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_IMAGES_TOTAL = 200 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_TOKEN = re.compile(r"^[0-9a-f]{24}$")
LAYOUT = re.compile(r"^(1|2|4(z|s|n|rn))$")
NUP_MARGIN = 14.0  # 종이 가장자리 여백 (pt)
NUP_GAP = 10.0  # 쪽 사이 간격 (pt)
# 2×2 칸 (열, 행 — 행 0 이 위) 을 채우는 순서
ORDERS = {
    "z": [(0, 0), (1, 0), (0, 1), (1, 1)],  # 왼→오, 위→아래 (Z)
    "s": [(1, 0), (0, 0), (1, 1), (0, 1)],  # 오→왼, 위→아래
    "n": [(0, 0), (0, 1), (1, 0), (1, 1)],  # 위→아래, 왼쪽 열부터
    "rn": [(1, 0), (1, 1), (0, 0), (0, 1)],  # 위→아래, 오른쪽 열부터
}
CSP = (
    "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
# 그리는 데 필요 없는(그리고 바깥에 닿을 수 있는) 태그는 통째로 지운다
_STRIP_BLOCK = re.compile(
    r"<(script|iframe|object|embed|frame|frameset|noscript|template|svg:script)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_STRIP_TAG = re.compile(
    r"<(?:script|iframe|object|embed|frame|link|meta|base|portal)\b[^>]*>",
    re.IGNORECASE,
)
_SRC = re.compile(r"""(\ssrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def _user_dir(username: str) -> Path:
    digest = hashlib.sha256(f"lecai-export:{username}".encode()).hexdigest()[:32]
    path = ROOT / digest
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _prune(user_dir: Path) -> None:
    now = time.time()
    for job in user_dir.iterdir():
        try:
            if now - job.stat().st_mtime > KEEP_SECONDS:
                shutil.rmtree(job, ignore_errors=True)
        except OSError:
            continue


def _job(username: str, token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise ConverterError("잘못된 요청입니다.")
    job = _user_dir(username) / token
    if not (job / "out.pdf").is_file():
        raise FileNotFoundError(token)
    return job


def _katex_css() -> str:
    """KaTeX 스타일 + 글꼴(data:). converter 와 같은 파일을 쓴다."""
    if "export-katex" in cv._inline_cache:
        return cv._inline_cache["export-katex"]
    katex = cv._asset("katex.min.css")
    katex = re.sub(r',url\(fonts/[^)]+\.(?:woff|ttf)\) format\("[^"]+"\)', "", katex)
    katex = re.sub(
        r"url\(fonts/([\w-]+\.woff2)\)",
        lambda m: (
            "url(data:font/woff2;base64,"
            + base64.b64encode(
                (cv.ASSETS / "katex-fonts" / m.group(1)).read_bytes()
            ).decode()
            + ")"
        ),
        katex,
    )
    cv._inline_cache["export-katex"] = katex
    return katex


def _inline_images(doc: str) -> str:
    """src="/static/…" 그림을 data: 로. 그 밖의 주소는 비운다 (CSP 로도 막히지만 요청 자체를 없앤다)."""
    used = 0
    cache: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        nonlocal used
        prefix, quote, raw = match.groups()
        url = htmllib.unescape(raw).strip()
        if url.startswith("data:image/"):
            return match.group(0)
        if url in cache:
            return f"{prefix}{quote}{cache[url]}{quote}"
        data = ""
        # 뷰어는 그림 주소를 절대 주소(https://…/static/…)로 갖고 있다 → 경로만 쓴다 (주소로 요청하지는 않는다)
        parts = urlsplit(url)
        path_part = parts.path if parts.scheme in ("http", "https", "") else ""
        if path_part.startswith("/static/"):
            try:
                path = (STATIC / unquote(path_part[len("/static/") :])).resolve()
                path.relative_to(STATIC)
                if path.suffix.lower() in IMAGE_EXT and path.is_file():
                    size = path.stat().st_size
                    if size <= MAX_IMAGE and used + size <= MAX_IMAGES_TOTAL:
                        used += size
                        kind = mimetypes.guess_type(path.name)[0] or "image/png"
                        data = f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode()}"
            except ValueError, OSError:
                data = ""
        cache[url] = data
        return f"{prefix}{quote}{data}{quote}"

    return _SRC.sub(replace, doc)


def _safe_html(doc: str) -> str:
    doc = _STRIP_BLOCK.sub("", doc)
    doc = _STRIP_TAG.sub("", doc)
    doc = _inline_images(doc)
    head = (
        f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
        f"<style>{_katex_css()}</style>"
    )
    # CSP 는 문서 맨 앞에 둔다 (사용자 내용보다 먼저 적용되게)
    body = re.sub(r"^\s*<!doctype[^>]*>", "", doc, flags=re.IGNORECASE)
    return f"<!DOCTYPE html><html><head>{head}</head>{body}</html>"


def render(username: str, doc: str) -> dict:
    """내보내기 문서를 PDF 로 (한 장에 1쪽). 모아찍기는 layout_pdf 로."""
    if len(doc.encode("utf-8")) > MAX_HTML:
        raise ConverterError("내보낼 내용이 너무 커요. 범위를 나눠 주세요.")
    chrome = cv.chrome_path()
    if not chrome:
        raise ConverterError("서버에 Chrome/Chromium 이 없어 PDF 를 만들 수 없어요.")
    user_dir = _user_dir(username)
    _prune(user_dir)
    token = secrets.token_hex(12)
    job = user_dir / token
    job.mkdir(mode=0o700)
    page = job / "page.html"
    page.write_text(_safe_html(doc), encoding="utf-8")
    pdf = job / "render.pdf"
    scratch = Path(tempfile.mkdtemp(prefix="lecai-ex-"))
    if not cv._slots.acquire(timeout=600):
        shutil.rmtree(job, ignore_errors=True)
        raise ConverterError("변환 대기열이 붐벼요. 잠시 뒤 다시 해 주세요.")
    try:
        cv._run(
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
            cwd=job,
            home=scratch,
            timeout=150,
        )
    finally:
        cv._slots.release()
        shutil.rmtree(scratch, ignore_errors=True)
    if not pdf.is_file() or pdf.stat().st_size == 0:
        shutil.rmtree(job, ignore_errors=True)
        raise ConverterError("PDF 를 만들지 못했어요.")
    pdf.replace(job / "out.pdf")
    page.unlink(missing_ok=True)
    return {
        "token": token,
        "pages": cv._page_count(job / "out.pdf"),
        "size": (job / "out.pdf").stat().st_size,
    }


def _nup(source: Path, target: Path, layout: str) -> None:
    """한 장에 2쪽(세로 쪽이면 가로 종이에 나란히, 가로 쪽이면 세로 종이에 위아래) 또는 4쪽(2×2, 순서 지정)."""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation

    reader = PdfReader(str(source))
    pages = list(reader.pages)
    first = pages[0].mediabox
    page_w, page_h = float(first.width), float(first.height)
    if layout == "2":
        sheet_w, sheet_h = page_h, page_w  # 같은 종이를 돌려서
        if page_w <= page_h:
            cols, rows, slots = 2, 1, [(0, 0), (1, 0)]
        else:
            cols, rows, slots = 1, 2, [(0, 0), (0, 1)]
    else:
        sheet_w, sheet_h = page_w, page_h
        cols, rows, slots = 2, 2, ORDERS[layout[1:]]
    cell_w = (sheet_w - 2 * NUP_MARGIN - (cols - 1) * NUP_GAP) / cols
    cell_h = (sheet_h - 2 * NUP_MARGIN - (rows - 1) * NUP_GAP) / rows
    writer = PdfWriter()
    for start in range(0, len(pages), len(slots)):
        sheet = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
        for page, (col, row) in zip(pages[start : start + len(slots)], slots):
            box = page.mediabox
            width, height = float(box.width), float(box.height)
            scale = min(cell_w / width, cell_h / height)
            # 칸 안 가운데 (PDF 좌표는 아래가 0 → 행 0 이 위)
            x = NUP_MARGIN + col * (cell_w + NUP_GAP) + (cell_w - width * scale) / 2
            y = (
                sheet_h
                - NUP_MARGIN
                - (row + 1) * cell_h
                - row * NUP_GAP
                + (cell_h - height * scale) / 2
            )
            sheet.merge_transformed_page(
                page,
                Transformation()
                .translate(-float(box.left), -float(box.bottom))
                .scale(scale)
                .translate(x, y),
            )
        writer.add_page(sheet)
    # 합친 쪽 내용은 압축이 풀린 채라 다시 압축하고, 쪽마다 복사된 글꼴 · 그림은 하나로 합친다
    for sheet in writer.pages:
        sheet.compress_content_streams()
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    with tmp.open("wb") as handle:
        writer.write(handle)
    tmp.replace(target)


def layout_pdf(username: str, token: str, layout: str = "1") -> Path:
    job = _job(username, token)
    if not LAYOUT.match(layout or ""):
        raise ConverterError("잘못된 레이아웃입니다.")
    if layout == "1":
        return job / "out.pdf"
    target = job / f"out-{layout}.pdf"
    if not target.is_file():
        _nup(job / "out.pdf", target, layout)
    return target


def layout_info(username: str, token: str, layout: str) -> dict:
    pdf = layout_pdf(username, token, layout)
    return {"pages": cv._page_count(pdf), "size": pdf.stat().st_size}


def page_png(username: str, token: str, number: int, width: int, layout: str) -> Path:
    pdf = layout_pdf(username, token, layout)
    job = pdf.parent
    width = max(200, min(2000, int(width)))
    target = job / "pages" / f"{layout}-p{number}-{width}.png"
    if not target.is_file():
        target.parent.mkdir(exist_ok=True)
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
                str(pdf),
                str(target.with_suffix("")),
            ],
            capture_output=True,
            timeout=60,
            check=False,
            env=cv._env(job),
        )
        if not target.is_file():
            raise FileNotFoundError(number)
    return target
