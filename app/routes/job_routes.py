# app/routes/job_routes.py
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException, Depends
from app.services.job_manager import JobManager
from app.services.processor import process_file_task
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
    user: str = Depends(get_current_user),
):
    file_path = os.path.join(settings.UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job_id = JobManager.create_job(file.filename, user)

    ext = os.path.splitext(file.filename)[1].lower()

    if ext in [".mp3", ".wav", ".m4a", ".flac"]:
        background_tasks.add_task(process_audio_task, job_id, file_path)
    else:
        background_tasks.add_task(process_file_task, job_id, file_path)

    return {"job_id": job_id, "message": "Upload successful"}


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
