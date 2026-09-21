# app/routes/job_routes.py
from fastapi import (
    APIRouter,
    UploadFile,
    File,
    Form,
    BackgroundTasks,
    HTTPException,
    Depends,
)
from app.services.job_manager import JobManager
from app.services.processor import (
    process_file_task,
    transcript_path,
    enrich_transcript_task,
    enrich_boards_task,
)
from app.services import transcript_input as ti
from app.services.transcript_input import AUDIO_EXTS
from app.db import docs_col
from typing import Optional, List
from app.services.audio_processor import process_audio_task, stt_precheck
from app.services.auth_manager import AuthManager
from app.core.config import settings
from app.routes.deps import get_current_user
import shutil
import os

router = APIRouter()


@router.post("/upload")
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    transcript: str = Form(""),
    transcript_file: Optional[UploadFile] = File(None),
    audio_language: str = Form(""),
    auto_import_parent_id: str = Form(""),
    slide_from: str = Form(""),
    slide_to: str = Form(""),
    user: str = Depends(get_current_user),
):
    ext = os.path.splitext(file.filename)[1].lower()
    # 뷰어에서 업로드한 경우: 완료 후 문서함의 지정 폴더로 자동 저장
    auto_import_parent_id = (auto_import_parent_id or "").strip()
    if auto_import_parent_id and auto_import_parent_id != "root":
        folder = docs_col.find_one(
            {"id": auto_import_parent_id, "owner": user, "type": "folder"}
        )
        if not folder:
            raise HTTPException(
                status_code=404, detail="저장할 폴더를 찾을 수 없습니다."
            )
    needs_stt = ext in AUDIO_EXTS or (
        transcript_file
        and transcript_file.filename
        and ti.is_audio(transcript_file.filename)
    )
    if needs_stt:
        problem = stt_precheck(AuthManager.get_user_settings(user))
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    file_path = os.path.join(settings.UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job_id = JobManager.create_job(file.filename, user)

    if ext in AUDIO_EXTS:
        if audio_language.strip():
            JobManager.update_fields(
                job_id, {"transcript_language": audio_language.strip()}
            )
        background_tasks.add_task(process_audio_task, job_id, file_path)
    else:
        if auto_import_parent_id:
            JobManager.update_fields(
                job_id, {"auto_import_parent_id": auto_import_parent_id}
            )
        # 강의 녹음본: 붙여넣은 텍스트 / 텍스트 문서 / 음성 파일 중 하나 (선택)
        try:
            _store_transcript_input(
                job_id,
                transcript,
                transcript_file,
                audio_language,
                _parse_slide_range(slide_from, slide_to),
            )
        except ValueError as e:
            JobManager.delete_job(job_id, user)
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(status_code=400, detail=str(e))
        background_tasks.add_task(process_file_task, job_id, file_path)

    return {"job_id": job_id, "message": "Upload successful"}


def _parse_slide_range(slide_from: str, slide_to: str):
    """'12', '30' → (12, 30). 비어 있으면 None. 잘못된 값은 ValueError."""
    a, b = (slide_from or "").strip(), (slide_to or "").strip()
    if not a and not b:
        return None
    try:
        start = int(a) if a else 1
        end = int(b) if b else 10**6
    except ValueError:
        raise ValueError("적용 범위(페이지)는 숫자로 입력해 주세요.")
    if start < 1 or end < start:
        raise ValueError(
            "적용 범위가 올바르지 않습니다 (시작 페이지 ≤ 끝 페이지, 1 이상)."
        )
    return (start, end)


def _store_transcript_input(
    job_id: str,
    transcript: str,
    transcript_file,
    audio_language: str = "",
    slide_range=None,
):
    """
    녹음본 입력을 작업(job_id)에 저장한다.
      - 붙여넣은 텍스트 / 텍스트 문서 → 본문을 transcript_path(job_id) 에 저장
      - 음성 파일 → UPLOAD_DIR 에 저장하고 job 문서에 transcript_audio_path 기록 (처리기가 STT 수행)
    반환: "text" | "audio" | None(입력 없음). 형식 오류는 ValueError.
    """
    transcript = (transcript or "").strip()
    has_file = bool(transcript_file and transcript_file.filename)

    if has_file:
        fname = transcript_file.filename
        ext = ti.ext_of(fname)
        # 원본 파일은 보관 대상: 처리기가 결과 폴더 transcripts/ 로 옮긴다 (음성은 STT 입력이기도 함)
        original_path = os.path.join(
            settings.UPLOAD_DIR, f"{job_id}.transcript_original{ext}"
        )
        if ti.is_audio(fname):
            with open(original_path, "wb") as buffer:
                shutil.copyfileobj(transcript_file.file, buffer)
            fields = {
                "transcript_audio_path": original_path,
                "transcript_original_path": original_path,
                "transcript_source": fname,
            }
            if (audio_language or "").strip():
                fields["transcript_language"] = (
                    audio_language.strip()
                )  # 이번 파일에만 적용
            if slide_range:
                fields["transcript_slide_from"], fields["transcript_slide_to"] = (
                    slide_range
                )
            JobManager.update_fields(job_id, fields)
            return "audio"
        if ti.is_text_doc(fname):
            with open(original_path, "wb") as buffer:
                shutil.copyfileobj(transcript_file.file, buffer)
            try:
                transcript = ti.extract_text(original_path, fname)
            except Exception as e:
                if os.path.exists(original_path):
                    os.remove(original_path)
                raise ValueError(f"텍스트 문서에서 본문을 추출하지 못했습니다: {e}")
            if not transcript:
                if os.path.exists(original_path):
                    os.remove(original_path)
                raise ValueError("텍스트 문서에서 추출된 본문이 비어 있습니다.")
            JobManager.update_fields(
                job_id,
                {"transcript_source": fname, "transcript_original_path": original_path},
            )
        else:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {ext or fname}")

    if slide_range:
        JobManager.update_fields(
            job_id,
            {
                "transcript_slide_from": slide_range[0],
                "transcript_slide_to": slide_range[1],
            },
        )
    if transcript:
        with open(transcript_path(job_id), "w", encoding="utf-8") as f:
            f.write(transcript)
        JobManager.set_transcript_flag(job_id, len(transcript))
        return "text"
    return None


@router.post("/transcript/{target_type}/{target_id}")
async def add_transcript(
    target_type: str,
    target_id: str,
    background_tasks: BackgroundTasks,
    transcript: str = Form(""),
    transcript_file: Optional[UploadFile] = File(None),
    audio_language: str = Form(""),
    slide_from: str = Form(""),
    slide_to: str = Form(""),
    user: str = Depends(get_current_user),
):
    """
    이미 생성된 슬라이드 설명에 강의 녹음본을 사후 반영한다.
    target_type: "doc" (문서함 문서) | "job" (완료된 작업).
    입력: transcript(붙여넣은 텍스트) 또는 transcript_file(텍스트 문서 / 음성 파일) 중 하나.
    새 작업 카드가 만들어져 대시보드에서 진행 상황을 볼 수 있다.
    """
    if target_type == "doc":
        target = docs_col.find_one({"id": target_id, "owner": user, "type": "file"})
        if not target:
            raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
        display_name = target["name"]
    elif target_type == "job":
        target = JobManager.get_job(target_id)
        if not target or target.get("owner") != user:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if target.get("status") != "completed":
            raise HTTPException(
                status_code=400, detail="완료된 작업에만 추가할 수 있습니다."
            )
        display_name = target["filename"]
    else:
        raise HTTPException(status_code=400, detail="잘못된 대상 유형입니다.")

    if not (transcript or "").strip() and not (
        transcript_file and transcript_file.filename
    ):
        raise HTTPException(
            status_code=400,
            detail="녹음본 텍스트, 텍스트 문서 또는 녹음 파일이 필요합니다.",
        )
    if (
        transcript_file
        and transcript_file.filename
        and ti.is_audio(transcript_file.filename)
    ):
        problem = stt_precheck(AuthManager.get_user_settings(user))
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    job_id = JobManager.create_job(f"[녹음본 추가] {display_name}", user)
    JobManager.update_fields(
        job_id,
        {"kind": "transcript", "target_type": target_type, "target_id": target_id},
    )
    try:
        kind = _store_transcript_input(
            job_id,
            transcript,
            transcript_file,
            audio_language,
            _parse_slide_range(slide_from, slide_to),
        )
    except ValueError as e:
        JobManager.delete_job(job_id, user)
        raise HTTPException(status_code=400, detail=str(e))
    if not kind:
        JobManager.delete_job(job_id, user)
        raise HTTPException(status_code=400, detail="녹음본 내용이 비어 있습니다.")

    background_tasks.add_task(enrich_transcript_task, job_id, target_type, target_id)
    return {"job_id": job_id, "message": "Transcript job started"}


BOARD_IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
)


