"""Password policy (T12.4.3, P12-D4): NIST 800-63B length rules plus a check against the
NCSC's published list of the 100,000 most commonly seen/breached passwords, plus "must not
contain the account's own email local part" (the single most common weak-password habit).

**The list + its licence (T12.4.3 Done-when).** The NCSC's own "Passwords, passwords
everywhere" analysis (built with Troy Hunt / Have I Been Pwned data) is the canonical
source, but `ncsc.gov.uk`'s copyright/licence footer is rendered client-side -- this agent
could not fetch a crawlable, citable redistribution licence statement directly from the
site on 2026-11-11 (checked the blog post and the homepage; no "Open Government Licence" /
"Crown copyright" text in the static HTML either page served). Per this task's own
documented fallback, the list is bundled from **SecLists**
(github.com/danielmiessler/SecLists, MIT License, Daniel Miessler and contributors),
file `Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt` -- itself sourced
from the NCSC list and named after it -- downloaded 2026-11-11
(sha256 `c58a601af16f63527d46630d70b29345b429a44a2f2ce49b2c1b59fc82abddaa` of the
gzip-compressed copy bundled here, so a later drift in the upstream mirror is a detectable
test failure, not a silent change). See `README.md` "Licences and attribution".

**Size (Done-when: "must not bloat images unnecessarily").** ~816 KB plain / ~420 KB
gzip-compressed, bundled at `backend/src/rag_app/data/password_denylist_ncsc_top100k.txt.gz`
and decompressed lazily into an in-memory ``frozenset`` on first use (cached for the rest of
the process). Negligible next to the multi-GB reranker snapshot already baked into this
image (T11.4.2) -- no extra download/build step, no pulled dependency.
"""

from __future__ import annotations

import gzip
from functools import lru_cache
from importlib import resources

MIN_LENGTH = 12
MAX_LENGTH = 256

_LIST_PACKAGE = "rag_app.data"
_LIST_RESOURCE = "password_denylist_ncsc_top100k.txt.gz"
# The exact sha256 of the bundled .gz file (not the decompressed text): a test
# (test_password_policy.py::test_the_bundled_list_matches_its_pinned_sha256) fails if the
# file on disk ever drifts from what this module was reviewed against.
LIST_SHA256 = "c58a601af16f63527d46630d70b29345b429a44a2f2ce49b2c1b59fc82abddaa"


def _list_bytes() -> bytes:
    return resources.files(_LIST_PACKAGE).joinpath(_LIST_RESOURCE).read_bytes()


@lru_cache(maxsize=1)
def denylist() -> frozenset[str]:
    """The bundled list, decompressed and lower-cased, once per process."""
    text = gzip.decompress(_list_bytes()).decode("utf-8", errors="strict")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def email_local_part(email: str) -> str:
    return email.split("@", 1)[0].lower()


def validate_password(password: str, email: str) -> list[str]:
    """Every problem with ``password`` for an account registering/resetting to ``email``
    (empty list = passes). Never echoes the password itself in a returned message."""
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"password must be at most {MAX_LENGTH} characters")
    local = email_local_part(email)
    # A local part under 3 characters is too common a substring of ordinary passwords to be
    # a useful signal (DA-style false-positive guard); real local parts are rarely this short.
    if len(local) >= 3 and local in password.lower():
        problems.append("password must not contain your email address")
    if password.lower() in denylist():
        problems.append(
            "password is one of the most commonly used or breached passwords -- choose a"
            " different one"
        )
    return problems
