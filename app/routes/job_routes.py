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
)
from app.services import transcript_input as ti
from app.services.transcript_input import AUDIO_EXTS
from app.db import docs_col
from typing import Optional
from app.services.audio_processor import process_audio_task
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
    user: str = Depends(get_current_user),
):
    file_path = os.path.join(settings.UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job_id = JobManager.create_job(file.filename, user)

    ext = os.path.splitext(file.filename)[1].lower()

    if ext in AUDIO_EXTS:
        background_tasks.add_task(process_audio_task, job_id, file_path)
    else:
        # 강의 녹음본: 붙여넣은 텍스트 / 텍스트 문서 / 음성 파일 중 하나 (선택)
        try:
            _store_transcript_input(job_id, transcript, transcript_file)
        except ValueError as e:
            JobManager.delete_job(job_id, user)
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(status_code=400, detail=str(e))
        background_tasks.add_task(process_file_task, job_id, file_path)

    return {"job_id": job_id, "message": "Upload successful"}


def _store_transcript_input(job_id: str, transcript: str, transcript_file):
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
        if ti.is_audio(fname):
            audio_path = os.path.join(
                settings.UPLOAD_DIR, f"{job_id}.transcript_audio{ext}"
            )
            with open(audio_path, "wb") as buffer:
                shutil.copyfileobj(transcript_file.file, buffer)
            JobManager.update_fields(
                job_id,
                {"transcript_audio_path": audio_path, "transcript_source": fname},
            )
            return "audio"
        if ti.is_text_doc(fname):
            tmp_path = os.path.join(
                settings.UPLOAD_DIR, f"{job_id}.transcript_doc{ext}"
            )
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(transcript_file.file, buffer)
            try:
                transcript = ti.extract_text(tmp_path, fname)
            except Exception as e:
                raise ValueError(f"텍스트 문서에서 본문을 추출하지 못했습니다: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            if not transcript:
                raise ValueError("텍스트 문서에서 추출된 본문이 비어 있습니다.")
            JobManager.update_fields(job_id, {"transcript_source": fname})
        else:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {ext or fname}")

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

    job_id = JobManager.create_job(f"[녹음본 추가] {display_name}", user)
    JobManager.update_fields(
        job_id,
        {"kind": "transcript", "target_type": target_type, "target_id": target_id},
    )
    try:
        kind = _store_transcript_input(job_id, transcript, transcript_file)
    except ValueError as e:
        JobManager.delete_job(job_id, user)
        raise HTTPException(status_code=400, detail=str(e))
    if not kind:
        JobManager.delete_job(job_id, user)
        raise HTTPException(status_code=400, detail="녹음본 내용이 비어 있습니다.")

    background_tasks.add_task(enrich_transcript_task, job_id, target_type, target_id)
    return {"job_id": job_id, "message": "Transcript job started"}


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
