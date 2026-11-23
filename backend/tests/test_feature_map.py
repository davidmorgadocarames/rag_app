"""Tests for the generated feature map (T13.17.1/T13.17.2, Harness-B).

``docs/FEATURE_MAP.md`` is GENERATED from ``rag_app.features.generate_feature_map()`` (which
itself renders ``REGISTRY`` + ``FEATURE_DOCS``, both in ``rag_app.features``). These tests fail
the build the moment the committed file drifts from the generator -- the actual Done-when of
T13.17.2 -- and prove the comparison is not vacuously true with an explicit negative control
(DA-13-E-1-style discipline: a check that can never fail is not a check).
"""

from __future__ import annotations

from pathlib import Path

from rag_app.features import FEATURE_DOCS, REGISTRY, generate_feature_map

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FEATURE_MAP_PATH = _REPO_ROOT / "docs" / "FEATURE_MAP.md"


def test_feature_map_file_matches_the_generator_byte_for_byte() -> None:
    """The actual Done-when of T13.17.2: the committed ``docs/FEATURE_MAP.md`` must be
    EXACTLY what ``generate_feature_map()`` produces from today's registry -- a stale file
    (someone hand-edited it, or added a registry entry without regenerating) fails here."""
    on_disk = _FEATURE_MAP_PATH.read_text(encoding="utf-8")
    regenerated = generate_feature_map()
    assert on_disk == regenerated, (
        "docs/FEATURE_MAP.md is stale vs rag_app.features.generate_feature_map() -- "
        "regenerate it: python -m rag_app.features map --write docs/FEATURE_MAP.md"
    )


def test_negative_control_a_stale_file_is_actually_caught() -> None:
    """Proves the comparison above is a REAL check, not a vacuous one: a mutated copy of the
    real file (one appended character) must NOT compare equal to the generator's output --
    if it did, the test above could never fail no matter how stale the real file became."""
    on_disk = _FEATURE_MAP_PATH.read_text(encoding="utf-8")
    mutated = on_disk + "x"
    regenerated = generate_feature_map()
    assert mutated != regenerated


def test_negative_control_a_new_registry_entry_without_a_doc_changes_the_output() -> None:
    """A second, independent negative control: regenerating against a REGISTRY-shaped tuple
    that has one more entry than FEATURE_DOCS covers must raise (KeyError) rather than
    silently rendering an incomplete map -- the same failure mode a real "added a feature,
    forgot FEATURE_DOCS" mistake would hit."""
    from dataclasses import replace

    extra_entry = replace(REGISTRY[0], id="test-only-not-in-feature-docs")
    try:
        generate_feature_map(entries=(*REGISTRY, extra_entry))
    except KeyError as exc:
        assert "test-only-not-in-feature-docs" in str(exc)
    else:
        raise AssertionError("expected a KeyError for an entry missing from FEATURE_DOCS")


def test_every_registry_id_has_a_feature_doc() -> None:
    """Completeness, the other direction from the KeyError control above: every REAL entry
    in the registry today has a FEATURE_DOCS row -- a removed/renamed entry would otherwise
    leave a stale, unreachable FEATURE_DOCS key behind unnoticed."""
    registry_ids = {entry.id for entry in REGISTRY}
    doc_ids = set(FEATURE_DOCS)
    assert registry_ids == doc_ids, (
        f"missing FEATURE_DOCS: {sorted(registry_ids - doc_ids)}; "
        f"stale FEATURE_DOCS (no such registry id): {sorted(doc_ids - registry_ids)}"
    )


def test_coverage_numbers_match_what_the_report_quotes() -> None:
    """Pins the two numbers the Harness-B report quotes (N features, M without e2e) to the
    real registry, so a future registry change that silently shifts them is caught here
    instead of only in a hand-typed report."""
    total = len(REGISTRY)
    without_e2e = len([entry for entry in REGISTRY if not FEATURE_DOCS[entry.id].e2e])
    assert total == 39
    assert without_e2e == 23


def test_generate_feature_map_is_deterministic() -> None:
    """Same input -> byte-identical output, twice in a row (no hidden timestamp/ordering
    non-determinism) -- required for the byte-compare test above to ever be meaningful."""
    assert generate_feature_map() == generate_feature_map()


def test_every_e2e_spec_named_in_the_map_file_actually_exists() -> None:
    """Catches a typo'd/renamed spec filename in FEATURE_DOCS before it ever reaches
    scripts/dev/verify.sh (T13.17.3), which trusts this list to find the covering spec."""
    e2e_dir = _REPO_ROOT / "frontend" / "e2e"
    named_specs = {spec for doc in FEATURE_DOCS.values() for spec in doc.e2e}
    for spec in sorted(named_specs):
        assert (e2e_dir / spec).is_file(), f"{spec} named in FEATURE_DOCS but not on disk"
