# app/services/transcript_input.py
"""
강의 녹음본 입력 처리: 텍스트 문서에서 본문 추출 / 음성 파일 판별.

지원 텍스트 문서
  - 일반 텍스트: txt, md, markdown, text, log, csv, tsv, json, xml, srt, vtt, sbv, lrc, html, htm
  - 워드프로세서: docx (내장 파서), doc, rtf, odt, hwp, hwpx, pages* (LibreOffice 변환)
  - PDF: pdftotext (poppler)
지원 음성 파일 (STT 서버로 전달)
  - mp3, m4a, m4b, wav, flac, ogg, oga, opus, webm, aac, wma, amr, aiff, aif, caf, mp4, mov, mkv
"""

import os
import re
import html
import shutil
import zipfile
import tempfile
import subprocess
import xml.etree.ElementTree as ET

PLAIN_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".text",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".srt",
    ".vtt",
    ".sbv",
    ".lrc",
    ".html",
    ".htm",
}
OFFICE_EXTS = {".docx", ".doc", ".rtf", ".odt", ".hwp", ".hwpx", ".pages", ".wps"}
PDF_EXTS = {".pdf"}
TEXT_DOC_EXTS = PLAIN_TEXT_EXTS | OFFICE_EXTS | PDF_EXTS

AUDIO_EXTS = (
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

ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16")


def ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def is_audio(filename: str) -> bool:
    return ext_of(filename) in AUDIO_EXTS


def is_text_doc(filename: str) -> bool:
    return ext_of(filename) in TEXT_DOC_EXTS


def decode_bytes(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# ---------- 자막/마크업 정리 ----------
_SRT_INDEX = re.compile(r"^\s*\d+\s*$", re.M)
_TIMESTAMP_LINE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}.*$",
    re.M,
)
_SBV_TIME = re.compile(r"^\s*\d+:\d{2}:\d{2}\.\d{3},\d+:\d{2}:\d{2}\.\d{3}\s*$", re.M)
_LRC_TIME = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
_VTT_TAG = re.compile(r"<[^>]+>")


def clean_subtitles(text: str, ext: str) -> str:
    if ext == ".vtt":
        text = re.sub(r"^WEBVTT.*?\n\n", "", text, count=1, flags=re.S)
        text = re.sub(
            r"^(NOTE|STYLE|REGION)\b.*?(?:\n\n|\Z)", "", text, flags=re.S | re.M
        )
    text = _TIMESTAMP_LINE.sub("", text)
    text = _SBV_TIME.sub("", text)
    text = _LRC_TIME.sub("", text)
    text = _SRT_INDEX.sub("", text)
    text = _VTT_TAG.sub("", text)
    return text


def html_to_text(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


# ---------- 문서 파서 ----------
def docx_to_text(path: str) -> str:
    """python-docx 없이 word/document.xml 의 문단 텍스트를 추출"""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        runs = []
        for node in p.iter():
            if node.tag == f"{{{ns['w']}}}t" and node.text:
                runs.append(node.text)
            elif node.tag in (f"{{{ns['w']}}}tab",):
                runs.append("\t")
            elif node.tag in (f"{{{ns['w']}}}br", f"{{{ns['w']}}}cr"):
                runs.append("\n")
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


def pdf_to_text(path: str) -> str:
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-layout", path, "-"],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pdftotext 실패: {result.stderr.decode('utf-8', 'ignore')[:200]}"
        )
    return decode_bytes(result.stdout)


def office_to_text(path: str) -> str:
    """LibreOffice 로 txt 변환 (doc, rtf, odt, hwp, hwpx 등)"""
    outdir = tempfile.mkdtemp(prefix="lecai-txt-")
    try:
        subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to",
                "txt:Text (encoded):UTF8",
                "--outdir",
                outdir,
                path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(outdir, f"{base}.txt")
        if not os.path.exists(out):
            raise RuntimeError("LibreOffice 변환 결과 파일이 없습니다.")
        with open(out, "rb") as f:
            return decode_bytes(f.read())
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def extract_text(path: str, filename: str = None) -> str:
    """텍스트 문서 파일에서 본문을 추출. 지원하지 않는 형식이면 ValueError."""
    ext = ext_of(filename or path)
    if ext in PLAIN_TEXT_EXTS:
        with open(path, "rb") as f:
            text = decode_bytes(f.read())
        if ext in (".srt", ".vtt", ".sbv", ".lrc"):
            text = clean_subtitles(text, ext)
        elif ext in (".html", ".htm"):
            text = html_to_text(text)
    elif ext == ".docx":
        try:
            text = docx_to_text(path)
        except Exception:
            text = office_to_text(path)
    elif ext in PDF_EXTS:
        text = pdf_to_text(path)
    elif ext in OFFICE_EXTS:
        text = office_to_text(path)
    else:
        raise ValueError(
            f"지원하지 않는 텍스트 문서 형식입니다: {ext or '(확장자 없음)'}"
        )

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"^[ \t]+", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
