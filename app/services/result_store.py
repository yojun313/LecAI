# app/services/result_store.py
"""
결과 디렉터리 저장 구조 (작업 결과 zip 과 문서함(static/docs) 모두 동일):

    <result_dir>/
        images/          page_001.png, page_002.png, ...
        desc/            slide_001.md, slide_002.md, ...  (슬라이드별 설명, LLM 출력 본문만)
        transcript.txt   (선택) 함께 제공된 강의 녹음본
        result.pdf       (선택) GENERATE_PDF=true 일 때만

예전 구조(result.md 하나에 전체 설명)는 ensure_desc_layout() 이 슬라이드별 파일로 변환한다.
뷰어/PDF 는 compose_markdown() 으로 슬라이드별 파일을 합쳐 예전과 동일한 마크다운을 만든다.
"""

import os
import re
import json
import shutil
import hashlib
from datetime import datetime

DESC_DIR = "desc"
IMAGES_DIR = "images"
LEGACY_MD = "result.md"
TRANSCRIPT_FILE = "transcript.txt"  # 예전 구조 호환용 (첫 녹음본 사본)
TRANSCRIPTS_DIR = "transcripts"  # 추가된 모든 녹음본 원문: 001_<label>.txt + index.json
BOARDS_DIR = "boards"  # 칠판 판서 사진: 001_<label>.jpg + index.json
BOARD_SECTION_HEADING = "### 🧑‍🏫 칠판 판서"
TRANSCRIPT_SECTION_HEADING = "### 🎙️ 강의 녹음 발췌"

SLIDE_FILE_RE = re.compile(r"^slide_(\d{3,})\.md$")
# 예전 result.md 의 슬라이드 블록: "## Slide N" 헤더로 시작
LEGACY_BLOCK_RE = re.compile(r"^## Slide (\d+)\s*$", re.M)


def slide_filename(idx: int) -> str:
    return f"slide_{idx:03d}.md"


def image_filename(idx: int) -> str:
    return f"page_{idx:03d}.png"


def image_for_slide(result_dir: str, idx: int):
    """
    슬라이드 idx 의 이미지 파일명. 현재 구조는 1부터(page_001.png = 슬라이드 1),
    예전 일부 문서는 0부터(page_000.png = 슬라이드 1) 번호가 매겨져 있어 둘 다 지원한다.
    이미지가 없으면 None.
    """
    images = os.path.join(result_dir, IMAGES_DIR)
    zero_based = os.path.exists(os.path.join(images, image_filename(0)))
    name = image_filename(idx - 1 if zero_based else idx)
    return name if os.path.exists(os.path.join(images, name)) else None


def desc_dir(result_dir: str) -> str:
    return os.path.join(result_dir, DESC_DIR)


def write_slide(result_dir: str, idx: int, text: str):
    os.makedirs(desc_dir(result_dir), exist_ok=True)
    with open(
        os.path.join(desc_dir(result_dir), slide_filename(idx)), "w", encoding="utf-8"
    ) as f:
        f.write((text or "").strip() + "\n")


def write_slides(result_dir: str, slides: dict):
    """slides: {idx: text}"""
    for idx, text in slides.items():
        write_slide(result_dir, idx, text)


def read_slides(result_dir: str) -> dict:
    """desc/ 의 슬라이드별 파일을 {idx: text} 로 읽는다 (없으면 빈 dict)."""
    d = desc_dir(result_dir)
    if not os.path.isdir(d):
        return {}
    slides = {}
    for name in os.listdir(d):
        m = SLIDE_FILE_RE.match(name)
        if not m:
            continue
        with open(os.path.join(d, name), "r", encoding="utf-8") as f:
            slides[int(m.group(1))] = f.read().strip()
    return slides


# 모델이 파일명을 제목으로 쓴 줄 (예: "## page_001.png", "# slide_3.jpg") - 뷰어/PDF 에서 숨긴다
FILENAME_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*(?:\*\*)?\s*(?:page|slide)?[_\- ]?\d{1,4}\.(?:png|jpe?g|webp)\s*(?:\*\*)?\s*$",
    re.I | re.M,
)


