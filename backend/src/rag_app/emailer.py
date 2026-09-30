"""Minimal email sender for verification links.

If SMTP is not configured (dev), nothing is sent and nothing personal is logged: the
address and the link (it carries a live verification token) never reach a log record
(T11.2.15, R6-5). In dev the UI gets the link from /auth/resend-verification.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from rag_app.config import get_settings

logger = logging.getLogger("rag_app.emailer")


def verification_link(token: str) -> str:
    base = get_settings().next_public_api_url.rstrip("/")
    return f"{base}/auth/verify?token={token}"


def send_verification_email(to_email: str, token: str) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning(
            "SMTP not configured: verification email not sent"
            " (dev: the link is returned by /auth/resend-verification)"
        )
        return
    link = verification_link(token)
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message["Subject"] = "Verify your SecRAG account"
    message.set_content(f"Verify your account: {link}")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_user:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)
