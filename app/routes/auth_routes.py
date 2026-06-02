# app/routes/auth_routes.py
from fastapi import APIRouter, HTTPException, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.services.auth_manager import AuthManager

router = APIRouter()


class VerifyRequest(BaseModel):
    email: str
    code: str


@router.post("/auth/signup/request")
async def api_signup_request(
    username: str = Form(...), password: str = Form(...), email: str = Form(...)
):
    result = AuthManager.request_signup(username, password, email)
    if result == "success":
        return {"message": "Verification email sent"}
    elif result == "username_exists":
        raise HTTPException(status_code=400, detail="Username already exists")
    elif result == "email_exists":
        raise HTTPException(status_code=400, detail="Email already exists")
    else:
        raise HTTPException(status_code=500, detail="Failed to send email")


@router.post("/auth/signup/verify")
async def api_signup_verify(req: VerifyRequest):
    if AuthManager.verify_and_create_user(req.email, req.code):
        return {"message": "User created successfully"}
    raise HTTPException(status_code=400, detail="Invalid verification code")


@router.post("/auth/signup")
async def api_signup(username: str = Form(...), password: str = Form(...)):
    if AuthManager.create_user(username, password):
        return {"message": "User created"}
    raise HTTPException(status_code=400, detail="User already exists")


@router.post("/auth/login")
async def api_login(username: str = Form(...), password: str = Form(...)):
    session_id = AuthManager.authenticate_user(username, password)
    if session_id:
        response = JSONResponse(content={"message": "Login successful"})

        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            max_age=60 * 60 * 24 * 7,  # 7일간 유지
            expires=60 * 60 * 24 * 7,
        )
        return response
    raise HTTPException(status_code=401, detail="Invalid credentials")
