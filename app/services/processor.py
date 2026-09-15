import os
import io
import re
import json
import shutil
import base64
import hashlib
import requests
import subprocess
import threading
import concurrent.futures
import time
from PIL import Image
from pdf2image import convert_from_path
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager
from app.services.audio_processor import transcribe_audio
from app.services import result_store as rs
from app.db import docs_col
from app.db.prompt import default_system_prompt, default_user_prompt

# ==========================================
# Global Lock
# ==========================================
# Local LLM 사용 시에만 작동할 Lock (GPU 자원 보호)
local_gpu_lock = threading.Lock()

# 기본 모델 (app/core/config.py 에서 관리)
DEFAULT_MODEL = settings.DEFAULT_MODEL

# USD / 1M tokens. https://developers.openai.com/api/docs/pricing
# GPT-5.6 이후 모델은 캐시 쓰기(cache_write_tokens)에 입력 단가의 1.25배가 청구된다.
PRICING_TABLE = {
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached": 0.02,
        "output": 1.20,
        "cache_write_mult": 1.25,
    },
    "gpt-5.6-terra": {
        "input": 2.00,
        "cached": 0.20,
        "output": 12.00,
        "cache_write_mult": 1.25,
    },
    "gpt-5.6-sol": {
        "input": 4.00,
        "cached": 0.40,
        "output": 20.00,
        "cache_write_mult": 1.25,
    },
    "gpt-5.2": {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5-mini": {"input": 0.25, "cached": 0.025, "output": 2.00},
    "gpt-4o": {"input": 2.50, "cached": 1.25, "output": 10.00},
}

# GPT-5.x 는 reasoning 모델이라 기본 effort(medium)의 추론 토큰이 출력 요금으로 청구된다.
# 슬라이드 설명 작업에는 low 로 충분하며 출력 토큰을 크게 줄인다. (none/minimal/low/medium/high/xhigh/max)
REASONING_EFFORT = "low"

# ==========================================
# 토큰/비용 절감 관련 설정
# ==========================================
# Batch API 는 입력/출력 모두 50% 할인 (캐시 할인은 적용되지 않음)
BATCH_DISCOUNT = 0.5
# API 로 전송하는 이미지의 긴 변 최대 픽셀. 결과물(zip)에 들어가는 이미지는 원본 해상도 유지.
# GPT-5.6 계열은 이미지를 32px 패치로 토큰화 (ceil(w/32) * ceil(h/32) * 1.2, detail=auto 는 원본 크기 유지).
#   150DPI 16:9 슬라이드(약 2000x1125) ≈ 2,722 토큰  →  1536x864 ≈ 1,555 토큰 (약 43% 절감)
MAX_IMAGE_SIDE = 1536
# 실시간(OpenAI) 처리 시 동시 요청 수
PARALLEL_WORKERS = 3
# Batch API 폴링 간격(초), 진행 로그 heartbeat 간격(초), 최대 대기(초)
BATCH_POLL_INTERVAL = 15
BATCH_HEARTBEAT = 600
BATCH_MAX_WAIT = 24 * 3600 + 1800
# Batch 입력 파일 하나의 최대 크기 (OpenAI 제한 200MB 보다 여유 있게)
BATCH_MAX_FILE_BYTES = 150 * 1024 * 1024
# 사용자 프롬프트 템플릿의 {filename} 자리에 들어가는 고정 토큰 (캐시 prefix 유지용)
FILENAME_PLACEHOLDER = "[파일명]"

# ==========================================
# Helper Functions
# ==========================================


