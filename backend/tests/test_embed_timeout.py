"""Configurable first-embed wait (T11.6b.6, block H, 11b).

The 2026-10-06 cold-start smoke failed at 183s: the Ollama image pull (130s for the
pre-block-H fat image) outlasted a hard-coded 120s embed timeout in embeddings.py, so the
backend answered HTTP 500 before Ollama ever finished waking up. These tests prove the
timeout is now a `Settings` field (`EMBED_TIMEOUT_SECONDS`, default 170), actually used by
`OllamaEmbedder.embed()`, validated fail-closed, and shared (not duplicated) by both
`/chat` and `/chat/stream` through the one retrieval path.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet

from rag_app import embeddings as embeddings_module
from rag_app.config import Settings, SettingsValidationError, validate_api_settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://u:p@127.0.0.1:15432/embed_timeout_unit",
        "jwt_secret": "j" * 32,
        "data_master_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_default_embed_timeout_is_170_seconds() -> None:
    assert _settings().embed_timeout_seconds == 170


def test_validate_api_settings_rejects_non_positive_embed_timeout() -> None:
    validate_api_settings(_settings(embed_timeout_seconds=1))  # smallest valid value: ok
    with pytest.raises(SettingsValidationError, match="EMBED_TIMEOUT_SECONDS must be positive"):
        validate_api_settings(_settings(embed_timeout_seconds=0))
    with pytest.raises(SettingsValidationError, match="EMBED_TIMEOUT_SECONDS must be positive"):
        validate_api_settings(_settings(embed_timeout_seconds=-1))


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"embeddings": [[0.1, 0.2]]}


def test_embed_uses_the_configured_timeout_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red on the old code: the hard-coded module constant ignored EMBED_TIMEOUT_SECONDS."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> _FakeResponse:
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(embeddings_module.httpx, "post", fake_post)
    monkeypatch.setenv("EMBED_TIMEOUT_SECONDS", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    embedder = embeddings_module.OllamaEmbedder()
    embedder.embed(["hello"])

    assert captured["timeout"] == 42


def test_an_explicit_timeout_overrides_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> _FakeResponse:
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(embeddings_module.httpx, "post", fake_post)
    monkeypatch.setenv("EMBED_TIMEOUT_SECONDS", "42")

    embedder = embeddings_module.OllamaEmbedder(timeout=7)
    embedder.embed(["hello"])

    assert captured["timeout"] == 7


def test_chat_and_chat_stream_share_the_same_embedder_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither answer path may build its own `OllamaEmbedder` with a timeout override —
    both reach embeddings only through `rag_app.retrieval`, which never overrides it."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    retrieval_src = (repo_root / "backend" / "src" / "rag_app" / "retrieval.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(retrieval_src)
    found_default_construction = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "OllamaEmbedder"
        ):
            assert not any(kw.arg == "timeout" for kw in node.keywords), (
                "retrieval.py must not override OllamaEmbedder's timeout "
                "(both /chat and /chat/stream must share EMBED_TIMEOUT_SECONDS)"
            )
            found_default_construction = True
    assert found_default_construction, "retrieval.py must construct OllamaEmbedder()"

    # No endpoint module (api/*.py) constructs its own OllamaEmbedder either.
    api_dir = repo_root / "backend" / "src" / "rag_app" / "api"
    for path in api_dir.glob("*.py"):
        assert "OllamaEmbedder(" not in path.read_text(encoding="utf-8"), (
            f"{path.name} must not build its own OllamaEmbedder "
            "(both chat paths go through retrieval.py)"
        )
