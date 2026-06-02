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
