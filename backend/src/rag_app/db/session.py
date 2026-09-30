"""Engine and session factory built from application settings."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from rag_app.config import get_job_settings


def make_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine from settings (or an explicit URL).

    Only the database URL is read (``JobSettings``), so Jobs build engines without the
    API's secrets (T11.2.2).

    ``hide_parameters``: a database error (e.g. a unique violation on ``users.email``) never
    renders the bound values into its message, so tracebacks in the logs carry no e-mail
    address, token hash or ciphertext (T11.2.15).
    """
    url = database_url or get_job_settings().database_url
    return create_engine(url, future=True, hide_parameters=True)


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Create a session factory bound to an engine."""
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False)
