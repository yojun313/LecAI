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
import shutil

DESC_DIR = "desc"
IMAGES_DIR = "images"
LEGACY_MD = "result.md"
TRANSCRIPT_FILE = "transcript.txt"
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


def slide_block(result_dir: str, idx: int, text: str) -> str:
    """뷰어/PDF 용 슬라이드 블록. 이미지가 있으면 함께 넣는다."""
    img = image_for_slide(result_dir, idx)
    header = f"## Slide {idx}\n\n"
    image = f"![{img}](./{IMAGES_DIR}/{img})\n\n" if img else ""
    return f"{header}{image}{text.strip()}\n\n---\n\n"


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


def upsert_transcript_section(text: str, section: str) -> str:
    """슬라이드 본문에 녹음 발췌 절을 추가한다. 이미 있으면 교체."""
    text = (text or "").rstrip()
    pattern = re.compile(
        re.escape(TRANSCRIPT_SECTION_HEADING) + r".*?(?=\n#{1,3} |\Z)", re.S
    )
    if pattern.search(text):
        text = pattern.sub(section.strip() + "\n", text, count=1).rstrip()
    else:
        text = f"{text}\n\n{section.strip()}"
    return text + "\n"


def save_transcript_copy(result_dir: str, transcript: str):
    with open(os.path.join(result_dir, TRANSCRIPT_FILE), "w", encoding="utf-8") as f:
        f.write(transcript)


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
    pdf_html_body = raw_html.replace("./images", f"file://{abs_image_dir}")

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
