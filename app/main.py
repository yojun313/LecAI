# main.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.core.config import settings
from app.routes import (
    chat_routes,
    view_routes,
    auth_routes,
    job_routes,
    user_routes,
    doc_routes,
    whisper_routes,
    converter_routes,
)
from app.services.whisper_notes import requeue_interrupted
import os

app = FastAPI(title="LecAI", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """HTML 페이지는 브라우저/프록시가 캐시하지 않도록 한다 (배포 직후에도 항상 최신 화면)"""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
async def on_startup():
    # 서버 재시작으로 끊긴 Whisper 전사 작업을 자동으로 이어서 돌린다
    requeue_interrupted()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join("static", "favicon.ico"))


app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(view_routes.router)
app.include_router(auth_routes.router, prefix="/api", tags=["Auth"])
app.include_router(job_routes.router, prefix="/api", tags=["Jobs"])
app.include_router(user_routes.router, prefix="/api", tags=["User"])
app.include_router(doc_routes.router, prefix="/api", tags=["Docs"])
app.include_router(chat_routes.router, prefix="/api", tags=["Chat"])
app.include_router(whisper_routes.router, prefix="/api", tags=["Whisper"])
app.include_router(converter_routes.router, prefix="/api", tags=["Converter"])
