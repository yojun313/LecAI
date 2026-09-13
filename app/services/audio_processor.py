# app/services/audio_processor.py
"""
음성 인식(STT). 제공자는 사용자 설정(stt_provider)으로 고른다. 기본값은 .env 의 STT_DEFAULT_PROVIDER.
  - custom : 매니저 서버 /api/analysis/whisper (GPU faster-whisper). URL/토큰/진행상황 서버는 .env.
             진행 메시지는 매니저 진행상황 서버의 웹소켓(/ws/{pid})을 구독해 작업 로그로 전달한다. 무료.
  - openai : OpenAI Audio API (/v1/audio/transcriptions). 각 사용자가 설정에 등록한 OpenAI API Key 사용.
             25MB 제한에 맞춰 ffmpeg 로 분할해 순서대로 전사한다. 모델은 .env 의 OPENAI_STT_MODEL.
transcribe_audio() 는 두 경우 모두 {"text", "text_with_time", "minutes", "cost_usd", "provider", "model"} 를 돌려준다.
"""

import os
import json
import uuid
import math
import time
import shutil
import tempfile
import threading
import subprocess
import requests
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager
from app.services.transcript_input import AUDIO_EXTS  # noqa: F401

STT_PROVIDERS = ("custom", "openai")

# GPU 서버 보호용 (custom 제공자만)
audio_lock = threading.Lock()

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

# custom 제공자 단계 메시지 → 대략적인 퍼센트
CUSTOM_PHASES = (("모델 로드", 20), ("변환 중", 45), ("완료", 90))
CUSTOM_MODEL_NAMES = {1: "small", 2: "medium", 3: "large"}

NO_KEY_MESSAGE = (
    "OpenAI 음성 인식은 사용자의 OpenAI API Key 로 동작합니다. 설정 메뉴에서 OpenAI API Key 를 먼저 등록하거나 "
    "음성 인식 엔진을 '커스텀 서버'로 바꿔 주세요."
)
NO_CUSTOM_MESSAGE = (
    "커스텀 음성 인식 서버(AUDIO_LLM_URL)가 서버 .env 에 설정되어 있지 않습니다."
)


# ==========================================
# 공통
# ==========================================
def custom_available() -> bool:
    return bool(settings.AUDIO_LLM_URL)


def stt_provider_for(user_settings: dict) -> str:
    """
    사용자가 명시적으로 고른 제공자는 그대로 존중한다 (서버 미설정 등은 stt_precheck 가 안내).
    고르지 않았으면 .env 의 STT_DEFAULT_PROVIDER, 그것이 custom 인데 서버가 없으면 openai.
    """
    pref = (user_settings.get("stt_provider") or "").lower()
    if pref in STT_PROVIDERS:
        return pref
    default = (settings.STT_DEFAULT_PROVIDER or "").lower()
    if default not in STT_PROVIDERS:
        default = "custom"
    if default == "custom" and not custom_available():
        return "openai"
    return default


def stt_model() -> str:
    return settings.OPENAI_STT_MODEL or "gpt-transcribe"


def stt_price_per_minute(model: str = None) -> float:
    return OPENAI_STT_PRICING.get(model or stt_model(), 0.0)


def user_stt_key(user_settings: dict) -> str:
    return (user_settings.get("openai_api_key") or "").strip()


def stt_precheck(user_settings: dict):
    """요청 단계에서 STT 가능 여부 확인. 불가하면 사용자에게 보여줄 메시지를 반환, 가능하면 None."""
    provider = stt_provider_for(user_settings)
    if provider == "openai" and not user_stt_key(user_settings):
        return NO_KEY_MESSAGE
    if provider == "custom" and not custom_available():
        return NO_CUSTOM_MESSAGE
    return None


def transcribe_audio(file_path: str, user_settings: dict, on_progress=None) -> dict:
    """
    on_progress(percent:int, message:str) 로 진행 상황을 알린다.
    """
    provider = stt_provider_for(user_settings)
    if provider == "openai":
        return _transcribe_openai(file_path, user_settings, on_progress)
    return _transcribe_custom(file_path, user_settings, on_progress)


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


