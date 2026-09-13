import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    CUSTOM_BASE_URL = os.getenv("PPT_LLM_URL", "").rstrip("/")
    CUSTOM_TOKEN = os.getenv("CUSTOM_TOKEN")
    # ---- 음성 인식(STT) ----
    # STT_PROVIDER: "custom" = 매니저 서버(/api/analysis/whisper, GPU Whisper) | "openai" = OpenAI 공식 API
    STT_PROVIDER = os.getenv("STT_PROVIDER", "custom").strip().lower()
    AUDIO_LLM_URL = os.getenv("AUDIO_LLM_URL", "")
    # 매니저 서버 토큰 (매니저 앱 /token 값). 없으면 CUSTOM_TOKEN 사용
    AUDIO_LLM_TOKEN = os.getenv("AUDIO_LLM_TOKEN") or os.getenv("CUSTOM_TOKEN")
    # 매니저 진행상황 서버 (예: https://manager.knpu.re.kr/progress). 비우면 진행 메시지 구독 생략
    AUDIO_PROGRESS_URL = os.getenv("AUDIO_PROGRESS_URL", "").rstrip("/")
    # OpenAI STT 모델 (gpt-transcribe / gpt-4o-mini-transcribe / gpt-4o-transcribe / whisper-1)
    OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-transcribe")
    # 서버 공용 OpenAI 키 (비우면 사용자가 설정에 등록한 키 사용)
    OPENAI_STT_API_KEY = os.getenv("OPENAI_STT_API_KEY", "")
    SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-change-me")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")

    BASE_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
    RESULT_DIR = os.path.join(BASE_DIR, "static", "results")
    DOCS_STATIC_DIR = os.path.join(
        BASE_DIR, "static", "docs"
    )  # 압축 해제된 파일 저장소

    BASE_URL = "http://localhost:8000"

    # 신규 사용자 / 설정 누락 시 기본 분석 모델 (OpenAI 공식 문서 기준, 2026-09 확인)
    DEFAULT_MODEL = "gpt-5.6-luna"

    MAIL_SENDER = os.getenv("MAIL_SENDER", "")

    # 결과물 PDF(result.pdf) 생성 여부. 기본 꺼짐. (.env: GENERATE_PDF=true)
    GENERATE_PDF = os.getenv("GENERATE_PDF", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


settings = Settings()

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.RESULT_DIR, exist_ok=True)
