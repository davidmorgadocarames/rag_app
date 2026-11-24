"""Structural check for every ``sealed-secrets.yaml`` under ``deploy/k8s`` (DEC-055, DA-16-B GO-
with-edits "structural-check half of D"): every YAML document in such a file must be a
``bitnami.com/v1alpha1`` ``SealedSecret`` with only ``spec.encryptedData`` populated (no
``stringData``, no plain ``spec.data``), and the ``template`` (if present) carries no
``stringData``/``data`` either. Also asserts no plain ``kind: Secret`` with a ``data``/
``stringData`` block exists anywhere under ``deploy/k8s`` -- a real secret value landing there
(a merge mistake, a wrong ``kubeseal`` invocation, a future edit to the wrong file) would be a
real leak even if gitleaks' own heuristic ever missed it or were disabled. This is a
deterministic YAML-shape assertion, independent of the DEC-055 gitleaks-allowlist decision
(status/blocks/DA-16-B.md): it is needed whatever the user picks for that.

Wired into the ``pytest`` gate step (no ``db`` marker -- runs under ``--fast`` and CI, no
cluster/kustomize needed, pure YAML parsing)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_ROOT = REPO_ROOT / "deploy" / "k8s"

SEALED_SECRET_FILES = sorted(K8S_ROOT.glob("**/sealed-secrets.yaml"))
ALL_YAML_FILES = sorted(K8S_ROOT.glob("**/*.yaml")) + sorted(K8S_ROOT.glob("**/*.yml"))


def _load_documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def test_at_least_one_sealed_secrets_file_exists() -> None:
    """Guards against the glob silently matching nothing (e.g. a rename) and every test below
    passing vacuously."""
    assert SEALED_SECRET_FILES, f"no sealed-secrets.yaml found under {K8S_ROOT}"


@pytest.mark.parametrize("path", SEALED_SECRET_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_sealed_secrets_file_is_sealedsecret_only(path: Path) -> None:
    docs = _load_documents(path)
    assert docs, f"{path} has no YAML documents"
    for doc in docs:
        assert doc.get("apiVersion") == "bitnami.com/v1alpha1", (
            f"{path}: expected apiVersion bitnami.com/v1alpha1, got {doc.get('apiVersion')!r} "
            f"(name={doc.get('metadata', {}).get('name')!r})"
        )
        assert doc.get("kind") == "SealedSecret", (
            f"{path}: expected kind SealedSecret, got {doc.get('kind')!r} "
            f"(name={doc.get('metadata', {}).get('name')!r}) -- a plain Secret must never live "
            "in a sealed-secrets.yaml file"
        )
        spec = doc.get("spec", {})
        assert "encryptedData" in spec, f"{path}: SealedSecret spec has no encryptedData"
        assert "stringData" not in spec, f"{path}: SealedSecret spec must not carry stringData"
        assert "data" not in spec, f"{path}: SealedSecret spec must not carry plain data"
        template = spec.get("template", {})
        assert (
            "stringData" not in template
        ), f"{path}: SealedSecret template must not carry stringData"
        assert "data" not in template, f"{path}: SealedSecret template must not carry plain data"


@pytest.mark.parametrize("path", ALL_YAML_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_plain_secret_with_data_anywhere_under_deploy_k8s(path: Path) -> None:
    """Even outside a sealed-secrets.yaml file: a bare `kind: Secret` carrying a `data` or
    `stringData` block would be a real, unsealed secret committed in git."""
    docs = _load_documents(path)
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        assert "data" not in doc and "stringData" not in doc, (
            f"{path}: plain kind: Secret with data/stringData found "
            f"(name={doc.get('metadata', {}).get('name')!r}) -- secrets must be SealedSecret "
            "(local overlay) or ExternalSecret (aks overlay), never a plain Secret, in git"
        )
