"""Minimal email sender for verification links.

If SMTP is not configured, nothing is sent and nothing personal is logged: the address and
the link (it carries a live verification token) never reach a log record (T11.2.15, R6-5).
The link never travels any other way either -- not even to the UI in dev (T12.1.11, DA-G2-5
taken further): locally, compose points SMTP at Mailpit (127.0.0.1-only), so the link is only
ever visible in Mailpit's own inbox.
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
    """Best-effort: a transient SMTP failure (e.g. Mailpit's container not accepting
    connections yet right after `docker compose up`, T12.1.11 -- its `depends_on` only waits
    for "started", not healthy, since Mailpit has no healthcheck) never turns into a 500 on
    register/resend-verification. The address and the exception text never share a log
    record (T11.2.15, R6-5) -- only the exception's type name is logged, never str(exc),
    which could otherwise echo the recipient back (some SMTP errors include it)."""
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning(
            "SMTP not configured: verification email not sent"
            " (no SMTP configured -- set SMTP_HOST, e.g. Mailpit locally)"
        )
        return
    link = verification_link(token)
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message["Subject"] = "Verify your SecRAG account"
    message.set_content(f"Verify your account: {link}")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_user:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except OSError as exc:
        logger.warning("verification email not sent: SMTP error (%s)", type(exc).__name__)
