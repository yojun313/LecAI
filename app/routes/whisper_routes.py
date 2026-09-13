# app/routes/whisper_routes.py
# Whisper 노트 API — KNPU whisper 사이트와 동일한 형태의 노트 CRUD/진행률 폴링.
# 인증은 LecAI 세션(deps.get_current_user), 소유자는 username 기준.

import os
import re
import uuid
from datetime import datetime
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.core.config import settings
from app.db import whisper_notes_col
from app.routes.deps import get_current_user
from app.services.whisper_notes import (
    audio_path,
    cancel_transcription,
    start_transcription,
)

router = APIRouter()

LIST_PROJECTION = {"_id": 0, "segments": 0, "text": 0}

ALLOWED_EXTS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".wma",
    ".amr",
    ".webm",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB

AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wma": "audio/x-ms-wma",
    ".amr": "audio/amr",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


def _get_owned_note(uid: str, user: str, projection=None):
    doc = whisper_notes_col.find_one({"uid": uid}, projection)
    if not doc:
        raise HTTPException(status_code=404, detail="노트를 찾을 수 없습니다")
    if doc.get("owner") != user:
        raise HTTPException(status_code=403, detail="본인의 노트만 접근할 수 있습니다")
    return doc


def _iso(doc: dict):
    for k in ("createdAt", "finishedAt"):
        if isinstance(doc.get(k), datetime):
            doc[k] = doc[k].isoformat()
    return doc


@router.get("/whisper/gpu-stats")
def gpu_stats(user=Depends(get_current_user)):
    """매니저 프록시를 통해 GPU 서버의 nvidia-smi 실시간 사용량을 가져온다 (모니터 위젯용)."""
    from app.services.audio_processor import _custom_headers

    if not settings.AUDIO_LLM_URL:
        return {"gpus": [], "error": "AUDIO_LLM_URL 미설정"}
    # AUDIO_LLM_URL = .../api/analysis/whisper → .../api/analysis/gpu/stats
    base = settings.AUDIO_LLM_URL.rstrip("/")
    if base.endswith("/whisper"):
        base = base[: -len("/whisper")]
    try:
        res = requests.get(f"{base}/gpu/stats", headers=_custom_headers(), timeout=8)
        return res.json()
    except Exception as e:
        return {"gpus": [], "error": str(e)}


@router.get("/whisper/notes")
def list_notes(q: str = None, user=Depends(get_current_user)):
    query = {"owner": user}
    if q:
        query["title"] = {"$regex": re.escape(q), "$options": "i"}
    items = [
        _iso(d)
        for d in whisper_notes_col.find(query, LIST_PROJECTION)
        .sort("createdAt", -1)
        .limit(500)
    ]
    return {"items": items}


@router.post("/whisper/notes")
async def create_note(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    model: int = Form(2),
    user=Depends(get_current_user),
):
    orig_name = file.filename or "recording"
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 파일 형식입니다: {ext or '(확장자 없음)'}",
        )

    uid = uuid.uuid4().hex
    path = os.path.join(settings.WHISPER_DIR, f"{uid}{ext}")

    size = 0
    try:
        with open(path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail="파일이 너무 큽니다 (최대 2GB)"
                    )
                out.write(chunk)
    except HTTPException:
        if os.path.exists(path):
            os.remove(path)
        raise

    whisper_notes_col.insert_one(
        {
            "uid": uid,
            "owner": user,
            "title": os.path.splitext(orig_name)[0],
            "origFilename": orig_name,
            "ext": ext,
            "size": size,
            "language": language,
            "model": max(1, min(3, int(model))),
            "status": "queued",
            "stage": "변환 대기 중",
            "progress": 0,
            "duration": 0,
            "segments": [],
            "segCount": 0,
            "text": "",
            "error": None,
            "createdAt": datetime.now(),
            "finishedAt": None,
        }
    )
    start_transcription(uid)
    return {"uid": uid}


@router.get("/whisper/notes/{uid}")
def get_note(uid: str, since: int = 0, user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0})
    segments = doc.get("segments") or []
    since = max(0, since)
    doc["segments"] = segments[since:]
    doc["segOffset"] = since
    doc["segTotal"] = len(segments)
    return _iso(doc)


