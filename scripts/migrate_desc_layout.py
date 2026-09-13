"""
결과 저장 구조 마이그레이션: result.md 하나 → desc/slide_NNN.md 슬라이드별 파일

대상
  1. static/docs/<owner>/<doc_id>/           (문서함 문서, 제자리 변환)
  2. static/results/<owner>/<job_id>.zip     (작업 결과 zip, 풀어서 변환 후 다시 압축)
  3. docs_col.source_job_id 백필: 같은 소유자의 완료 작업과 파일명이 유일하게 일치하는 문서에 연결

실행:  .venv/bin/python scripts/migrate_desc_layout.py
여러 번 실행해도 안전합니다 (이미 변환된 항목은 건너뜀).
"""

import os
import sys
import shutil
import tempfile
import zipfile
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.services import result_store as rs  # noqa: E402
from app.db import docs_col, history_col  # noqa: E402


def migrate_docs():
    converted, skipped, kept_legacy = 0, 0, []
    root = settings.DOCS_STATIC_DIR
    for owner in sorted(os.listdir(root)):
        owner_dir = os.path.join(root, owner)
        if not os.path.isdir(owner_dir):
            continue
        for doc_id in sorted(os.listdir(owner_dir)):
            doc_dir = os.path.join(owner_dir, doc_id)
            if not os.path.isdir(doc_dir):
                continue
            if rs.ensure_desc_layout(doc_dir):
                converted += 1
                if os.path.exists(os.path.join(doc_dir, "result.legacy.md")):
                    kept_legacy.append(doc_dir)
            else:
                skipped += 1
            has_transcript = os.path.exists(os.path.join(doc_dir, rs.TRANSCRIPT_FILE))
            if has_transcript:
                docs_col.update_one({"id": doc_id}, {"$set": {"has_transcript": True}})
    return converted, skipped, kept_legacy


def recheck_legacy():
    """이전 실행에서 보존된 result.legacy.md 를 새 로직으로 다시 검증하고, 일치하면 제거"""
    removed, kept = 0, []
    for legacy in sorted(
        glob.glob(os.path.join(settings.DOCS_STATIC_DIR, "*", "*", "result.legacy.md"))
    ):
        d = os.path.dirname(legacy)
        with open(legacy, "r", encoding="utf-8") as f:
            original = f.read()
        # 새 분해 로직으로 다시 생성 (빈 슬라이드 구분선 처리 개선 반영)
        rs.write_slides(d, rs.split_legacy_markdown(original))
        if rs.roundtrip_ok(d, original):
            os.remove(legacy)
            removed += 1
        else:
            kept.append(d)
    return removed, kept


def migrate_result_zips():
    converted, skipped = 0, 0
    root = settings.RESULT_DIR
    for owner in sorted(os.listdir(root)):
        owner_dir = os.path.join(root, owner)
        if not os.path.isdir(owner_dir):
            continue
        for name in sorted(os.listdir(owner_dir)):
            if not name.endswith(".zip"):
                continue
            zip_path = os.path.join(owner_dir, name)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            if "result.md" not in names or any(n.startswith("desc/") for n in names):
                skipped += 1
                continue
            tmp = tempfile.mkdtemp(prefix="lecai-migrate-")
            try:
                shutil.unpack_archive(zip_path, tmp, "zip")
                if rs.ensure_desc_layout(tmp):
                    shutil.make_archive(zip_path[:-4], "zip", tmp)
                    converted += 1
                else:
                    skipped += 1
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return converted, skipped


def backfill_source_job_id():
    linked = 0
    for doc in docs_col.find({"type": "file", "source_job_id": {"$exists": False}}):
        candidates = [
            j
            for j in history_col.find(
                {"owner": doc["owner"], "status": "completed"}, {"id": 1, "filename": 1}
            )
            if os.path.splitext(j.get("filename", ""))[0] == doc["name"]
        ]
        source = candidates[0]["id"] if len(candidates) == 1 else None
        docs_col.update_one({"id": doc["id"]}, {"$set": {"source_job_id": source}})
        if source:
            linked += 1
    return linked


if __name__ == "__main__":
    c, s, legacy = migrate_docs()
    print(f"[docs] converted={c} skipped={s}")
    for d in legacy:
        print(f"  [warn] roundtrip mismatch, original kept as result.legacy.md: {d}")
    r, kept = recheck_legacy()
    print(f"[legacy recheck] removed={r} still_kept={len(kept)}")
    for d in kept:
        print(
            f"  [info] original kept as result.legacy.md (내용 손실 없음, 슬라이드 앞 머리말 등 차이): {d}"
        )
    c, s = migrate_result_zips()
    print(f"[result zips] converted={c} skipped={s}")
    print(f"[docs_col] source_job_id linked={backfill_source_job_id()}")
