# app/routes/deps.py
from fastapi import Request, HTTPException
from app.services.auth_manager import AuthManager


def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    user = AuthManager.get_user_by_session(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user
