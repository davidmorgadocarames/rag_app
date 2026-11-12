"""One SMTP implementation (T12.4.7) for verification/already-registered/reset links.

If SMTP is not configured, nothing is sent and nothing personal is logged: the address and
any link (it may carry a live token) never reach a log record (T11.2.15, R6-5). A link never
travels any other way either -- not even to the UI in dev (T12.1.11, DA-G2-5 taken further):
locally, compose points SMTP at Mailpit (127.0.0.1-only), so it is only ever visible in
Mailpit's own inbox. ACS SMTP on Azure (12b-P): the same ``SMTP_HOST``/``SMTP_PORT``/
``SMTP_USER``/``SMTP_PASSWORD`` settings point at the Azure Communication Services SMTP
endpoint instead -- no code path changes, only the configured values.

T12.4.5/T12.4.6: every link points at the FRONTEND's own page (``PUBLIC_APP_URL``) --
``/verify`` and ``/reset`` -- never the backend directly. A GET only renders the page; the
page's own button click issues the POST that actually consumes the token, so a mail scanner
pre-fetching the GET link can never consume it.

T12.4.7: ``EMAIL_SENT_TOTAL``/``EMAIL_FAILED_TOTAL`` (``rag_app.metrics``) count every send
attempt by ``kind`` -- never by recipient (no PII label).
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from rag_app import metrics
from rag_app.config import get_settings

logger = logging.getLogger("rag_app.emailer")


def verification_link(token: str) -> str:
    base = get_settings().public_app_url.rstrip("/")
    return f"{base}/verify?token={token}"


def password_reset_link(token: str) -> str:
    base = get_settings().public_app_url.rstrip("/")
    return f"{base}/reset?token={token}"


def _send(to_email: str, subject: str, body: str, *, kind: str) -> None:
    """Shared best-effort send (T12.1.11): a transient SMTP failure (e.g. Mailpit's
    container not accepting connections yet right after `docker compose up` -- its
    `depends_on` only waits for "started", not healthy, since Mailpit has no healthcheck)
    never turns into a 500 on register/resend-verification/forgot-password/login. The
    address and the exception text never share a log record (T11.2.15, R6-5) -- only the
    exception's type name is logged, never str(exc), which could otherwise echo the
    recipient back (some SMTP errors include it). ``kind`` is a small fixed vocabulary (the
    EMAIL_SENT_TOTAL/EMAIL_FAILED_TOTAL label, T12.4.7) -- never the address itself."""
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning(
            "SMTP not configured: email not sent"
            " (no SMTP configured -- set SMTP_HOST, e.g. Mailpit locally)"
        )
        metrics.EMAIL_FAILED_TOTAL.labels(kind=kind).inc()
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
        metrics.EMAIL_FAILED_TOTAL.labels(kind=kind).inc()
        return
    metrics.EMAIL_SENT_TOTAL.labels(kind=kind).inc()


def send_verification_email(to_email: str, token: str) -> None:
    link = verification_link(token)
    _send(
        to_email,
        "Verify your SecRAG account",
        f"Verify your account: {link}",
        kind="verification",
    )


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
        kind="already_registered",
    )


def send_password_reset_email(to_email: str, token: str) -> None:
    """T12.4.6: the real reset link, to the real account owner only."""
    link = password_reset_link(token)
    _send(
        to_email,
        "Reset your SecRAG password",
        f"Reset your password: {link} (expires in 30 minutes)",
        kind="password_reset",
    )


def send_password_reset_unknown_account_email(to_email: str) -> None:
    """T12.4.6: the enumeration-safe forgot-password contract sends THIS instead of the
    real reset link when ``to_email`` has no (live, verified) account -- the CALLER always
    gets the SAME generic response either way (``GENERIC_FORGOT_PASSWORD_MESSAGE``); only
    whoever actually owns the address's inbox learns anything, and what they learn is
    harmless (no account exists here, or it is unverified). Carries no token/link."""
    _send(
        to_email,
        "SecRAG password reset requested",
        "Someone requested a password reset for this email address, but no SecRAG account"
        " uses it. If this was you, you may not have an account yet -- try registering"
        " instead. If it wasn't you, no action is needed.",
        kind="password_reset_unknown",
    )
