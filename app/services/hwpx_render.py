"""한글 hwpx → HTML (UnivDash 파일 뷰어의 렌더러를 그대로 옮김).

LibreOffice 는 hwpx 를 못 읽어서 직접 해석해 문단 · 글자 모양 · 표 · 그림을 HTML 로 그린다.
그림은 data: URI 로 넣는다 (외부 · 로컬 파일을 참조하지 않음).
"""

import base64
import html
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

MAX_ZIP_MEMBER = (
    60 * 1024 * 1024
)  # hwpx 안의 XML · 그림 하나의 최대 크기 (압축 폭탄 방지)
MAX_INLINE_IMAGES = 25 * 1024 * 1024


class HwpxError(RuntimeError):
    pass


NS = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "opf": "http://www.idpf.org/2007/opf/",
}
HP = "{%s}" % NS["hp"]
HC = "{%s}" % NS["hc"]


def _px(hwpunit) -> float:
    """HWPUNIT(1/7200 인치) → CSS px (1/96 인치)."""
    try:
        return round(int(hwpunit) / 75, 1)
    except TypeError, ValueError:
        return 0.0


def _border_css(element) -> str | None:
    if element is None or element.get("type", "NONE") == "NONE":
        return None
    try:
        width = max(1, round(float(element.get("width", "0.12").split()[0]) * 3.78))
    except ValueError:
        width = 1
    style = {
        "DASH": "dashed",
        "DOT": "dotted",
        "DOUBLE_SLIM": "double",
        "DOUBLE": "double",
    }.get(element.get("type"), "solid")
    return f"{width}px {style} {element.get('color', '#000')}"


