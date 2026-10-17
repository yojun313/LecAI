# app/routes/youtube_routes.py
import os
import shutil
import asyncio
from typing import List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import tempfile
import zipfile
from datetime import datetime
from pydantic import BaseModel
from app.routes.deps import get_current_user
from app.services.job_manager import JobManager
from app.services import youtube_service as yt
from app.core.config import settings
from app.db import history_col

router = APIRouter()


class ProbeRequest(BaseModel):
    urls: List[str] | str


class DownloadRequest(BaseModel):
    urls: List[str]
    options: dict = {}
    title: str = ""
    thumbnail: str = ""


@router.post("/youtube/probe")
async def youtube_probe(req: ProbeRequest, user: str = Depends(get_current_user)):
    urls = yt.parse_urls(req.urls)
    if not urls:
        raise HTTPException(status_code=400, detail="YouTube 링크를 찾지 못했습니다.")
    items = await asyncio.to_thread(yt.probe, urls)
    return {"items": items}


@router.post("/youtube/download")
async def youtube_download(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    user: str = Depends(get_current_user),
):
    urls = yt.parse_urls(req.urls)
    if not urls:
        raise HTTPException(status_code=400, detail="다운로드할 링크가 없습니다.")
    opts = yt.normalize_options(req.options)
    name = (req.title or "").strip() or (
        f"YouTube {len(urls)}개" if len(urls) > 1 else "YouTube 영상"
    )
    job_id = JobManager.create_job(f"[YouTube] {name[:80]}", user)
    fields = {"kind": "youtube", "yt_urls": urls, "yt_options": opts}
    thumb = (req.thumbnail or "").strip()
    if thumb.startswith("https://"):
        fields["yt_thumbnail"] = thumb[:500]
    JobManager.update_fields(job_id, fields)
    background_tasks.add_task(yt.youtube_download_task, job_id)
    return {"job_id": job_id}


@router.get("/youtube/jobs")
async def youtube_jobs(user: str = Depends(get_current_user)):
    jobs = list(
        history_col.find({"owner": user, "kind": "youtube"}, {"_id": 0})
        .sort("created_at", -1)
        .limit(100)
    )
    for j in jobs:
        d = yt.job_dir(user, j["id"])
        j["files"] = [
            {"name": n, "size": os.path.getsize(os.path.join(d, n))}
            for n in (j.get("yt_files") or [])
            if os.path.isfile(os.path.join(d, n))
        ]
    return jobs


def _owned_job(job_id: str, user: str):
    job = JobManager.get_job(job_id)
    if not job or job.get("owner") != user or job.get("kind") != "youtube":
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return job


def _remove_job_files(user: str, job_id: str):
    shutil.rmtree(yt.job_dir(user, job_id), ignore_errors=True)
    z = os.path.join(settings.RESULT_DIR, user, f"youtube_{job_id}.zip")
    if os.path.exists(z):
        os.remove(z)


@router.get("/youtube/all.zip")
async def youtube_all_zip(user: str = Depends(get_current_user)):
    """완료된 모든 다운로드 파일을 ZIP 하나로 (항목별 폴더, 같은 이름은 번호를 붙여 구분)"""
    jobs = list(
        history_col.find(
            {"owner": user, "kind": "youtube", "status": "completed"},
            {"_id": 0, "id": 1, "filename": 1, "yt_files": 1},
        ).sort("created_at", -1)
    )
    entries = []
    for j in jobs:
        d = yt.job_dir(user, j["id"])
        for n in j.get("yt_files") or []:
            path = os.path.join(d, n)
            if os.path.isfile(path):
                entries.append((path, n))
    if not entries:
        raise HTTPException(status_code=404, detail="받을 수 있는 파일이 없습니다.")

    def build():
        fd, tmp = tempfile.mkstemp(
            prefix="lecai-yt-all-", suffix=".zip", dir=settings.UPLOAD_DIR
        )
        os.close(fd)
        used = set()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, name in entries:
                arc, base, ext, k = name, *os.path.splitext(name), 2
                while arc in used:
                    arc = f"{base} ({k}){ext}"
                    k += 1
                used.add(arc)
                zf.write(path, arc)
        return tmp

    tmp = await asyncio.to_thread(build)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return FileResponse(
        tmp,
        filename=f"YouTube_전체_{stamp}.zip",
        media_type="application/zip",
        background=BackgroundTask(os.remove, tmp),
    )


@router.delete("/youtube/jobs")
async def youtube_delete_all(user: str = Depends(get_current_user)):
    """진행 중인 작업을 제외한 모든 다운로드 항목과 파일 삭제"""
    jobs = list(
        history_col.find(
            {"owner": user, "kind": "youtube"}, {"_id": 0, "id": 1, "status": 1}
        )
    )
    deleted = skipped = 0
    for j in jobs:
        if j.get("status") in ("pending", "processing"):
            skipped += 1
            continue
        _remove_job_files(user, j["id"])
        JobManager.delete_job(j["id"], user)
        deleted += 1
    return {"deleted": deleted, "skipped": skipped}


@router.get("/youtube/file/{job_id}/{name}")
async def youtube_file(job_id: str, name: str, user: str = Depends(get_current_user)):
    _owned_job(job_id, user)
    d = yt.job_dir(user, job_id)
    path = os.path.realpath(os.path.join(d, name))
    if not path.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일이 없습니다.")
    return FileResponse(path, filename=name)


@router.get("/youtube/zip/{job_id}")
async def youtube_zip(job_id: str, user: str = Depends(get_current_user)):
    job = _owned_job(job_id, user)
    path = os.path.join(settings.RESULT_DIR, user, f"youtube_{job_id}.zip")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일이 없습니다.")
    return FileResponse(
        path,
        filename=f"{yt.safe_filename(job['filename'].replace('[YouTube] ', ''))}.zip",
    )


@router.delete("/youtube/jobs/{job_id}")
async def youtube_delete(job_id: str, user: str = Depends(get_current_user)):
    _owned_job(job_id, user)
    shutil.rmtree(yt.job_dir(user, job_id), ignore_errors=True)
    z = os.path.join(settings.RESULT_DIR, user, f"youtube_{job_id}.zip")
    if os.path.exists(z):
        os.remove(z)
    JobManager.delete_job(job_id, user)
    return {"status": "deleted"}