@router.post("/boards/doc/{doc_id}")
async def add_board_photos(
    doc_id: str,
    background_tasks: BackgroundTasks,
    photos: List[UploadFile] = File(...),
    note: str = Form(""),
    slide_from: str = Form(""),
    slide_to: str = Form(""),
    user: str = Depends(get_current_user),
):
    """칠판 판서 사진(여러 장)을 문서의 해당 슬라이드에 반영하는 작업을 만든다."""
    target = docs_col.find_one({"id": doc_id, "owner": user, "type": "file"})
    if not target:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    photos = [p for p in photos if p and p.filename]
    if not photos:
        raise HTTPException(
            status_code=400, detail="판서 사진을 한 장 이상 선택하세요."
        )
    for p in photos:
        if os.path.splitext(p.filename)[1].lower() not in BOARD_IMAGE_EXTS:
            raise HTTPException(
                status_code=400, detail=f"지원하지 않는 이미지 형식입니다: {p.filename}"
            )
    try:
        slide_range = _parse_slide_range(slide_from, slide_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user_settings = AuthManager.get_user_settings(user)
    if not (user_settings.get("openai_api_key") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="판서 분석에는 OpenAI API Key 가 필요합니다. 설정에서 먼저 등록해 주세요.",
        )

    job_id = JobManager.create_job(f"[판서 추가] {target['name']}", user)
    paths = []
    for i, p in enumerate(photos, 1):
        path = os.path.join(
            settings.UPLOAD_DIR, f"{job_id}_board_{i:02d}_board_{p.filename}"
        )
        with open(path, "wb") as buffer:
            shutil.copyfileobj(p.file, buffer)
        paths.append(path)
    fields = {
        "kind": "board",
        "target_type": "doc",
        "target_id": doc_id,
        "board_paths": paths,
        "board_note": note.strip(),
    }
    if slide_range:
        fields["transcript_slide_from"], fields["transcript_slide_to"] = slide_range
    JobManager.update_fields(job_id, fields)
    background_tasks.add_task(enrich_boards_task, job_id, doc_id)
    return {"job_id": job_id, "message": "Board job started"}


@router.get("/jobs")
async def get_my_jobs(user: str = Depends(get_current_user)):
    return JobManager.get_jobs_by_user(user)


@router.get("/status/{job_id}")
async def get_status(job_id: str, user: str = Depends(get_current_user)):
    job = JobManager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404)
    if job.get("owner") != user:
        raise HTTPException(status_code=403, detail="Not your job")
    return job


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, user: str = Depends(get_current_user)):
    success = JobManager.delete_job(job_id, user)
    if not success:
        raise HTTPException(
            status_code=404, detail="Job not found or permission denied"
        )
    return {"message": "Job deleted"}
