"""한글 hwp (HWP 5.0, OLE 형식) → 문단 HTML.

LibreOffice 는 HWP 97 이전 형식만 읽어서, 요즘 hwp 는 직접 본문 레코드(BodyText/SectionN)를 읽어
글자(문단 · 표 안 글자 포함)만 뽑는다. 그림 · 글자 모양은 빠진다.
암호가 걸렸거나 배포용 문서는 본문이 암호화돼 있어 변환할 수 없다.
"""

import html
import re
import struct
import zlib

import olefile

TAG_PARA_TEXT = 67  # HWPTAG_BEGIN(16) + 51
MAX_SECTION_BYTES = 80 * 1024 * 1024  # 압축 해제 상한 (압축 폭탄 방지)
_ONE_WCHAR = {
    0,
    10,
    13,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
}  # 나머지 제어 문자는 8 wchar 를 차지


class Hwp5Error(RuntimeError):
    pass


def _inflate(data: bytes) -> bytes:
    inflater = zlib.decompressobj(-15)
    out = inflater.decompress(data, MAX_SECTION_BYTES)
    if inflater.unconsumed_tail:
        raise Hwp5Error("문서가 너무 커요.")
    return out


def _records(data: bytes):
    pos = 0
    while pos + 4 <= len(data):
        (header,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, size = header & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > len(data):
                return
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, data[pos : pos + size]
        pos += size


def para_text(payload: bytes) -> str:
    count = len(payload) // 2
    chars = struct.unpack_from(f"<{count}H", payload)
    out, i = [], 0
    while i < count:
        code = chars[i]
        if code >= 32:
            out.append(chr(code))
            i += 1
        elif code in _ONE_WCHAR:
            out.append({10: "\n", 30: " ", 31: " "}.get(code, ""))
            i += 1
        else:
            if code == 9:
                out.append("\t")
            i += 8
    text = "".join(out)
    # 홀로 남은 서로게이트(잘린 글자) 제거
    return re.sub(r"[\ud800-\udfff]", "", text)


def section_paragraphs(data: bytes) -> list[str]:
    return [
        para_text(payload) for tag, payload in _records(data) if tag == TAG_PARA_TEXT
    ]


def hwp5_to_html(path: str) -> str:
    try:
        ole = olefile.OleFileIO(path)
    except (OSError, ValueError) as error:
        raise Hwp5Error("hwp 문서를 읽지 못했습니다.") from error
    with ole:
        if not ole.exists("FileHeader"):
            raise Hwp5Error("hwp 문서를 읽지 못했습니다.")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise Hwp5Error("HWP 5.0 문서가 아닙니다.")
        flags = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
        if flags & 0b110:
            raise Hwp5Error("암호가 걸렸거나 배포용인 hwp 문서는 변환할 수 없어요.")
        compressed = bool(flags & 1)
        sections = sorted(
            (
                entry
                for entry in ole.listdir()
                if len(entry) == 2
                and entry[0] == "BodyText"
                and re.fullmatch(r"Section\d+", entry[1])
            ),
            key=lambda entry: int(entry[1][7:]),
        )
        pages, has_text = [], False
        for entry in sections:
            raw = ole.openstream(entry).read()
            try:
                data = _inflate(raw) if compressed else raw
            except zlib.error as error:
                raise Hwp5Error("hwp 본문을 풀지 못했습니다.") from error
            paragraphs = section_paragraphs(data)
            has_text = has_text or any(text.strip() for text in paragraphs)
            pages.append(
                "".join(
                    f'<p class="hwp-p">{html.escape(text) if text.strip() else "&nbsp;"}</p>'
                    for text in paragraphs
                )
            )
    if not has_text:
        raise Hwp5Error("문서에서 글자를 찾지 못했습니다.")
    return "".join(
        f'<section class="hwp5-section">{page}</section>' for page in pages if page
    )