def strip_filename_headings(text: str) -> str:
    return FILENAME_HEADING_RE.sub("", text or "").lstrip("\n")


def slide_block(result_dir: str, idx: int, text: str) -> str:
    """뷰어/PDF 용 슬라이드 블록. 이미지가 있으면 함께 넣는다."""
    img = image_for_slide(result_dir, idx)
    header = f"## Slide {idx}\n\n"
    image = f"![{img}](./{IMAGES_DIR}/{img})\n\n" if img else ""
    return f"{header}{image}{strip_filename_headings(text).strip()}\n\n---\n\n"


def compose_markdown(result_dir: str) -> str:
    """슬라이드별 파일을 합쳐 하나의 마크다운 문서로 만든다. desc/ 가 없으면 예전 result.md 를 그대로 반환."""
    slides = read_slides(result_dir)
    if not slides:
        legacy = os.path.join(result_dir, LEGACY_MD)
        if os.path.exists(legacy):
            with open(legacy, "r", encoding="utf-8") as f:
                return f.read()
        return ""
    return "".join(slide_block(result_dir, idx, slides[idx]) for idx in sorted(slides))


def split_legacy_markdown(md_text: str) -> dict:
    """
    예전 result.md ("## Slide N\\n\\n![..](./images/..)\\n\\n본문\\n\\n---\\n\\n" 반복) 를 {idx: 본문} 으로 분해.
    본문에서 헤더/이미지 줄/끝의 구분선만 제거하고 나머지는 그대로 둔다.
    """
    matches = list(LEGACY_BLOCK_RE.finditer(md_text))
    slides = {}
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        body = md_text[start:end]
        # 슬라이드 이미지 줄 제거 (블록 첫 이미지만)
        body = re.sub(
            r"^\s*!\[[^\]]*\]\(\./images/[^)]+\)\s*\n", "", body, count=1, flags=re.M
        )
        # 블록 끝의 구분선 제거 (본문이 비어 구분선만 남은 경우 포함)
        body = re.sub(r"(?:^|\n)\s*---\s*$", "", body.rstrip())
        slides[idx] = body.strip()
    return slides


def _normalize(text: str) -> str:
    # 이미지 줄은 비교에서 제외: compose 가 실제 존재하는 이미지로 다시 만들기 때문 (error.png 참조 등)
    text = re.sub(r"!\[[^\]]*\]\(\./images/[^)]+\)", "", text)
    # 파일명 제목 줄도 제외: compose 가 뷰어/PDF 에서 숨기기 때문
    text = strip_filename_headings(text)
    return re.sub(r"\s+", " ", text).strip()


def roundtrip_ok(result_dir: str, original: str) -> bool:
    """desc/ 를 합친 결과가 원본 result.md 와 (이미지 줄·공백 제외) 일치하는지"""
    return _normalize(compose_markdown(result_dir)) == _normalize(original)


def ensure_desc_layout(result_dir: str) -> bool:
    """
    desc/ 가 없고 result.md 만 있으면 슬라이드별 파일로 변환한다.
    합쳐서 다시 만든 문서가 원본과 (공백 제외) 일치할 때만 result.md 를 제거한다.
    반환: 변환을 수행했는지
    """
    legacy = os.path.join(result_dir, LEGACY_MD)
    if os.path.isdir(desc_dir(result_dir)) and read_slides(result_dir):
        return False
    if not os.path.exists(legacy):
        return False

    with open(legacy, "r", encoding="utf-8") as f:
        original = f.read()
    slides = split_legacy_markdown(original)
    if not slides:
        return False

    write_slides(result_dir, slides)
    if roundtrip_ok(result_dir, original):
        os.remove(legacy)
    else:
        # 불일치 시 원본을 보존 (desc/ 는 뷰어에서 우선 사용됨)
        shutil.move(legacy, os.path.join(result_dir, "result.legacy.md"))
    return True


def _marker(sid: str, end=False) -> str:
    return f"<!-- {'/' if end else ''}transcript:{sid} -->"


