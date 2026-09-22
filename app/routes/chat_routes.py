# app/routes/chat_routes.py
"""
슬라이드별 AI 질문 (스트리밍).
문맥: 문서 목차(전체 슬라이드 한 줄 요약) + 해당 슬라이드 설명(녹음 발췌·판서 절 포함) + 슬라이드 이미지.
대화 기록은 클라이언트가 보관해 함께 보낸다(서버 무상태). 이미지·문맥은 고정 prefix 라 프롬프트 캐싱이 적용된다.
"""

import os
import json
import requests
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.core.config import settings
from app.db import docs_col
from app.routes.deps import get_current_user
from app.services.auth_manager import AuthManager
from app.services import result_store as rs
from app.services.processor import (
    get_target_model,
    apply_request_options,
    image_to_data_url,
    build_outline,
    parse_usage,
    calculate_total_cost,
    is_reasoning_model,
)

router = APIRouter()

MAX_HISTORY = 20  # 서버로 보내는 최근 대화 수 (질문+답변)
MAX_MESSAGE_CHARS = 4000

CHAT_SYSTEM_PROMPT = """당신은 대학 전공 강의를 함께 공부하는 친절하고 정확한 AI 튜터입니다.
학생이 지금 보고 있는 강의 슬라이드 한 장(이미지)과 그 슬라이드의 설명, 그리고 강의 자료 전체 목차가 주어집니다.
학생의 질문에 이 슬라이드를 기준으로 답하되, 필요하면 목차의 앞뒤 슬라이드 내용을 참고해 흐름을 설명하십시오.

[답변 규칙]
- 한국어로, 핵심부터 간결하게. 질문에 직접 답한 뒤 필요한 만큼만 부연할 것.
- 수식·기호는 반드시 LaTeX($...$, $$...$$)로 쓰고, 처음 나오는 기호는 뜻을 짧게 밝힐 것.
- 슬라이드에 없는 내용을 슬라이드에 있다고 말하지 말 것. 일반 지식으로 보충할 때는 "(보충)"이라고 표시할 것.
- 슬라이드 설명에 강의 녹음 발췌나 칠판 판서가 있으면 강의자가 실제로 말한/적은 내용으로서 우선 참고할 것.
- 학생이 "다시", "더 쉽게", "예를 들어" 등으로 이어 물으면 직전 답변을 기준으로 이어서 답할 것.
- 마크다운(제목, 불릿, 표, 코드 블록)을 적절히 사용할 것."""


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    slide: int


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/chat/doc/{doc_id}")
async def chat_about_slide(
    doc_id: str, req: ChatRequest, user: str = Depends(get_current_user)
):
    target = docs_col.find_one({"id": doc_id, "owner": user, "type": "file"})
    if not target:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="질문을 입력해 주세요.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="질문이 너무 깁니다.")

    user_settings = AuthManager.get_user_settings(user)
    try:
        model_config = get_target_model(user_settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if model_config["provider"] != "openai":
        raise HTTPException(
            status_code=400, detail="AI 질문은 OpenAI 모델에서만 사용할 수 있습니다."
        )

    doc_dir = os.path.join(settings.DOCS_STATIC_DIR, user, doc_id)
    rs.ensure_desc_layout(doc_dir)
    slides = rs.read_slides(doc_dir)
    if req.slide not in slides:
        raise HTTPException(status_code=404, detail="해당 슬라이드를 찾을 수 없습니다.")
    slide_text = slides[req.slide]
    total = len(slides)
    outline = build_outline(slides)
    img_name = rs.image_for_slide(doc_dir, req.slide)
    img_path = os.path.join(doc_dir, rs.IMAGES_DIR, img_name) if img_name else None

    # ---- 메시지 구성: 고정 prefix(system + 문맥 + 이미지) → 대화 기록 → 새 질문 ----
    context_text = (
        f"[강의 자료 전체 목차: {total}장]\n{outline}\n\n"
        f"[현재 슬라이드: {req.slide} / {total}]\n[슬라이드 설명]\n{slide_text}"
    )
    first_user_parts = [
        {
            "type": "text",
            "text": "지금 보고 있는 슬라이드 이미지입니다. 이 슬라이드에 대해 질문하겠습니다.",
        }
    ]
    if img_path and os.path.exists(img_path):
        first_user_parts.append(
            {"type": "image_url", "image_url": {"url": image_to_data_url(img_path)}}
        )
    messages = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "system", "content": context_text},
        {"role": "user", "content": first_user_parts},
        {
            "role": "assistant",
            "content": "네, 이 슬라이드의 이미지와 설명을 확인했습니다. 무엇이든 질문해 주세요.",
        },
    ]
    for m in req.history[-MAX_HISTORY:]:
        if m.content and m.content.strip():
            messages.append({"role": m.role, "content": m.content[:MAX_MESSAGE_CHARS]})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": model_config["model_id"],
        "messages": messages,
        "max_completion_tokens": 4000,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    apply_request_options(payload, model_config)
    # 같은 문서·슬라이드의 연속 질문이 같은 캐시로 가도록 키를 구체화
    payload["prompt_cache_key"] = f"lecai-chat-{doc_id}-{req.slide}"
    if is_reasoning_model(model_config["model_id"]):
        payload["reasoning_effort"] = "low"

    url = f"{model_config['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model_config['api_key']}",
        "Content-Type": "application/json",
    }

    def stream():
        try:
            with requests.post(
                url, headers=headers, json=payload, stream=True, timeout=(30, 600)
            ) as resp:
                if resp.status_code != 200:
                    detail = resp.text[:300]
                    if resp.status_code == 400 and "reasoning_effort" in detail:
                        payload.pop("reasoning_effort", None)
                    yield _sse({"error": f"OpenAI 오류 {resp.status_code}: {detail}"})
                    return
                usage = None
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []) or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield _sse({"delta": delta})
                info = {}
                if usage:
                    u = parse_usage({"usage": usage})
                    usd, _ = calculate_total_cost(model_config["model_id"], u)
                    if usd:
                        AuthManager.update_user_cumulative_usage(user, usd)
                    info = {
                        "tokens": u["prompt"] + u["completion"],
                        "cached": u["cached"],
                        "cost_usd": usd,
                    }
                yield _sse({"done": True, "model": model_config["model_id"], **info})
        except requests.exceptions.RequestException as e:
            yield _sse({"error": f"연결 오류: {e}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
