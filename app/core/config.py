import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    CUSTOM_BASE_URL = os.getenv("PPT_LLM_URL", "").rstrip("/")
    CUSTOM_TOKEN = os.getenv("CUSTOM_TOKEN")
    # ---- 음성 인식(STT) ----
    # 제공자는 사용자가 설정 화면에서 고른다 (custom | openai). 아직 고르지 않은 사용자의 기본값:
    STT_DEFAULT_PROVIDER = os.getenv("STT_DEFAULT_PROVIDER", "custom").strip().lower()
    # custom: 매니저 서버 GPU Whisper (무료). URL/토큰/진행상황 서버는 서버 .env 에서 관리
    AUDIO_LLM_URL = os.getenv("AUDIO_LLM_URL", "")
    # 실시간(NDJSON 스트림) 엔드포인트. 비우면 AUDIO_LLM_URL + "/stream" 을 먼저 시도하고, 없으면(404/405) 일괄 방식으로 자동 전환
    AUDIO_LLM_STREAM_URL = os.getenv("AUDIO_LLM_STREAM_URL", "").strip()
    AUDIO_LLM_TOKEN = os.getenv("AUDIO_LLM_TOKEN") or os.getenv(
        "CUSTOM_TOKEN"
    )  # 매니저 앱 /token 값
    AUDIO_PROGRESS_URL = os.getenv("AUDIO_PROGRESS_URL", "").rstrip(
        "/"
    )  # 예: https://manager.knpu.re.kr/progress
    # openai: 각 사용자의 OpenAI API Key 사용. 모델: gpt-transcribe(기본, $0.0045/분) 등
    OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-transcribe")
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
    # Whisper 노트의 원본 음성 저장소 — static/ 밖에 둔다 (인증 없이 공개되면 안 됨)
    WHISPER_DIR = os.path.join(BASE_DIR, "data", "whisper")

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
os.makedirs(settings.WHISPER_DIR, exist_ok=True)
