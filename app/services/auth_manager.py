# app/services/auth_manager.py

import uuid
import hashlib
import bcrypt
import random
from app.services.email_service import (
    send_verification_email,
    send_password_reset_email,
)
import re
import time
import secrets
from app.db import users_col, sessions_col
from app.db.prompt import default_system_prompt, default_user_prompt
from app.core.config import settings

DEFAULT_MODEL = settings.DEFAULT_MODEL

verification_codes = {}

# 비밀번호 재설정: email(lower) → {code_hash, expires, attempts, sent_at, usernames, token, token_expires}
password_resets = {}
RESET_CODE_TTL = 10 * 60
RESET_RESEND_COOLDOWN = 60
RESET_MAX_ATTEMPTS = 5
RESET_TOKEN_TTL = 15 * 60


class AuthManager:
    @staticmethod
    def _pre_hash(password: str) -> bytes:
        """
        bcrypt의 72바이트 제한을 우회하기 위해
        SHA-256으로 먼저 해싱하여 64글자(bytes)로 고정합니다.
        """
        return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("utf-8")

    @staticmethod
    def request_signup(username, password, email):
        # 1. 사용자명 중복 체크 (MongoDB)
        if users_col.find_one({"username": username}):
            return "username_exists"

        # 2. 이메일 중복 체크 (MongoDB)
        if users_col.find_one({"email": email}):
            return "email_exists"

        # 인증 코드 생성
        code = str(random.randint(100000, 999999))

        # 메일 발송
        if send_verification_email(email, code):
            # 임시 저장 (검증용)
            verification_codes[email] = {
                "username": username,
                "password": password,
                "code": code,
            }
            return "success"
        else:
            return "mail_failed"

    # ---------- 비밀번호 재설정 (이메일 인증) ----------
    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def request_password_reset(email: str) -> str:
        """
        반환: "sent" | "cooldown" | "unknown" | "mail_failed"
        (라우트는 sent/unknown 을 같은 응답으로 돌려 이메일 등록 여부를 드러내지 않는다)
        """
        email = (email or "").strip()
        key = email.lower()
        if not key:
            return "unknown"
        now = time.time()
        prev = password_resets.get(key)
        if prev and now - prev.get("sent_at", 0) < RESET_RESEND_COOLDOWN:
            return "cooldown"
        users = list(
            users_col.find(
                {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
                {"username": 1},
            )
        )
        if not users:
            # 등록되지 않은 이메일도 같은 쿨다운을 적용해 응답 패턴으로 구분할 수 없게
            password_resets[key] = {"sent_at": now, "code_hash": None, "expires": now}
            return "unknown"
        code = f"{secrets.randbelow(1000000):06d}"
        usernames = [u["username"] for u in users]
        if not send_password_reset_email(email, code, usernames):
            return "mail_failed"
        password_resets[key] = {
            "code_hash": AuthManager._hash_code(code),
            "expires": now + RESET_CODE_TTL,
            "attempts": 0,
            "sent_at": now,
            "usernames": usernames,
        }
        return "sent"

    @staticmethod
    def verify_password_reset_code(email: str, code: str):
        """코드가 맞으면 재설정 토큰과 아이디 목록을 돌려준다. 실패 시 (None, 사유)"""
        key = (email or "").strip().lower()
        data = password_resets.get(key)
        now = time.time()
        if not data or not data.get("code_hash") or now > data["expires"]:
            return (
                None,
                "인증 코드가 만료되었거나 요청 내역이 없습니다. 코드를 다시 받아 주세요.",
            )
        if data["attempts"] >= RESET_MAX_ATTEMPTS:
            return None, "시도 횟수를 초과했습니다. 코드를 다시 받아 주세요."
        data["attempts"] += 1
        if not secrets.compare_digest(
            data["code_hash"], AuthManager._hash_code((code or "").strip())
        ):
            left = RESET_MAX_ATTEMPTS - data["attempts"]
            return None, f"인증 코드가 올바르지 않습니다. (남은 시도 {left}회)"
        token = secrets.token_urlsafe(32)
        data["token"] = token
        data["token_expires"] = now + RESET_TOKEN_TTL
        data["code_hash"] = None  # 코드는 한 번만 사용
        return {"token": token, "usernames": data["usernames"]}, None

    @staticmethod
    def reset_password(email: str, token: str, username: str, new_password: str):
        key = (email or "").strip().lower()
        data = password_resets.get(key)
        if (
            not data
            or not data.get("token")
            or time.time() > data.get("token_expires", 0)
        ):
            return False, "인증이 만료되었습니다. 처음부터 다시 진행해 주세요."
        if not secrets.compare_digest(data["token"], token or ""):
            return False, "잘못된 요청입니다."
        if username not in data.get("usernames", []):
            return False, "이 이메일에 연결된 계정이 아닙니다."
        if len(new_password or "") < 8:
            return False, "비밀번호는 8자 이상이어야 합니다."
        hashed = bcrypt.hashpw(
            AuthManager._pre_hash(new_password), bcrypt.gensalt()
        ).decode("utf-8")
        users_col.update_one({"username": username}, {"$set": {"password": hashed}})
        sessions_col.delete_many({"username": username})  # 모든 기기 로그아웃
        password_resets.pop(key, None)
        return True, None

    @staticmethod
    def verify_and_create_user(email, code):
        data = verification_codes.get(email)
        if not data:
            return False  # 요청 내역 없음

        if data["code"] != code:
            return False  # 코드 불일치

        # 검증 완료 -> 실제 계정 생성 준비
        username = data["username"]
        password = data["password"]

        # 비밀번호 해싱
        safe_pw = AuthManager._pre_hash(password)
        hashed_pw = bcrypt.hashpw(safe_pw, bcrypt.gensalt()).decode("utf-8")

        # MongoDB에 저장할 문서 구조
        new_user = {
            "username": username,
            "password": hashed_pw,
            "email": email,
            "verified": True,
            "openai_api_key": "",
            "preferred_model": DEFAULT_MODEL,
            "use_batch_api": False,
            "audio_language": "auto",
            "audio_model_level": 2,
        }

        # DB 저장
        users_col.insert_one(new_user)

        # 임시 데이터 삭제
        del verification_codes[email]
        return True

    @staticmethod
    def create_user(username, password):
        if users_col.find_one({"username": username}):
            return False

        safe_pw = AuthManager._pre_hash(password)
        hashed_pw = bcrypt.hashpw(safe_pw, bcrypt.gensalt()).decode("utf-8")

        new_user = {
            "username": username,
            "password": hashed_pw,
            "email": "",
            "verified": False,
            "openai_api_key": "",
            "preferred_model": DEFAULT_MODEL,
            "use_batch_api": False,
            "audio_language": "auto",
            "audio_model_level": 2,
        }

        users_col.insert_one(new_user)
        return True

    @staticmethod
    def authenticate_user(username, password):
        # DB에서 유저 조회
        user = users_col.find_one({"username": username})
        if not user:
            return None

        stored_hash = user["password"].encode("utf-8")
        safe_pw = AuthManager._pre_hash(password)

        # 비밀번호 검증
        if bcrypt.checkpw(safe_pw, stored_hash):
            session_id = str(uuid.uuid4())

            # 세션 DB에 저장
            sessions_col.insert_one({"session_id": session_id, "username": username})
            return session_id
        return None

    @staticmethod
    def get_user_by_session(session_id):
        # DB에서 세션 조회
        session = sessions_col.find_one({"session_id": session_id})
        if session:
            return session["username"]
        return None

    @staticmethod
    def logout(session_id):
        # DB에서 세션 삭제
        sessions_col.delete_one({"session_id": session_id})

    @staticmethod
    def update_user_settings(
        username,
        api_key,
        model_choice,
        audio_lang="auto",
        audio_model=2,
        custom_prompt=None,
        custom_user_prompt=None,
        profile_url=None,
        use_batch_api=False,
        stt_provider=None,
    ):
        update_data = {
            "openai_api_key": api_key,
            "preferred_model": model_choice,
            "audio_language": audio_lang,
            "audio_model_level": int(audio_model),
            "use_batch_api": bool(use_batch_api),
        }
        if stt_provider in ("custom", "openai"):
            update_data["stt_provider"] = stt_provider

        if custom_prompt is not None:
            update_data["custom_prompt"] = custom_prompt

        if custom_user_prompt is not None:
            update_data["custom_user_prompt"] = custom_user_prompt

        if profile_url:
            update_data["profile_img"] = profile_url

        result = users_col.update_one({"username": username}, {"$set": update_data})
        return result.matched_count > 0

    @staticmethod
    def reset_prompts_to_default(known_defaults=()):
        """
        모든 사용자의 custom_prompt / custom_user_prompt 를 제거하여 현재 기본 프롬프트
        (app/db/prompt.py)를 사용하도록 한다. 필드를 지워 두면 이후 기본값이 바뀔 때도
        자동으로 따라간다.

        known_defaults: 과거 기본 프롬프트 문자열 목록. 저장된 값이 이 목록(또는 현재 기본값)과
        일치하면 그대로 제거하고, 사용자가 직접 수정한 프롬프트라면 custom_prompt_prev /
        custom_user_prompt_prev 에 백업한 뒤 제거한다.
        반환값: {"reset": [...], "backed_up": [...], "untouched": [...]}
        """
        import re

        def norm(text):
            return re.sub(r"[\s*`]+", "", text or "")

        defaults = {norm(default_system_prompt), norm(default_user_prompt)}
        defaults.update(norm(d) for d in known_defaults)

        report = {"reset": [], "backed_up": [], "untouched": []}
        for user in users_col.find(
            {}, {"username": 1, "custom_prompt": 1, "custom_user_prompt": 1}
        ):
            unset, backup = {}, {}
            for field in ("custom_prompt", "custom_user_prompt"):
                if field not in user:
                    continue
                value = user[field]
                unset[field] = ""
                if value and value.strip() and norm(value) not in defaults:
                    backup[f"{field}_prev"] = value

            if not unset:
                report["untouched"].append(user["username"])
                continue

            update = {"$unset": unset}
            if backup:
                update["$set"] = backup
            users_col.update_one({"_id": user["_id"]}, update)
            (report["backed_up"] if backup else report["reset"]).append(
                user["username"]
            )

        return report

    @staticmethod
    def set_hide_api_key_notice(username, hide: bool):
        result = users_col.update_one(
            {"username": username}, {"$set": {"hide_api_key_notice": bool(hide)}}
        )
        return result.matched_count > 0

    @staticmethod
    def update_preferred_model(username, model_choice):
        result = users_col.update_one(
            {"username": username}, {"$set": {"preferred_model": model_choice}}
        )
        return result.matched_count > 0

    @staticmethod
    def get_user_settings(username):
        user = users_col.find_one({"username": username})

        if user:
            return {
                "openai_api_key": user.get("openai_api_key", ""),
                "preferred_model": user.get("preferred_model", DEFAULT_MODEL),
                "use_batch_api": bool(user.get("use_batch_api", False)),
                "stt_provider": user.get("stt_provider") or "",
                "hide_api_key_notice": bool(user.get("hide_api_key_notice", False)),
                "audio_language": user.get("audio_language", "auto"),
                "audio_model_level": user.get("audio_model_level", 2),
                "custom_prompt": user.get("custom_prompt", default_system_prompt),
                "custom_user_prompt": user.get(
                    "custom_user_prompt", default_user_prompt
                ),
                "profile_img": user.get("profile_img", "/static/default_avatar.png"),
            }

        return {
            "openai_api_key": "",
            "preferred_model": DEFAULT_MODEL,
            "use_batch_api": False,
            "stt_provider": "",
            "hide_api_key_notice": False,
            "audio_language": "auto",
            "audio_model_level": 2,
            "custom_prompt": default_system_prompt,
            "custom_user_prompt": default_user_prompt,
        }

    @staticmethod
    def update_user_cumulative_usage(username: str, cost_usd: float):
        users_col.update_one(
            {"username": username}, {"$inc": {"total_spent_usd": cost_usd}}
        )

    @staticmethod
    def get_user_usage(username: str):
        user = users_col.find_one({"username": username}, {"total_spent_usd": 1})
        if user and "total_spent_usd" in user:
            return user["total_spent_usd"]
        return 0.0
