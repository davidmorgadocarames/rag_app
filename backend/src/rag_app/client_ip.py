"""Signed client IP (T12.1.6, T5/V7, PHASE_PLANNING "Client IP"; window widened to ~300 s and
the fallback reasons split out in T12.3.0a, DEBT-038).

Once the BFF is in front (12a-D, T12.1.7), the backend only ever sees the frontend's
in-environment connection address (Azure: a shared internal address, F4) -- rate limiting and
abuse tracking keyed by ``request.client.host`` would then bucket every user together. The
BFF instead signs the real client address it derived from ``X-Forwarded-For`` and sends it as
``X-SecRAG-Client-IP``: ``client_ip|timestamp|hex_hmac`` where
``hex_hmac = HMAC-SHA256(key, f"{client_ip}|{method}|{path}|{timestamp}")``, binding the
signature to the exact request it travelled with and to a ``SIGNATURE_WINDOW_SECONDS``
freshness window (replay of an old header on a different route, or after the window, fails).

**T12.3.0a (DEBT-038):** the original 30 s window turned out to be tighter than a cold start
(backend wake + the frontend's own cold start can together exceed 30 s before the first
request completes), so a perfectly genuine first request was indistinguishable from a replay
and silently fell back to the shared connection address -- defeating per-account/per-IP
limiting exactly when a demo/interview needed it most. Widened to 300 s: still far too short
for a captured header to be useful to an attacker (no state changes on GET; a 429/403 is the
worst a replayed header buys on a mutating route), comfortably longer than any realistic cold
start. ``classify_client_ip_header`` returns WHICH of the three failure shapes happened
(``forged`` / ``stale``) or ``verified``, so the caller (``rag_app.api.deps._client_ip``) can
tell a cold start (stale) from an actual forged/garbled header (forged) in its metric --
without ever labelling the metric with an IP (log hygiene).

``CLIENT_IP_HMAC_KEYS`` (comma-separated, current key first) lets a key rotate: the previous
key stays verifiable for one rotation. An empty setting means the feature has not been
adopted yet (12a-C/D): callers should treat that as "nothing to verify", not as a security
event -- see ``rag_app.api.deps._client_ip``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Literal

CLIENT_IP_HEADER = "X-SecRAG-Client-IP"
# T12.3.0a (DEBT-038): 30 -> 300. Binding to method+path+timestamp means a wider window never
# allows replay onto a different route or after this many seconds -- it only tolerates a
# slower round trip between the BFF signing the header and the backend verifying it.
SIGNATURE_WINDOW_SECONDS = 300

ClientIpResult = Literal["verified", "stale", "forged"]


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


def classify_client_ip_header(
    header_value: str, keys: list[str], method: str, path: str, *, now: int | None = None
) -> tuple[str | None, ClientIpResult]:
    """``(client_ip, "verified")`` when a key signs exactly this ``client_ip|method|path``
    within the freshness window. Otherwise ``(None, result)``:

    - ``"forged"``: malformed header, or no configured key's signature matches at all (wrong
      key, wrong IP/method/path baked into the signature, or garbage) -- whatever the
      timestamp says, this header was never validly signed for this request.
    - ``"stale"``: a key DOES match (the header really was signed for this exact
      ``client_ip|method|path``), but the timestamp is outside ``SIGNATURE_WINDOW_SECONDS``
      (replay of an old, otherwise-genuine header).

    Checking every key for a match BEFORE looking at the timestamp is what lets a stale-but-
    genuine header be told apart from a forged one -- a forged signature never matches any
    key regardless of how fresh its timestamp claims to be.
    """
    try:
        client_ip, ts_text, signature = header_value.split("|", 2)
        timestamp = int(ts_text)
    except ValueError:
        return None, "forged"
    matched = any(
        key and hmac.compare_digest(_signature(key, client_ip, method, path, timestamp), signature)
        for key in keys
    )
    if not matched:
        return None, "forged"
    moment = now if now is not None else int(time.time())
    if abs(moment - timestamp) > SIGNATURE_WINDOW_SECONDS:
        return None, "stale"
    return client_ip, "verified"


def verify_client_ip_header(
    header_value: str, keys: list[str], method: str, path: str, *, now: int | None = None
) -> str | None:
    """The verified client IP, or ``None`` when the header is malformed, stale (outside
    ``SIGNATURE_WINDOW_SECONDS``) or signed by none of ``keys``. A thin wrapper over
    ``classify_client_ip_header`` for callers that only care about the ip/no-ip outcome."""
    client_ip, result = classify_client_ip_header(header_value, keys, method, path, now=now)
    return client_ip if result == "verified" else None
