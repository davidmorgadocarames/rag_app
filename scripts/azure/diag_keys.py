"""11.1 key diagnosis (T11.1.3): can the running app unwrap its users' data keys?

Runs INSIDE the backend (the Azure replica through `az containerapp exec`, or a local
throwaway copy), reading settings and the database exactly as the app does
(`rag_app.config.get_settings`, `rag_app.db.session.make_engine`, `rag_app.crypto.unwrap_key`),
in a read-only transaction. It prints ONLY these five lines — no key, email, id, host or
exception message:

    N: <user_keys rows>
    OK: <unwrapped with DATA_MASTER_KEY>
    KO: <InvalidToken or any other unwrap failure>
    JWT_SECRET length >= 32: OK|KO
    DATA_MASTER_KEY valid Fernet: OK|KO

A failure prints "error (<exception class>)" in place of a value (never the message, which
may echo a setting). Classification (PHASE_PLANNING 11.1), with the db-tunnel counts:
users gone → (a) data gone; KO > 0 → (b) unreadable, key changed; N > 0 and KO = 0 →
(c) session only.

`scripts/azure/exec_oneliner.py` wraps this file into a single `python -c …` argument for
`az containerapp exec --command`.
"""


def _diag() -> list[str]:
    def err(exc: BaseException) -> str:
        return f"error ({type(exc).__name__})"

    try:
        from cryptography.fernet import Fernet
        from sqlalchemy import text

        from rag_app.config import get_settings
        from rag_app.crypto import unwrap_key
        from rag_app.db.session import make_engine

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - only the class name is printed
        return [
            f"N: {err(exc)}",
            "OK: -",
            "KO: -",
            "JWT_SECRET length >= 32: KO",
            "DATA_MASTER_KEY valid Fernet: KO",
        ]

    try:
        Fernet(settings.data_master_key.encode())
        master = "OK"
    except Exception:  # noqa: BLE001
        master = "KO"
    jwt = "OK" if len(settings.jwt_secret) >= 32 else "KO"

    try:
        engine = make_engine()
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            wrapped = [row[0] for row in conn.execute(text("SELECT wrapped_key FROM user_keys"))]
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        e = err(exc)
        return [
            f"N: {e}",
            f"OK: {e}",
            f"KO: {e}",
            f"JWT_SECRET length >= 32: {jwt}",
            f"DATA_MASTER_KEY valid Fernet: {master}",
        ]

    ok = ko = 0
    for blob in wrapped:
        try:
            unwrap_key(bytes(blob))
            ok += 1
        except Exception:  # noqa: BLE001 - InvalidToken, or an unusable master key
            ko += 1
    return [
        f"N: {len(wrapped)}",
        f"OK: {ok}",
        f"KO: {ko}",
        f"JWT_SECRET length >= 32: {jwt}",
        f"DATA_MASTER_KEY valid Fernet: {master}",
    ]


print("\n".join(_diag()))