class _Hwpx:
    def __init__(self, archive: zipfile.ZipFile):
        self.zip = archive
        self.chars: dict[str, str] = {}
        self.paras: dict[str, str] = {}
        self.borders: dict[str, str] = {}
        self.fonts: dict[str, str] = {}
        self.bin: dict[str, str] = {}
        self.image_bytes = 0
        self._styles()

    def read(self, name: str) -> bytes:
        info = self.zip.getinfo(name)
        if info.file_size > MAX_ZIP_MEMBER:
            raise HwpxError("문서 안의 항목이 너무 큽니다.")
        return self.zip.read(info)

    def _styles(self) -> None:
        try:
            head = ET.fromstring(self.read("Contents/header.xml"))
        except KeyError, ET.ParseError:
            return
        for face in head.iter("{%s}fontface" % NS["hh"]):
            if face.get("lang") == "HANGUL":
                for font in face.iter("{%s}font" % NS["hh"]):
                    self.fonts[font.get("id")] = font.get("face", "")
        for pr in head.iter("{%s}charPr" % NS["hh"]):
            css = []
            try:
                css.append(f"font-size:{int(pr.get('height', '1000')) / 100:.1f}pt")
            except ValueError:
                pass
            if pr.get("textColor") and pr.get("textColor") not in ("#000000", "none"):
                css.append(f"color:{pr.get('textColor')}")
            if pr.get("shadeColor") and pr.get("shadeColor") not in ("none", "#FFFFFF"):
                css.append(f"background:{pr.get('shadeColor')}")
            if pr.find("{%s}bold" % NS["hh"]) is not None:
                css.append("font-weight:700")
            if pr.find("{%s}italic" % NS["hh"]) is not None:
                css.append("font-style:italic")
            under = pr.find("{%s}underline" % NS["hh"])
            strike = pr.find("{%s}strikeout" % NS["hh"])
            deco = []
            if under is not None and under.get("type", "NONE") != "NONE":
                deco.append("underline")
            if strike is not None and strike.get("shape", "NONE") not in ("NONE", ""):
                deco.append("line-through")
            if deco:
                css.append(f"text-decoration:{' '.join(deco)}")
            ref = pr.find("{%s}fontRef" % NS["hh"])
            if ref is not None and self.fonts.get(ref.get("hangul")):
                css.append(
                    f"font-family:'{self.fonts[ref.get('hangul')]}',var(--hwp-font)"
                )
            self.chars[pr.get("id")] = ";".join(css)
        for pr in head.iter("{%s}paraPr" % NS["hh"]):
            css = []
            align = pr.find("{%s}align" % NS["hh"])
            if align is not None:
                css.append(
                    "text-align:"
                    + {
                        "CENTER": "center",
                        "RIGHT": "right",
                        "JUSTIFY": "justify",
                        "DISTRIBUTE": "justify",
                    }.get(align.get("horizontal"), "left")
                )
            margin = pr.find(".//{%s}margin" % NS["hh"])
            if margin is not None:
                for child, prop in (
                    ("left", "margin-left"),
                    ("right", "margin-right"),
                    ("prev", "margin-top"),
                    ("next", "margin-bottom"),
                    ("intent", "text-indent"),
                ):
                    element = margin.find(HC + child)
                    if element is not None and element.get("value") not in (None, "0"):
                        css.append(f"{prop}:{_px(element.get('value')) / 2}px")
            spacing = pr.find(".//{%s}lineSpacing" % NS["hh"])
            if spacing is not None and spacing.get("type") == "PERCENT":
                try:
                    css.append(
                        f"line-height:{max(1.0, int(spacing.get('value', '160')) / 100):.2f}"
                    )
                except ValueError:
                    pass
            self.paras[pr.get("id")] = ";".join(css)
        for fill in head.iter("{%s}borderFill" % NS["hh"]):
            css = []
            for side in ("left", "right", "top", "bottom"):
                value = _border_css(fill.find("{%s}%sBorder" % (NS["hh"], side)))
                css.append(f"border-{side}:{value}" if value else f"border-{side}:0")
            brush = fill.find(".//{%s}winBrush" % NS["hc"])
            if brush is not None and brush.get("faceColor") not in (
                None,
                "none",
                "#FFFFFF",
            ):
                css.append(f"background:{brush.get('faceColor')}")
            self.borders[fill.get("id")] = ";".join(css)
        try:
            manifest = ET.fromstring(self.read("Contents/content.hpf"))
            for item in manifest.iter("{%s}item" % NS["opf"]):
                self.bin[item.get("id")] = item.get("href", "")
        except KeyError, ET.ParseError:
            pass

    # 문단 · 글자
    def paragraph(self, p) -> str:
        parts = []
        for run in p.findall(HP + "run"):
            style = self.chars.get(run.get("charPrIDRef"), "")
            text = []
            for child in run:
                tag = child.tag
                if tag == HP + "t":
                    text.append(self.text(child))
                elif tag == HP + "tbl":
                    if text:
                        parts.append(f'<span style="{style}">{"".join(text)}</span>')
                        text = []
                    parts.append(self.table(child))
                elif tag in (HP + "pic", HP + "picture"):
                    parts.append(self.picture(child))
                elif tag in (
                    HP + "rect",
                    HP + "container",
                    HP + "ellipse",
                    HP + "polygon",
                ):
                    parts.append(self.shape(child))
                elif tag in (HP + "equation",):
                    script = child.find(HP + "script")
                    if script is not None and script.text:
                        parts.append(
                            f'<code class="hwp-eq">{html.escape(script.text)}</code>'
                        )
            if text:
                parts.append(f'<span style="{style}">{"".join(text)}</span>')
        body = "".join(parts) or "&nbsp;"
        return f'<p class="hwp-p" style="{self.paras.get(p.get("paraPrIDRef"), "")}">{body}</p>'

    def text(self, t) -> str:
        out = [html.escape(t.text or "")]
        for child in t:
            if child.tag == HP + "tab":
                out.append('<span class="hwp-tab"></span>')
            elif child.tag == HP + "lineBreak":
                out.append("<br>")
            elif child.tag in (HP + "nbSpace", HP + "fwSpace"):
                out.append("&nbsp;")
            out.append(html.escape(child.tail or ""))
        return "".join(out)

    def sublist(self, element) -> str:
        sub = element.find(HP + "subList")
        if sub is None:
            sub = element.find(".//" + HP + "subList")
        return (
            "".join(self.paragraph(p) for p in sub.findall(HP + "p"))
            if sub is not None
            else ""
        )

    def table(self, tbl) -> str:
        rows = []
        for tr in tbl.findall(HP + "tr"):
            cells = []
            for tc in tr.findall(HP + "tc"):
                span = tc.find(HP + "cellSpan")
                size = tc.find(HP + "cellSz")
                margin = tc.find(HP + "cellMargin")
                attrs = []
                if span is not None:
                    if span.get("colSpan", "1") != "1":
                        attrs.append(f'colspan="{int(span.get("colSpan"))}"')
                    if span.get("rowSpan", "1") != "1":
                        attrs.append(f'rowspan="{int(span.get("rowSpan"))}"')
                css = [
                    self.borders.get(tc.get("borderFillIDRef"), "border:1px solid #000")
                ]
                if size is not None:
                    css.append(
                        f"width:{_px(size.get('width'))}px;height:{_px(size.get('height'))}px"
                    )
                if margin is not None:
                    css.append(
                        f"padding:{_px(margin.get('top'))}px {_px(margin.get('right'))}px {_px(margin.get('bottom'))}px {_px(margin.get('left'))}px"
                    )
                sub = tc.find(HP + "subList")
                if sub is not None:
                    css.append(
                        "vertical-align:"
                        + {"CENTER": "middle", "BOTTOM": "bottom"}.get(
                            sub.get("vertAlign"), "top"
                        )
                    )
                cells.append(
                    f'<td {" ".join(attrs)} style="{";".join(css)}">{self.sublist(tc)}</td>'
                )
            rows.append(f"<tr>{''.join(cells)}</tr>")
        size = tbl.find(HP + "sz")
        width = f"width:{_px(size.get('width'))}px" if size is not None else ""
        return f'<table class="hwp-table" style="{width}">{"".join(rows)}</table>'

    def picture(self, pic) -> str:
        image = pic.find(".//" + HC + "img")
        if image is None:
            return ""
        href = self.bin.get(image.get("binaryItemIDRef"), "")
        if not href or href not in self.zip.namelist():
            return ""
        info = self.zip.getinfo(href)
        if self.image_bytes + info.file_size > MAX_INLINE_IMAGES:
            return '<span class="hwp-missing">[그림 생략: 문서가 너무 큼]</span>'
        self.image_bytes += info.file_size
        ext = Path(href).suffix.lower().lstrip(".")
        mime = {
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "png": "png",
            "gif": "gif",
            "bmp": "bmp",
            "webp": "webp",
            "svg": "svg+xml",
        }.get(ext)
        if not mime:
            return f'<span class="hwp-missing">[그림: {html.escape(ext)}]</span>'
        size = pic.find(HP + "curSz")
        if size is None or size.get("width") in (None, "0"):
            size = pic.find(HP + "sz")
        width = (
            f'width="{_px(size.get("width"))}"'
            if size is not None and size.get("width") not in (None, "0")
            else ""
        )
        data = base64.b64encode(self.read(href)).decode()
        return f'<img class="hwp-img" {width} src="data:image/{mime};base64,{data}" alt="">'

    def shape(self, shape) -> str:
        draw = shape.find(".//" + HP + "drawText")
        inner = self.sublist(draw if draw is not None else shape)
        pics = "".join(self.picture(p) for p in shape.iter(HP + "pic"))
        return f'<div class="hwp-box">{inner}{pics}</div>' if inner or pics else ""

    def render(self) -> str:
        sections = sorted(
            (
                n
                for n in self.zip.namelist()
                if re.fullmatch(r"Contents/section\d+\.xml", n)
            ),
            key=lambda n: int(re.findall(r"\d+", n)[0]),
        )
        pages = []
        for name in sections:
            root = ET.fromstring(self.read(name))
            page = root.find(".//" + HP + "pagePr")
            width, padding = 794, "72px 76px"
            if page is not None:
                width = _px(page.get("width")) or width
                margin = page.find(HP + "margin")
                if margin is not None:
                    padding = f"{_px(margin.get('top'))}px {_px(margin.get('right'))}px {_px(margin.get('bottom'))}px {_px(margin.get('left'))}px"
            body = "".join(self.paragraph(p) for p in root.findall(HP + "p"))
            pages.append(
                f'<section class="hwp-page" style="width:{width}px;padding:{padding}">{body}</section>'
            )
        return "".join(pages)


def hwpx_to_html(path: str) -> str:
    """hwpx 파일 → 쪽(section.hwp-page) 이 이어진 HTML 조각."""
    try:
        with zipfile.ZipFile(path) as archive:
            rendered = _Hwpx(archive).render()
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise HwpxError("hwpx 문서를 읽지 못했습니다.") from error
    if not rendered.strip():
        raise HwpxError("문서에 내용이 없습니다.")
    return rendered
