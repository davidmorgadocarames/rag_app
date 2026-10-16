"""Signed client IP (T12.1.6, T5/V7, PHASE_PLANNING "Client IP").

Once the BFF is in front (12a-D, T12.1.7), the backend only ever sees the frontend's
in-environment connection address (Azure: a shared internal address, F4) -- rate limiting and
abuse tracking keyed by ``request.client.host`` would then bucket every user together. The
BFF instead signs the real client address it derived from ``X-Forwarded-For`` and sends it as
``X-SecRAG-Client-IP``: ``client_ip|timestamp|hex_hmac`` where
``hex_hmac = HMAC-SHA256(key, f"{client_ip}|{method}|{path}|{timestamp}")``, binding the
signature to the exact request it travelled with and to a 30 s freshness window (replay of an
old header on a different route, or after the window, fails).

``CLIENT_IP_HMAC_KEYS`` (comma-separated, current key first) lets a key rotate: the previous
key stays verifiable for one rotation. An empty setting means the feature has not been
adopted yet (12a-C/D): callers should treat that as "nothing to verify", not as a security
event -- see ``rag_app.api.deps._client_ip``.
"""

from __future__ import annotations

import hashlib
import hmac
import time

CLIENT_IP_HEADER = "X-SecRAG-Client-IP"
SIGNATURE_WINDOW_SECONDS = 30


def _signature(key: str, client_ip: str, method: str, path: str, timestamp: int) -> str:
    message = f"{client_ip}|{method}|{path}|{timestamp}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def sign_client_ip(
    key: str, client_ip: str, method: str, path: str, *, now: int | None = None
) -> str:
    """Build the header value. The real signer is the BFF (TypeScript,
    ``frontend/lib/clientIp.ts``) -- this is the reference implementation tests on both sides
    are checked against, so the wire format cannot silently drift between them."""
    timestamp = now if now is not None else int(time.time())
    signature = _signature(key, client_ip, method, path, timestamp)
    return f"{client_ip}|{timestamp}|{signature}"


def verify_client_ip_header(
    header_value: str, keys: list[str], method: str, path: str, *, now: int | None = None
) -> str | None:
    """The verified client IP, or ``None`` when the header is malformed, stale (outside the
    30 s window) or signed by none of ``keys``."""
    try:
        client_ip, ts_text, signature = header_value.split("|", 2)
        timestamp = int(ts_text)
    except ValueError:
        return None
    moment = now if now is not None else int(time.time())
    if abs(moment - timestamp) > SIGNATURE_WINDOW_SECONDS:
        return None
    for key in keys:
        if not key:
            continue
        expected = _signature(key, client_ip, method, path, timestamp)
        if hmac.compare_digest(expected, signature):
            return client_ip
    return None
