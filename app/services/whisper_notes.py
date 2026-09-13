# app/services/whisper_notes.py
# Whisper 노트 페이지의 백그라운드 전사 워커.
# KNPU whisper 사이트와 동일한 흐름: 업로드 즉시 노트를 만들고, 매니저 서버의
# /api/analysis/whisper/stream (NDJSON) 을 커스텀 토큰(AUDIO_LLM_TOKEN)으로 소비하면서
# 세그먼트를 실시간으로 DB에 쌓는다. 프론트는 1초 폴링으로 진행률/부분 전사를 본다.

import json
import os
import threading
import time
import traceback
from datetime import datetime

import requests

from app.core.config import settings
from app.db import whisper_notes_col
from app.services.audio_processor import _custom_headers, _custom_stream_url

# 매니저→GPU 터널이 순단될 수 있으므로 전송 계층 오류는 이 간격으로 재시도한다
_RETRY_DELAYS = [5, 15, 30]  # 초

# GPU 서버에 GPU가 2장이라 동시에 2개까지 허용 (GPU 서버가 덜 바쁜 쪽으로 배정)
_gpu_sem = threading.Semaphore(int(os.getenv("WHISPER_CONCURRENCY", "2")))

_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# 진행 중 작업 레지스트리 (uid → {"event": Event, "resp": Response|None})
_active_jobs = {}
_active_lock = threading.Lock()


class _Cancelled(Exception):
    pass


def _cancel_url(job_id: str) -> str:
    # AUDIO_LLM_URL = https://manager.knpu.re.kr/api/analysis/whisper
    # → 매니저 프록시의 /api/analysis/whisper/cancel/{job_id}
    return settings.AUDIO_LLM_URL.rstrip("/") + f"/cancel/{job_id}"


def cancel_transcription(uid: str) -> bool:
    """진행 중(대기 포함)인 전사를 중단한다. 로컬 스트림을 끊고,
    매니저 프록시를 통해 GPU 쪽 디코딩도 명시적으로 중단시킨다."""
    with _active_lock:
        entry = _active_jobs.get(uid)
    if entry:
        entry["event"].set()
        resp = entry.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    if settings.AUDIO_LLM_URL:
        try:
            requests.post(_cancel_url(uid), headers=_custom_headers(), timeout=5)
        except Exception:
            pass
    return entry is not None


def audio_path(doc: dict) -> str:
    return os.path.join(settings.WHISPER_DIR, f"{doc['uid']}{doc['ext']}")


def _update(uid: str, fields: dict):
    whisper_notes_col.update_one({"uid": uid}, {"$set": fields})


def _format_paragraphs(segments, max_len=120):
    paragraphs = []
    buf = ""
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if len(buf) + len(text) <= max_len:
            buf += " " + text
        else:
            paragraphs.append(buf.strip())
            buf = text
    if buf:
        paragraphs.append(buf.strip())
    return "\n\n".join(paragraphs)


def _transcribe_once(uid: str, doc: dict, path: str, entry: dict):
    """스트림에 한 번 연결해 끝까지 소비한다. 전송 오류는 호출부에서 재시도."""
    language = doc.get("language") or "auto"
    option = {
        # 매니저/GPU 는 "auto"/None 모두 자동 감지로 처리한다
        "language": None if language in ("", "auto") else language,
        "model": int(doc.get("model", 2) or 2),
        "job_id": uid,  # GPU 쪽 /whisper/cancel/{job_id} 중단용
    }
    duration = 0.0
    seg_count = 0

    with open(path, "rb") as f:
        resp = requests.post(
            _custom_stream_url(),
            headers=_custom_headers(),
            files={"file": (doc.get("origFilename") or f"audio{doc['ext']}", f)},
            data={"option": json.dumps(option)},
            stream=True,
            timeout=(30, 3600),
        )
    entry["resp"] = resp

    with resp:
        if resp.status_code == 401:
            raise RuntimeError(
                "STT 서버 인증 실패(401): AUDIO_LLM_TOKEN이 만료되었을 수 있습니다. "
                "매니저 앱에서 다시 로그인 후 /token 값을 서버 .env에 갱신하세요."
            )
        if resp.status_code != 200:
            raise RuntimeError(f"STT API Error: {resp.status_code} - {resp.text[:300]}")

        for raw in resp.iter_lines(decode_unicode=True):
            if entry["event"].is_set():
                raise _Cancelled()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            etype = event.get("type")

            if etype == "status":
                _update(uid, {"stage": event.get("message") or "처리 중"})

            elif etype == "info":
                duration = float(event.get("duration") or 0)
                _update(
                    uid,
                    {
                        "duration": duration,
                        "detectedLanguage": event.get("language"),
                        "stage": "음성 인식 중",
                    },
                )

            elif etype == "segment":
                seg = {
                    "start": event.get("start", 0),
                    "end": event.get("end", 0),
                    "text": event.get("text", ""),
                }
                seg_count += 1
                progress = 0
                if duration > 0:
                    progress = min(99, round(seg["end"] / duration * 100))
                whisper_notes_col.update_one(
                    {"uid": uid},
                    {
                        "$push": {"segments": seg},
                        "$set": {
                            "progress": progress,
                            "stage": "음성 인식 중",
                            "segCount": seg_count,
                        },
                    },
                )

            elif etype == "cancelled":
                raise _Cancelled()

            elif etype == "error":
                raise RuntimeError(event.get("message") or "STT 서버 오류")

            elif etype == "done":
                return

    if entry["event"].is_set():
        raise _Cancelled()


