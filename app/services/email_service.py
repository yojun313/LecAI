# app/services/email_service.py

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from app.core.config import settings


def send_verification_email(receiver: str, code: str):
    sender = settings.MAIL_SENDER
    password = settings.MAIL_PASSWORD

    title = "[LecAI] 회원가입 인증 코드"
    text = f"아래 인증 코드를 입력하여 회원가입을 완료해주세요.\n\n인증 코드: {code}."

    msg = MIMEMultipart()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(text, "plain"))

    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, receiver, msg.as_string())
        return True
    except Exception as e:
        print(f"[ERROR] 메일 발송 실패: {e}")
        return False


def _send(receiver: str, title: str, text: str) -> bool:
    msg = MIMEMultipart()
    msg["Subject"] = title
    msg["From"] = settings.MAIL_SENDER
    msg["To"] = receiver
    msg.attach(MIMEText(text, "plain"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(settings.MAIL_SENDER, settings.MAIL_PASSWORD)
            server.sendmail(settings.MAIL_SENDER, receiver, msg.as_string())
        return True
    except Exception as e:
        print(f"[ERROR] 메일 발송 실패: {e}")
        return False


def send_password_reset_email(receiver: str, code: str, usernames: list) -> bool:
    ids = ", ".join(usernames)
    text = (
        "LecAI 비밀번호 재설정 요청을 받았습니다.\n\n"
        f"인증 코드: {code}\n"
        f"계정 아이디: {ids}\n\n"
        "이 코드는 10분 동안만 유효합니다.\n"
        "본인이 요청하지 않았다면 이 메일을 무시하세요. 비밀번호는 바뀌지 않습니다."
    )
    return _send(receiver, "[LecAI] 비밀번호 재설정 인증 코드", text)