class _StreamUnavailable(Exception):
    """스트림 엔드포인트가 없거나(404/405) 프록시되지 않은 경우 → 일괄 방식으로 전환"""


def _hms_ms(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    sec = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def _format_paragraphs(segments, max_len: int = 120) -> str:
    """GPU 서버의 format_paragraphs 와 같은 규칙 (약 120자 단위 문단)"""
    paragraphs, buf = [], ""
    for seg in segments:
        text = seg["text"].strip()
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


def _custom_option(user_settings: dict, pid: str = None) -> dict:
    language = user_settings.get("audio_language", "auto")
    option = {
        "language": None
        if language in ("", "auto", None)
        else language,  # None = 자동 인식
        "model": int(user_settings.get("audio_model_level", 2) or 2),
    }
    if pid:
        option["pid"] = pid
    return option


def _custom_headers() -> dict:
    headers = {}
    if settings.AUDIO_LLM_TOKEN:
        headers["Authorization"] = f"Bearer {settings.AUDIO_LLM_TOKEN}"
    return headers


def _custom_stream_url() -> str:
    if settings.AUDIO_LLM_STREAM_URL:
        return settings.AUDIO_LLM_STREAM_URL
    return settings.AUDIO_LLM_URL.rstrip("/") + "/stream"


def _transcribe_custom_stream(file_path: str, user_settings: dict, report) -> dict:
    """
    GPU 서버의 /analysis/whisper/stream (NDJSON) 소비. faster-whisper 가 세그먼트를 디코딩하는 즉시
    {"type":"segment","start","end","text"} 가 오므로 seg.end / info.duration 으로 정확한 실시간 진행률을 낸다.
    이벤트: status → info(duration) → segment* → done | error
    """
    fname = os.path.basename(file_path)
    option = _custom_option(user_settings)
    report(5, f"커스텀 음성 인식 서버로 전송 중... ({fname})")
    with open(file_path, "rb") as f:
        resp = requests.post(
            _custom_stream_url(),
            headers=_custom_headers(),
            files={"file": (fname, f, "audio/mpeg")},
            data={"option": json.dumps(option)},
            stream=True,
            timeout=(30, 3600),
        )
    with resp:
        if resp.status_code in (404, 405):
            raise _StreamUnavailable(
                f"stream endpoint unavailable ({resp.status_code})"
            )
        if resp.status_code == 401:
            raise RuntimeError(
                "커스텀 STT 서버 인증 실패(401): AUDIO_LLM_TOKEN 이 만료되었을 수 있습니다. 매니저 앱에서 다시 로그인 후 /token 값을 .env 에 갱신하세요."
            )
        if resp.status_code != 200:
            raise RuntimeError(f"STT API Error: {resp.status_code} - {resp.text[:300]}")
        ctype = resp.headers.get("content-type", "")
        if (
            "ndjson" not in ctype
            and "json" in ctype
            and not ctype.startswith("application/x-ndjson")
        ):
            # 일반 JSON 을 돌려주는 옛 엔드포인트가 응답한 경우 (프록시가 /stream 을 /whisper 로 라우팅하는 등)
            body = resp.json()
            if isinstance(body, dict) and body.get("text"):
                raise _StreamUnavailable("non-stream json response")

        segments, duration, language, done = [], 0.0, None, False
        last_report = {"pct": -1, "time": 0.0}

        def maybe_report(pct, message, force=False):
            now = time.time()
            if force or pct - last_report["pct"] >= 2 or now - last_report["time"] >= 3:
                last_report["pct"], last_report["time"] = pct, now
                report(pct, message)

        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            kind = ev.get("type")
            if kind == "status":
                stage = ev.get("stage", "")
                pct = 10 if stage == "model_loading" else 15
                maybe_report(pct, f"[음성 인식] {ev.get('message', stage)}", force=True)
            elif kind == "info":
                duration = float(ev.get("duration") or 0)
                language = ev.get("language")
                maybe_report(
                    15,
                    f"[음성 인식] 오디오 길이 {_hms(duration)} · 언어 {language or '자동'} · 실시간 전사 시작",
                    force=True,
                )
            elif kind == "segment":
                segments.append(ev)
                end = float(ev.get("end") or 0)
                pct = 15 + int(80 * min(end / duration, 1.0)) if duration else 50
                snippet = (ev.get("text") or "").strip()
                if len(snippet) > 40:
                    snippet = snippet[:40] + "…"
                maybe_report(
                    pct,
                    f'[음성 인식] {_hms(end)} / {_hms(duration)} ({pct}%) · {len(segments)}구간 · "{snippet}"',
                )
            elif kind == "error":
                raise RuntimeError(f"STT 서버 오류: {ev.get('message', '')}")
            elif kind == "done":
                done = True
                break

    if not done and not segments:
        raise RuntimeError("STT 스트림이 결과 없이 끊겼습니다.")
    text = _format_paragraphs(segments)
    if not text.strip():
        raise RuntimeError(
            "음성 인식 결과가 비어 있습니다. 파일에서 음성이 감지되지 않았습니다 (무음·잡음만 있거나 잘못된 파일)."
        )
    text_with_time = "\n".join(
        f"[{_hms_ms(float(s['start']))} - {_hms_ms(float(s['end']))}] {s['text'].strip()}"
        for s in segments
    )
    report(
        95, f"음성 인식 완료 ({len(text):,}자, {len(segments)}구간, {_hms(duration)})"
    )
    return {
        "text": text,
        "text_with_time": text_with_time,
        "minutes": round(duration / 60, 2) if duration else None,
        "cost_usd": 0.0,
        "provider": "custom",
        "model": f"whisper-{CUSTOM_MODEL_NAMES.get(option['model'], option['model'])}",
        "language": language,
    }


def _transcribe_custom_batch(file_path: str, user_settings: dict, report) -> dict:
    """예전 /whisper (일괄 응답) + 매니저 진행상황 서버 웹소켓 단계 메시지"""
    model_level = int(user_settings.get("audio_model_level", 2) or 2)
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
    if subscriber:
        subscriber.start()
    report(5, f"커스텀 음성 인식 서버로 전송 중... ({fname}, 일괄 방식)")
    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                settings.AUDIO_LLM_URL,
                headers=_custom_headers(),
                files={"file": (fname, f, "audio/mpeg")},
                data={"option": json.dumps(_custom_option(user_settings, pid))},
                timeout=3600,
            )
    finally:
        if subscriber:
            subscriber.stop()

    if response.status_code == 401:
        raise RuntimeError(
            "커스텀 STT 서버 인증 실패(401): AUDIO_LLM_TOKEN 이 만료되었을 수 있습니다. 매니저 앱에서 다시 로그인 후 /token 값을 .env 에 갱신하세요."
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
        "model": f"whisper-{CUSTOM_MODEL_NAMES.get(model_level, model_level)}",
    }


