import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    CUSTOM_BASE_URL = os.getenv("PPT_LLM_URL", "").rstrip("/")
    CUSTOM_TOKEN = os.getenv("CUSTOM_TOKEN")
    AUDIO_LLM_URL = os.getenv("AUDIO_LLM_URL", "")
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

    MAIL_SENDER = os.getenv("MAIL_SENDER", "")


settings = Settings()

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.RESULT_DIR, exist_ok=True)