def get_headers(api_key=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {api_key or settings.CUSTOM_TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def get_target_model(user_settings):
    pref = user_settings.get("preferred_model", DEFAULT_MODEL)
    user_key = user_settings.get("openai_api_key", "")

    system_prompt = user_settings.get("custom_prompt", "")
    user_prompt_template = user_settings.get("custom_user_prompt", "")

    config = {
        "model_id": "gpt-4o",
        "base_url": settings.CUSTOM_BASE_URL,
        "api_key": None,
        "provider": "local",
        "system_prompt": system_prompt,
        "user_prompt_template": user_prompt_template,
        "use_batch": False,
    }

    if pref.startswith("gpt"):
        if not user_key:
            raise ValueError(
                "OpenAI 모델이 선택되었으나 API Key가 설정되지 않았습니다."
            )
        config.update(
            {
                "provider": "openai",
                "model_id": pref,
                "base_url": "https://api.openai.com/v1",
                "api_key": user_key,
                "use_batch": bool(user_settings.get("use_batch_api", False)),
            }
        )
    else:
        try:
            url = f"{settings.CUSTOM_BASE_URL}/models"
            resp = requests.get(url, headers=get_headers(), timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    config["model_id"] = models[0]["id"]
        except Exception:
            pass

    return config


def image_to_data_url(path: str, max_side: int = MAX_IMAGE_SIDE) -> str:
    """
    이미지를 data URL 로 변환. 긴 변이 max_side 를 넘으면 메모리 상에서만 축소하여
    이미지 입력 토큰과 전송 페이로드를 줄인다. (디스크의 원본은 그대로 유지)
    """
    with Image.open(path) as img:
        if max_side and max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw, mime = buf.getvalue(), "image/png"
        else:
            with open(path, "rb") as f:
                raw = f.read()
            ext = os.path.splitext(path)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"

    encoded = base64.b64encode(raw).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


def resolve_prompts(model_config: dict):
    system_instruction = (model_config.get("system_prompt") or "").strip()
    if not system_instruction:
        system_instruction = default_system_prompt.strip()

    user_template = (model_config.get("user_prompt_template") or "").strip()
    if not user_template:
        user_template = default_user_prompt.strip()

    return system_instruction, user_template


def build_messages(image_path: str, model_config: dict):
    """
    Prompt Caching 친화적인 메시지 구성.

    OpenAI(및 vLLM 등 prefix cache 를 지원하는 서버)는 요청의 '앞부분'이 이전 요청과
    완전히 동일할 때만 캐시를 재사용한다. 따라서
      - 모든 슬라이드에서 동일한 system prompt + 사용자 지시문을 맨 앞에 두고
      - 슬라이드마다 달라지는 이미지와 파일명은 맨 뒤에 배치한다.
    사용자 템플릿의 {filename} 은 고정 토큰으로 치환하고, 실제 파일명은 마지막 텍스트
    블록으로 전달하여 지시문 부분이 매 요청 바이트 단위로 동일하도록 만든다.
    """
    filename = os.path.basename(image_path)
    system_instruction, user_template = resolve_prompts(model_config)
    transcript = model_config.get("transcript")

    has_filename = "{filename}" in user_template
    static_text = user_template.replace("{filename}", FILENAME_PLACEHOLDER)
    if transcript:
        static_text = static_text + "\n" + TRANSCRIPT_INSTRUCTION

    user_content = [
        {"type": "text", "text": static_text},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
    ]
    if has_filename:
        user_content.append(
            {"type": "text", "text": f'{FILENAME_PLACEHOLDER} = "{filename}"'}
        )

    messages = [{"role": "system", "content": system_instruction}]
    if transcript:
        # 녹음본 '통째로'. 모든 슬라이드 요청에서 바이트 단위로 동일 → 캐시 prefix 에 포함
        messages.append(
            {
                "role": "system",
                "content": TRANSCRIPT_HEADER.format(transcript=transcript),
            }
        )
    messages.append({"role": "user", "content": user_content})
    return messages


def prompt_cache_key(model_config: dict) -> str:
    """
    동일한 프롬프트(모델 + system + 지시문)를 쓰는 요청들이 같은 캐시 서버로
    라우팅되도록 하는 키. 같은 사용자의 다른 작업에서도 캐시가 재사용된다.
    """
    system_instruction, user_template = resolve_prompts(model_config)
    transcript = model_config.get("transcript") or ""
    digest = hashlib.sha256(
        f"{model_config['model_id']}\n{system_instruction}\n{user_template}\n{transcript}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"lecai-{digest[:32]}"


def gpt_version(model_id: str):
    """'gpt-5.6-luna' -> (5, 6), 'gpt-5-mini' -> (5, 0), 'gpt-4o' -> (4, 0). 파싱 실패 시 None"""
    m = re.match(r"gpt-(\d+)(?:\.(\d+))?", model_id)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def is_gpt56_or_later(model_id: str) -> bool:
    v = gpt_version(model_id)
    return v is not None and v >= (5, 6)


def is_reasoning_model(model_id: str) -> bool:
    v = gpt_version(model_id)
    return v is not None and v >= (5, 0)


def supports_extended_cache(model_id: str) -> bool:
    """prompt_cache_retention="24h" 지원 모델 (GPT-5.6 미만의 gpt-5.x, gpt-4.1). GPT-5.6+ 에서는 deprecated."""
    if is_gpt56_or_later(model_id):
        return False
    return model_id.startswith(("gpt-5", "gpt-4.1"))


def build_payload(image_path: str, model_config: dict, for_batch: bool = False):
    payload = {
        "model": model_config["model_id"],
        "messages": build_messages(image_path, model_config),
        "max_completion_tokens": 10000,
    }
    return apply_request_options(payload, model_config, for_batch)


def apply_request_options(payload: dict, model_config: dict, for_batch: bool = False):
    """reasoning effort / prompt cache 옵션을 요청에 붙인다 (OpenAI 전용)."""
    if model_config["provider"] != "openai":
        return payload

    model_id = model_config["model_id"]

    # reasoning 모델: 추론 토큰(출력 요금)을 줄이기 위해 effort 를 낮춘다.
    if is_reasoning_model(model_id) and REASONING_EFFORT:
        payload["reasoning_effort"] = REASONING_EFFORT

    # 캐시 옵션은 실시간 요청에만. (Batch 는 별도 요금 체계)
    if not for_batch:
        # 동일 프롬프트 요청이 같은 캐시로 라우팅되도록 하는 키 (모든 모델 공통)
        payload["prompt_cache_key"] = prompt_cache_key(model_config)
        if supports_extended_cache(model_id):
            # GPT-5.6 미만: 기본 캐시(5~10분) 대신 24시간 유지
            payload["prompt_cache_retention"] = "24h"
        # GPT-5.6 이상: prompt_cache_options.ttl 은 "30m" 이 유일값이자 기본값이고
        # mode 도 기본(implicit)이 슬라이드 단발 요청에 맞으므로 별도 옵션을 보내지 않는다.

    return payload


def empty_usage() -> dict:
    return {"prompt": 0, "cached": 0, "cache_write": 0, "completion": 0}


def parse_usage(result: dict) -> dict:
    usage_info = empty_usage()
    try:
        usage = result.get("usage", {}) or {}
        details = usage.get("prompt_tokens_details", {}) or {}
        usage_info["prompt"] = usage.get("prompt_tokens", 0) or 0
        usage_info["completion"] = usage.get("completion_tokens", 0) or 0
        usage_info["cached"] = details.get("cached_tokens", 0) or 0
        # GPT-5.6+: 캐시에 새로 기록된 토큰 수 (1.25배 청구)
        usage_info["cache_write"] = details.get("cache_write_tokens", 0) or 0
    except Exception:
        pass
    return usage_info


# 모델에 따라 지원 여부가 달라 400 이 나면 제거하고 재시도하는 선택 파라미터
OPTIONAL_PARAMS = (
    "prompt_cache_retention",
    "prompt_cache_options",
    "reasoning_effort",
    "response_format",
)


def describe_image(image_path: str, model_config: dict):
    payload = build_payload(image_path, model_config)
    return _post_chat(payload, model_config, os.path.basename(image_path))


def _post_chat(payload: dict, model_config: dict, label: str, timeout: int = 180):
    """chat/completions 호출 + 재시도. (content, usage) 반환"""
    url = f"{model_config['base_url']}/chat/completions"
    headers = get_headers(model_config["api_key"])
    filename = label

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)

            if resp.status_code == 200:
                result = resp.json()
                content = result["choices"][0]["message"].get("content", "")
                finish_reason = result["choices"][0].get("finish_reason")

                if not content or not content.strip():
                    print(
                        f"[Empty Response] {filename} returned empty content. Retrying... ({attempt + 1}/{max_retries})"
                    )
                    print(f"Reason: {finish_reason}")
                    time.sleep(2)
                    continue

                return content, parse_usage(result)

            elif resp.status_code == 429:
                wait_time = (attempt + 1) * 5
                print(
                    f"[Rate Limit] 429 Error on {filename}. Waiting {wait_time}s... (Attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait_time)
                continue

            elif resp.status_code == 400 and any(
                p in payload and p in resp.text for p in OPTIONAL_PARAMS
            ):
                # 모델이 지원하지 않는 선택 옵션이면 해당 옵션만 제거하고 재시도
                for p in OPTIONAL_PARAMS:
                    if p in payload and p in resp.text:
                        print(
                            f"[Param] {model_config['model_id']} rejected '{p}'. Retrying without it."
                        )
                        payload.pop(p, None)
                continue

            else:
                print(f"[API Error] {resp.status_code}: {resp.text}")
                if resp.status_code >= 500:
                    time.sleep(3)
                    continue
                raise RuntimeError(
                    f"OpenAI API Error: {resp.status_code} - {resp.text}"
                )

        except requests.exceptions.Timeout:
            print(f"[Timeout] {filename} timed out. Retrying...")
            time.sleep(3)
            continue

        except Exception as e:
            print(f"[Exception] {str(e)}")
            if attempt == max_retries - 1:
                raise e
            time.sleep(2)

    raise RuntimeError(f"Failed to process {filename} after {max_retries} attempts.")


def convert_ppt_to_pdf(ppt_path: str, output_dir: str):
    subprocess.run(
        [
            "soffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            ppt_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = os.path.splitext(os.path.basename(ppt_path))[0]
    return os.path.join(output_dir, f"{base}.pdf")


def calculate_total_cost(model_id, total_usage, exchange_rate=1400, batch=False):
    matched_model = next((m for m in PRICING_TABLE if m in model_id), "default")
    if matched_model == "default":
        return 0.0, 0

    rates = PRICING_TABLE[matched_model]
    cached = total_usage.get("cached", 0)
    cache_write = (
        total_usage.get("cache_write", 0) if "cache_write_mult" in rates else 0
    )
    regular_input = max(total_usage["prompt"] - cached - cache_write, 0)

    usd_cost = (
        (regular_input * rates["input"])
        + (cached * rates["cached"])
        + (cache_write * rates["input"] * rates.get("cache_write_mult", 1.0))
        + (total_usage["completion"] * rates["output"])
    ) / 1000000

    if batch:
        usd_cost *= BATCH_DISCOUNT

    return round(usd_cost, 4), int(usd_cost * exchange_rate)


# ==========================================
# 진행 상태 (실시간/배치 공용)
# ==========================================


class AnalysisState:
    """슬라이드 분석 진행 상태. 실시간 처리분과 배치 처리분의 사용량을 따로 집계한다."""

    def __init__(self, job_id: str, model_config: dict, total_pages: int):
        self.job_id = job_id
        self.model_config = model_config
        self.total_pages = total_pages
        self.results_map = {}
        self.usage = empty_usage()
        self.batch_usage = empty_usage()
        self.completed = 0
        self.lock = threading.Lock()

    def add_result(self, idx, filename, content, usage, from_batch=False):
        with self.lock:
            self.results_map[idx] = (filename, content)
            for k in self.usage:
                self.usage[k] += usage.get(k, 0)
                if from_batch:
                    self.batch_usage[k] += usage.get(k, 0)
            self.completed += 1

    def add_failure(self, idx, error):
        with self.lock:
            self.results_map[idx] = (
                "error.png",
                f"**[분석 실패]** 오류가 발생했습니다: {error}",
            )
            self.completed += 1

    @property
    def total_tokens(self):
        return self.usage["prompt"] + self.usage["completion"]

    def cost(self):
        """실시간 처리분은 정가, 배치 처리분은 50% 할인가로 합산"""
        realtime_usage = {k: self.usage[k] - self.batch_usage[k] for k in self.usage}
        usd_rt, _ = calculate_total_cost(self.model_config["model_id"], realtime_usage)
        usd_b, _ = calculate_total_cost(
            self.model_config["model_id"], self.batch_usage, batch=True
        )
        usd = round(usd_rt + usd_b, 4)
        return usd, int(usd * 1400)

    def log_progress(self, prefix="분석 중"):
        with self.lock:
            msg = f"{prefix} ({self.completed}/{self.total_pages}) | 누적 토큰: {self.total_tokens:,}"
            if self.usage["cached"]:
                msg += f" (캐시 적중 {self.usage['cached']:,})"
            if self.model_config["provider"] == "openai":
                usd_val, krw_val = self.cost()
                msg += f" | 예상 비용: ${usd_val:.3f} (₩{krw_val:,})"
            JobManager.update_progress(
                self.job_id, self.completed, self.total_pages, msg
            )


# ==========================================
# 실시간 처리 (OpenAI 병렬 / Local 순차)
# ==========================================


def _run_realtime(
    state: AnalysisState, items, max_workers: int, prefix="분석 중", task=None
):
    """
    items: [(idx, item), ...]  item 은 기본적으로 이미지 경로.
    task(idx, item, model_config) -> (content, usage) 를 넘기면 다른 종류의 슬라이드 작업(예: 녹음본 반영)에도 사용 가능.
    OpenAI 병렬 처리 시 첫 슬라이드는 단독으로 먼저 보내 prompt cache 를 채운 뒤
    (cache warm-up) 나머지를 병렬로 보낸다. 동시에 출발하면 전부 캐시 미스가 난다.
    """
    items = list(items)
    if not items:
        return

    def run_one(idx, item):
        if task:
            content, usage = task(idx, item, state.model_config)
            label = ""
        else:
            content, usage = describe_image(item, state.model_config)
            label = os.path.basename(item)
        state.add_result(idx, label, content, usage)

    def run_safely(idx, img_path):
        try:
            run_one(idx, img_path)
        except Exception as e:
            print(f"[FINAL ERROR] Slide {idx} processing failed: {e}")
            state.add_failure(idx, str(e))
        state.log_progress(prefix)

    warmup, rest = items[0], items[1:]
    run_safely(*warmup)

    if not rest:
        return

    if max_workers <= 1:
        for idx, img_path in rest:
            run_safely(idx, img_path)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_safely, idx, p) for idx, p in rest]
        concurrent.futures.wait(futures)


# ==========================================
# 강의 녹음본 (Transcript) - 캐시 prefix 방식
# ==========================================
# 녹음본을 쪼개지 않고 '통째로' 모든 슬라이드 요청에 넣는다. 대신 요청의 고정 prefix
# (system prompt → 녹음본 → 고정 지시문) 위치에 두어 Prompt Caching 이 적용되게 한다.
#   - 첫 슬라이드(워밍업)에서 캐시에 기록(입력 단가의 1.25배, GPT-5.6+)
#   - 이후 슬라이드는 캐시 단가(정가의 10%)로 녹음본을 읽는다
#   - 슬라이드마다 달라지는 이미지/파일명은 항상 맨 뒤에 둔다
# 272K 입력 토큰을 넘으면 요금이 2배가 되므로 그 아래로 잘라 보낸다.
MAX_TRANSCRIPT_CHARS = 300_000  # 약 150k 토큰 (한국어 기준 여유 있게)

TRANSCRIPT_HEADER = """[강의 녹음본 전체 전사]
아래는 이 강의 자료를 설명한 강의 녹음의 전체 전사본입니다. 이후 요청마다 슬라이드 이미지가 한 장씩 주어지며,
당신은 이 전사본 전체에서 해당 슬라이드를 설명하는 부분을 스스로 찾아 활용해야 합니다.
전사본에는 오타, 잘못 인식된 단어, 군더더기 말이 포함될 수 있습니다.

<transcript>
{transcript}
</transcript>"""

TRANSCRIPT_INSTRUCTION = """
[강의 녹음본 활용 지침]
system 에 제공된 강의 녹음본 전체에서 이 슬라이드를 설명하는 부분을 찾아, 설명의 맨 마지막에 아래 절을 추가할 것.
### 🎙️ 강의 녹음 발췌
- **강의자 설명 요약:** 강의자가 이 슬라이드에서 실제로 말한 내용(강조점, 예시, 보충 설명, 시험 힌트 등)을 2~5문장으로 요약.
- **녹음 원문:** 해당 부분의 전사 원문을 인용 블록(>)으로 발췌. 원문을 임의로 다듬거나 지어내지 말고 그대로 옮기되,
  이 슬라이드와 직접 관련된 핵심 부분 위주로 최대 15문장 이내로 발췌할 것. 명백한 오인식 단어는 뒤에 (원문 오인식 추정) 표시 가능.
- 녹음본에 이 슬라이드에 해당하는 내용이 없으면 이 절 전체를 생략할 것."""


def transcript_path(job_id: str) -> str:
    return os.path.join(settings.UPLOAD_DIR, f"{job_id}.transcript.txt")


def _attach_original_if_any(job: dict, result_dir: str, entry: dict) -> dict:
    """작업에 딸린 녹음본 원본(음성/텍스트 문서)을 결과 폴더 transcripts/ 로 옮긴다."""
    path = (job or {}).get("transcript_original_path")
    if path and os.path.exists(path):
        return rs.attach_original(
            result_dir, entry, path, job.get("transcript_source") or ""
        )
    return entry


def _cleanup_transcript_inputs(job_id: str):
    """작업 종료 시 UPLOAD_DIR 에 남은 녹음본 입력 파일 정리 (성공 시엔 이미 결과 폴더로 이동됨)"""
    if os.path.exists(transcript_path(job_id)):
        os.remove(transcript_path(job_id))
    job = JobManager.get_job(job_id) or {}
    for key in ("transcript_original_path", "transcript_audio_path"):
        path = job.get(key)
        if path and os.path.exists(path):
            os.remove(path)


def load_transcript(job_id: str):
    path = transcript_path(job_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read().strip()
    return text or None


def resolve_transcript_for_job(job_id: str, user_settings: dict):
    """
    작업에 딸린 녹음본을 확정한다. 텍스트가 있으면 그대로, 음성 파일만 있으면 STT 를 먼저 수행해
    transcript_path 에 저장한다 (진행 상황은 작업 로그로 표시). 반환: 정규화된 녹음본 또는 None
    """
    job = JobManager.get_job(job_id) or {}
    audio_path = job.get("transcript_audio_path")
    if job.get("transcript_language"):
        # 업로드 모달에서 고른 언어 (이번 파일에만 적용)
        user_settings = dict(user_settings, audio_language=job["transcript_language"])
    if audio_path and os.path.exists(audio_path) and not load_transcript(job_id):
        try:
            JobManager.update_progress(
                job_id,
                0,
                0,
                f"녹음 파일 음성 인식(STT) 시작: {job.get('transcript_source', '')}",
            )
            result = transcribe_audio(
                audio_path,
                user_settings,
                on_progress=lambda p, m: JobManager.update_progress(
                    job_id, p, 100, f"[STT] {m}"
                ),
            )
            text = result["text"]
            if result.get("cost_usd"):
                AuthManager.update_user_cumulative_usage(
                    (job.get("owner") or ""), result["cost_usd"]
                )
            with open(transcript_path(job_id), "w", encoding="utf-8") as f:
                f.write(text)
            JobManager.set_transcript_flag(job_id, len(text))
            JobManager.update_progress(
                job_id,
                100,
                100,
                f"[STT] 완료: {len(text):,}자 ({result.get('provider', '?')} / {result.get('model', '?')})",
            )
        finally:
            pass  # 음성 원본은 보관을 위해 남겨 둔다 (결과 폴더로 이동)
    text = load_transcript(job_id)
    return normalize_transcript(text) if text else None


def normalize_transcript(text: str) -> str:
    """공백 정리만 수행 (내용은 쪼개거나 요약하지 않음). 캐시 prefix 안정성을 위해 결정적이어야 한다."""
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS]
    return text


# ==========================================
# OpenAI Batch API (50% 할인)
# ==========================================


class BatchUnavailable(Exception):
    """배치 등록 자체가 불가능한 경우 (큐 한도 초과 등) → 실시간 처리로 대체"""


def _batch_upload_file(base_url, api_key, data: bytes, name: str) -> str:
    resp = requests.post(
        f"{base_url}/files",
        headers=get_headers(api_key, content_type=None),
        files={"file": (name, data, "application/jsonl")},
        data={"purpose": "batch"},
        timeout=600,
    )
    if resp.status_code != 200:
        raise BatchUnavailable(f"파일 업로드 실패 ({resp.status_code}): {resp.text}")
    return resp.json()["id"]


def _batch_create(base_url, api_key, file_id, job_id) -> dict:
    resp = requests.post(
        f"{base_url}/batches",
        headers=get_headers(api_key),
        json={
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": {"lecai_job_id": job_id},
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise BatchUnavailable(f"배치 생성 실패 ({resp.status_code}): {resp.text}")
    return resp.json()


def _batch_get(base_url, api_key, batch_id) -> dict:
    resp = requests.get(
        f"{base_url}/batches/{batch_id}", headers=get_headers(api_key), timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def _batch_cancel(base_url, api_key, batch_id):
    try:
        requests.post(
            f"{base_url}/batches/{batch_id}/cancel",
            headers=get_headers(api_key),
            timeout=30,
        )
    except Exception as e:
        print(f"[Batch] cancel failed for {batch_id}: {e}")


def _file_delete(base_url, api_key, file_id):
    if not file_id:
        return
    try:
        requests.delete(
            f"{base_url}/files/{file_id}", headers=get_headers(api_key), timeout=30
        )
    except Exception as e:
        print(f"[Batch] file delete failed for {file_id}: {e}")


def _file_content(base_url, api_key, file_id) -> str:
    resp = requests.get(
        f"{base_url}/files/{file_id}/content",
        headers=get_headers(api_key),
        timeout=600,
    )
    resp.raise_for_status()
    return resp.text


def _build_batch_chunks(items, model_config):
    """슬라이드별 요청을 JSONL 로 직렬화하고 파일 크기 한도에 맞춰 나눈다."""
    chunks, current, current_size = [], [], 0
    for idx, img_path in items:
        line = json.dumps(
            {
                "custom_id": f"slide-{idx}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": build_payload(img_path, model_config, for_batch=True),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if current and current_size + len(line) + 1 > BATCH_MAX_FILE_BYTES:
            chunks.append(current)
            current, current_size = [], 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


BATCH_TERMINAL = {"completed", "failed", "expired", "cancelled"}
BATCH_STATUS_KO = {
    "validating": "검증 중",
    "in_progress": "처리 중",
    "finalizing": "마무리 중",
    "completed": "완료",
    "failed": "실패",
    "expired": "만료",
    "cancelling": "취소 중",
    "cancelled": "취소됨",
}


def _run_batch(state: AnalysisState, items, image_by_idx):
    """
    OpenAI Batch API 로 슬라이드를 처리한다. 진행률은 배치의 request_counts 를 폴링해
    JobManager 에 반영하므로 대시보드의 진행 표시는 실시간 처리와 동일하게 유지된다.
    반환값: 배치로 처리되지 못한 (idx, path) 목록 → 호출측에서 실시간 처리
    """
    cfg = state.model_config
    base_url, api_key = cfg["base_url"], cfg["api_key"]
    job_id = state.job_id
    items = list(items)

    JobManager.update_progress(
        job_id,
        0,
        state.total_pages,
        f"배치 요청 파일 생성 중 ({len(items)}개 슬라이드)",
    )
    chunks = _build_batch_chunks(items, cfg)

    batches = []  # {"id", "input_file_id", "status", "counts"}
    try:
        for n, chunk in enumerate(chunks, 1):
            JobManager.update_progress(
                job_id, 0, state.total_pages, f"배치 업로드 중 ({n}/{len(chunks)})"
            )
            data = b"\n".join(chunk) + b"\n"
            file_id = _batch_upload_file(
                base_url, api_key, data, f"lecai-{job_id}-{n}.jsonl"
            )
            try:
                batch = _batch_create(base_url, api_key, file_id, job_id)
            except BatchUnavailable:
                _file_delete(base_url, api_key, file_id)
                raise
            batches.append(
                {
                    "id": batch["id"],
                    "input_file_id": file_id,
                    "status": batch.get("status", "validating"),
                    "counts": batch.get("request_counts", {}) or {},
                    "output_file_id": None,
                    "error_file_id": None,
                }
            )
    except BatchUnavailable:
        for b in batches:
            _batch_cancel(base_url, api_key, b["id"])
            _file_delete(base_url, api_key, b["input_file_id"])
        raise

    ids_text = ", ".join(b["id"] for b in batches)
    JobManager.update_progress(
        job_id,
        0,
        state.total_pages,
        f"배치 등록 완료 ({ids_text}) | OpenAI 대기열에서 처리 중 (최대 24시간, 50% 할인)",
    )

    # ---- 폴링: 진행률/상태 변화가 있을 때만 로그를 남기고, 그 외엔 heartbeat 만 ----
    started = time.time()
    last_signature, last_log_at = None, time.time()
    while True:
        for b in batches:
            if b["status"] in BATCH_TERMINAL:
                continue
            try:
                info = _batch_get(base_url, api_key, b["id"])
            except Exception as e:
                print(f"[Batch] poll failed for {b['id']}: {e}")
                continue
            b["status"] = info.get("status", b["status"])
            b["counts"] = info.get("request_counts", {}) or b["counts"]
            b["output_file_id"] = info.get("output_file_id")
            b["error_file_id"] = info.get("error_file_id")

        completed = sum(b["counts"].get("completed", 0) for b in batches)
        failed = sum(b["counts"].get("failed", 0) for b in batches)
        statuses = sorted({b["status"] for b in batches})
        status_label = " / ".join(BATCH_STATUS_KO.get(s, s) for s in statuses)
        done = completed + failed

        signature = (completed, failed, tuple(statuses))
        now = time.time()
        if signature != last_signature or now - last_log_at >= BATCH_HEARTBEAT:
            msg = f"배치 {status_label} ({done}/{state.total_pages})"
            if failed:
                msg += f" | 실패 {failed}개 (실시간으로 재처리 예정)"
            JobManager.update_progress(job_id, done, state.total_pages, msg)
            last_signature, last_log_at = signature, now
        else:
            JobManager.update_progress(job_id, done, state.total_pages)

        if all(b["status"] in BATCH_TERMINAL for b in batches):
            break

        if now - started > BATCH_MAX_WAIT:
            JobManager.update_progress(
                job_id,
                done,
                state.total_pages,
                "배치 대기 시간 초과 → 취소 후 실시간 처리",
            )
            for b in batches:
                if b["status"] not in BATCH_TERMINAL:
                    _batch_cancel(base_url, api_key, b["id"])
            break

        time.sleep(BATCH_POLL_INTERVAL)

    # ---- 결과 수집 (부분 완료/만료된 배치의 결과도 최대한 회수) ----
    JobManager.update_progress(job_id, done, state.total_pages, "배치 결과 수집 중...")
    for b in batches:
        if b["output_file_id"]:
            try:
                raw = _file_content(base_url, api_key, b["output_file_id"])
            except Exception as e:
                print(f"[Batch] output download failed for {b['id']}: {e}")
                raw = ""
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    idx = int(row["custom_id"].split("-", 1)[1])
                    response = row.get("response") or {}
                    body = response.get("body") or {}
                    if response.get("status_code") != 200 or row.get("error"):
                        continue
                    content = body["choices"][0]["message"].get("content", "")
                    if not content or not content.strip():
                        continue
                    state.add_result(
                        idx,
                        os.path.basename(image_by_idx[idx]),
                        content,
                        parse_usage(body),
                        from_batch=True,
                    )
                except Exception as e:
                    print(f"[Batch] bad result line skipped: {e}")

        # 업로드/결과 파일은 사용자 계정 스토리지에 남으므로 정리
        _file_delete(base_url, api_key, b["input_file_id"])
        _file_delete(base_url, api_key, b["output_file_id"])
        _file_delete(base_url, api_key, b["error_file_id"])

    state.log_progress("배치 분석 완료")

    remaining = [(idx, p) for idx, p in items if idx not in state.results_map]
    return remaining


# ==========================================
# Main Processing Logic
# ==========================================


def _process_job_internal(job_id: str, file_path: str, model_config: dict, owner: str):
    """
    실제 파일 처리 로직 (실시간 비용 로그 포함)
    """
    work_dir = os.path.join(settings.UPLOAD_DIR, job_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        JobManager.start_processing(job_id)

        # 1. 이미지 변환 (PDF/PPT -> Images)
        images = []
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".pdf":
            raw_images = convert_from_path(file_path, fmt="png", dpi=150)
            for i, img in enumerate(raw_images):
                p = os.path.join(work_dir, f"page_{i + 1:03d}.png")
                img.save(p)
                images.append(p)
        elif ext in [".ppt", ".pptx"]:
            pdf_path = convert_ppt_to_pdf(file_path, work_dir)
            raw_images = convert_from_path(pdf_path, fmt="png", dpi=150)
            for i, img in enumerate(raw_images):
                p = os.path.join(work_dir, f"page_{i + 1:03d}.png")
                img.save(p)
                images.append(p)

        result_base = os.path.join(settings.RESULT_DIR, job_id)
        result_images_dir = os.path.join(result_base, "images")
        os.makedirs(result_images_dir, exist_ok=True)

        for img_path in images:
            shutil.copy2(
                img_path, os.path.join(result_images_dir, os.path.basename(img_path))
            )

        total_pages = len(images)

        # 강의 녹음본(텍스트 또는 음성→STT)이 있으면 통째로 프롬프트 prefix 에 넣는다 (Prompt Caching 으로 비용 절감)
        transcript = resolve_transcript_for_job(
            job_id, AuthManager.get_user_settings(owner)
        )
        if transcript:
            model_config = dict(model_config, transcript=transcript)
            approx_tokens = len(transcript) // 2  # 한국어 대략 추정치
            JobManager.update_progress(
                job_id,
                0,
                total_pages,
                f"강의 녹음본 포함 ({len(transcript):,}자, 약 {approx_tokens:,} 토큰) | "
                f"모든 슬라이드에 통째로 전달, 프롬프트 캐싱으로 2번째 슬라이드부터 캐시 단가 적용",
            )

        state = AnalysisState(job_id, model_config, total_pages)
        items = list(enumerate(images, 1))
        image_by_idx = dict(items)

        # 2. LLM 분석 (Batch / 병렬 / 순차)
        if model_config["provider"] == "openai" and model_config.get("use_batch"):
            try:
                remaining = _run_batch(state, items, image_by_idx)
            except BatchUnavailable as e:
                print(f"[Batch] unavailable, falling back to realtime: {e}")
                JobManager.update_progress(
                    job_id, 0, total_pages, f"배치 등록 실패 → 실시간 처리로 전환 ({e})"
                )
                remaining = items
            if remaining:
                JobManager.update_progress(
                    job_id,
                    state.completed,
                    total_pages,
                    f"배치 미처리 슬라이드 {len(remaining)}개 → 실시간 재처리",
                )
                _run_realtime(state, remaining, PARALLEL_WORKERS, prefix="재처리 중")

        elif model_config["provider"] == "openai":
            _run_realtime(state, items, PARALLEL_WORKERS)

        else:
            # Local LLM: 순차 처리 (GPU 보호)
            _run_realtime(state, items, 1)

        results_map = state.results_map

        # 3. 슬라이드별 저장 (desc/slide_NNN.md). 뷰어/PDF 는 result_store.compose_markdown 으로 합친다.
        if transcript:
            job_doc = JobManager.get_job(job_id) or {}
            entry = rs.save_transcript(
                result_base,
                transcript,
                job_doc.get("transcript_source") or "업로드 시 녹음본",
            )
            _attach_original_if_any(job_doc, result_base, entry)
            results_map = {
                idx: (fname, rs.mark_transcript_section(text, entry["sid"]))
                for idx, (fname, text) in results_map.items()
            }
        rs.write_slides(
            result_base, {idx: text for idx, (_, text) in results_map.items()}
        )

        # 4. 최종 완료 처리
        if model_config["provider"] == "openai":
            usd_val, krw_val = state.cost()
            job = JobManager.get_job(job_id)
            if job:
                AuthManager.update_user_cumulative_usage(job["owner"], usd_val)
            final_log = f"작업 완료! 총 비용: ${usd_val} (약 ₩{krw_val:,}) | 총 토큰: {state.total_tokens:,}"
            if state.usage["cached"]:
                final_log += f" | 캐시 적중 토큰: {state.usage['cached']:,}"
            if state.batch_usage["prompt"]:
                final_log += " | Batch 50% 할인 적용"
        else:
            final_log = f"작업 완료! 총 토큰: {state.total_tokens:,}"
        JobManager.update_progress(job_id, total_pages, total_pages, final_log)

        # PDF 생성 (.env GENERATE_PDF=true 일 때만)
        if settings.GENERATE_PDF:
            JobManager.update_progress(
                job_id, total_pages, total_pages, "PDF 생성 중..."
            )
            rs.render_pdf(result_base)

        # 압축 및 정리
        user_result_dir = os.path.join(settings.RESULT_DIR, owner)
        os.makedirs(user_result_dir, exist_ok=True)
        shutil.make_archive(os.path.join(user_result_dir, job_id), "zip", result_base)

        # [Cleanup] 압축 후 원본 폴더 삭제
        if os.path.exists(result_base):
            shutil.rmtree(result_base)

        JobManager.mark_completed(job_id, f"/static/results/{owner}/{job_id}.zip")

        # 5. 뷰어에서 올린 작업이면 문서함의 지정 폴더로 자동 저장
        _auto_import_to_docs(job_id, owner)

    except Exception as e:
        JobManager.mark_failed(job_id, str(e))
    finally:
        # [Cleanup] 임시 작업 폴더 및 업로드 원본 삭제
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
        if os.path.exists(file_path):
            os.remove(file_path)
        _cleanup_transcript_inputs(job_id)


# ==========================================
# 강의 녹음본 사후 반영 (이미 생성된 설명에 추가)
# ==========================================
# 슬라이드 이미지는 다시 보내지 않는다. 고정 prefix(system 지침 → 녹음본 통째로 → 고정 지시문)는
# 캐시되고, 슬라이드마다 '기존 설명 텍스트'만 뒤에 붙여 보낸다. 해당 슬라이드를 설명한 부분이 없으면
# 모델이 NONE 만 출력하고, 그 슬라이드는 건드리지 않는다.
# 정확도 우선: 슬라이드-녹음본 매핑은 저(low) effort 로는 경계 오판이 잦아 medium 을 쓴다.
ENRICH_REASONING_EFFORT = "medium"
# 슬라이드 목차(캐시 prefix)에 넣는 슬라이드별 힌트 길이
OUTLINE_HINT_CHARS = 160

ENRICH_SYSTEM_PROMPT = """당신은 강의 녹음 전사본의 어느 부분이 어느 슬라이드를 설명하는지 정확히 판별하는 전문 AI 조교입니다.
system 에 (1) 강의 녹음본 전체와 (2) 전체 슬라이드 목차가 주어지고, 요청마다 대상 슬라이드 하나의 기존 설명(마크다운)이 주어집니다.
당신의 임무는 녹음본에서 '오직 대상 슬라이드를 설명하는 부분'만 찾아 정리하는 것입니다. 잘못 매핑하는 것이 빠뜨리는 것보다 훨씬 나쁩니다.

[판별 절차 - 반드시 순서대로 수행]
1. 목차에서 대상 슬라이드와 바로 앞·뒤 슬라이드가 무엇을 다루는지 확인한다. 강의는 대체로 슬라이드 순서대로 진행되므로,
   앞 슬라이드를 설명한 부분과 뒤 슬라이드를 설명한 부분 '사이'에 대상 슬라이드의 설명이 위치한다.
2. 녹음본에서 대상 슬라이드의 고유한 내용(제목, 용어, 수식, 그림, 예제, 숫자)이 언급되는 부분을 찾는다.
   슬라이드 전환 신호("다음 슬라이드", "이 그림을 보면", "여기 보시면", 새 주제 제시 등)를 경계의 근거로 삼는다.
3. 찾은 부분의 각 문장에 대해 "이 문장이 앞 슬라이드나 뒤 슬라이드가 아니라 바로 이 슬라이드를 설명한다"고 확신할 수 있는지 검토한다.
   같은 용어가 여러 슬라이드에 걸쳐 나오면, 목차의 슬라이드별 차이(정의 vs 예제 vs 증명 vs 비교 등)로 구분한다.
4. 확신도를 판정한다. 대상 슬라이드의 고유 내용이 녹음본에서 명시적으로 다뤄지고 경계도 분명하면 '높음',
   주제는 맞지만 경계가 불분명하거나 짧게 스쳐 지나가면 '보통', 그 외는 '낮음'이다.

[출력 규칙]
- 확신도가 '높음' 또는 '보통'이면 아래 형식의 마크다운만 출력할 것 (다른 텍스트 금지):
### 🎙️ 강의 녹음 발췌
**매칭 근거:** 이 부분이 이 슬라이드를 가리킨다고 판단한 근거(녹음본에 등장한 슬라이드 고유 용어·표현, 앞뒤 슬라이드와의 경계)를 1~2문장. 확신도(높음/보통)를 함께 표기.
**강의자 설명 요약:** 강의자가 이 슬라이드에서 실제로 말한 내용(강조점, 예시, 보충 설명, 시험 힌트 등)을 한국어 2~5문장으로 요약.
**슬라이드에 없는 추가 내용:** 기존 슬라이드 설명에 없지만 강의자가 덧붙인 정보만 불릿으로 정리. 없으면 이 줄을 생략.
> 녹음 원문 인용은 인용 블록(>)에 한 문장(또는 한 발화)씩 한 줄로, 녹음본의 문장을 '한 글자도 바꾸지 않고' 그대로 옮길 것.
> 요약·의역·오탈자 수정·문장 결합 금지. 이 슬라이드와 직접 관련된 핵심 부분 위주로 최대 15줄.
- 확신도가 '낮음'이거나, 녹음본에 대상 슬라이드에 해당하는 내용이 없거나, 다른 슬라이드의 내용을 끌어와야만 채울 수 있다면 정확히 NONE 만 출력할 것.
- 앞·뒤 슬라이드에 속하는 문장을 이 슬라이드에 넣지 말 것. 한 문장은 그 문장이 설명하는 슬라이드 하나에만 속한다.
- 목차/표지/구분 슬라이드는 강의자가 그 슬라이드를 명시적으로 언급한 경우가 아니면 NONE.
- 수식은 LaTeX($...$)로, 한국어로 작성할 것."""

ENRICH_OUTLINE_HEADER = """[전체 슬라이드 목차]
아래는 이 강의 자료의 모든 슬라이드와 각 슬라이드의 핵심 내용 요약입니다. 대상 슬라이드의 앞·뒤 슬라이드가 무엇을 다루는지 확인하여
녹음본에서 대상 슬라이드에 해당하는 부분의 경계를 정확히 정하는 데 사용하십시오.
{outline}"""

ENRICH_INSTRUCTION = (
    "아래 대상 슬라이드의 기존 설명을 읽고, system 의 강의 녹음본에서 이 슬라이드를 설명한 부분을 판별 절차에 따라 찾아 "
    "규칙에 맞는 '강의 녹음 발췌' 절을 출력하거나 NONE 을 출력하십시오."
)


def outline_hint(markdown_text: str, limit: int = OUTLINE_HINT_CHARS) -> str:
    """슬라이드 설명에서 목차용 한 줄 요약: '핵심 주제' 절 우선, 없으면 첫 제목+첫 문장"""
    text = markdown_text or ""
    # 이전 녹음 발췌 절은 제외
    text = re.sub(
        r"<!-- transcript:[^ ]+ -->.*?<!-- /transcript:[^ ]+ -->", "", text, flags=re.S
    )
    m = re.search(r"핵심 주제[^\n]*\n+(.+?)(?:\n#{1,6}\s|\n\*\*|$)", text, re.S)
    hint = m.group(1) if m else text
    hint = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", hint)
    hint = re.sub(r"[#*_`>|]", "", hint)
    hint = re.sub(r"\s+", " ", hint).strip()
    return hint[:limit]


def build_outline(slides: dict) -> str:
    return "\n".join(
        f"{idx}: {outline_hint(slides[idx]) or '(내용 없음)'}" for idx in sorted(slides)
    )


def enrich_slide(idx, item, model_config):
    """item = (total_pages, slide_text). 반환 (content, usage)"""
    total, slide_text = item
    # 앞뒤 슬라이드 위치 힌트 (변하는 부분이므로 맨 뒤)
    neighbors = f"앞 슬라이드: {idx - 1 if idx > 1 else '없음'} / 뒤 슬라이드: {idx + 1 if idx < total else '없음'}"
    payload = {
        "model": model_config["model_id"],
        "messages": [
            {"role": "system", "content": ENRICH_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": TRANSCRIPT_HEADER.format(
                    transcript=model_config["transcript"]
                ),
            },
            {
                "role": "system",
                "content": ENRICH_OUTLINE_HEADER.format(
                    outline=model_config.get("outline", "")
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ENRICH_INSTRUCTION},
                    {
                        "type": "text",
                        "text": f"[대상 슬라이드: {idx} / 전체 {total}] ({neighbors})\n[대상 슬라이드의 기존 설명]\n{slide_text}",
                    },
                ],
            },
        ],
        "max_completion_tokens": 6000,
    }
    apply_request_options(payload, model_config)
    if "reasoning_effort" in payload:
        payload["reasoning_effort"] = ENRICH_REASONING_EFFORT
    return _post_chat(payload, model_config, f"enrich-slide-{idx}")


def _is_none_answer(content: str) -> bool:
    text = (content or "").strip().strip("`").strip()
    return text.upper() == "NONE" or text.startswith("**[분석 실패]**")


def _extract_transcript_section(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text, flags=re.S).strip()
    pos = text.find(rs.TRANSCRIPT_SECTION_HEADING)
    if pos >= 0:
        return text[pos:].strip()
    return f"{rs.TRANSCRIPT_SECTION_HEADING}\n\n{text}"


def _norm_for_match(text: str) -> str:
    """인용 검증용 정규화: 공백·문장부호·따옴표 제거"""
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def verify_quotes(section: str, transcript: str, min_len: int = 6):
    """
    발췌 절의 인용(> 줄)이 실제 녹음본에 그대로 존재하는지 검증한다.
    검증되지 않은 인용 줄은 제거한다. 반환: (정리된 절, 검증된 줄 수, 전체 인용 줄 수)
    """
    norm_t = _norm_for_match(transcript)
    lines = section.split("\n")
    kept, verified, total = [], 0, 0
    for line in lines:
        if line.lstrip().startswith(">"):
            body = line.lstrip()[1:].strip()
            if not body or body.startswith("녹음 원문"):
                kept.append(line)
                continue
            total += 1
            nb = _norm_for_match(body)
            if len(nb) >= min_len and nb in norm_t:
                verified += 1
                kept.append(line)
            # 검증 실패 줄은 버림 (원문에 없는 문장 = 의역/창작 가능성)
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    # 인용이 모두 사라졌으면 빈 인용 블록 헤더도 정리
    cleaned = re.sub(r"\n>\s*녹음 원문[^\n]*\n(?=\s*(\n|$))", "\n", cleaned)
    return cleaned.strip(), verified, total


def enrich_transcript_task(job_id: str, target_type: str, target_id: str):
    """
    사후 녹음본 반영 진입점. target_type: "doc"(문서함 문서, 제자리 갱신) | "job"(작업 결과 zip 갱신 + 연결된 문서 전파)
    """
    job = JobManager.get_job(job_id)
    if not job:
        return
    owner = job.get("owner")
    user_settings = AuthManager.get_user_settings(owner)
    work_dir = os.path.join(settings.UPLOAD_DIR, job_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        JobManager.start_processing(job_id)

        # 1. 녹음본 확보 (붙여넣은 텍스트 / 텍스트 문서 본문 / 음성 파일 STT)
        transcript = resolve_transcript_for_job(job_id, user_settings)
        if not transcript:
            raise RuntimeError("녹음본 텍스트가 비어 있습니다.")

        # 2. 대상 결과 디렉터리 확보
        zip_path = None
        if target_type == "doc":
            doc = docs_col.find_one({"id": target_id, "owner": owner, "type": "file"})
            if not doc:
                raise RuntimeError("대상 문서를 찾을 수 없습니다.")
            result_dir = os.path.join(settings.DOCS_STATIC_DIR, owner, target_id)
            result_url = f"/api/docs/download/{target_id}"
        else:
            zip_path = os.path.join(settings.RESULT_DIR, owner, f"{target_id}.zip")
            if not os.path.exists(zip_path):
                raise RuntimeError(
                    "작업 결과 파일(zip)이 없습니다. 문서함의 문서에 추가해 주세요."
                )
            result_dir = os.path.join(work_dir, "result")
            shutil.unpack_archive(zip_path, result_dir, "zip")
            result_url = f"/static/results/{owner}/{target_id}.zip"

        rs.ensure_desc_layout(result_dir)
        slides = rs.read_slides(result_dir)
        if not slides:
            raise RuntimeError("슬라이드 설명(desc/)을 찾을 수 없습니다.")
        total = len(slides)

        # 출처 식별: 같은 녹음본을 다시 넣으면 해당 출처의 절만 교체되고, 다른 녹음본이면 누적된다
        source_label = (job.get("transcript_source") or "붙여넣은 텍스트").strip()
        entry = rs.save_transcript(result_dir, transcript, source_label)
        entry = _attach_original_if_any(job, result_dir, entry)
        sid = entry["sid"]
        label = f"{entry['label']} · {entry['added_at'][:10]}"
        prior = [e for e in rs.list_transcripts(result_dir) if e["sid"] != sid]
        JobManager.update_progress(
            job_id,
            0,
            total,
            f"녹음본 #{entry['seq']} '{entry['label']}' 등록"
            + (f" (기존 녹음본 {len(prior)}개와 함께 누적)" if prior else ""),
        )

        # 3. 슬라이드마다 녹음본에서 해당 부분 찾기 (이미지 없이, 녹음본+목차는 캐시 prefix)
        model_config = get_target_model(user_settings)
        model_config = dict(
            model_config, transcript=transcript, outline=build_outline(slides)
        )
        approx_tokens = len(transcript) // 2
        JobManager.update_progress(
            job_id,
            0,
            total,
            f"녹음본 {len(transcript):,}자(약 {approx_tokens:,} 토큰)를 {total}개 슬라이드에 대조 | "
            f"이미지 재전송 없음, 프롬프트 캐싱 적용",
        )
        state = AnalysisState(job_id, model_config, total)
        items = [(idx, (total, slides[idx])) for idx in sorted(slides)]
        workers = PARALLEL_WORKERS if model_config["provider"] == "openai" else 1
        _run_realtime(state, items, workers, prefix="녹음본 대조 중", task=enrich_slide)

        # 4. 해당 부분이 있는 슬라이드에만 절 추가 (인용은 녹음본 원문 대조로 검증)
        added, rejected, dropped_lines = 0, 0, 0
        for idx, (_, content) in state.results_map.items():
            if _is_none_answer(content):
                continue
            section = _extract_transcript_section(content)
            section, verified, quoted = verify_quotes(section, transcript)
            dropped_lines += quoted - verified
            if quoted and verified == 0:
                # 인용이 하나도 원문에 없으면 매핑 자체를 신뢰할 수 없어 버린다
                rejected += 1
                print(f"[ENRICH] slide {idx}: quotes not found in transcript, rejected")
                continue
            section = re.sub(
                r"^### 🎙️ 강의 녹음 발췌.*$",
                f"### 🎙️ 강의 녹음 발췌 ({label})",
                section,
                count=1,
                flags=re.M,
            )
            rs.write_slide(
                result_dir, idx, rs.upsert_transcript_section(slides[idx], section, sid)
            )
            added += 1
        if rejected or dropped_lines:
            JobManager.update_progress(
                job_id,
                total,
                total,
                f"인용 검증: 원문에 없는 인용 {dropped_lines}줄 제거, 신뢰 불가 슬라이드 {rejected}개 제외",
            )
        if settings.GENERATE_PDF:
            JobManager.update_progress(job_id, total, total, "PDF 생성 중...")
            rs.render_pdf(result_dir)

        # 5. 저장/전파
        if target_type == "job":
            shutil.make_archive(zip_path[:-4], "zip", result_dir)
            JobManager.set_transcript_flag(target_id, len(transcript))
            for doc in docs_col.find({"source_job_id": target_id, "type": "file"}):
                doc_dir = os.path.join(
                    settings.DOCS_STATIC_DIR, doc["owner"], doc["id"]
                )
                if not os.path.isdir(doc_dir):
                    continue
                for name in (
                    rs.DESC_DIR,
                    rs.TRANSCRIPTS_DIR,
                    rs.TRANSCRIPT_FILE,
                    "result.pdf",
                ):
                    src = os.path.join(result_dir, name)
                    dst = os.path.join(doc_dir, name)
                    if os.path.isdir(src):
                        shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(src, dst)
                    elif os.path.exists(src):
                        shutil.copy2(src, dst)
                docs_col.update_one(
                    {"id": doc["id"]}, {"$set": {"has_transcript": True}}
                )
        else:
            docs_col.update_one({"id": target_id}, {"$set": {"has_transcript": True}})

        if model_config["provider"] == "openai":
            usd_val, krw_val = state.cost()
            AuthManager.update_user_cumulative_usage(owner, usd_val)
            final_log = (
                f"녹음본 #{entry['seq']} 반영 완료: {added}/{total}개 슬라이드에 발췌 추가 | "
                f"비용: ${usd_val} (약 ₩{krw_val:,}) | 캐시 적중 토큰: {state.usage['cached']:,}"
            )
        else:
            final_log = f"녹음본 #{entry['seq']} 반영 완료: {added}/{total}개 슬라이드에 발췌 추가"
        JobManager.update_progress(job_id, total, total, final_log)
        JobManager.mark_completed(job_id, result_url)

    except Exception as e:
        print(f"[ENRICH ERROR] {e}")
        JobManager.mark_failed(job_id, str(e))
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
        _cleanup_transcript_inputs(job_id)


def _auto_import_to_docs(job_id: str, owner: str):
    """job 에 auto_import_parent_id 가 있으면 결과 zip 을 문서함 폴더로 가져온다 (실패해도 작업은 완료 상태 유지)"""
    job = JobManager.get_job(job_id) or {}
    parent = (job.get("auto_import_parent_id") or "").strip()
    if not parent:
        return
    try:
        from app.services.doc_manager import DocManager

        parent_id = None if parent == "root" else parent
        folder_name = "최상위"
        if parent_id:
            folder = docs_col.find_one(
                {"id": parent_id, "owner": owner, "type": "folder"}
            )
            if not folder:
                raise RuntimeError("지정한 폴더가 더 이상 존재하지 않습니다.")
            folder_name = folder["name"]
        zip_path = os.path.join(settings.RESULT_DIR, owner, f"{job_id}.zip")
        doc = DocManager.upload_zip_doc(
            owner=owner,
            file_path=zip_path,
            filename=job.get("filename", "document"),
            parent_id=parent_id,
            source_job_id=job_id,
        )
        JobManager.update_fields(job_id, {"imported_doc_id": doc["id"]})
        JobManager.update_progress(
            job_id,
            100,
            100,
            f"문서함 '{folder_name}' 폴더에 자동 저장됨 → 뷰어에서 바로 열 수 있습니다.",
        )
    except Exception as e:
        print(f"[AUTO IMPORT] failed for {job_id}: {e}")
        JobManager.update_progress(
            job_id,
            100,
            100,
            f"문서함 자동 저장 실패: {e} (대시보드에서 'Viewer로 보내기'로 수동 저장 가능)",
        )


def process_file_task(job_id: str, file_path: str):
    """
    Celery나 BackgroundTasks에서 호출되는 진입점
    """
    job = JobManager.get_job(job_id)
    if not job:
        return

    owner = job.get("owner")
    user_settings = AuthManager.get_user_settings(owner)

    try:
        model_config = get_target_model(user_settings)
    except Exception as e:
        JobManager.mark_failed(job_id, f"설정 오류: {str(e)}")
        return

    # 모델 타입에 따라 Lock 사용 여부 결정
    if model_config["provider"] == "local":
        print(f"[Queue] Job {job_id} is waiting for GPU lock...")
        with local_gpu_lock:
            _process_job_internal(job_id, file_path, model_config, owner)
    else:
        mode = "Batch" if model_config.get("use_batch") else "API"
        print(f"[Queue] Job {job_id} is starting immediately ({mode} Mode).")
        _process_job_internal(job_id, file_path, model_config, owner)
