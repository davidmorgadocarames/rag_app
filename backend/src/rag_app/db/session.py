"""Engine and session factory built from application settings."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from rag_app.config import get_settings


def make_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine from settings (or an explicit URL)."""
    url = database_url or get_settings().database_url
    return create_engine(url, future=True)


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Create a session factory bound to an engine."""
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False)
