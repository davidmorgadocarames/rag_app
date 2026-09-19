"""Minimal email sender for verification links.

If SMTP is not configured (dev), the verification link is logged instead of sent, so
the flow is usable end-to-end without a mail server.
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
    link = verification_link(token)
    if not settings.smtp_host:
        logger.warning("SMTP not configured; verification link for %s: %s", to_email, link)
        return
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
