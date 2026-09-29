"""ENV setting and the fail-closed start-up guard for development-only flags (T11.0.10);
the registry is derived from field metadata, with a name-based backstop (DA-C-5)."""

from __future__ import annotations

from typing import Annotated, Optional

import pytest
from fastapi.testclient import TestClient
from pydantic import StrictBool, ValidationError

from rag_app.api.app import create_app
from rag_app.config import (
    DEV_ONLY_MARK,
    DEV_ONLY_NAME,
    DevOnlyFlagInProdError,
    Settings,
    check_dev_only_flags,
    dev_only_flag,
    dev_only_flags,
)


class _SettingsWithDummyFlag(Settings):
    """Settings plus a stand-in for a future dev-only feature (fake LLM, lab, …). The name
    does not look dev-only, so only the declaration makes it one."""

    sandbox_feature: bool = dev_only_flag("stand-in for a future development-only feature")


class _SettingsWithUndeclaredFlag(Settings):
    """A future dev flag whose author forgot `dev_only_flag`."""

    fake_llm: bool = False


@pytest.fixture(autouse=True)
def _no_env_from_outside(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests decide ENV themselves; the developer's shell or .env must not leak in.
    for name in ("ENV", "SANDBOX_FEATURE", "FAKE_LLM", "DEFENCE_LAB"):
        monkeypatch.delenv(name, raising=False)


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
    monkeypatch.setenv("SANDBOX_FEATURE", "true")
    settings = _SettingsWithDummyFlag(_env_file=None)
    assert settings.env == "prod"
    with pytest.raises(DevOnlyFlagInProdError, match="sandbox_feature"):
        check_dev_only_flags(settings)  # registry derived from the declaration


def test_dummy_dev_flag_is_allowed_with_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SANDBOX_FEATURE", "true")
    check_dev_only_flags(_SettingsWithDummyFlag(_env_file=None))


def test_prod_without_dev_flags_starts() -> None:
    check_dev_only_flags(_SettingsWithDummyFlag(_env_file=None))


def test_the_registry_is_derived_from_the_field_declarations() -> None:
    assert dev_only_flags(_SettingsWithDummyFlag) == ["sandbox_feature"]
    assert dev_only_flags(Settings) == []  # none has landed yet (Phases 13/16/19/20)


def test_an_undeclared_flag_with_a_dev_only_name_is_still_refused_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backstop: forgetting `dev_only_flag` on e.g. `fake_llm` cannot open it in prod."""
    monkeypatch.setenv("FAKE_LLM", "true")
    assert dev_only_flags(_SettingsWithUndeclaredFlag) == ["fake_llm"]
    with pytest.raises(DevOnlyFlagInProdError, match="fake_llm"):
        check_dev_only_flags(_SettingsWithUndeclaredFlag(_env_file=None))


class _SettingsWithWrappedBools(Settings):
    """DA-C2-4: undeclared dev-named flags typed as bool wrappers, plus non-bool look-alikes."""

    fake_llm: bool | None = None
    defence_lab: Optional[bool] = None  # noqa: UP007 - the typing spelling is the point
    data_explorer: StrictBool = False
    debug_endpoints: Annotated[bool | None, "doc"] = None
    demo_label: str = "x"  # dev-looking name, but not a switch
    mock_count: int | None = None


def test_bool_wrappers_with_a_dev_only_name_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dev_only_flags(_SettingsWithWrappedBools) == [
        "data_explorer",
        "debug_endpoints",
        "defence_lab",
        "fake_llm",
    ]
    monkeypatch.setenv("DEFENCE_LAB", "true")
    with pytest.raises(DevOnlyFlagInProdError, match="defence_lab"):
        check_dev_only_flags(_SettingsWithWrappedBools(_env_file=None))


@pytest.mark.parametrize(
    ("name", "looks_dev_only"),
    [
        ("fake_llm", True),
        ("defence_lab", True),
        ("data_explorer", True),
        ("code_fix_enabled", True),
        ("debug_endpoints", True),
        ("mock_embeddings", True),
        ("require_email_verification", False),
        ("device_name", False),
        ("develop", False),
    ],
)
def test_dev_only_name_pattern(name: str, looks_dev_only: bool) -> None:
    assert (DEV_ONLY_NAME.search(name) is not None) is looks_dev_only


def test_every_settings_field_that_looks_dev_only_is_declared_dev_only() -> None:
    """Fails when a field NAMED like a dev feature is added without `dev_only_flag`, and when
    a declared one is not a bool that defaults to off."""
    for name, field in Settings.model_fields.items():
        extra = field.json_schema_extra
        declared = isinstance(extra, dict) and extra.get(DEV_ONLY_MARK) is True
        if DEV_ONLY_NAME.search(name):
            assert declared, f"{name} looks development-only: declare it with dev_only_flag()"
        if declared:
            assert field.annotation is bool and field.default is False, name


def test_api_lifespan_refuses_to_start_with_a_dev_flag_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard runs at API start-up: the app never serves with a dev-only flag in prod."""
    monkeypatch.setenv("SANDBOX_FEATURE", "true")
    monkeypatch.setattr(
        "rag_app.api.app.get_settings",
        lambda: _SettingsWithDummyFlag(_env_file=None),
    )
    with pytest.raises(DevOnlyFlagInProdError), TestClient(create_app()):
        pass


def test_api_lifespan_starts_with_the_real_registry() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
