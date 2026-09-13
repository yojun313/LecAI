# app/services/audio_processor.py
"""
음성 인식(STT). 제공자는 .env 의 STT_PROVIDER 로 선택한다.
  - custom : 매니저 서버 /api/analysis/whisper (GPU faster-whisper). 진행 메시지는 매니저 진행상황 서버의
             웹소켓(/ws/{pid})을 구독해 그대로 작업 로그로 전달한다.
  - openai : OpenAI Audio API (/v1/audio/transcriptions). 25MB 제한에 맞춰 ffmpeg 로 분할해 순서대로 전사한다.
transcribe_audio() 는 두 경우 모두 {"text", "text_with_time", "minutes", "cost_usd", "provider", "model"} 를 돌려준다.
"""

import os
import json
import uuid
import math
import shutil
import tempfile
import threading
import subprocess
import requests
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager
from app.services.transcript_input import AUDIO_EXTS  # noqa: F401

# GPU 서버 보호용 (custom 제공자만)
audio_lock = threading.Lock()

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

# custom 제공자 단계 메시지 → 대략적인 퍼센트
CUSTOM_PHASES = (("모델 로드", 20), ("변환 중", 45), ("완료", 90))


def transcribe_audio(file_path: str, user_settings: dict, on_progress=None) -> dict:
    """
    on_progress(percent:int, message:str) 로 진행 상황을 알린다.
    """
    provider = (settings.STT_PROVIDER or "custom").lower()
    if provider == "openai":
        return _transcribe_openai(file_path, user_settings, on_progress)
    if provider == "custom":
        return _transcribe_custom(file_path, user_settings, on_progress)
    raise RuntimeError(f"알 수 없는 STT_PROVIDER 입니다: {provider} (custom | openai)")


# ==========================================
# custom: 매니저 서버 (GPU Whisper)
# ==========================================
class _ProgressSubscriber:
    """매니저 진행상황 서버의 웹소켓을 구독해 메시지를 콜백으로 전달 (실패해도 STT 자체는 계속)"""

    def __init__(self, base_url: str, pid: str, title: str, on_event):
        self.base_url = base_url.rstrip("/")
        self.pid = pid
        self.title = title
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread = None
        self._ws = None

    def start(self):
        try:
            resp = requests.post(
                f"{self.base_url}/process",
                json={"title": self.title, "process_id": self.pid},
                timeout=10,
            )
            if resp.status_code not in (200, 400):  # 400 = 이미 존재
                print(
                    f"[STT progress] register failed: {resp.status_code} {resp.text[:100]}"
                )
                return
        except Exception as e:
            print(f"[STT progress] register error: {e}")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            from websockets.sync.client import connect
        except Exception as e:
            print(f"[STT progress] websockets unavailable: {e}")
            return
        ws_url = self.base_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        try:
            with connect(f"{ws_url}/ws/{self.pid}", open_timeout=10) as ws:
                self._ws = ws
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {"type": "message", "text": str(raw)}
                    self.on_event(payload)
        except Exception as e:
            if not self._stop.is_set():
                print(f"[STT progress] websocket error: {e}")

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)


