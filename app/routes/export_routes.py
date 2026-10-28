"""뷰어 내보내기 PDF (모아찍기: 한 장에 2 · 4쪽). 한 장에 1쪽은 브라우저 인쇄 창으로 저장한다."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.routes.deps import get_current_user
from app.services import export_service
from app.services.converter_service import ConverterError

router = APIRouter(prefix="/export")

NO_STORE = {"Cache-Control": "private, no-store"}
LAYOUT = r"^(1|2|4(z|s|n|rn))$"


class ExportRequest(BaseModel):
    html: str = Field(..., min_length=1, max_length=export_service.MAX_HTML)


def _not_found():
    return HTTPException(
        status_code=404, detail="PDF 가 만료됐어요. 다시 만들어 주세요."
    )


@router.post("/pdf")
async def render(body: ExportRequest, user: str = Depends(get_current_user)):
    try:
        return await run_in_threadpool(export_service.render, user, body.html)
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/pdf/{token}/layout")
async def layout(
    token: str,
    layout: str = Query("1", pattern=LAYOUT),
    user: str = Depends(get_current_user),
):
    try:
        return await run_in_threadpool(export_service.layout_info, user, token, layout)
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/pdf/{token}/page/{number}")
async def page(
    token: str,
    number: int,
    w: int = Query(900, ge=200, le=2000),
    layout: str = Query("1", pattern=LAYOUT),
    user: str = Depends(get_current_user),
):
    try:
        path = await run_in_threadpool(
            export_service.page_png, user, token, number, w, layout
        )
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FileResponse(path, media_type="image/png", headers=NO_STORE)


@router.get("/pdf/{token}/download")
async def download(
    token: str,
    layout: str = Query("1", pattern=LAYOUT),
    name: str = Query("export.pdf", max_length=200),
    user: str = Depends(get_current_user),
):
    try:
        path = await run_in_threadpool(export_service.layout_pdf, user, token, layout)
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    safe = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in name).strip()
    safe = safe or "export.pdf"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=safe,
        content_disposition_type="attachment",
        headers={**NO_STORE, "X-Content-Type-Options": "nosniff"},
    )
