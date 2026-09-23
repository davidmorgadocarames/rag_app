"""FastAPI application factory and routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag_app import __version__
from rag_app.api import auth, conversations
from rag_app.api.deps import AnswerFn, SessionDep, get_answerer, rate_limit_chat
from rag_app.api.schemas import ChatRequest, ChatResponse, CitationOut, HealthResponse
from rag_app.config import get_settings

AnswererDep = Annotated[AnswerFn, Depends(get_answerer)]


def create_app() -> FastAPI:
    app = FastAPI(title="SecRAG API", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[get_settings().frontend_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(conversations.router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(rate_limit_chat)])
    def chat(
        request: ChatRequest,
        session: SessionDep,
        answerer: AnswererDep,
    ) -> ChatResponse:
        answer = answerer(session, request.question, request.version)
        return ChatResponse(
            answer=answer.text,
            abstained=answer.abstained,
            grounded=answer.grounded,
            citations=[
                CitationOut(
                    marker=c.marker,
                    chunk_uid=c.chunk_uid,
                    heading=c.heading,
                    version=c.version,
                    effective_date=c.effective_date,
                )
                for c in answer.citations
            ],
        )

    return app


app = create_app()