@router.patch("/whisper/notes/{uid}")
def rename_note(uid: str, body: dict, user=Depends(get_current_user)):
    _get_owned_note(uid, user, {"_id": 0, "uid": 1, "owner": 1})
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력해 주세요")
    whisper_notes_col.update_one({"uid": uid}, {"$set": {"title": title[:200]}})
    return {"ok": True}


@router.post("/whisper/notes/{uid}/retry")
def retry_note(uid: str, user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0})
    if doc.get("status") in ("queued", "processing"):
        raise HTTPException(status_code=409, detail="이미 변환이 진행 중입니다")
    if not os.path.isfile(audio_path(doc)):
        raise HTTPException(
            status_code=404, detail="음성 파일이 없습니다. 다시 업로드해 주세요"
        )
    start_transcription(uid)
    return {"ok": True}


@router.post("/whisper/notes/{uid}/cancel")
def cancel_note(uid: str, user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0, "uid": 1, "owner": 1, "status": 1})
    if doc.get("status") not in ("queued", "processing"):
        raise HTTPException(status_code=409, detail="진행 중인 변환이 아닙니다")
    cancel_transcription(uid)
    return {"ok": True}


@router.delete("/whisper/notes/{uid}")
def delete_note(uid: str, user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0})
    # 변환이 돌고 있으면 GPU 작업까지 먼저 중단시킨다 (삭제 = 즉시 종료)
    if doc.get("status") in ("queued", "processing"):
        cancel_transcription(uid)
    whisper_notes_col.delete_one({"uid": uid})
    path = audio_path(doc)
    if os.path.isfile(path):
        os.remove(path)
    return {"ok": True}


@router.get("/whisper/notes/{uid}/audio")
def get_audio(uid: str, request: Request, user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0, "uid": 1, "owner": 1, "ext": 1})
    path = audio_path(doc)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="음성 파일을 찾을 수 없습니다")

    media_type = AUDIO_MEDIA_TYPES.get(doc["ext"], "application/octet-stream")
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")

    # 브라우저 오디오 플레이어의 탐색(seek)에는 Range 응답이 필수다.
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if not m:
            raise HTTPException(status_code=416, detail="잘못된 Range 헤더")
        start_s, end_s = m.groups()
        if start_s == "" and end_s == "":
            raise HTTPException(status_code=416, detail="잘못된 Range 헤더")
        if start_s == "":
            length = min(int(end_s), file_size)
            start = file_size - length
            end = file_size - 1
        else:
            start = int(start_s)
            end = min(int(end_s), file_size - 1) if end_s else file_size - 1
        if start >= file_size or start > end:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{file_size}"}
            )

        def iter_range(s=start, e=end):
            with open(path, "rb") as f:
                f.seek(s)
                remaining = e - s + 1
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
            },
        )

    return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})


def _ts_srt(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


@router.get("/whisper/notes/{uid}/export")
def export_note(uid: str, fmt: str = "txt", user=Depends(get_current_user)):
    doc = _get_owned_note(uid, user, {"_id": 0})
    if doc.get("status") not in ("done", "stopped"):
        raise HTTPException(
            status_code=409, detail="변환이 완료(또는 중단)된 노트만 내보낼 수 있습니다"
        )

    segments = doc.get("segments") or []
    title = doc.get("title") or "note"

    if fmt == "srt":
        lines = []
        for i, seg in enumerate(segments, 1):
            lines.append(
                f"{i}\n{_ts_srt(seg['start'])} --> {_ts_srt(seg['end'])}\n{seg['text']}\n"
            )
        content = "\n".join(lines)
        filename = f"{title}.srt"
    elif fmt == "time":
        content = "\n".join(
            f"[{_ts_srt(seg['start'])} - {_ts_srt(seg['end'])}] {seg['text']}"
            for seg in segments
        )
        filename = f"{title}_타임스탬프.txt"
    else:
        content = doc.get("text") or "\n".join(seg["text"] for seg in segments)
        filename = f"{title}.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="export.txt"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
