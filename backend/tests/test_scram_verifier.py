"""Client-side SCRAM-SHA-256 verifiers for role passwords (DA-C-3).

scripts/db/scram_verifier.pl is checked against an independent Python implementation of
RFC 5802 / RFC 7677 (hashlib.pbkdf2_hmac + HMAC). The end-to-end part (login works, the SQL
sent holds no plaintext) is in test_db_roles.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRAM = REPO_ROOT / "scripts" / "db" / "scram_verifier.pl"
VERIFIER = re.compile(
    r"^SCRAM-SHA-256\$(?P<i>\d+):(?P<salt>[A-Za-z0-9+/=]+)"
    r"\$(?P<stored>[A-Za-z0-9+/=]+):(?P<server>[A-Za-z0-9+/=]+)$"
)

pytestmark = pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl")


def _verifier(value: str | None, name: str = "TEST_ROLE_PASSWORD") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != name}
    if value is not None:
        env[name] = value
    return subprocess.run(
        ["perl", str(SCRAM), name], env=env, capture_output=True, text=True, timeout=30
    )


def _expected(password: str, salt: bytes, iterations: int) -> tuple[str, str]:
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client_key).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return base64.b64encode(stored).decode(), base64.b64encode(server).decode()


@pytest.mark.parametrize("password", ["ci-throwaway-purger", "p@ss w0rd!$'\"\\`~", "x"])
def test_verifier_matches_rfc_5802(password: str) -> None:
    proc = _verifier(password)
    assert proc.returncode == 0, proc.stderr
    match = VERIFIER.match(proc.stdout.strip())
    assert match, proc.stdout
    salt = base64.b64decode(match["salt"])
    assert int(match["i"]) == 4096 and len(salt) == 16
    assert (match["stored"], match["server"]) == _expected(password, salt, 4096)
    if len(password) >= 8:  # a 1-char password can occur in base64 by chance
        assert password not in proc.stdout


def test_every_verifier_has_a_fresh_salt() -> None:
    one, two = (_verifier("same-password").stdout for _ in range(2))
    assert one != two


@pytest.mark.parametrize("value", [None, "", "contraseña", "tab\there"])
def test_missing_or_non_ascii_passwords_are_refused(value: str | None) -> None:
    proc = _verifier(value)
    assert proc.returncode != 0 and proc.stdout == ""


@pytest.mark.parametrize("arg", ["not a name", "1ABC", "PW=secret"])
def test_the_argument_is_a_variable_name_never_the_password(arg: str) -> None:
    proc = subprocess.run(["perl", str(SCRAM), arg], capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and "usage" in proc.stderr


def test_apply_roles_sends_only_the_verifier() -> None:
    """The SQL template of apply_roles.sh references the verifier, never the password."""
    text = (REPO_ROOT / "scripts" / "db" / "apply_roles.sh").read_text(encoding="utf-8")
    assert "\\\\getenv role_verifier SECRAG_ROLE_VERIFIER" in text
    assert "PASSWORD :'role_verifier'" in text
    assert "role_password" not in text
    assert 'perl "$here/scram_verifier.pl" "$var"' in text