def _transcribe_custom(file_path: str, user_settings: dict, on_progress=None) -> dict:
    if not custom_available():
        raise RuntimeError(NO_CUSTOM_MESSAGE)

    def report(percent, message):
        if on_progress:
            on_progress(percent, message)

    # GPU 는 한 번에 한 작업만 (GPU 보호). 스트림 엔드포인트가 없으면 일괄 방식으로 자동 전환.
    with audio_lock:
        try:
            return _transcribe_custom_stream(file_path, user_settings, report)
        except _StreamUnavailable as e:
            print(f"[STT] stream unavailable, falling back to batch: {e}")
            report(5, "실시간 스트림 엔드포인트가 없어 일괄 방식으로 전환합니다.")
            return _transcribe_custom_batch(file_path, user_settings, report)


# ==========================================
# openai: OpenAI Audio API (사용자 키)
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


def _transcribe_openai(file_path: str, user_settings: dict, on_progress=None) -> dict:
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
    if job.get("transcript_language"):
        user_settings = dict(user_settings, audio_language=job["transcript_language"])

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

        cost_note = (
            f" | 예상 비용 ${result['cost_usd']}" if result.get("cost_usd") else ""
        )
        JobManager.update_progress(
            job_id,
            100,
            100,
            f"완료 ({result['provider']} / {result['model']}{cost_note})",
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
