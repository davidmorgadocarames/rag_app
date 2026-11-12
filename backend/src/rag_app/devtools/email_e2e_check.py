"""``email-e2e`` gate step (T12.4.9): register -> Mailpit API -> open the verification link
-> verified; the same shape for the password-reset flow (forgot-password -> Mailpit ->
reset -> login with the new password). Exercises a REAL running backend (a native uvicorn,
started by ``scripts/gate.sh``'s ``step_email-e2e``) and a REAL Mailpit (HTTP API), never a
mock -- the actual SMTP send, token hashing, expiry and single-use path, end to end.

T12.4.10 ("no Bearer anywhere"): authentication after login rides the session cookie for
every later call -- never an ``Authorization`` header. The cookie is threaded BY HAND (read
off each response's ``Set-Cookie`` and resent as a plain ``Cookie`` header) rather than via
httpx's own cookie jar: like a browser-less curl, httpx's jar enforces a cookie's ``Secure``
flag against the ACTUAL connection scheme (RFC 6265) -- it has no "localhost is a secure
context" exception (that is a BROWSER-only rule), so it would silently refuse to resend the
Secure session/CSRF cookies ``set_session_cookie``/``set_csrf_cookie`` issue over this native
backend's plain ``http://``.

    python -m rag_app.devtools.email_e2e_check --backend-url URL --mailpit-url URL

Exit code: 0 PASS, 1 FAIL.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import uuid

import httpx

_TOKEN_RE = re.compile(r"token=([^\s&\"'<]+)")
_SESSION_COOKIE = "__Host-secrag_session"
_CSRF_COOKIE = "__Host-secrag_csrf"


class EmailE2eError(RuntimeError):
    """A step failed; the message never carries the test account's password."""


class CookieAuth:
    """Manual cookie jar (see the module docstring): tracks the session/CSRF cookie values
    and provides the headers to send them back on, bypassing httpx's own Secure-flag
    enforcement."""

    def __init__(self) -> None:
        self.session: str | None = None
        self.csrf: str | None = None

    def absorb(self, response: httpx.Response) -> None:
        for raw in response.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if name == _SESSION_COOKIE:
                self.session = value
            elif name == _CSRF_COOKIE:
                self.csrf = value

    def headers(self) -> dict[str, str]:
        if self.session is None:
            return {}
        cookie = f"{_SESSION_COOKIE}={self.session}"
        if self.csrf is not None:
            cookie = f"{cookie}; {_CSRF_COOKIE}={self.csrf}"
        headers = {"Cookie": cookie}
        if self.csrf is not None:
            headers["X-CSRF-Token"] = self.csrf
        return headers


def _wait_for_email(mailpit_url: str, to_email: str, *, timeout_s: float = 20.0) -> str:
    """Polls Mailpit's own HTTP API (never a mocked inbox) until a message to ``to_email``
    arrives, then returns its plain-text body. Mirrors ``frontend/e2e/mailpit.ts`` (the
    Playwright `e2e` step's own Mailpit client) at the Python/httpx level."""
    deadline = time.monotonic() + timeout_s
    with httpx.Client(base_url=mailpit_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            search = client.get("/api/v1/search", params={"query": f"to:{to_email}"})
            search.raise_for_status()
            messages = search.json().get("messages") or []
            if messages:
                message = client.get(f"/api/v1/message/{messages[0]['ID']}")
                message.raise_for_status()
                body = message.json()
                text = body.get("Text") or body.get("HTML") or ""
                if text:
                    return text
            time.sleep(0.5)
    raise EmailE2eError(f"no email arrived at Mailpit for this address within {timeout_s}s")


def _extract_token(body: str) -> str:
    match = _TOKEN_RE.search(body)
    if not match:
        raise EmailE2eError("no 'token=' found in the email body")
    return match.group(1)


def run(backend_url: str, mailpit_url: str) -> None:
    email = f"email-e2e-{uuid.uuid4().hex[:12]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    new_password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    auth = CookieAuth()

    with httpx.Client(base_url=backend_url, timeout=10.0) as client:
        # --- register -> Mailpit -> verify --------------------------------------------------
        register = client.post("/auth/register", json={"email": email, "password": password})
        if register.status_code != 202:
            raise EmailE2eError(f"register: HTTP {register.status_code} ({register.text})")
        verify_token = _extract_token(_wait_for_email(mailpit_url, email))
        verify = client.post("/auth/verify", json={"token": verify_token})
        if verify.status_code != 200:
            raise EmailE2eError(f"verify: HTTP {verify.status_code} ({verify.text})")

        login = client.post("/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            raise EmailE2eError(f"login after verify: HTTP {login.status_code} ({login.text})")
        auth.absorb(login)
        if auth.session is None:
            raise EmailE2eError("login did not set the session cookie")
        # No Bearer anywhere (T12.4.10): only the cookie (threaded by hand, see the module
        # docstring) authenticates this call -- no Authorization header at all.
        me = client.get("/auth/me", headers=auth.headers())
        if me.status_code != 200:
            raise EmailE2eError(f"GET /auth/me with the cookie: HTTP {me.status_code}")

        # --- forgot-password -> Mailpit -> reset ---------------------------------------------
        forgot = client.post("/auth/forgot-password", json={"email": email})
        if forgot.status_code != 200:
            raise EmailE2eError(f"forgot-password: HTTP {forgot.status_code} ({forgot.text})")
        reset_token = _extract_token(_wait_for_email(mailpit_url, email))
        reset = client.post("/auth/reset", json={"token": reset_token, "password": new_password})
        if reset.status_code != 200:
            raise EmailE2eError(f"reset: HTTP {reset.status_code} ({reset.text})")

        # Reset must have revoked the earlier session (rag_app.sessions.revoke_all): the
        # cookie this script is still holding must no longer authenticate.
        me_after_reset = client.get("/auth/me", headers=auth.headers())
        if me_after_reset.status_code != 401:
            raise EmailE2eError(
                "the pre-reset session cookie still authenticates after /auth/reset"
                f" (HTTP {me_after_reset.status_code})"
            )
        old_login = client.post("/auth/login", json={"email": email, "password": password})
        if old_login.status_code != 401:
            raise EmailE2eError("the OLD password still logs in after reset")
        new_login = client.post("/auth/login", json={"email": email, "password": new_password})
        if new_login.status_code != 200:
            raise EmailE2eError(
                f"login with the NEW password: HTTP {new_login.status_code} ({new_login.text})"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True, help="e.g. http://127.0.0.1:18100")
    parser.add_argument("--mailpit-url", required=True, help="e.g. http://127.0.0.1:18026")
    args = parser.parse_args(argv)
    try:
        run(args.backend_url, args.mailpit_url)
    except (EmailE2eError, httpx.HTTPError) as exc:
        print(f"email-e2e: FAIL -- {exc}", file=sys.stderr)
        return 1
    print("email-e2e: PASS (register -> verify -> login; forgot-password -> reset -> login)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
