# app/routes/converter_routes.py
# Converter API — 여러 형식의 파일을 PDF 로. 결과는 본인 것만 보고 받을 수 있다 (사용자별 폴더).

import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.routes.deps import get_current_user
from app.services import converter_service as converter
from app.services.converter_service import ConverterError

router = APIRouter(prefix="/converter")

NO_STORE = {"Cache-Control": "private, no-store"}


def _not_found():
    return HTTPException(
        status_code=404,
        detail="변환 결과를 찾을 수 없어요 (지워졌거나 기간이 지났어요).",
    )


class RenderOptions(BaseModel):
    zoom: float = Field(1.0, ge=0.4, le=2.0)
    landscape: bool = False


class BundleRequest(BaseModel):
    tokens: list[str] = Field(..., min_length=1, max_length=100)
    mode: str = Field("merge", pattern="^(merge|zip)$")


@router.get("/formats")
async def formats(user: str = Depends(get_current_user)):
    return {"formats": converter.formats(), "max_bytes": converter.MAX_UPLOAD_BYTES}


@router.get("/jobs")
async def list_jobs(user: str = Depends(get_current_user)):
    return {"jobs": await run_in_threadpool(converter.list_jobs, user)}


@router.post("/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    zoom: float = Form(1.0),
    landscape: bool = Form(False),
    user: str = Depends(get_current_user),
):
    length = request.headers.get("content-length")
    if (
        length
        and length.isdigit()
        and int(length) > converter.MAX_UPLOAD_BYTES + 1024 * 1024
    ):
        raise HTTPException(status_code=413, detail="파일이 너무 커요 (최대 100MB).")
    try:
        job, meta = await run_in_threadpool(
            converter.start_job, user, file.filename or ""
        )
    except ConverterError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        size = 0
        with open(job / meta["source"], "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > converter.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail="파일이 너무 커요 (최대 100MB)."
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="빈 파일이에요.")
    except BaseException:
        converter.discard(job)
        raise
    finally:
        await file.close()
    try:
        return await run_in_threadpool(converter.finish_job, job, meta, zoom, landscape)
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/jobs/{token}/render")
async def rerender(
    token: str, body: RenderOptions, user: str = Depends(get_current_user)
):
    try:
        return await run_in_threadpool(
            converter.rerender, user, token, body.zoom, body.landscape
        )
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/jobs/{token}/page/{number}")
async def page(
    token: str, number: int, w: int = 900, user: str = Depends(get_current_user)
):
    try:
        path = await run_in_threadpool(converter.page_png, user, token, number, w)
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FileResponse(path, media_type="image/png", headers=NO_STORE)


@router.get("/jobs/{token}/pdf")
async def download(token: str, user: str = Depends(get_current_user)):
    try:
        path, name = converter.pdf_path(user, token)
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=name,
        content_disposition_type="attachment",
        headers={**NO_STORE, "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/jobs/{token}")
async def delete(token: str, user: str = Depends(get_current_user)):
    try:
        await run_in_threadpool(converter.delete, user, token)
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True}


@router.post("/bundle")
async def bundle(body: BundleRequest, user: str = Depends(get_current_user)):
    try:
        path, name = await run_in_threadpool(
            converter.bundle, user, body.tokens, body.mode
        )
    except FileNotFoundError as error:
        raise _not_found() from error
    except ConverterError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    cleanup = BackgroundTask(lambda: (os.remove(path), os.rmdir(path.parent)))
    return FileResponse(
        path,
        media_type="application/zip" if body.mode == "zip" else "application/pdf",
        filename=name,
        content_disposition_type="attachment",
        headers={**NO_STORE, "X-Content-Type-Options": "nosniff"},
        background=cleanup,
    )
