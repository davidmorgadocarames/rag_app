"""Minimal email sender for verification/already-registered/reset links.

If SMTP is not configured, nothing is sent and nothing personal is logged: the address and
any link (it may carry a live token) never reach a log record (T11.2.15, R6-5). A link never
travels any other way either -- not even to the UI in dev (T12.1.11, DA-G2-5 taken further):
locally, compose points SMTP at Mailpit (127.0.0.1-only), so it is only ever visible in
Mailpit's own inbox.

T12.4.5: every link points at the FRONTEND's own page (``PUBLIC_APP_URL``) -- ``/verify`` and
``/reset`` -- never the backend directly. A GET only renders the page; the page's own button
click issues the POST that actually consumes the token, so a mail scanner pre-fetching the
GET link can never consume it.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from rag_app.config import get_settings

logger = logging.getLogger("rag_app.emailer")


def verification_link(token: str) -> str:
    base = get_settings().public_app_url.rstrip("/")
    return f"{base}/verify?token={token}"


def _send(to_email: str, subject: str, body: str) -> None:
    """Shared best-effort send (T12.1.11): a transient SMTP failure (e.g. Mailpit's
    container not accepting connections yet right after `docker compose up` -- its
    `depends_on` only waits for "started", not healthy, since Mailpit has no healthcheck)
    never turns into a 500 on register/resend-verification/login. The address and the
    exception text never share a log record (T11.2.15, R6-5) -- only the exception's type
    name is logged, never str(exc), which could otherwise echo the recipient back (some SMTP
    errors include it)."""
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning(
            "SMTP not configured: email not sent"
            " (no SMTP configured -- set SMTP_HOST, e.g. Mailpit locally)"
        )
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_user:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except OSError as exc:
        logger.warning("email not sent: SMTP error (%s)", type(exc).__name__)


def send_verification_email(to_email: str, token: str) -> None:
    link = verification_link(token)
    _send(to_email, "Verify your SecRAG account", f"Verify your account: {link}")


def send_already_registered_email(to_email: str) -> None:
    """T12.4.4: the enumeration-safe register contract sends this instead of the normal
    verification email when ``to_email`` already has an account -- the CALLER (the person
    submitting the register form) always gets the SAME generic 202 either way; only the
    real owner of the address, by actually having access to its inbox, learns anything.
    Carries no token/link -- there is nothing to consume."""
    _send(
        to_email,
        "You already have a SecRAG account",
        "Someone tried to register this email address, but you already have an account."
        " If this was you, sign in instead. If it wasn't, no action is needed -- your"
        " account is unaffected.",
    )
