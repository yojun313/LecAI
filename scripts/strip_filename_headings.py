"""
슬라이드 설명 파일(desc/slide_NNN.md)에서 파일명 제목 줄("## page_001.png" 등)을 제거한다.
대상: static/docs/<owner>/<doc_id>/desc/*.md, static/results/<owner>/<job_id>.zip 안의 desc/*.md
실행: .venv/bin/python scripts/strip_filename_headings.py   (여러 번 실행해도 안전)
"""

import os
import sys
import glob
import shutil
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings  # noqa: E402
from app.services import result_store as rs  # noqa: E402


def strip_dir(result_dir: str):
    changed = 0
    for path in glob.glob(os.path.join(result_dir, rs.DESC_DIR, "slide_*.md")):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        new = rs.strip_filename_headings(text).lstrip("\n")
        if new != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new.rstrip() + "\n")
            changed += 1
    return changed


def main():
    docs_changed, docs_files = 0, 0
    for d in glob.glob(os.path.join(settings.DOCS_STATIC_DIR, "*", "*")):
        if not os.path.isdir(os.path.join(d, rs.DESC_DIR)):
            continue
        n = strip_dir(d)
        docs_files += n
        docs_changed += 1 if n else 0
    print(f"[docs] documents touched={docs_changed}, slide files changed={docs_files}")

    zips_changed, zip_files = 0, 0
    for z in glob.glob(os.path.join(settings.RESULT_DIR, "*", "*.zip")):
        with zipfile.ZipFile(z) as zf:
            if not any(n.startswith("desc/") for n in zf.namelist()):
                continue
        tmp = tempfile.mkdtemp(prefix="lecai-strip-")
        try:
            shutil.unpack_archive(z, tmp, "zip")
            n = strip_dir(tmp)
            if n:
                shutil.make_archive(z[:-4], "zip", tmp)
                zips_changed += 1
                zip_files += n
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(
        f"[result zips] zips rewritten={zips_changed}, slide files changed={zip_files}"
    )


if __name__ == "__main__":
    main()
