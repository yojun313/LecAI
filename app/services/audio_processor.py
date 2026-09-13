# app/services/audio_processor.py
"""
음성 인식(STT): OpenAI Audio API (/v1/audio/transcriptions) 전용.
  - 각 사용자가 설정에 등록한 OpenAI API Key 로 호출한다 (서버 공용 키 없음).
  - 25MB 업로드 제한에 맞춰 ffmpeg 로 모노 16kHz mp3 로 변환하며 분할하고, 조각을 순서대로 전사한다.
  - 모델은 .env 의 OPENAI_STT_MODEL (기본 gpt-transcribe, $0.0045/분).
transcribe_audio() 는 {"text", "text_with_time", "minutes", "cost_usd", "provider", "model"} 를 돌려준다.
"""

import os
import math
import shutil
import tempfile
import subprocess
import requests
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager
from app.services.transcript_input import AUDIO_EXTS  # noqa: F401

OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"

# OpenAI STT 요금 (USD / 분). https://developers.openai.com/api/docs/pricing (2026-09 확인)
OPENAI_STT_PRICING = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-transcribe-diarize": 0.006,
    "whisper-1": 0.006,
}
# OpenAI 업로드 제한 25MB. 모노 16kHz 48kbps mp3 로 변환 후 20분(≈7MB) 단위로 분할
OPENAI_CHUNK_SECONDS = 1200
OPENAI_AUDIO_BITRATE = "48k"
OPENAI_MAX_CHUNK_BYTES = 24 * 1024 * 1024

NO_KEY_MESSAGE = "음성 인식(STT)은 사용자의 OpenAI API Key 로 동작합니다. 설정 메뉴에서 OpenAI API Key 를 먼저 등록해 주세요."


def stt_model() -> str:
    return settings.OPENAI_STT_MODEL or "gpt-transcribe"


def stt_price_per_minute(model: str = None) -> float:
    return OPENAI_STT_PRICING.get(model or stt_model(), 0.0)


