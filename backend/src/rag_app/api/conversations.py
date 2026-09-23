"""Conversation history + streaming chat.

Chat answers are streamed over Server-Sent Events so the UI can show which pipeline
stage is running and render tokens as they arrive. Messages are persisted encrypted
with the user's per-user key (GDPR crypto-shred; deleting the user cascades to
conversations and messages — see rag_app.erasure).
"""

from __future__ import annotations

import json
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

# Streaming responses outlive the request-scoped session, so the SSE generator uses
# its own session from this factory (created lazily, one engine reused per process).
_stream_sessions: sessionmaker[Session] | None = None


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


def _messages(session: SessionDep, conv_id: uuid.UUID) -> list[Message]:
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


def _persist_message(
    session: SessionDep,
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


def _chat_events(
    user_id: uuid.UUID, wrapped_key: bytes, request: ChatStreamRequest
) -> Iterator[str]:
    # Own session: the SSE body outlives the request-scoped session (see factory above).
    key = unwrap_key(wrapped_key)
    with _stream_session_factory()() as session:
        if request.conversation_id:
            conv = _get_owned_conversation(session, user_id, request.conversation_id)
        else:
            conv = Conversation(user_id=user_id)
            session.add(conv)
            session.commit()

        _persist_message(session, conv, key, "user", {"text": request.question}, None)

        answer: Answer | None = None
        usage = Usage()
        try:
            for event in answer_question_stream(session, request.question, version=request.version):
                if isinstance(event, StreamStage):
                    yield _sse({"type": "stage", "stage": event.stage})
                elif isinstance(event, StreamToken):
                    yield _sse({"type": "token", "text": event.text})
                elif isinstance(event, StreamResult):
                    answer = event.answer
                    usage = event.usage
        except Exception:  # noqa: BLE001 - surface a clean error event, keep the stream valid
            yield _sse({"type": "error", "detail": "generation failed"})
            return

        if answer is None:  # pragma: no cover - the stream always yields a result
            yield _sse({"type": "error", "detail": "no answer produced"})
            return

        payload = {
            "text": answer.text,
            "citations": _citation_dicts(answer),
            "abstained": answer.abstained,
            "grounded": answer.grounded,
        }
        _persist_message(session, conv, key, "assistant", payload, usage)

        total = _total_tokens(_messages(session, conv.id))
        yield _sse(
            {
                "type": "done",
                "conversation_id": str(conv.id),
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
def chat_stream(request: ChatStreamRequest, user: CurrentUserDep) -> StreamingResponse:
    # Read the wrapped key now, while the request session is still alive; the SSE
    # generator then works from plain bytes on its own session.
    if user.key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no data key")
    return StreamingResponse(
        _chat_events(user.id, user.key.wrapped_key, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- conversation CRUD ------------------------------------------------------


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(session: SessionDep, user: CurrentUserDep) -> list[ConversationOut]:
    key = _user_key(user)
    conversations = list(
        session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.created_at.desc())
        )
    )
    out: list[ConversationOut] = []
    for conv in conversations:
        messages = _messages(session, conv.id)
        out.append(
            ConversationOut(
                id=str(conv.id),
                title=_title_for(key, conv, messages),
                created_at=conv.created_at,
                total_tokens=_total_tokens(messages),
            )
        )
    return out


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUserDep
) -> ConversationDetailOut:
    key = _user_key(user)
    conv = _get_owned_conversation(session, user.id, conversation_id)
    messages = _messages(session, conv.id)
    out_messages: list[MessageOut] = []
    for m in messages:
        data = _decrypt_json(key, m.content_encrypted)
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
            )
        )
    return ConversationDetailOut(
        id=str(conv.id), title=_title_for(key, conv, messages), messages=out_messages
    )


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
