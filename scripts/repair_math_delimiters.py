"""
슬라이드 설명에서 짝이 어긋난 수식 구분자를 고친다.
  - "$$" 로 열고 "\\]" 로 닫은(또는 "\\[" 로 열고 "$$" 로 닫은) 블록 → 같은 구분자로 통일
  - 그래도 "$$" 개수가 홀수면 슬라이드 끝에 "$$" 를 붙여 닫는다 (다음 슬라이드로 번지지 않게)
대상: static/docs/*/*/desc/*.md, static/results/*/*.zip
실행: .venv/bin/python scripts/repair_math_delimiters.py
"""

import os
import re
import sys
import glob
import shutil
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings  # noqa: E402
from app.services import result_store as rs  # noqa: E402

# 여는 $$ 다음에 $$ 없이 \] 가 먼저 나오는 경우 → \] 를 $$ 로
MIXED_A = re.compile(r"(^|\n)\$\$([^$]*?)\n\\\]", re.S)
# 여는 \[ 다음에 \] 없이 $$ 가 먼저 나오는 경우 → $$ 를 \] 로
MIXED_B = re.compile(r"(^|\n)\\\[((?:(?!\\\]).)*?)\n\$\$", re.S)


def repair(text: str):
    orig = text
    text = MIXED_A.sub(lambda m: f"{m.group(1)}$${m.group(2)}\n$$", text)
    text = MIXED_B.sub(lambda m: f"{m.group(1)}\\[{m.group(2)}\n\\]", text)
    if text.count("$$") % 2:
        text = text.rstrip() + "\n$$\n"
    fences = len(re.findall(r"^\s*(```|~~~)", text, re.M))
    if fences % 2:
        text = text.rstrip() + "\n```\n"
    return text, text != orig


def repair_dir(result_dir: str):
    fixed = []
    for path in glob.glob(os.path.join(result_dir, rs.DESC_DIR, "slide_*.md")):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        new, changed = repair(text)
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            fixed.append(os.path.basename(path))
    return fixed


def main():
    total = 0
    for d in glob.glob(os.path.join(settings.DOCS_STATIC_DIR, "*", "*")):
        if not os.path.isdir(os.path.join(d, rs.DESC_DIR)):
            continue
        fixed = repair_dir(d)
        if fixed:
            total += len(fixed)
            print(f"[docs] {d}: {fixed}")
    print(f"[docs] slide files repaired={total}")

    zips = 0
    for z in glob.glob(os.path.join(settings.RESULT_DIR, "*", "*.zip")):
        with zipfile.ZipFile(z) as zf:
            if not any(n.startswith("desc/") for n in zf.namelist()):
                continue
        tmp = tempfile.mkdtemp(prefix="lecai-math-")
        try:
            shutil.unpack_archive(z, tmp, "zip")
            if repair_dir(tmp):
                shutil.make_archive(z[:-4], "zip", tmp)
                zips += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"[result zips] rewritten={zips}")


if __name__ == "__main__":
    main()
