"""FastAPI dependencies (injectable so endpoints are testable without a live stack)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from rag_app.db.session import make_session_factory
from rag_app.generation import Answer, answer_question

AnswerFn = Callable[[Session, str, str | None], Answer]

_session_factory = None


def get_session() -> Iterator[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    with _session_factory() as session:
        yield session


def get_answerer() -> AnswerFn:
    def _answer(session: Session, question: str, version: str | None) -> Answer:
        return answer_question(session, question, version=version)

    return _answer


SessionDep = Annotated[Session, Depends(get_session)]
