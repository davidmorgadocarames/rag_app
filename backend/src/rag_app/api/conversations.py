"""Conversation history + streaming chat.

Chat answers are streamed over Server-Sent Events so the UI can show which pipeline
stage is running and render tokens as they arrive. Messages are persisted encrypted
with the user's per-user key (GDPR crypto-shred; deleting the user cascades to
conversations and messages — see rag_app.erasure).

Stream contract (T11.2.16): the **first** event is ``{"type": "conversation",
"conversation_id": …}``; the stream then ends with exactly one ``done`` or ``error`` event,
both carrying the id. Every failure (data key unwrap, storage, embeddings/retrieval,
reranking, LLM) becomes an ``error`` event — never an unhandled exception that cuts the
stream. A failed turn is persisted as an assistant **error marker**, so a conversation never
holds a user message without a reply and the next message reuses the same conversation.
A client that goes away mid-stream (closed tab, network drop, ingress idle cut) gets an
``interrupted`` marker the same way (DA-G2-3).

Connections (DA-G2-2): since FastAPI 0.118 a ``yield`` dependency exits only after the
response is sent, so ``chat_stream`` closes the request session before it returns the
stream — no connection sits "idle in transaction" for the whole answer. The pipeline runs on
a worker thread with its own session, committed after every event (the read transaction
ends as soon as retrieval is done); while it is busy (a reranker download at a cold start,
a slow model) the stream sends an SSE comment ``: keep-alive`` every ``KEEPALIVE_SECONDS``
so no proxy cuts an idle connection. Clients ignore comment frames.

Daily answer cap (R6-1, DA-G3-1): ``chat_stream`` reserves the answer in ``usage_daily``
at request start, before the stream (and any LLM call) begins — so an interrupted or failed
turn is already counted. When the cap is reached the stream is just the ``conversation``
event and an ``error`` event (``daily_cap_reached``) with the id; nothing is stored and the
pipeline never runs. The chit-chat fast path (canned reply, no LLM call) is not counted.
Pre-LLM failures do not use up an answer (DA-31b-1): the data key is unwrapped in the
endpoint before the reservation (``key_unavailable``, nothing counted), and a turn whose user
message cannot be stored gives its reservation back.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from collections.abc import AsyncIterator, Generator
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import iterate_in_threadpool
from starlette.types import Receive, Scope, Send

from rag_app import metrics, warmup
from rag_app.api.auth import CurrentUserDep
from rag_app.api.deps import SessionDep, rate_limit_account, rate_limit_chat
from rag_app.api.schemas import (
    ChatStreamRequest,
    ConversationDetailOut,
    ConversationOut,
    MessageOut,
    MessageResponse,
    RenameRequest,
)
from rag_app.assistant.fast_path import match_fast_path
from rag_app.assistant.memory import previous_intent_from_summary, select_last_turn, summarize_turn
from rag_app.authz import require_permission
from rag_app.config import get_settings
from rag_app.crypto import decrypt, encrypt, unwrap_key
from rag_app.csrf import require_csrf
from rag_app.db.models import Conversation, Message, User
from rag_app.db.session import make_session_factory
from rag_app.generation import (
    Answer,
    StreamResult,
    StreamStage,
    StreamStep,
    StreamToken,
    answer_question_stream,
)
from rag_app.llm import Usage, make_chat_client
from rag_app.reranking import get_shared_reranker
from rag_app.usage_cap import (
    DAILY_CAP_CODE,
    DAILY_CAP_MESSAGE,
    add_tokens,
    release_answer,
    reserve_answer,
)

router = APIRouter()
logger = logging.getLogger("rag_app.api.conversations")

# Streaming responses outlive the request-scoped session, so the SSE generator uses
# its own session from this factory (created lazily, one engine reused per process). The
# pipeline worker threads can ask for it concurrently, so it is built under a lock
# (DA-G3-2): two first streams at once never build two engines (one pool leaked).
_stream_sessions: sessionmaker[Session] | None = None
_stream_sessions_lock = threading.Lock()

# Error events: a stable code for the UI plus a user-facing message. The exception itself is
# logged by class name only — its text may carry hosts, prompts or other request data.
ERROR_KEY = "key_unavailable"
ERROR_STORAGE = "storage_failed"
ERROR_RETRIEVAL = "retrieval_failed"
ERROR_GENERATION = "generation_failed"
ERROR_INTERRUPTED = "interrupted"
ERROR_MESSAGES = {
    ERROR_KEY: (
        "Your conversation data cannot be decrypted right now, so this message was not"
        " processed. Please contact the administrator."
    ),
    ERROR_STORAGE: "The message could not be saved. Please try again in a moment.",
    ERROR_RETRIEVAL: (
        "The search service (embeddings or retrieval) is unavailable, so no answer was"
        " generated. Please try again in a moment."
    ),
    ERROR_GENERATION: (
        "The language model is unavailable, so no answer was generated. Please try again"
        " in a moment."
    ),
    ERROR_INTERRUPTED: (
        "The connection closed before the answer finished, so no answer was saved. Please"
        " ask again."
    ),
    DAILY_CAP_CODE: DAILY_CAP_MESSAGE,
}
# An SSE comment line: keeps the connection busy while a stage takes long (DA-G2-3; the Azure
# ingress cuts a connection idle for ~240 s). EventSource and lib/chatStream.ts skip it.
KEEPALIVE_SECONDS = 15.0
KEEPALIVE_FRAME = ": keep-alive\n\n"
# Pipeline stages before the LLM is involved: a failure there is a retrieval failure.
_RETRIEVAL_STAGES = frozenset({"retrieving", "reranking"})
UNREADABLE_TITLE = "Unreadable conversation"
UNREADABLE_MESSAGE = "[this message cannot be decrypted]"


def _stream_session_factory() -> sessionmaker[Session]:
    global _stream_sessions
    factory = _stream_sessions
    if factory is None:
        with _stream_sessions_lock:
            factory = _stream_sessions
            if factory is None:
                factory = _stream_sessions = make_session_factory()
    return factory


# --- encryption helpers -----------------------------------------------------


def _user_key(user: User) -> bytes:
    if user.key is None:  # pragma: no cover - a normal user always has a key
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no data key")
    return unwrap_key(user.key.wrapped_key)


def _user_key_or_none(user: User) -> bytes | None:
    """The user's data key, or None when it cannot be unwrapped (wrong master key, damaged
    row): callers degrade to "unreadable" instead of failing with a 500."""
    if user.key is None:
        return None
    try:
        return unwrap_key(user.key.wrapped_key)
    except Exception as exc:  # noqa: BLE001 - any unwrap failure means "unreadable"
        logger.warning("data key cannot be unwrapped (%s)", type(exc).__name__)
        return None


def _encrypt_json(key: bytes, payload: dict[str, Any]) -> bytes:
    return encrypt(key, json.dumps(payload))


def _decrypt_json(key: bytes, blob: bytes) -> dict[str, Any]:
    data = json.loads(decrypt(key, blob))
    return data if isinstance(data, dict) else {}


def _citation_dicts(answer: Answer) -> list[dict[str, Any]]:
    return [
        {
            "marker": c.marker,
            "chunk_uid": c.chunk_uid,
            "heading": c.heading,
            "version": c.version,
            "effective_date": c.effective_date,
        }
        for c in answer.citations
    ]


# --- lookup / aggregation helpers -------------------------------------------


def _get_owned_conversation(session: Session, user_id: uuid.UUID, conv_id: str) -> Conversation:
    """Load a conversation the user owns, or 404 (no existence leak for others')."""
    try:
        cid = uuid.UUID(conv_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc
    conv = session.get(Conversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conv


def _messages(session: Session, conv_id: uuid.UUID) -> list[Message]:
    return list(
        session.scalars(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
        )
    )


def _total_tokens(messages: list[Message]) -> int:
    return sum((m.prompt_tokens or 0) + (m.completion_tokens or 0) for m in messages)


def _title_for(key: bytes, conv: Conversation, messages: list[Message]) -> str:
    if conv.title_encrypted is not None:
        return decrypt(key, conv.title_encrypted)
    for m in messages:
        if m.role == "user":
            text = _decrypt_json(key, m.content_encrypted).get("text", "")
            return (text[:60] or "New chat").strip()
    return "New chat"


# --- streaming chat ---------------------------------------------------------


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _error_event(conversation_id: str | None, code: str) -> str:
    return _sse(
        {
            "type": "error",
            "conversation_id": conversation_id,
            "code": code,
            "detail": ERROR_MESSAGES[code],
        }
    )


def _persist_message(
    session: Session,
    conv: Conversation,
    key: bytes,
    role: str,
    payload: dict[str, Any],
    usage: Usage | None,
) -> None:
    session.add(
        Message(
            conversation_id=conv.id,
            role=role,
            content_encrypted=_encrypt_json(key, payload),
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
    )
    session.commit()


def _persist_error_marker(session: Session, conv: Conversation, key: bytes, code: str) -> None:
    """The failed turn's reply: an assistant message flagged ``error`` (best effort — the
    error event is sent whether or not this write succeeds)."""
    payload = {
        "text": ERROR_MESSAGES[code],
        "error": True,
        "error_code": code,
        "citations": [],
        "abstained": False,
        "grounded": False,
    }
    try:
        session.rollback()  # a failed flush/commit leaves the session unusable otherwise
        _persist_message(session, conv, key, "assistant", payload, None)
    except Exception as exc:  # noqa: BLE001 - never let the marker write break the stream
        session.rollback()
        logger.warning("chat stream: error marker not saved (%s)", type(exc).__name__)


def _decrypted_turns(key: bytes, messages: list[Message]) -> list[tuple[str, str, bool]]:
    """``(role, text, error)`` for each message, in order, skipping any that fail to
    decrypt — one damaged row must not break memory read, same "never crash on one bad row"
    discipline as ``_title_for``/``list_conversations``."""
    turns: list[tuple[str, str, bool]] = []
    for m in messages:
        try:
            data = _decrypt_json(key, m.content_encrypted)
        except Exception:  # noqa: BLE001 - one damaged message must not break memory read
            continue
        turns.append((m.role, data.get("text", ""), bool(data.get("error", False))))
    return turns


def _load_memory(
    key: bytes, conv: Conversation, prior_messages: list[Message]
) -> tuple[str | None, str | None, str | None]:
    """T14.2.5 (memory read): the conversation's own summary + its last clean turn, from the
    state strictly BEFORE the current turn — ``prior_messages`` must be fetched before
    :func:`_start_turn` persists the new user message. Scoped to exactly ONE conversation by
    construction: ``conv``/``prior_messages`` always come from the single, already
    ownership-checked conversation this turn belongs to (``_get_owned_conversation`` for an
    existing id, or a brand-new ``Conversation`` row) — there is no code path here that can
    read another conversation's rows.

    ``previous_intent`` (DA-router-reliability-1, F24-into-14): read from the SAME decrypted
    ``summary`` (``assistant.memory.previous_intent_from_summary``) — never a separate
    database read, since it is piggybacked onto the already-stored
    ``ConversationSummary.last_intent`` field (no new column; see that function's own
    docstring for the self-reported staleness boundary)."""
    summary: str | None = None
    if conv.summary_encrypted is not None:
        try:
            summary = decrypt(key, conv.summary_encrypted)
        except Exception as exc:  # noqa: BLE001 - a damaged/unreadable summary is just "none"
            logger.warning(
                "chat stream: conversation summary cannot be decrypted (%s)", type(exc).__name__
            )
    last_turn = select_last_turn(_decrypted_turns(key, prior_messages))
    previous_intent = previous_intent_from_summary(summary)
    return summary, last_turn, previous_intent


def _start_turn(
    session: Session, user_id: uuid.UUID, key: bytes, request: ChatStreamRequest
) -> tuple[Conversation, str | None, str | None, str | None]:
    """Get or create the conversation, read its memory (T14.2.5 — BEFORE this turn's own
    message exists), and store the user message, all in one transaction: a new conversation
    never exists without its first message, and memory always reflects the state strictly
    before the turn it is about to inform."""
    if request.conversation_id:
        conv = _get_owned_conversation(session, user_id, request.conversation_id)
        prior_messages = _messages(session, conv.id)
    else:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        session.flush()
        prior_messages = []
    summary, last_turn, previous_intent = _load_memory(key, conv, prior_messages)
    session.add(
        Message(
            conversation_id=conv.id,
            role="user",
            content_encrypted=_encrypt_json(key, {"text": request.question}),
        )
    )
    session.commit()
    return conv, summary, last_turn, previous_intent


def _update_conversation_summary(
    session: Session,
    conv: Conversation,
    key: bytes,
    *,
    previous_summary: str | None,
    last_turn: str | None,
    question: str,
    answer: str,
    observed_intent: str | None = None,
) -> None:
    """T14.2.6 (memory write): AFTER the final answer event (the assistant reply is already
    persisted by the caller), BEFORE the stream's own ``done`` event. Best-effort by design:
    ANY failure (an unreachable LLM, a damaged key, storage) is logged and otherwise
    swallowed — memory is a quality feature, it must never turn an already-successful answer
    into a failed stream. ``summarize_turn`` itself already turns an invalid/unreachable
    update into ``None`` ("dropped"); the try/except here additionally covers the
    encrypt/commit step.

    ``observed_intent`` (DA-router-reliability-1): THIS turn's own ``decision.intent``
    (from the pipeline's final ``StreamResult``), written onto the new summary's
    ``last_intent`` so the NEXT turn's :func:`_load_memory` can read it back as
    ``previous_intent``. Only reaches this far when the caller did not skip this call for
    an exact fast-path turn (see ``generation.answer_question_stream``'s own self-reported
    staleness note for that narrow case)."""
    try:
        chat = make_chat_client()
        new_summary = summarize_turn(
            chat,
            previous_summary=previous_summary,
            last_turn=last_turn,
            question=question,
            answer=answer,
            observed_intent=observed_intent,
        )
        if new_summary is not None:
            conv.summary_encrypted = encrypt(key, new_summary)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - memory is best-effort, see docstring
        session.rollback()
        logger.warning("chat stream: conversation summary not updated (%s)", type(exc).__name__)


class _PipelineFailed:
    """An exception raised by the pipeline on the worker thread, handed to the stream."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_PIPELINE_END = object()


def _pipeline_events(
    request: ChatStreamRequest,
    *,
    summary: str | None,
    last_turn: str | None,
    previous_intent: str | None = None,
) -> Generator[object | None, None, None]:
    """The pipeline's events, produced on a worker thread with its OWN session (committed
    after every event, so no read transaction stays open while the model generates).
    Yields ``None`` whenever ``KEEPALIVE_SECONDS`` pass without an event (the caller sends a
    keep-alive). Re-raises the pipeline's exception here. Closing this generator (client gone)
    tells the worker to stop at its next event.

    ``summary``/``last_turn``/``previous_intent`` (T14.2.5, DA-router-reliability-1): the
    conversation's memory, already read by the caller (``_load_memory``, on the OUTER
    session) before this worker thread's own session even exists — forwarded unchanged into
    ``answer_question_stream``."""
    events: queue.Queue[object] = queue.Queue()
    cancel = threading.Event()

    def work() -> None:
        try:
            with _stream_session_factory()() as session:
                # T11.4.1: the ONE process-wide shared reranker — never a fresh load per
                # stream.
                for event in answer_question_stream(
                    session,
                    request.question,
                    version=request.version,
                    reranker=get_shared_reranker(),
                    summary=summary,
                    last_turn=last_turn,
                    previous_intent=previous_intent,
                ):
                    session.commit()  # ends the read transaction; a no-op when none is open
                    if cancel.is_set():
                        break
                    events.put(event)
        except BaseException as exc:  # noqa: BLE001 - handed to the stream, which maps it
            events.put(_PipelineFailed(exc))
        finally:
            events.put(_PIPELINE_END)

    threading.Thread(target=work, name="chat-pipeline", daemon=True).start()
    try:
        while True:
            try:
                item = events.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield None
                continue
            if item is _PIPELINE_END:
                return
            if isinstance(item, _PipelineFailed):
                raise item.exc
            yield item
    finally:
        cancel.set()


def _chat_events(
    user_id: uuid.UUID,
    wrapped_key: bytes,
    request: ChatStreamRequest,
    *,
    blocked: str | None = None,
    counted: bool = False,
) -> Generator[str, None, None]:
    """The SSE body. ``blocked``: an error code decided at request start (the data key
    cannot be unwrapped, the daily cap, or its counter unavailable) — the turn ends right
    away, nothing is stored, no pipeline.
    ``counted``: the turn holds a ``usage_daily`` reservation, so its tokens are added."""
    conv_id = request.conversation_id
    if blocked is not None:
        yield _sse({"type": "conversation", "conversation_id": conv_id})
        yield _error_event(conv_id, blocked)
        return
    try:
        key = unwrap_key(wrapped_key)
    except Exception as exc:  # noqa: BLE001 - e.g. InvalidToken: master key changed
        # Nothing can be stored encrypted, so no conversation is created or touched.
        logger.warning("chat stream: data key cannot be unwrapped (%s)", type(exc).__name__)
        yield _sse({"type": "conversation", "conversation_id": conv_id})
        yield _error_event(conv_id, ERROR_KEY)
        return

    # Own session: the SSE body outlives the request-scoped session (see factory above).
    with _stream_session_factory()() as session:
        try:
            conv, memory_summary, memory_last_turn, memory_previous_intent = _start_turn(
                session, user_id, key, request
            )
        except Exception as exc:  # noqa: BLE001 - storage down, or the conversation vanished
            session.rollback()
            logger.warning("chat stream: turn not stored (%s)", type(exc).__name__)
            if counted:  # DA-31b-1: the pipeline never runs, so the answer is given back
                release_answer(session, get_settings().daily_answer_cap)
            yield _sse({"type": "conversation", "conversation_id": conv_id})
            yield _error_event(conv_id, ERROR_STORAGE)
            return
        conv_id = str(conv.id)
        replied = False  # an assistant message (answer or marker) is stored for this turn
        try:
            yield _sse({"type": "conversation", "conversation_id": conv_id})

            answer: Answer | None = None
            usage = Usage()
            used_fast_path = False
            observed_intent: str | None = None
            stage = "starting"
            pipeline = _pipeline_events(
                request,
                summary=memory_summary,
                last_turn=memory_last_turn,
                previous_intent=memory_previous_intent,
            )
            try:
                for event in pipeline:
                    if event is None:
                        yield KEEPALIVE_FRAME
                    elif isinstance(event, StreamStage):
                        stage = event.stage
                        yield _sse({"type": "stage", "stage": event.stage})
                    elif isinstance(event, StreamStep):
                        # T13.12.5 (DEBT-062): a real sub-step boundary WITHIN the current
                        # stage (today only retrieving/embed_query) -- existing clients
                        # ignore an unknown "type" (frontend/lib/chatStream.ts's `dispatch`),
                        # so this is additive, never a breaking change to the SSE contract.
                        yield _sse(
                            {
                                "type": "step",
                                "stage": event.stage,
                                "step": event.step,
                                "phase": event.phase,
                            }
                        )
                    elif isinstance(event, StreamToken):
                        yield _sse({"type": "token", "text": event.text})
                    elif isinstance(event, StreamResult):
                        answer = event.answer
                        usage = event.usage
                        used_fast_path = event.used_fast_path
                        observed_intent = event.intent or None
                if answer is None:
                    raise RuntimeError("the pipeline produced no answer")
            except Exception as exc:  # noqa: BLE001 - surface a clean error event, keep the stream valid
                code = ERROR_RETRIEVAL if stage in _RETRIEVAL_STAGES else ERROR_GENERATION
                logger.warning("chat stream: %s at stage %s (%s)", code, stage, type(exc).__name__)
                _persist_error_marker(session, conv, key, code)
                replied = True
                yield _error_event(conv_id, code)
                return
            finally:
                pipeline.close()

            if counted:
                add_tokens(session, usage.total_tokens)
            payload = {
                "text": answer.text,
                "citations": _citation_dicts(answer),
                "abstained": answer.abstained,
                "grounded": answer.grounded,
            }
            try:
                _persist_message(session, conv, key, "assistant", payload, usage)
                replied = True
                total = _total_tokens(_messages(session, conv.id))
            except Exception as exc:  # noqa: BLE001 - the answer exists but cannot be stored
                logger.warning("chat stream: answer not stored (%s)", type(exc).__name__)
                if not replied:
                    _persist_error_marker(session, conv, key, ERROR_STORAGE)
                    replied = True
                yield _error_event(conv_id, ERROR_STORAGE)
                return
            # T14.2.6 (memory write): AFTER the answer above is persisted, BEFORE the "done"
            # event below closes the turn — best-effort, never breaks the stream (see
            # _update_conversation_summary's own docstring).
            #
            # DA-14-C1 finding 1 fix (14-C1-fix, HIGH): skipped entirely when this turn took
            # the exact fast path (``used_fast_path``, the SAME ``match_fast_path`` decision
            # ``_reserve_stream_answer`` above already used to exempt the turn from the daily
            # cap). A bare greeting/thanks/farewell has no topic/entity/question content
            # worth merging into the running summary, so nothing is lost; before this fix a
            # cap-exempt turn cost TWO uncounted LLM calls (the chit-chat reply AND this
            # write) instead of one. Every other answered turn (including router-classified
            # chit-chat, which is NOT fast-path-exempt and is already counted against the
            # cap) still writes memory exactly as before.
            if not used_fast_path:
                _update_conversation_summary(
                    session,
                    conv,
                    key,
                    previous_summary=memory_summary,
                    last_turn=memory_last_turn,
                    question=request.question,
                    answer=answer.text,
                    observed_intent=observed_intent,
                )
            yield _sse(
                {
                    "type": "done",
                    "conversation_id": conv_id,
                    "answer": answer.text,
                    "abstained": answer.abstained,
                    "grounded": answer.grounded,
                    "citations": _citation_dicts(answer),
                    "usage": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    },
                    "conversation_total_tokens": total,
                }
            )
        except GeneratorExit:
            # The client went away mid-stream (DA-G2-3): the user message stays answered.
            if not replied:
                logger.warning("chat stream: client disconnected; turn marked interrupted")
                _persist_error_marker(session, conv, key, ERROR_INTERRUPTED)
            raise


def _chat_events_with_metrics(*args: Any, **kwargs: Any) -> Generator[str, None, None]:
    """``_chat_events`` wrapped so one SSE turn counts toward the in-flight chat gauge
    (T11.3.2) for its whole duration — including the pipeline run, not just request setup.
    ``yield from`` forwards ``GeneratorExit``/exceptions through unchanged, so
    ``_chat_events``'s own disconnect handling (the ``interrupted`` marker) is untouched."""
    with metrics.track_in_flight():
        yield from _chat_events(*args, **kwargs)


async def _closing_body(events: Generator[str, None, None]) -> AsyncIterator[str]:
    """The sync SSE generator as the response body, CLOSED when the body ends for any reason
    — a finished stream, a client disconnect (the task is cancelled) or an error — so its
    ``GeneratorExit`` handler stores the ``interrupted`` marker right away instead of
    whenever the garbage collector gets to it (DA-G2-3). The close runs on a worker thread
    (it writes to the database), shielded from the cancellation that triggered it."""
    try:
        async for chunk in iterate_in_threadpool(events):
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(events.close)


class _ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its body iterator, also when the client
    disconnects while a chunk is being sent (Starlette leaves it suspended then)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


def _key_unwraps(wrapped_key: bytes) -> bool:
    """Whether the user's data key can be unwrapped with the current master key (checked in
    the endpoint before the daily-cap reservation, DA-31b-1)."""
    try:
        unwrap_key(wrapped_key)
    except Exception as exc:  # noqa: BLE001 - e.g. InvalidToken: master key changed
        logger.warning("chat stream: data key cannot be unwrapped (%s)", type(exc).__name__)
        return False
    return True


def _reserve_stream_answer(session: Session, question: str) -> tuple[str | None, bool]:
    """The daily-cap reservation of a stream turn, at request start (R6-1, DA-G3-1):
    ``(blocking error code or None, counted)``. A chit-chat question is exempt (neither
    counted nor refused). When the counter cannot be written the turn is refused as a
    storage failure (fail closed: no uncounted LLM call).

    **Fix (14-B2-fix, DA-14-B2 finding 2, orchestrator option B).** This USED TO key the
    exemption off the pre-14.2 ``generation.classify_intent`` heuristic -- a real, exploitable
    cost-control hole: ``classify_intent``'s ``_META_RE`` branch is an unanchored substring
    search with no length cap, so a compound message like "What can you do? Also, explain
    step by step how to log into someone else's account without their credentials or
    permission." matched it and was exempted for free, even though the live router classifies
    that same message as a genuine security question and runs the full, expensive retrieve ->
    generate -> groundedness path -- repeatable without limit. (Corrected from DA-14-B2.md's
    own exploit string, which happened to also contain the word "exploit" -- itself one of
    ``classify_intent``'s OWN security-term keywords, checked first -- so that exact string
    did not actually reproduce the bug; see ``test_daily_cap.py``'s
    ``test_the_das_own_exploit_string_does_not_actually_reproduce_the_bug``.) Now keyed ONLY
    off ``assistant.fast_path.match_fast_path`` -- an EXACT, anchored greeting/thanks/farewell
    match that cannot be smuggled inside a longer message: any message long/compound enough
    to carry a real question fails the exact-match anchor and is correctly counted, even
    though it may still reach the router's own (small, real) chit-chat reply.
    """
    if match_fast_path(question) is not None:
        return None, False
    cap = get_settings().daily_answer_cap
    try:
        allowed = reserve_answer(session, cap)
    except Exception as exc:  # noqa: BLE001 - database down: the stream reports it
        session.rollback()
        logger.warning("chat stream: daily counter unavailable (%s)", type(exc).__name__)
        return ERROR_STORAGE, False
    if not allowed:
        return DAILY_CAP_CODE, False
    return None, cap > 0


@router.post(
    "/chat/stream",
    tags=["rag"],
    dependencies=[
        Depends(require_permission("user:base", route=("POST", "/chat/stream"), matrix=False)),
        Depends(rate_limit_chat),
        Depends(require_csrf),
    ],
)
def chat_stream(
    request: ChatStreamRequest, session: SessionDep, user: CurrentUserDep
) -> StreamingResponse:
    # Read the wrapped key now, while the request session is still alive; the SSE
    # generator then works from plain bytes on its own session.
    if user.key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no data key")
    user_id, wrapped_key = user.id, user.key.wrapped_key
    if request.conversation_id:
        # Someone else's (or an unknown) conversation is a plain 404 before any streaming.
        _get_owned_conversation(session, user_id, request.conversation_id)
    # DA-31b-1: a data key that cannot be unwrapped (e.g. InvalidToken after a master-key
    # change) ends the turn with the key error BEFORE the reservation — the turn can never reach
    # the pipeline, so it must not use up an answer of the daily cap.
    if _key_unwraps(wrapped_key):
        blocked, counted = _reserve_stream_answer(session, request.question)
    else:
        blocked, counted = ERROR_KEY, False
    # DA-G2-2: the dependency's session would otherwise stay checked out, idle in
    # transaction, until the whole stream is sent (FastAPI >= 0.118 exits `yield`
    # dependencies after the response). Closing it ends the transaction and returns the
    # connection; the dependency's own close later is a no-op.
    session.close()
    events = _chat_events_with_metrics(
        user_id, wrapped_key, request, blocked=blocked, counted=counted
    )
    return _ClosingStreamingResponse(
        _closing_body(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- conversation CRUD ------------------------------------------------------


@router.get(
    "/conversations",
    response_model=list[ConversationOut],
    tags=["rag"],
    dependencies=[
        Depends(require_permission("user:base", route=("GET", "/conversations"), matrix=False)),
        Depends(rate_limit_account("conversations_list")),
    ],
)
def list_conversations(session: SessionDep, user: CurrentUserDep) -> list[ConversationOut]:
    """Never a 500 because of one bad row (T11.2.16): a conversation whose title or first
    message cannot be decrypted — or every one, when the data key cannot be unwrapped — is
    listed as unreadable with a placeholder title.

    DEC-050 (T13.J4-fix, DA-13-J3J4-5): this is the backend call the frontend's `/chat` page
    already makes on every page load with a valid session (``refreshConversations``,
    `frontend/app/chat/page.tsx`) -- reusing it (rather than a new route) triggers the
    Ollama warm-up the moment an AUTHENTICATED chat page loads, same single-flight guard as
    the API lifespan and a successful login, never from an unauthenticated request (this
    route is gated by ``CurrentUserDep`` above -- getting here already proves a valid
    session)."""
    warmup.trigger_warm_up()
    key = _user_key_or_none(user)
    conversations = list(
        session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.created_at.desc())
        )
    )
    out: list[ConversationOut] = []
    unreadable = 0
    for conv in conversations:
        messages = _messages(session, conv.id)
        title: str | None = None
        if key is not None:
            try:
                title = _title_for(key, conv, messages)
            except Exception:  # noqa: BLE001 - one damaged row must not fail the whole list
                title = None
        if title is None:
            unreadable += 1
        out.append(
            ConversationOut(
                id=str(conv.id),
                title=title if title is not None else UNREADABLE_TITLE,
                created_at=conv.created_at,
                total_tokens=_total_tokens(messages),
                unreadable=title is None,
            )
        )
    if unreadable:
        logger.warning("conversation list: %d unreadable conversation(s)", unreadable)
    return out


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailOut,
    tags=["rag"],
    dependencies=[
        Depends(
            require_permission(
                "user:base", route=("GET", "/conversations/{conversation_id}"), matrix=False
            )
        ),
        Depends(rate_limit_account("conversations_get")),
    ],
)
def get_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUserDep
) -> ConversationDetailOut:
    conv = _get_owned_conversation(session, user.id, conversation_id)
    key = _user_key_or_none(user)
    if key is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this conversation cannot be decrypted right now"
        )
    messages = _messages(session, conv.id)
    out_messages: list[MessageOut] = []
    for m in messages:
        try:
            data = _decrypt_json(key, m.content_encrypted)
        except Exception:  # noqa: BLE001 - show the damaged message, keep the rest readable
            data = {"text": UNREADABLE_MESSAGE, "error": True, "grounded": False}
        out_messages.append(
            MessageOut(
                role=m.role,
                content=data.get("text", ""),
                citations=data.get("citations", []),
                abstained=bool(data.get("abstained", False)),
                grounded=bool(data.get("grounded", True)),
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                created_at=m.created_at,
                error=bool(data.get("error", False)),
            )
        )
    try:
        title = _title_for(key, conv, messages)
    except Exception:  # noqa: BLE001
        title = UNREADABLE_TITLE
    return ConversationDetailOut(id=str(conv.id), title=title, messages=out_messages)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=MessageResponse,
    tags=["rag"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission(
                "user:base", route=("PATCH", "/conversations/{conversation_id}"), matrix=False
            )
        ),
        Depends(rate_limit_account("conversations_rename")),
    ],
)
def rename_conversation(
    conversation_id: str, request: RenameRequest, session: SessionDep, user: CurrentUserDep
) -> MessageResponse:
    # SQL-injection safe by construction: the title never reaches raw SQL. It is
    # length-validated (RenameRequest), Fernet-encrypted here, and written via the ORM
    # as a bound parameter — no string interpolation. (Parameterized queries are the
    # OWASP-recommended defense; we do not escape/blacklist characters.)
    key = _user_key(user)
    conv = _get_owned_conversation(session, user.id, conversation_id)
    conv.title_encrypted = encrypt(key, request.title)
    session.commit()
    return MessageResponse(detail="renamed")


@router.delete(
    "/conversations/{conversation_id}",
    response_model=MessageResponse,
    tags=["rag"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission(
                "user:base", route=("DELETE", "/conversations/{conversation_id}"), matrix=False
            )
        ),
        Depends(rate_limit_account("conversations_delete")),
    ],
)
def delete_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUserDep
) -> MessageResponse:
    conv = _get_owned_conversation(session, user.id, conversation_id)
    session.delete(conv)
    session.commit()
    return MessageResponse(detail="deleted")