def upsert_transcript_section(text: str, section: str, sid: str = "default") -> str:
    """
    슬라이드 본문에 녹음 발췌 절을 추가한다. 절은 출처(sid)별로 누적되며, 같은 sid 가 이미 있으면 그 절만 교체한다.
    (마커는 HTML 주석이라 뷰어/PDF 에는 보이지 않는다)
    """
    text = (text or "").rstrip()
    block = f"{_marker(sid)}\n{section.strip()}\n{_marker(sid, end=True)}"
    pattern = re.compile(
        re.escape(_marker(sid)) + r".*?" + re.escape(_marker(sid, end=True)), re.S
    )
    if pattern.search(text):
        text = pattern.sub(lambda m: block, text, count=1)
    else:
        # 마커 없는 옛 형식(단일 절)이 있으면 그대로 두고 뒤에 누적
        text = f"{text}\n\n{block}"
    return text.rstrip() + "\n"


def mark_transcript_section(text: str, sid: str) -> str:
    """LLM 이 본문 안에 직접 써 넣은(마커 없는) 발췌 절을 찾아 sid 마커로 감싼다. 없으면 그대로."""
    text = (text or "").rstrip()
    if _marker(sid) in text:
        return text + "\n"
    m = re.search(
        re.escape(TRANSCRIPT_SECTION_HEADING) + r".*?(?=\n#{1,3} |\Z)", text, re.S
    )
    if not m:
        return text + "\n"
    section = m.group(0).strip()
    rest = (text[: m.start()] + text[m.end() :]).rstrip()
    return upsert_transcript_section(rest, section, sid)


def transcript_sids(text: str) -> list:
    return re.findall(r"<!-- transcript:([^ ]+) -->", text or "")


def transcript_sid(transcript: str) -> str:
    """녹음본 내용 해시 → 출처 id (같은 녹음본을 다시 넣으면 같은 id)"""
    return hashlib.sha1(
        re.sub(r"\s+", " ", transcript).strip().encode("utf-8")
    ).hexdigest()[:10]


def _index_path(result_dir: str) -> str:
    return os.path.join(result_dir, TRANSCRIPTS_DIR, "index.json")


