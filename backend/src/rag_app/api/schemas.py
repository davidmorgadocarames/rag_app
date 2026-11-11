"""Request/response models for the API."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_app.password_policy import validate_password


class StrictModel(BaseModel):
    """Base for every request BODY (T12.3.6, mass-assignment defence): an unexpected field
    (``role``, ``is_admin``, ``mfa_verified_at``, ``user_id``, ...) is REJECTED with 422
    rather than silently dropped -- explicit, tested proof over pydantic's default
    "ignore extra fields" behaviour. Response models stay plain ``BaseModel`` (never built
    from untrusted client input)."""

    model_config = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    status: str


class ChatRequest(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    version: str | None = None


class CitationOut(BaseModel):
    marker: int
    chunk_uid: str
    heading: str
    version: str
    effective_date: str | None


class ChatResponse(BaseModel):
    answer: str
    abstained: bool
    grounded: bool
    citations: list[CitationOut]


class RegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=255)
    # The real floor/ceiling (T12.4.3: >= 12, <= 256, NCSC-list check, must not contain the
    # email local part) is `rag_app.password_policy.validate_password`, below -- this Field's
    # own bound is only a cheap backstop against an absurdly long body.
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _check_password_policy(self) -> RegisterRequest:
        problems = validate_password(self.password, self.email)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class ResendVerificationRequest(StrictModel):
    # T12.4.4: resend is now reached by email, not by an authenticated session -- a freshly
    # registered (unverified) account has no session to authenticate with any more (no
    # session on register). Same shape/length bound as RegisterRequest.email.
    email: str = Field(min_length=3, max_length=255)


class VerifyEmailRequest(StrictModel):
    # T12.4.5: POST, not a GET query param -- a mail scanner pre-fetching the frontend's
    # /verify?token=... link (a plain GET) can never consume the token; only the page's own
    # button click issues this POST.
    token: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str | None  # None once erasure scrubbed it (0005)
    email_verified: bool


class MessageResponse(BaseModel):
    detail: str


class ResendVerificationResponse(BaseModel):
    # T12.1.11: the verification link never travels in an API response (dev or prod) — only
    # by email, now via Mailpit locally (DA-G2-5 taken further: not even ENV=dev is special).
    detail: str


class ChatStreamRequest(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    version: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: dt.datetime
    total_tokens: int
    # True when this conversation cannot be decrypted (data key or one row unreadable):
    # listed with a placeholder title instead of failing the whole list (T11.2.16).
    unreadable: bool = False


class MessageOut(BaseModel):
    role: str
    content: str
    citations: list[CitationOut] = []
    abstained: bool = False
    grounded: bool = True
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: dt.datetime
    # An assistant error marker: the turn failed (T11.2.16); content is the message.
    error: bool = False


class ConversationDetailOut(BaseModel):
    id: str
    title: str
    messages: list[MessageOut]


class RenameRequest(StrictModel):
    title: str = Field(min_length=1, max_length=200)


# --- Admin MFA (T12.3.4/T12.3.5) ----------------------------------------------------------


class MfaEnrollResponse(BaseModel):
    # The PLAINTEXT base32 secret, shown once so the account page can render a manual-entry
    # fallback next to the QR -- never stored in the clear (user_mfa.secret_encrypted is).
    secret: str
    provisioning_uri: str


class MfaConfirmRequest(StrictModel):
    code: str = Field(min_length=6, max_length=6)


class MfaConfirmResponse(BaseModel):
    # Shown to the user exactly once; never stored in the clear and never logged.
    recovery_codes: list[str]


class MfaStepUpRequest(StrictModel):
    # A 6-digit TOTP code OR a recovery code ("XXXX-XXXX-XXXX", generous upper bound).
    code: str = Field(min_length=6, max_length=64)
