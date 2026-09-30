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
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rag_app.api.auth import CurrentUserDep
from rag_app.api.deps import SessionDep, rate_limit_chat
from rag_app.api.schemas import (
    ChatStreamRequest,
    ConversationDetailOut,
    ConversationOut,
    MessageOut,
    MessageResponse,
    RenameRequest,
)
from rag_app.crypto import decrypt, encrypt, unwrap_key
from rag_app.db.models import Conversation, Message, User
from rag_app.db.session import make_session_factory
from rag_app.generation import (
    Answer,
    StreamResult,
    StreamStage,
    StreamToken,
    answer_question_stream,
)
from rag_app.llm import Usage

router = APIRouter()
logger = logging.getLogger("rag_app.api.conversations")

# Streaming responses outlive the request-scoped session, so the SSE generator uses
# its own session from this factory (created lazily, one engine reused per process).
_stream_sessions: sessionmaker[Session] | None = None

# Error events: a stable code for the UI plus a user-facing message. The exception itself is
# logged by class name only — its text may carry hosts, prompts or other request data.
ERROR_KEY = "key_unavailable"
ERROR_STORAGE = "storage_failed"
ERROR_RETRIEVAL = "retrieval_failed"
ERROR_GENERATION = "generation_failed"
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
}
# Pipeline stages before the LLM is involved: a failure there is a retrieval failure.
_RETRIEVAL_STAGES = frozenset({"retrieving", "reranking"})
UNREADABLE_TITLE = "Unreadable conversation"
UNREADABLE_MESSAGE = "[this message cannot be decrypted]"


def _stream_session_factory() -> sessionmaker[Session]:
    global _stream_sessions
    if _stream_sessions is None:
        _stream_sessions = make_session_factory()
    return _stream_sessions


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


def _start_turn(
    session: Session, user_id: uuid.UUID, key: bytes, request: ChatStreamRequest
) -> Conversation:
    """Get or create the conversation and store the user message, in one transaction: a new
    conversation never exists without its first message."""
    if request.conversation_id:
        conv = _get_owned_conversation(session, user_id, request.conversation_id)
    else:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        session.flush()
    session.add(
        Message(
            conversation_id=conv.id,
            role="user",
            content_encrypted=_encrypt_json(key, {"text": request.question}),
        )
    )
    session.commit()
    return conv


def _chat_events(
    user_id: uuid.UUID, wrapped_key: bytes, request: ChatStreamRequest
) -> Iterator[str]:
    conv_id = request.conversation_id
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
            conv = _start_turn(session, user_id, key, request)
        except Exception as exc:  # noqa: BLE001 - storage down, or the conversation vanished
            session.rollback()
            logger.warning("chat stream: turn not stored (%s)", type(exc).__name__)
            yield _sse({"type": "conversation", "conversation_id": conv_id})
            yield _error_event(conv_id, ERROR_STORAGE)
            return
        conv_id = str(conv.id)
        yield _sse({"type": "conversation", "conversation_id": conv_id})

        answer: Answer | None = None
        usage = Usage()
        stage = "starting"
        try:
            for event in answer_question_stream(session, request.question, version=request.version):
                if isinstance(event, StreamStage):
                    stage = event.stage
                    yield _sse({"type": "stage", "stage": event.stage})
                elif isinstance(event, StreamToken):
                    yield _sse({"type": "token", "text": event.text})
                elif isinstance(event, StreamResult):
                    answer = event.answer
                    usage = event.usage
            if answer is None:
                raise RuntimeError("the pipeline produced no answer")
        except Exception as exc:  # noqa: BLE001 - surface a clean error event, keep the stream valid
            code = ERROR_RETRIEVAL if stage in _RETRIEVAL_STAGES else ERROR_GENERATION
            logger.warning("chat stream: %s at stage %s (%s)", code, stage, type(exc).__name__)
            _persist_error_marker(session, conv, key, code)
            yield _error_event(conv_id, code)
            return

        payload = {
            "text": answer.text,
            "citations": _citation_dicts(answer),
            "abstained": answer.abstained,
            "grounded": answer.grounded,
        }
        try:
            _persist_message(session, conv, key, "assistant", payload, usage)
            total = _total_tokens(_messages(session, conv.id))
        except Exception as exc:  # noqa: BLE001 - the answer exists but cannot be stored
            logger.warning("chat stream: answer not stored (%s)", type(exc).__name__)
            _persist_error_marker(session, conv, key, ERROR_STORAGE)
            yield _error_event(conv_id, ERROR_STORAGE)
            return
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


@router.post("/chat/stream", dependencies=[Depends(rate_limit_chat)])
def chat_stream(
    request: ChatStreamRequest, session: SessionDep, user: CurrentUserDep
) -> StreamingResponse:
    # Read the wrapped key now, while the request session is still alive; the SSE
    # generator then works from plain bytes on its own session.
    if user.key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no data key")
    if request.conversation_id:
        # Someone else's (or an unknown) conversation is a plain 404 before any streaming.
        _get_owned_conversation(session, user.id, request.conversation_id)
    return StreamingResponse(
        _chat_events(user.id, user.key.wrapped_key, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- conversation CRUD ------------------------------------------------------


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(session: SessionDep, user: CurrentUserDep) -> list[ConversationOut]:
    """Never a 500 because of one bad row (T11.2.16): a conversation whose title or first
    message cannot be decrypted — or every one, when the data key cannot be unwrapped — is
    listed as unreadable with a placeholder title."""
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


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
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


@router.patch("/conversations/{conversation_id}", response_model=MessageResponse)
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


@router.delete("/conversations/{conversation_id}", response_model=MessageResponse)
def delete_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUserDep
) -> MessageResponse:
    conv = _get_owned_conversation(session, user.id, conversation_id)
    session.delete(conv)
    session.commit()
    return MessageResponse(detail="deleted")
