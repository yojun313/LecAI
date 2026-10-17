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


class ResetRequest(BaseModel):
    email: str


class ResetVerify(BaseModel):
    email: str
    code: str


class ResetConfirm(BaseModel):
    email: str
    token: str
    username: str
    password: str


@router.post("/auth/password-reset/request")
async def api_password_reset_request(req: ResetRequest):
    if "@" not in (req.email or ""):
        raise HTTPException(status_code=400, detail="이메일 주소를 확인해 주세요.")
    result = AuthManager.request_password_reset(req.email)
    if result == "cooldown":
        raise HTTPException(
            status_code=429, detail="잠시 후 다시 시도해 주세요. (60초에 한 번 발송)"
        )
    if result == "mail_failed":
        raise HTTPException(
            status_code=500,
            detail="메일 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
        )
    # sent / unknown 은 같은 응답 (이메일 등록 여부 비공개)
    return {"message": "입력한 이메일로 가입된 계정이 있으면 인증 코드를 보냈습니다."}


@router.post("/auth/password-reset/verify")
async def api_password_reset_verify(req: ResetVerify):
    result, error = AuthManager.verify_password_reset_code(req.email, req.code)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return result


@router.post("/auth/password-reset/confirm")
async def api_password_reset_confirm(req: ResetConfirm):
    ok, error = AuthManager.reset_password(
        req.email, req.token, req.username, req.password
    )
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"message": "비밀번호를 변경했습니다. 새 비밀번호로 로그인해 주세요."}
