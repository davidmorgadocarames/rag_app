"""Log hygiene for the API process (T11.2.15, R6-5).

uvicorn's access log records the full request line, query string included. The e-mail
verification link is a GET with ``?token=<raw token>`` (a mail client can only open a URL),
so every click would put a live verification token into the container logs (and on Azure
into Log Analytics). ``AccessLogRedactor`` rewrites the path argument of every
``uvicorn.access`` record before any handler formats it: each query value becomes
``[redacted]`` (keys are kept for debugging). All query values are redacted, not only
``token``, so a future query parameter cannot leak by default.

The filter sits on the ``uvicorn.access`` **logger**, so it applies to every handler
(uvicorn's own and any added later). ``install_log_redaction`` is idempotent and is called
by ``create_app``; uvicorn's ``dictConfig`` runs before the app is imported and does not
remove logger filters.
"""

from __future__ import annotations

import logging

ACCESS_LOGGER = "uvicorn.access"
REDACTED = "[redacted]"
# uvicorn's access records: args = (client_addr, method, full_path, http_version, status)
_PATH_ARG = 2


def redact_query(path: str) -> str:
    """``/a?token=x&b=`` → ``/a?token=[redacted]&b=[redacted]``; no query → unchanged."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    parts = []
    for segment in query.split("&"):
        name, eq, _value = segment.partition("=")
        parts.append(f"{name}={REDACTED}" if eq else REDACTED)
    return f"{base}?{'&'.join(parts)}"


class AccessLogRedactor(logging.Filter):
    """Redact query strings in uvicorn access-log records (never drops a record)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        path = args[_PATH_ARG] if isinstance(args, tuple) and len(args) > _PATH_ARG else None
        if isinstance(args, tuple) and isinstance(path, str):
            redacted = redact_query(path)
            if redacted != path:
                record.args = (*args[:_PATH_ARG], redacted, *args[_PATH_ARG + 1 :])
            return True
        # Any other shape (a custom access logger, a pre-formatted line): redact every
        # word of the rendered message that carries a query string.
        message = record.getMessage()
        if "?" in message:
            record.msg = " ".join(
                redact_query(word) if "?" in word else word for word in message.split(" ")
            )
            record.args = None
        return True


def install_log_redaction() -> None:
    """Attach the redactor to the uvicorn access logger once."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, AccessLogRedactor) for f in logger.filters):
        logger.addFilter(AccessLogRedactor())