def user_stt_key(user_settings: dict) -> str:
    return (user_settings.get("openai_api_key") or "").strip()


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _split_audio(path: str, workdir: str) -> list:
    """모노 16kHz mp3 로 변환하며 OPENAI_CHUNK_SECONDS 단위로 분할. 반환: 조각 파일 경로 목록(순서대로)"""
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            OPENAI_AUDIO_BITRATE,
            "-f",
            "segment",
            "-segment_time",
            str(OPENAI_CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            pattern,
        ],
        check=True,
        timeout=3600,
    )
    chunks = sorted(
        os.path.join(workdir, n) for n in os.listdir(workdir) if n.startswith("chunk_")
    )
    if not chunks:
        raise RuntimeError(
            "오디오를 변환하지 못했습니다 (지원하지 않는 파일이거나 손상된 파일)."
        )
    for c in chunks:
        if os.path.getsize(c) > OPENAI_MAX_CHUNK_BYTES:
            raise RuntimeError(f"분할 조각이 25MB 를 넘습니다: {os.path.basename(c)}")
    return chunks


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{(seconds % 3600) // 60:02}:{seconds % 60:02}"


def transcribe_audio(file_path: str, user_settings: dict, on_progress=None) -> dict:
    """
    on_progress(percent:int, message:str) 로 진행 상황을 알린다.
    """
    api_key = user_stt_key(user_settings)
    if not api_key:
        raise RuntimeError(NO_KEY_MESSAGE)
    model = stt_model()
    language = user_settings.get("audio_language", "auto")

    def report(percent, message):
        if on_progress:
            on_progress(percent, message)

    fname = os.path.basename(file_path)
    total_seconds = _probe_duration(file_path)
    workdir = tempfile.mkdtemp(prefix="lecai-stt-")
    try:
        report(5, f"오디오 변환/분할 중 ({fname}, {_hms(total_seconds)})")
        chunks = _split_audio(file_path, workdir)
        n = len(chunks)
        report(10, f"OpenAI {model} 음성 인식 시작 (조각 {n}개)")

        texts, timed = [], []
        prev_tail = ""
        for i, chunk in enumerate(chunks):
            offset = i * OPENAI_CHUNK_SECONDS
            data = {"model": model, "response_format": "json"}
            if language not in ("", "auto", None):
                data["language"] = language  # ISO-639-1. 자동 인식이면 생략
            if prev_tail:
                data["prompt"] = prev_tail  # 문맥 연속성 (용어/표기 유지)

            text = None
            for attempt in range(3):
                try:
                    with open(chunk, "rb") as f:
                        resp = requests.post(
                            OPENAI_TRANSCRIPTION_URL,
                            headers={"Authorization": f"Bearer {api_key}"},
                            files={"file": (os.path.basename(chunk), f, "audio/mpeg")},
                            data=data,
                            timeout=900,
                        )
                    if resp.status_code == 200:
                        text = (resp.json().get("text") or "").strip()
                        break
                    if resp.status_code == 401:
                        raise RuntimeError(
                            "OpenAI API Key 가 유효하지 않습니다 (401). 설정에서 키를 확인해 주세요."
                        )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        report(
                            10 + int(80 * i / n),
                            f"OpenAI 응답 {resp.status_code}, 재시도 {attempt + 1}/3",
                        )
                        continue
                    raise RuntimeError(
                        f"OpenAI STT Error: {resp.status_code} - {resp.text[:300]}"
                    )
                except requests.exceptions.Timeout:
                    report(
                        10 + int(80 * i / n),
                        f"OpenAI 응답 시간 초과, 재시도 {attempt + 1}/3",
                    )
            if text is None:
                raise RuntimeError(f"조각 {i + 1}/{n} 전사 실패")

            texts.append(text)
            timed.append(f"[{_hms(offset)}] {text}")
            prev_tail = text[-300:]
            report(10 + int(80 * (i + 1) / n), f"음성 인식 {i + 1}/{n} 조각 완료")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    full_text = "\n\n".join(t for t in texts if t)
    if not full_text.strip():
        raise RuntimeError("STT 결과가 비어 있습니다.")

    minutes = round(total_seconds / 60, 2) if total_seconds else None
    cost = (
        round(math.ceil(total_seconds / 60) * stt_price_per_minute(model), 4)
        if total_seconds
        else 0.0
    )
    report(
        95,
        f"음성 인식 완료 ({len(full_text):,}자, {minutes or '?'}분, 예상 비용 ${cost})",
    )
    return {
        "text": full_text,
        "text_with_time": "\n".join(timed),
        "minutes": minutes,
        "cost_usd": cost,
        "provider": "openai",
        "model": model,
    }


# ==========================================
# 단독 오디오 업로드 작업
# ==========================================
def process_audio_task(job_id: str, file_path: str):
    """단독 오디오 업로드: STT 결과를 텍스트 파일로 저장해 zip 으로 제공"""
    job = JobManager.get_job(job_id)
    if not job:
        return

    owner = job.get("owner")
    user_settings = AuthManager.get_user_settings(owner)

    try:
        JobManager.start_processing(job_id)
        JobManager.update_progress(job_id, 0, 100, "오디오 변환 준비 중...")
        result = transcribe_audio(
            file_path,
            user_settings,
            on_progress=lambda p, m: JobManager.update_progress(job_id, p, 100, m),
        )
        if result.get("cost_usd"):
            AuthManager.update_user_cumulative_usage(owner, result["cost_usd"])

        result_base = os.path.join(settings.RESULT_DIR, job_id)
        os.makedirs(result_base, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        with open(
            os.path.join(result_base, f"{base_name}.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(result["text"])
        with open(
            os.path.join(result_base, f"{base_name}_timestamps.txt"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(result["text_with_time"])

        JobManager.update_progress(job_id, 97, 100, "결과물 압축 중...")
        user_result_dir = os.path.join(settings.RESULT_DIR, owner)
        os.makedirs(user_result_dir, exist_ok=True)
        shutil.make_archive(os.path.join(user_result_dir, job_id), "zip", result_base)

        JobManager.update_progress(
            job_id,
            100,
            100,
            f"완료 (OpenAI {result['model']} | {result['minutes'] or '?'}분 | 예상 비용 ${result['cost_usd']})",
        )
        JobManager.mark_completed(job_id, f"/static/results/{owner}/{job_id}.zip")

    except Exception as e:
        print(f"[AUDIO ERROR] {e}")
        JobManager.mark_failed(job_id, str(e))
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        result_dir = os.path.join(settings.RESULT_DIR, job_id)
        if os.path.exists(result_dir):
            shutil.rmtree(result_dir)