def _transcribe_custom(file_path: str, user_settings: dict, on_progress=None) -> dict:
    if not settings.AUDIO_LLM_URL:
        raise RuntimeError(
            "AUDIO_LLM_URL 이 설정되지 않아 음성 인식을 사용할 수 없습니다."
        )

    def report(percent, message):
        if on_progress:
            on_progress(percent, message)

    language = user_settings.get("audio_language", "auto")
    model_level = int(user_settings.get("audio_model_level", 2))
    fname = os.path.basename(file_path)
    pid = str(uuid.uuid4())

    state = {"percent": 10}

    def on_event(payload):
        kind = payload.get("type")
        if kind == "message":
            text = str(payload.get("text", "")).strip()
            for key, pct in CUSTOM_PHASES:
                if key in text:
                    state["percent"] = max(state["percent"], pct)
            report(state["percent"], text)
        elif kind == "progress":
            cur, total = payload.get("current", 0), payload.get("total", 0) or 0
            if total:
                state["percent"] = max(state["percent"], 10 + int(80 * cur / total))
            report(
                state["percent"],
                payload.get("message") or f"음성 인식 진행 {cur}/{total}",
            )
        elif kind == "status":
            report(state["percent"], f"상태: {payload.get('phase', '')}")

    subscriber = None
    if settings.AUDIO_PROGRESS_URL:
        subscriber = _ProgressSubscriber(
            settings.AUDIO_PROGRESS_URL, pid, f"LecAI STT: {fname}", on_event
        )

    headers = {}
    if settings.AUDIO_LLM_TOKEN:
        headers["Authorization"] = f"Bearer {settings.AUDIO_LLM_TOKEN}"
    option = {
        "pid": pid,
        "language": None if language in ("", "auto") else language,
        "model": model_level,
    }

    with audio_lock:
        if subscriber:
            subscriber.start()
        report(5, f"오디오 서버로 전송 중... ({fname})")
        try:
            with open(file_path, "rb") as f:
                response = requests.post(
                    settings.AUDIO_LLM_URL,
                    headers=headers,
                    files={"file": (fname, f, "audio/mpeg")},
                    data={"option": json.dumps(option)},
                    timeout=3600,
                )
        finally:
            if subscriber:
                subscriber.stop()

    if response.status_code == 401:
        raise RuntimeError(
            "STT 서버 인증 실패(401): AUDIO_LLM_TOKEN 이 만료되었을 수 있습니다. 매니저 앱에서 다시 로그인 후 /token 값을 갱신하세요."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"STT API Error: {response.status_code} - {response.text[:300]}"
        )

    result = response.json()
    text = (result.get("text") or "").strip()
    if not text:
        raise RuntimeError("STT 결과가 비어 있습니다.")
    duration = float(result.get("duration") or 0)
    report(95, f"음성 인식 완료 ({len(text):,}자)")
    return {
        "text": text,
        "text_with_time": result.get("text_with_time", ""),
        "minutes": round(duration / 60, 2) if duration else None,
        "cost_usd": 0.0,
        "provider": "custom",
        "model": f"whisper-level-{model_level}",
    }


# ==========================================
# openai: OpenAI Audio API
# ==========================================
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


def _split_audio_for_openai(path: str, workdir: str) -> list:
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
    for c in chunks:
        if os.path.getsize(c) > OPENAI_MAX_CHUNK_BYTES:
            raise RuntimeError(f"분할 조각이 25MB 를 넘습니다: {os.path.basename(c)}")
    return chunks


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{(seconds % 3600) // 60:02}:{seconds % 60:02}"


def _transcribe_openai(file_path: str, user_settings: dict, on_progress=None) -> dict:
    api_key = settings.OPENAI_STT_API_KEY or user_settings.get("openai_api_key", "")
    if not api_key:
        raise RuntimeError(
            "OpenAI STT 를 쓰려면 OPENAI_STT_API_KEY(.env) 또는 사용자 설정의 OpenAI API Key 가 필요합니다."
        )
    model = settings.OPENAI_STT_MODEL or "gpt-transcribe"
    language = user_settings.get("audio_language", "auto")

    def report(percent, message):
        if on_progress:
            on_progress(percent, message)

    fname = os.path.basename(file_path)
    total_seconds = _probe_duration(file_path)
    workdir = tempfile.mkdtemp(prefix="lecai-stt-")
    try:
        report(5, f"오디오 변환/분할 중 ({fname}, {_hms(total_seconds)})")
        chunks = _split_audio_for_openai(file_path, workdir)
        n = len(chunks)
        report(10, f"OpenAI {model} 음성 인식 시작 (조각 {n}개)")

        texts, timed = [], []
        prev_tail = ""
        for i, chunk in enumerate(chunks):
            offset = i * OPENAI_CHUNK_SECONDS
            data = {"model": model, "response_format": "json"}
            if language not in ("", "auto"):
                data["language"] = language
            if prev_tail:
                data["prompt"] = prev_tail  # 문맥 연속성 (용어/표기 유지)

            text = None
            for attempt in range(3):
                try:
                    with open(chunk, "rb") as f:
                        resp = requests.post(
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            files={"file": (os.path.basename(chunk), f, "audio/mpeg")},
                            data=data,
                            timeout=900,
                        )
                    if resp.status_code == 200:
                        text = (resp.json().get("text") or "").strip()
                        break
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
    price = OPENAI_STT_PRICING.get(model, 0.0)
    cost = round(math.ceil(total_seconds / 60) * price, 4) if total_seconds else 0.0
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
        queue_pos = JobManager.get_queue_position(job_id)
        if queue_pos > 0:
            JobManager.update_progress(
                job_id, 0, 0, f"대기열 진입: 앞선 작업 {queue_pos}개 대기 중"
            )
        else:
            JobManager.update_progress(job_id, 0, 0, "오디오 변환 준비 중...")
    except Exception:
        pass

    try:
        JobManager.start_processing(job_id)
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
            job_id, 100, 100, f"완료 ({result['provider']} / {result['model']})"
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
