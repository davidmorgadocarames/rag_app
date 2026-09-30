"""Global daily answer cap (R6-1, T11.2.14).

Registration is open and every request reaches the API through one shared proxy IP, so the
per-IP rate limiter is effectively global and the Azure alerts only notify. This cap bounds
what one day of abuse can cost: at most ``DAILY_ANSWER_CAP`` answers per UTC day, for every
user together.

The counter is ``usage_daily`` (0005): one row per UTC day with ``answers`` and ``tokens`` —
no user id, no question, no personal data.

**Reserved at request start (DA-G3-1).** ``reserve_answer`` counts the answer *before* any
LLM call, in ONE statement::

    INSERT … VALUES (today, 1) ON CONFLICT (day)
    DO UPDATE SET answers = answers + 1 WHERE answers < cap RETURNING answers

Postgres locks the day's row for the update and re-checks ``answers < cap`` on its latest
version, so N concurrent requests at ``cap - 1`` get exactly one success (race-safe, no
read-then-write). A turn that later fails or is interrupted has already been counted — it
spent (or may have spent) tokens. Tokens are added afterwards, when the pipeline reports
them (``add_tokens``); ``/chat`` has no token accounting, so its answers count with 0 tokens.

A turn that never reaches the pipeline does not use up an answer (DA-31b-1): the stream
endpoint unwraps the user's data key *before* reserving, so an account whose key cannot be
unwrapped is refused without counting, and a turn whose user message cannot be stored gives
its reservation back (``release_answer``).

The chit-chat fast path (a greeting answered with a canned reply, no LLM call) is not
counted. ``DAILY_ANSWER_CAP=0`` switches the cap off (``ENV=dev`` only — ``ENV=prod`` requires
a positive cap, see ``rag_app.config``); then nothing is read or written.

When the cap is reached, the refusal is logged as ``daily answer cap reached`` with the day
and the cap only (the Phase 13 banner reads ``usage_daily``/this line).
"""

from __future__ import annotations

import datetime as dt
import logging
import math

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("rag_app.usage_cap")

DAILY_CAP_CODE = "daily_cap_reached"
DAILY_CAP_MESSAGE = (
    "SecRAG has reached its daily answer limit (a cost guard for this public demo)."
    " Please come back after midnight UTC."
)

_RESERVE_SQL = text(
    "INSERT INTO usage_daily AS u (day, answers, tokens) VALUES (:day, 1, 0)"
    " ON CONFLICT (day) DO UPDATE SET answers = u.answers + 1 WHERE u.answers < :cap"
    " RETURNING u.answers"
)
_RELEASE_SQL = text("UPDATE usage_daily SET answers = answers - 1 WHERE day = :day AND answers > 0")
_ADD_TOKENS_SQL = text(
    "INSERT INTO usage_daily AS u (day, answers, tokens) VALUES (:day, 0, :tokens)"
    " ON CONFLICT (day) DO UPDATE SET tokens = u.tokens + EXCLUDED.tokens"
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def seconds_until_utc_midnight(now: dt.datetime | None = None) -> int:
    """Seconds until the next UTC midnight, when the counter starts again (``Retry-After``)."""
    now = (now or utc_now()).astimezone(dt.UTC)
    midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)
    return max(1, math.ceil((midnight - now).total_seconds()))


def reserve_answer(session: Session, cap: int, *, now: dt.datetime | None = None) -> bool:
    """Count one answer for today (UTC) if the cap allows it; False when the cap is reached.

    ``cap <= 0`` means the cap is off: always True, the database is not touched. Commits.
    """
    if cap <= 0:
        return True
    day = (now or utc_now()).astimezone(dt.UTC).date()
    row = session.execute(_RESERVE_SQL, {"day": day, "cap": cap}).first()
    session.commit()
    if row is None:
        logger.warning("daily answer cap reached: day=%s cap=%d (answer refused)", day, cap)
        return False
    if int(row[0]) == cap:
        logger.warning("daily answer cap reached: day=%s cap=%d (last answer)", day, cap)
    return True


def release_answer(session: Session, cap: int, *, now: dt.datetime | None = None) -> None:
    """Give back a reservation whose turn never reached the pipeline (DA-31b-1: the user
    message could not be stored). Best effort: when the database is down the answer stays
    counted (fail closed). A turn that straddles midnight gives it back to the new day, never
    below 0. ``cap <= 0``: the cap is off, nothing to do."""
    if cap <= 0:
        return
    day = (now or utc_now()).astimezone(dt.UTC).date()
    try:
        session.execute(_RELEASE_SQL, {"day": day})
        session.commit()
    except Exception as exc:  # noqa: BLE001 - the answer simply stays counted
        session.rollback()
        logger.warning("usage_daily: reservation not released (%s)", type(exc).__name__)


def add_tokens(session: Session, tokens: int, *, now: dt.datetime | None = None) -> None:
    """Add a finished turn's tokens to today's row (best effort — never breaks a reply)."""
    if tokens <= 0:
        return
    day = (now or utc_now()).astimezone(dt.UTC).date()
    try:
        session.execute(_ADD_TOKENS_SQL, {"day": day, "tokens": int(tokens)})
        session.commit()
    except Exception as exc:  # noqa: BLE001 - accounting must not fail the answer
        session.rollback()
        logger.warning("usage_daily: tokens not added (%s)", type(exc).__name__)
