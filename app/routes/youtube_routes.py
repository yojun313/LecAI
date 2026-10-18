# app/routes/youtube_routes.py
import os
import shutil
import asyncio
from typing import List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from urllib.parse import quote
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
    source: dict = {}  # 채널/재생목록에서 받을 때: {type, title, url, avatar}


class SearchRequest(BaseModel):
    query: str
    kind: str = "video"
    sort: str = "relevance"
    period: str = "any"
    page: int = 1


class CollectionRequest(BaseModel):
    url: str
    tabs: List[str] = ["videos"]


def _zip_response(entries, filename: str):
    return StreamingResponse(
        yt.stream_zip(entries),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.post("/youtube/search")
async def youtube_search(req: SearchRequest, user: str = Depends(get_current_user)):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="검색어를 입력해 주세요.")
    try:
        return await asyncio.to_thread(
            yt.search, req.query, req.kind, req.sort, req.page, req.period
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"검색 실패: {e}")


@router.post("/youtube/collection")
async def youtube_collection(
    req: CollectionRequest, user: str = Depends(get_current_user)
):
    """채널 링크(@핸들 등) 또는 재생목록 링크 → 전체 영상 목록"""
    try:
        return await asyncio.to_thread(yt.list_collection, req.url, req.tabs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
    if len(req.urls) > yt.MAX_DOWNLOAD_URLS:
        raise HTTPException(
            status_code=400,
            detail=f"한 번에 최대 {yt.MAX_DOWNLOAD_URLS}개까지 받을 수 있습니다.",
        )
    urls = yt.parse_urls(req.urls, limit=yt.MAX_DOWNLOAD_URLS)
    if not urls:
        raise HTTPException(status_code=400, detail="다운로드할 링크가 없습니다.")
    opts = yt.normalize_options(req.options)
    name = (req.title or "").strip() or (
        f"YouTube {len(urls)}개" if len(urls) > 1 else "YouTube 영상"
    )
    job_id = JobManager.create_job(f"[YouTube] {name[:80]}", user)
    fields = {
        "kind": "youtube",
        "yt_urls": urls,
        "yt_count": len(urls),
        "yt_options": opts,
    }
    thumb = (req.thumbnail or "").strip()
    if thumb.startswith("https://"):
        fields["yt_thumbnail"] = thumb[:500]
    src = req.source or {}
    if src.get("type") in ("channel", "playlist"):
        avatar = str(src.get("avatar") or "")
        fields["yt_source"] = {
            "type": src["type"],
            "title": str(src.get("title") or "")[:120],
            "url": str(src.get("url") or "")[:300],
            "avatar": avatar[:500] if avatar.startswith("https://") else "",
        }
    JobManager.update_fields(job_id, fields)
    background_tasks.add_task(yt.youtube_download_task, job_id)
    return {"job_id": job_id}


@router.get("/youtube/jobs")
async def youtube_jobs(user: str = Depends(get_current_user)):
    # 목록 폴링은 가볍게: 로그는 마지막 몇 줄만, 영상 링크 목록 대신 개수만
    jobs = list(
        history_col.find(
            {"owner": user, "kind": "youtube"}, {"_id": 0, "logs": {"$slice": -3}}
        )
        .sort("created_at", -1)
        .limit(100)
    )
    for j in jobs:
        j["yt_count"] = j.get("yt_count") or len(j.get("yt_urls") or [])
        j.pop("yt_urls", None)
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

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return _zip_response(entries, f"YouTube_전체_{stamp}.zip")


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
    d = yt.job_dir(user, job_id)
    entries = [(os.path.join(d, n), n) for n in yt.list_job_files(d)]
    if not entries:
        raise HTTPException(status_code=404, detail="파일이 없습니다.")
    name = yt.safe_filename(job["filename"].replace("[YouTube] ", ""))
    return _zip_response(entries, f"{name}.zip")


@router.post("/youtube/jobs/{job_id}/cancel")
async def youtube_cancel(job_id: str, user: str = Depends(get_current_user)):
    """진행/대기 중인 작업 중지. 지금 받는 영상을 멈추고, 그때까지 받은 파일은 남긴다."""
    job = _owned_job(job_id, user)
    if job.get("status") not in ("pending", "processing"):
        raise HTTPException(status_code=400, detail="진행 중인 작업이 아닙니다.")
    JobManager.update_fields(job_id, {"yt_cancel": True, "yt_live": "중지하는 중..."})
    return {"status": "cancelling"}


@router.post("/youtube/jobs/{job_id}/retry")
async def youtube_retry(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: str = Depends(get_current_user),
):
    """이어받기: 이미 받은 영상은 건너뛰고 남은(실패·중지된) 영상만 다시 받는다."""
    job = _owned_job(job_id, user)
    if job.get("status") in ("pending", "processing"):
        raise HTTPException(status_code=400, detail="이미 진행 중입니다.")
    JobManager.update_fields(
        job_id,
        {
            "status": "pending",
            "error": None,
            "progress": 0,
            "yt_cancel": False,
            "yt_cancelled": False,
            "yt_live": "대기 중...",
        },
    )
    background_tasks.add_task(yt.youtube_download_task, job_id)
    return {"status": "queued"}


@router.delete("/youtube/jobs/{job_id}")
async def youtube_delete(job_id: str, user: str = Depends(get_current_user)):
    _owned_job(job_id, user)
    shutil.rmtree(yt.job_dir(user, job_id), ignore_errors=True)
    z = os.path.join(settings.RESULT_DIR, user, f"youtube_{job_id}.zip")
    if os.path.exists(z):
        os.remove(z)
    JobManager.delete_job(job_id, user)
    return {"status": "deleted"}
