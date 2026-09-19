"""FastAPI application factory and routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI

from rag_app import __version__
from rag_app.api import auth
from rag_app.api.deps import AnswerFn, SessionDep, get_answerer, rate_limit_chat
from rag_app.api.schemas import ChatRequest, ChatResponse, CitationOut, HealthResponse

AnswererDep = Annotated[AnswerFn, Depends(get_answerer)]


def create_app() -> FastAPI:
    app = FastAPI(title="SecRAG API", version=__version__)
    app.include_router(auth.router)

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
