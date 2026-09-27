"""DB tests on a harness-created database (marker ``db``; run in gate --full and CI)."""

from __future__ import annotations

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from db_harness import BACKEND_DIR, HarnessRefusal, assert_harness_database
from rag_app.db.models import EMBEDDING_DIM, Chunk, Document

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "documents",
    "chunks",
    "users",
    "user_keys",
    "conversations",
    "messages",
    "deletion_requests",
    "email_verification_tokens",
}


def _head_revision() -> str:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


def test_upgrade_head_created_the_schema(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        has_vector = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        ).scalar_one()
    assert version == _head_revision()
    assert EXPECTED_TABLES <= tables
    assert has_vector


def test_vector_search_round_trip(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        doc = Document(slug=f"doc-{uuid.uuid4().hex[:8]}", version="2021")
        session.add(doc)
        session.flush()
        dim = EMBEDDING_DIM
        for ordinal, hot in enumerate((0, 1)):
            vector = [0.0] * dim
            vector[hot] = 1.0
            session.add(
                Chunk(
                    document_id=doc.id,
                    chunk_uid=f"{doc.slug}-{ordinal}",
                    heading="h",
                    ordinal=ordinal,
                    text=f"text {ordinal}",
                    embedding=vector,
                    version="2021",
                )
            )
        session.commit()
        query = [0.0] * dim
        query[1] = 1.0
        nearest = session.query(Chunk).order_by(Chunk.embedding.cosine_distance(query)).first()
        assert nearest is not None and nearest.ordinal == 1


def test_a_database_not_created_by_the_harness_is_refused(admin_url: URL) -> None:
    engine = create_engine(admin_url, future=True)
    try:
        with pytest.raises(HarnessRefusal):
            assert_harness_database(engine)
    finally:
        engine.dispose()
