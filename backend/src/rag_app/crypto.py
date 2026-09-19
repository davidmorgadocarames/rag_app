"""Per-user encryption for GDPR crypto-shredding.

Each user has a data key (Fernet) that encrypts their content. The data key is stored
wrapped by a master key (from settings). Deleting the wrapped key makes the user's
ciphertext unrecoverable; combined with hard deletion (see rag_app.erasure) this gives
defense-in-depth erasure that also covers ciphertext lingering in backups.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from rag_app.config import get_settings


def _master() -> Fernet:
    return Fernet(get_settings().data_master_key.encode())


def generate_user_key() -> bytes:
    """Generate a new per-user data key (Fernet key)."""
    return Fernet.generate_key()


def wrap_key(user_key: bytes) -> bytes:
    """Encrypt a per-user key with the master key for storage."""
    return _master().encrypt(user_key)


def unwrap_key(wrapped_key: bytes) -> bytes:
    """Decrypt a stored per-user key with the master key."""
    return _master().decrypt(wrapped_key)


def encrypt(user_key: bytes, plaintext: str) -> bytes:
    return Fernet(user_key).encrypt(plaintext.encode())


def decrypt(user_key: bytes, token: bytes) -> str:
    return Fernet(user_key).decrypt(token).decode()
