# main.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.core.config import settings
from app.routes import view_routes, auth_routes, job_routes, user_routes, doc_routes
import os

app = FastAPI(title="LecAI")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join("static", "favicon.ico"))


app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(view_routes.router)
app.include_router(auth_routes.router, prefix="/api", tags=["Auth"])
app.include_router(job_routes.router, prefix="/api", tags=["Jobs"])
app.include_router(user_routes.router, prefix="/api", tags=["User"])
app.include_router(doc_routes.router, prefix="/api", tags=["Docs"])
