"""PDF 모아찍기 (한 장에 여러 쪽). 쪽 내용을 벡터 그대로 줄여 새 종이에 배치한다.

옵션 (normalize 로 정리한 dict):
- n: 한 장에 몇 쪽 (1 · 2 · 4 · 6 · 8 · 9 · 16)
- order: 채우는 순서 — z(가로로 왼→오, 다음 줄) · rz(가로로 오→왼) · n(세로로 위→아래, 왼쪽 열부터) · rn(세로, 오른쪽 열부터)
- sheet: 종이 방향 — auto(쪽이 가장 크게 보이는 쪽으로) · portrait · landscape
- paper: 종이 크기 — a4 · letter · source(원본 쪽 크기)
- spacing: 여백 — none · narrow · wide
- border: 쪽마다 얇은 테두리
"""

import secrets
from pathlib import Path

COUNTS = (1, 2, 4, 6, 8, 9, 16)
ORDERS = ("z", "rz", "n", "rn")
SHEETS = ("auto", "portrait", "landscape")
PAPERS = {"a4": (595.28, 841.89), "letter": (612.0, 792.0), "source": None}
SPACING = {
    "none": (0.0, 0.0),
    "narrow": (14.0, 8.0),
    "wide": (28.0, 16.0),
}  # (가장자리, 쪽 사이) pt
# 한 장에 n 쪽일 때 가능한 (열, 행)
GRIDS = {
    2: [(2, 1), (1, 2)],
    4: [(2, 2)],
    6: [(3, 2), (2, 3)],
    8: [(4, 2), (2, 4)],
    9: [(3, 3)],
    16: [(4, 4)],
}
DEFAULT = {
    "n": 1,
    "order": "z",
    "sheet": "auto",
    "paper": "a4",
    "spacing": "narrow",
    "border": False,
}


def normalize(value) -> dict:
    value = value if isinstance(value, dict) else {}
    out = dict(DEFAULT)
    try:
        n = int(value.get("n", 1))
    except TypeError, ValueError:
        n = 1
    out["n"] = n if n in COUNTS else 1
    if value.get("order") in ORDERS:
        out["order"] = value["order"]
    if value.get("sheet") in SHEETS:
        out["sheet"] = value["sheet"]
    if value.get("paper") in PAPERS:
        out["paper"] = value["paper"]
    if value.get("spacing") in SPACING:
        out["spacing"] = value["spacing"]
    out["border"] = bool(value.get("border"))
    return out


def page_ratio(source: Path) -> float | None:
    """첫 쪽의 가로/세로 비 (돌려 놓은 쪽은 보이는 방향으로)."""
    from pypdf import PdfReader

    try:
        page = PdfReader(str(source)).pages[0]
        box = page.cropbox
        width, height = float(box.width), float(box.height)
        if int(page.get("/Rotate") or 0) % 180 == 90:
            width, height = height, width
        return round(width / height, 4) if width > 0 and height > 0 else None
    except Exception:
        return None


def plan(layout: dict, page_w: float, page_h: float) -> dict:
    """종이 크기 · 방향과 격자를 고른다 (쪽이 가장 크게 들어가는 조합)."""
    paper = PAPERS[layout["paper"]] or (page_w, page_h)
    short, long_ = sorted(paper)
    margin, gap = SPACING[layout["spacing"]]
    sheets = {
        "portrait": [(short, long_)],
        "landscape": [(long_, short)],
        "auto": [(short, long_), (long_, short)],
    }[layout["sheet"]]
    best = None
    for sheet_w, sheet_h in sheets:
        for cols, rows in GRIDS[layout["n"]]:
            cell_w = (sheet_w - 2 * margin - (cols - 1) * gap) / cols
            cell_h = (sheet_h - 2 * margin - (rows - 1) * gap) / rows
            scale = min(cell_w / page_w, cell_h / page_h)
            if best is None or scale > best["scale"] + 1e-6:
                best = {
                    "sheet": (sheet_w, sheet_h),
                    "cols": cols,
                    "rows": rows,
                    "cell": (cell_w, cell_h),
                    "scale": scale,
                }
    return best


def slots(order: str, cols: int, rows: int) -> list[tuple[int, int]]:
    """(열, 행) 을 채우는 순서. 행 0 이 위."""
    if order in ("z", "rz"):
        cells = [(c, r) for r in range(rows) for c in range(cols)]
    else:
        cells = [(c, r) for c in range(cols) for r in range(rows)]
    if order in ("rz", "rn"):
        cells = [(cols - 1 - c, r) for c, r in cells]
    return cells


def nup(source: Path, target: Path, layout: dict) -> dict:
    """source 의 쪽들을 layout 대로 모아 target 에 쓴다. 만든 장 수와 격자를 돌려준다."""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation
    from pypdf.generic import ContentStream

    layout = normalize(layout)
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        reader.decrypt("")
    pages = list(reader.pages)
    if not pages:
        raise ValueError("빈 PDF")
    for page in pages:  # 돌려 놓은 쪽은 보이는 방향 그대로 내용을 돌려 둔다
        if int(page.get("/Rotate") or 0) % 360:
            page.transfer_rotation_to_content()
    first = pages[0].cropbox
    p = plan(layout, float(first.width), float(first.height))
    sheet_w, sheet_h = p["sheet"]
    cols, rows = p["cols"], p["rows"]
    cell_w, cell_h = p["cell"]
    margin, gap = SPACING[layout["spacing"]]
    order = slots(layout["order"], cols, rows)
    per = cols * rows

    writer = PdfWriter()
    for start in range(0, len(pages), per):
        sheet = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
        frames = []
        for page, (col, row) in zip(pages[start : start + per], order):
            box = page.cropbox
            width, height = float(box.width), float(box.height)
            scale = min(cell_w / width, cell_h / height)
            # 칸 안 가운데 (PDF 좌표는 아래가 0)
            x = margin + col * (cell_w + gap) + (cell_w - width * scale) / 2
            y = (
                sheet_h
                - margin
                - (row + 1) * cell_h
                - row * gap
                + (cell_h - height * scale) / 2
            )
            sheet.merge_transformed_page(
                page,
                Transformation()
                .translate(-float(box.left), -float(box.bottom))
                .scale(scale)
                .translate(x, y),
                expand=False,
            )
            frames.append((x, y, width * scale, height * scale))
        if layout["border"] and frames:
            ops = "q 0.55 0.58 0.63 RG 0.6 w " + " ".join(
                f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re S" for x, y, w, h in frames
            )
            ops += " Q"
            stamp = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
            stream = ContentStream(None, None)
            stream.set_data(ops.encode())
            stamp.replace_contents(stream)
            sheet.merge_page(stamp)
        writer.add_page(sheet)
    # 합친 쪽 내용은 압축이 풀린 채라 다시 압축하고, 쪽마다 복사된 글꼴 · 그림은 하나로 합친다
    for sheet in writer.pages:
        sheet.compress_content_streams()
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    with tmp.open("wb") as handle:
        writer.write(handle)
    tmp.replace(target)
    return {
        "sheets": len(writer.pages),
        "cols": cols,
        "rows": rows,
        "landscape": sheet_w > sheet_h,
    }