def list_transcripts(result_dir: str) -> list:
    path = _index_path(result_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_transcript(
    result_dir: str, transcript: str, label: str = "", slide_range=None
) -> dict:
    """
    녹음본 원문을 transcripts/NNN_<label>.txt 로 보관하고 index.json 에 기록한다.
    같은 내용(sid)이 이미 있으면 파일을 덮어쓰고 기존 항목을 갱신한다. 반환: index 항목
    """
    sid = transcript_sid(transcript)
    entries = list_transcripts(result_dir)
    existing = next((e for e in entries if e.get("sid") == sid), None)
    safe_label = (
        re.sub(r"[^\w\-가-힣. ]+", "_", label or "transcript").strip()[:60]
        or "transcript"
    )
    if existing:
        entry = existing
        entry["label"] = label or entry.get("label", "")
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if slide_range:
            entry["slide_from"], entry["slide_to"] = slide_range
        else:
            entry.pop("slide_from", None)
            entry.pop("slide_to", None)
    else:
        seq = len(entries) + 1
        entry = {
            "sid": sid,
            "seq": seq,
            "label": label or f"녹음본 {seq}",
            "file": f"{seq:03d}_{safe_label}.txt",
            "chars": len(transcript),
            "kind": "paste",  # paste | audio | text_doc (원본이 붙으면 갱신)
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        if slide_range:
            entry["slide_from"], entry["slide_to"] = slide_range
        entries.append(entry)
    os.makedirs(os.path.join(result_dir, TRANSCRIPTS_DIR), exist_ok=True)
    with open(
        os.path.join(result_dir, TRANSCRIPTS_DIR, entry["file"]), "w", encoding="utf-8"
    ) as f:
        f.write(transcript)
    with open(_index_path(result_dir), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    # 첫 녹음본은 예전 구조 호환을 위해 transcript.txt 로도 남긴다
    if entry["seq"] == 1:
        with open(
            os.path.join(result_dir, TRANSCRIPT_FILE), "w", encoding="utf-8"
        ) as f:
            f.write(transcript)
    return entry


def save_transcript_copy(result_dir: str, transcript: str, label: str = ""):
    return save_transcript(result_dir, transcript, label)


AUDIO_ORIGINAL_EXTS = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".wav",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".webm",
    ".aac",
    ".wma",
    ".amr",
    ".aiff",
    ".aif",
    ".caf",
    ".mp4",
    ".mov",
    ".mkv",
)
# 브라우저 <audio> 로 바로 재생 가능한 형식
BROWSER_PLAYABLE_EXTS = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".wav",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".webm",
    ".aac",
    ".mp4",
)


def attach_original(
    result_dir: str, entry: dict, src_path: str, original_name: str
) -> dict:
    """
    녹음본의 원본 파일(음성 또는 텍스트 문서)을 transcripts/ 로 옮겨 index 에 기록한다.
    같은 sid 로 다시 추가되면 원본을 덮어쓴다. 반환: 갱신된 index 항목
    """
    if not src_path or not os.path.exists(src_path):
        return entry
    ext = os.path.splitext(original_name or src_path)[1].lower()
    base = os.path.splitext(entry["file"])[0]
    dest_name = f"{base}{ext}"
    dest = os.path.join(result_dir, TRANSCRIPTS_DIR, dest_name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src_path, dest)

    entries = list_transcripts(result_dir)
    for e in entries:
        if e.get("sid") == entry["sid"]:
            e["original"] = dest_name
            e["original_name"] = original_name or dest_name
            e["kind"] = "audio" if ext in AUDIO_ORIGINAL_EXTS else "text_doc"
            e["size"] = os.path.getsize(dest)
            entry = e
            break
    with open(_index_path(result_dir), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    return entry


# ---------- 칠판 판서 사진 ----------
def _boards_index_path(result_dir: str) -> str:
    return os.path.join(result_dir, BOARDS_DIR, "index.json")


def list_boards(result_dir: str) -> list:
    path = _boards_index_path(result_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_boards_index(result_dir: str, entries: list):
    os.makedirs(os.path.join(result_dir, BOARDS_DIR), exist_ok=True)
    with open(_boards_index_path(result_dir), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def save_board_photo(
    result_dir: str, src_path: str, original_name: str, label: str = ""
) -> dict:
    """
    판서 사진을 boards/NNN_<label>.jpg 로 보관 (긴 변 2000px 이하 JPEG 로 정규화). 같은 사진(해시)이면 기존 항목 갱신.
    반환: index 항목 {sid, seq, file, original_name, label, added_at, slides: []}
    """
    from PIL import Image, ImageOps

    with open(src_path, "rb") as f:
        sid = "board-" + hashlib.sha1(f.read()).hexdigest()[:10]
    entries = list_boards(result_dir)
    existing = next((e for e in entries if e.get("sid") == sid), None)
    safe_label = (
        re.sub(
            r"[^\w\-가-힣. ]+",
            "_",
            label or os.path.splitext(original_name)[0] or "board",
        ).strip()[:60]
        or "board"
    )
    if existing:
        entry = existing
    else:
        seq = len(entries) + 1
        entry = {
            "sid": sid,
            "seq": seq,
            "file": f"{seq:03d}_{safe_label}.jpg",
            "original_name": original_name,
            "label": label or f"판서 {seq}",
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "slides": [],
        }
        entries.append(entry)
    os.makedirs(os.path.join(result_dir, BOARDS_DIR), exist_ok=True)
    dest = os.path.join(result_dir, BOARDS_DIR, entry["file"])
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        if max(img.size) > 2000:
            img.thumbnail((2000, 2000), Image.LANCZOS)
        img.convert("RGB").save(dest, "JPEG", quality=88, optimize=True)
    entry["size"] = os.path.getsize(dest)
    _write_boards_index(result_dir, entries)
    return entry


def set_board_slides(result_dir: str, sid: str, slides: list):
    entries = list_boards(result_dir)
    for e in entries:
        if e.get("sid") == sid:
            e["slides"] = sorted(set(int(s) for s in slides))
    _write_boards_index(result_dir, entries)


def rename_board(result_dir: str, sid: str, new_label: str):
    """판서 사진의 표시 이름을 바꾸고, 슬라이드에 붙은 절의 제목/이미지 alt 도 함께 갱신한다. 반환: 갱신된 항목 또는 None"""
    new_label = (new_label or "").strip()
    if not new_label:
        return None
    entries = list_boards(result_dir)
    entry = next((e for e in entries if e.get("sid") == sid), None)
    if not entry:
        return None
    old_label = entry.get("label", "")
    entry["label"] = new_label
    _write_boards_index(result_dir, entries)
    if old_label and old_label != new_label:
        for idx, text in read_slides(result_dir).items():
            block_re = re.compile(
                re.escape(_marker(f"{sid}-s{idx}"))
                + r".*?"
                + re.escape(_marker(f"{sid}-s{idx}", end=True)),
                re.S,
            )
            m = block_re.search(text)
            if not m:
                continue
            block = m.group(0)
            block = block.replace(
                f"{BOARD_SECTION_HEADING} ({old_label} ·",
                f"{BOARD_SECTION_HEADING} ({new_label} ·",
                1,
            )
            block = block.replace(
                f"![{old_label}](./{BOARDS_DIR}/", f"![{new_label}](./{BOARDS_DIR}/", 1
            )
            if block != m.group(0):
                write_slide(
                    result_dir, idx, text[: m.start()] + block + text[m.end() :]
                )
    return entry


def describe_boards(result_dir: str, url_base: str) -> list:
    items = []
    for e in list_boards(result_dir):
        item = dict(e)
        item["url"] = f"{url_base}/{BOARDS_DIR}/{e['file']}"
        items.append(item)
    return items


def describe_transcripts(result_dir: str, url_base: str) -> list:
    """뷰어 목록용: index 항목에 다운로드/재생 URL 을 붙인다."""
    items = []
    for e in list_transcripts(result_dir):
        item = dict(e)
        item["text_url"] = f"{url_base}/{TRANSCRIPTS_DIR}/{e['file']}"
        if e.get("original"):
            item["original_url"] = f"{url_base}/{TRANSCRIPTS_DIR}/{e['original']}"
            ext = os.path.splitext(e["original"])[1].lower()
            item["playable"] = ext in BROWSER_PLAYABLE_EXTS
        else:
            item["original_url"] = None
            item["playable"] = False
        items.append(item)
    return items


def render_pdf(result_dir: str, md_content: str = None):
    """합쳐진 마크다운을 result.pdf 로 저장 (wkhtmltopdf). 호출측에서 GENERATE_PDF 를 확인할 것."""
    import markdown
    import pdfkit

    if md_content is None:
        md_content = compose_markdown(result_dir)
    raw_html = markdown.markdown(md_content)
    abs_image_dir = os.path.abspath(os.path.join(result_dir, IMAGES_DIR)).replace(
        "\\", "/"
    )
    abs_board_dir = os.path.abspath(os.path.join(result_dir, BOARDS_DIR)).replace(
        "\\", "/"
    )
    pdf_html_body = raw_html.replace("./images", f"file://{abs_image_dir}").replace(
        "./boards", f"file://{abs_board_dir}"
    )

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: sans-serif; padding: 20px; line-height: 1.6; }}
            img {{ max-width: 100%; height: auto; display: block; margin: 20px auto; border: 1px solid #ddd; }}
            h2 {{ border-bottom: 2px solid #333; padding-bottom: 10px; margin-top: 30px; page-break-before: always; }}
            h2:first-of-type {{ page-break-before: auto; }}
            blockquote {{ background: #f9f9f9; border-left: 10px solid #ccc; margin: 1.5em 10px; padding: 0.5em 10px; }}
        </style>
    </head>
    <body>
        {pdf_html_body}
    </body>
    </html>
    """
    pdf_options = {
        "quiet": "",
        "enable-local-file-access": "",
        "encoding": "UTF-8",
        "no-outline": None,
    }
    pdfkit.from_string(
        full_html, os.path.join(result_dir, "result.pdf"), options=pdf_options
    )