def _run(uid: str):
    doc = whisper_notes_col.find_one({"uid": uid})
    if not doc:
        return

    path = audio_path(doc)
    if not os.path.isfile(path):
        _update(
            uid,
            {
                "status": "error",
                "stage": "오류",
                "error": "업로드된 파일을 찾을 수 없습니다",
            },
        )
        return

    entry = {"event": threading.Event(), "resp": None}
    with _active_lock:
        _active_jobs[uid] = entry

    try:
        _update(uid, {"status": "queued", "stage": "변환 대기 중", "progress": 0})
        _run_inner(uid, doc, path, entry)
    finally:
        with _active_lock:
            _active_jobs.pop(uid, None)


def _finish_cancelled(uid: str):
    """중단 처리: 노트가 아직 있으면(=중단 버튼) 부분 결과를 남기고 'stopped'로,
    이미 삭제됐으면(=삭제로 인한 중단) 아무것도 하지 않는다."""
    doc = whisper_notes_col.find_one({"uid": uid}, {"segments": 1})
    if not doc:
        return
    segments = doc.get("segments", [])
    _update(
        uid,
        {
            "status": "stopped",
            "stage": "중단됨",
            "text": _format_paragraphs(segments),
            "finishedAt": datetime.now(),
        },
    )


def _run_inner(uid: str, doc: dict, path: str, entry: dict):
    with _gpu_sem:
        if entry["event"].is_set():
            _finish_cancelled(uid)
            return
        if not settings.AUDIO_LLM_URL:
            _update(
                uid,
                {
                    "status": "error",
                    "stage": "오류",
                    "error": "서버 설정 오류: AUDIO_LLM_URL이 비어 있습니다 (.env 확인)",
                },
            )
            return

        max_attempts = len(_RETRY_DELAYS) + 1
        for attempt in range(1, max_attempts + 1):
            try:
                _update(
                    uid, {"status": "processing", "stage": "음성 인식 서버에 연결 중"}
                )
                # 재시도 시 직전 시도의 부분 결과가 섞이지 않게 비운다
                whisper_notes_col.update_one(
                    {"uid": uid},
                    {"$set": {"segments": [], "segCount": 0, "progress": 0}},
                )

                _transcribe_once(uid, doc, path, entry)

                final = whisper_notes_col.find_one({"uid": uid}, {"segments": 1}) or {}
                segments = final.get("segments", [])
                _update(
                    uid,
                    {
                        "status": "done",
                        "progress": 100,
                        "stage": "완료",
                        "text": _format_paragraphs(segments),
                        "finishedAt": datetime.now(),
                    },
                )
                return

            except _Cancelled:
                _finish_cancelled(uid)
                return

            except _TRANSPORT_ERRORS as e:
                # 중단으로 응답이 닫혀도 전송 오류로 나타난다 — 재시도 금지
                if entry["event"].is_set():
                    _finish_cancelled(uid)
                    return
                if attempt < max_attempts:
                    delay = _RETRY_DELAYS[attempt - 1]
                    print(
                        f"[WHISPER] 연결 끊김 ({type(e).__name__}), "
                        f"{delay}초 후 재시도 {attempt}/{len(_RETRY_DELAYS)} — {uid}"
                    )
                    _update(
                        uid,
                        {
                            "stage": f"연결이 끊겨 {delay}초 후 재시도합니다 "
                            f"({attempt}/{len(_RETRY_DELAYS)})",
                            "progress": 0,
                        },
                    )
                    time.sleep(delay)
                    continue
                print(
                    f"[WHISPER] transcription failed for {uid}:\n"
                    f"{traceback.format_exc()}"
                )
                _update(
                    uid,
                    {
                        "status": "error",
                        "stage": "오류",
                        "error": "음성 인식 서버에 연결할 수 없습니다. "
                        "잠시 후 '다시 시도'를 눌러 주세요.",
                    },
                )
                return

            except Exception as e:
                if entry["event"].is_set():
                    _finish_cancelled(uid)
                    return
                print(
                    f"[WHISPER] transcription failed for {uid}:\n"
                    f"{traceback.format_exc()}"
                )
                msg = str(e) or type(e).__name__
                _update(uid, {"status": "error", "stage": "오류", "error": msg[:500]})
                return


def start_transcription(uid: str):
    whisper_notes_col.update_one(
        {"uid": uid},
        {
            "$set": {
                "status": "queued",
                "stage": "변환 대기 중",
                "progress": 0,
                "segments": [],
                "segCount": 0,
                "error": None,
            }
        },
    )
    threading.Thread(target=_run, args=(uid,), daemon=True).start()


def requeue_interrupted():
    """서버 재시작으로 끊긴 작업을 자동으로 다시 돌린다 (파일이 남아 있으면)."""
    for doc in whisper_notes_col.find({"status": {"$in": ["queued", "processing"]}}):
        if os.path.isfile(audio_path(doc)):
            start_transcription(doc["uid"])
        else:
            _update(
                doc["uid"],
                {
                    "status": "error",
                    "stage": "오류",
                    "error": "서버 재시작으로 작업이 중단되었습니다. 파일을 다시 업로드해 주세요.",
                },
            )
