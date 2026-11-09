"""TOTP admin MFA (T12.3.4/T12.3.5): secret generation/verification (RFC 6238, via
``pyotp``) and single-use recovery codes. No network call, no QR rendering here -- the
frontend renders the QR client-side from the ``otpauth://`` provisioning URI this module
builds (the secret itself never needs to touch a third-party service).

The TOTP secret is encrypted at rest with the master key (``rag_app.crypto.wrap_key`` /
``unwrap_key`` -- the SAME wrapping as a user's data key) before it is ever written to
``user_mfa.secret_encrypted``; it is never logged.
"""

from __future__ import annotations

import secrets

import pyotp

from rag_app.security import hash_token

ISSUER = "SecRAG"

# Recovery codes: readable-but-not-ambiguous alphabet (no 0/O, 1/I/l) grouped for typing,
# e.g. "7KPQ-9XRT-4MNB". 10 bytes of entropy per code (comfortably more than a TOTP code).
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECOVERY_CODE_COUNT = 10
_RECOVERY_GROUP_LEN = 4
_RECOVERY_GROUPS = 3


def generate_totp_secret() -> str:
    """A fresh base32 TOTP secret (``pyotp.random_base32``, 160 bits)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """``otpauth://totp/...`` URI for a client-side QR code (``pyotp``'s own builder --
    never hand-rolled string formatting, so the encoding of the email/issuer is correct)."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify_totp_code(secret: str, code: str) -> bool:
    """A code valid for the current 30 s step, with ``valid_window=1`` (one step either
    side) -- the usual small clock-skew tolerance; still well inside the code's own 30-90 s
    life, nothing like the request-signature windows elsewhere in this codebase."""
    code = code.strip()
    if not code.isdigit():
        return False
    return pyotp.totp.TOTP(secret).verify(code, valid_window=1)


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """``count`` fresh plaintext recovery codes, grouped for readability. Caller is
    responsible for hashing them (``hash_recovery_code``) before storage and showing the
    plaintext to the user exactly once -- never logged, never stored."""
    codes = []
    length = _RECOVERY_GROUP_LEN * _RECOVERY_GROUPS
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(length))
        groups = [raw[i : i + _RECOVERY_GROUP_LEN] for i in range(0, len(raw), _RECOVERY_GROUP_LEN)]
        codes.append("-".join(groups))
    return codes


def hash_recovery_code(code: str) -> str:
    """SHA-256 of the code, normalised (upper-case, dashes stripped) so a user retyping it
    with different casing/spacing still matches -- same convention as every other token in
    this codebase (``rag_app.security.hash_token``)."""
    return hash_token(code.strip().upper().replace("-", ""))
