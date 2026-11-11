"""Password policy (T12.4.3, P12-D4, OWASP-A07 F10): length, email-local-part, and the
NCSC/SecLists breached-password denylist. See rag_app/password_policy.py for the licence
note on the bundled list."""

from __future__ import annotations

import gzip
import hashlib
from importlib import resources

from rag_app.password_policy import (
    _LIST_PACKAGE,
    _LIST_RESOURCE,
    LIST_SHA256,
    MAX_LENGTH,
    MIN_LENGTH,
    denylist,
    email_local_part,
    validate_password,
)


def test_the_bundled_list_matches_its_pinned_sha256() -> None:
    """Fails loudly if the bundled .gz file ever drifts from what this module was reviewed
    against (a corrupted copy, an accidental re-download, a merge mistake)."""
    raw = resources.files(_LIST_PACKAGE).joinpath(_LIST_RESOURCE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LIST_SHA256


def test_the_list_decompresses_to_a_large_lowercase_set() -> None:
    entries = denylist()
    assert len(entries) > 50_000  # the "100k" list, after de-duplication
    assert "123456" in entries  # the best-known example, present in every mirror
    assert all(entry == entry.lower() for entry in entries)


def test_the_list_is_not_bloated() -> None:
    """Done-when: "must not bloat images unnecessarily" -- the gzip copy bundled in the
    image stays well under a megabyte (it measured ~420 KB when added), negligible next to
    the multi-GB reranker snapshot already baked into this image (T11.4.2)."""
    raw = resources.files(_LIST_PACKAGE).joinpath(_LIST_RESOURCE).read_bytes()
    assert len(raw) < 1_000_000
    assert len(gzip.decompress(raw)) < 2_000_000


def test_email_local_part() -> None:
    assert email_local_part("Bob.Smith@Example.com") == "bob.smith"
    assert email_local_part("no-at-sign") == "no-at-sign"


def test_too_short_is_rejected() -> None:
    problems = validate_password("short1A!", "someone@example.com")
    assert any("at least" in p for p in problems)


def test_too_long_is_rejected() -> None:
    problems = validate_password("x" * (MAX_LENGTH + 1), "someone@example.com")
    assert any("at most" in p for p in problems)


def test_exactly_at_the_floor_and_ceiling_are_accepted_on_length_alone() -> None:
    # A value that clears the length bound, contains no part of the email, and is not in
    # the denylist -- isolates the length check from the other two.
    floor = "zQ9!kx7Lm2Pv"
    assert len(floor) == MIN_LENGTH
    assert validate_password(floor, "someone@example.com") == []
    ceiling = ("zQ9!kx7Lm2Pv" * (MAX_LENGTH // 12 + 1))[:MAX_LENGTH]
    assert validate_password(ceiling, "someone@example.com") == []


def test_password_containing_the_email_local_part_is_rejected() -> None:
    problems = validate_password("BobSmith2026!", "bobsmith@example.com")
    assert any("email address" in p for p in problems)


def test_a_short_local_part_is_not_a_false_positive_trigger() -> None:
    """A 1-2 character local part is too common a substring of ordinary passwords to be a
    useful signal -- the check only applies from 3 characters."""
    assert validate_password("myStrongPass99", "ab@example.com") == []


def test_a_common_breached_password_is_rejected() -> None:
    problems = validate_password("Password123456", "someone@example.com")
    assert any("commonly used or breached" in p for p in problems)


def test_a_strong_unlisted_password_passes_every_check() -> None:
    assert validate_password("Zq9#mK2vLpXr7T", "someone@example.com") == []


def test_the_denylist_check_is_case_insensitive() -> None:
    assert "password123456" in denylist() or "Password123456".lower() in denylist()
    problems = validate_password("PASSWORD123456", "someone@example.com")
    assert any("commonly used or breached" in p for p in problems)
