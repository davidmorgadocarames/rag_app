"""ENV setting and the fail-closed start-up guard for development-only flags (T11.0.10)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rag_app import config
from rag_app.api.app import create_app
from rag_app.config import DevOnlyFlagInProdError, Settings, check_dev_only_flags


class _SettingsWithDummyFlag(Settings):
    """Settings plus a stand-in for a future dev-only feature (fake LLM, lab, …)."""

    dummy_dev_feature: bool = False


@pytest.fixture(autouse=True)
def _no_env_from_outside(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests decide ENV themselves; the developer's shell or .env must not leak in.
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("DUMMY_DEV_FEATURE", raising=False)


def test_env_defaults_to_prod_without_env_or_env_file() -> None:
    assert Settings(_env_file=None).env == "prod"


def test_env_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    assert Settings(_env_file=None).env == "dev"


def test_unknown_env_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dummy_dev_flag_with_prod_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUMMY_DEV_FEATURE", "true")
    settings = _SettingsWithDummyFlag(_env_file=None)
    assert settings.env == "prod"
    with pytest.raises(DevOnlyFlagInProdError, match="dummy_dev_feature"):
        check_dev_only_flags(settings, ["dummy_dev_feature"])


def test_dummy_dev_flag_is_allowed_with_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("DUMMY_DEV_FEATURE", "true")
    check_dev_only_flags(_SettingsWithDummyFlag(_env_file=None), ["dummy_dev_feature"])


def test_prod_without_dev_flags_starts() -> None:
    check_dev_only_flags(_SettingsWithDummyFlag(_env_file=None), ["dummy_dev_feature"])


def test_api_lifespan_refuses_to_start_with_a_dev_flag_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard runs at API start-up: the app never serves with a dev-only flag in prod."""
    monkeypatch.setenv("DUMMY_DEV_FEATURE", "true")
    monkeypatch.setattr(config, "DEV_ONLY_FLAGS", ["dummy_dev_feature"])
    monkeypatch.setattr(
        "rag_app.api.app.get_settings",
        lambda: _SettingsWithDummyFlag(_env_file=None),
    )
    with pytest.raises(DevOnlyFlagInProdError), TestClient(create_app()):
        pass


def test_api_lifespan_starts_with_the_real_registry() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
