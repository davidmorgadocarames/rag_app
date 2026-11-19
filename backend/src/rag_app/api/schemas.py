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


class ForgotPasswordRequest(StrictModel):
    # Same shape/length bound as RegisterRequest.email/ResendVerificationRequest.email.
    email: str = Field(min_length=3, max_length=255)


class ResetPasswordRequest(StrictModel):
    # T12.4.6: POST with the token in the body -- same discipline as VerifyEmailRequest (a
    # mail scanner pre-fetching the frontend's /reset?token=... link can never consume it).
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _check_password_length_and_denylist(self) -> ResetPasswordRequest:
        # The full policy (T12.4.3) also forbids a password containing the ACCOUNT's own
        # email local part -- not knowable here (the token is not resolved to a user yet),
        # so the route handler re-checks that one rule against the real email once the
        # token's owner is known (rag_app.api.auth.reset_password), before consuming the
        # token. The length/denylist checks are email-independent and run here already.
        problems = validate_password(self.password, "")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class UserOut(BaseModel):
    id: str
    email: str | None  # None once erasure scrubbed it (0005)
    email_verified: bool
    # T13.7.4: the account's OWN MFA status, nothing else -- no role (the frontend never
    # learns "is this account an admin" from this endpoint; it reuses an existing
    # admin-permissioned route's pass/fail for that, see
    # frontend/app/account/security/page.tsx). Lets /account/security show the step-up form
    # by default for a returning account that already enabled MFA, instead of forcing every
    # new session through the enrolment flow again (DEBT-051/DEC-041, 13-F2).
    mfa_enabled: bool


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


class MfaEnrollRequest(StrictModel):
    # DEBT-051/DEC-041: proof of the CURRENT factor, required only when re-enrolling an
    # ALREADY-ENABLED record (a session cookie + CSRF token alone must never be enough to
    # replace an enabled admin's secret and wipe their recovery codes). ``None`` for
    # first-time enrolment, where there is no existing factor to prove. Same bound as
    # ``MfaStepUpRequest.code`` (a 6-digit TOTP code OR an "XXXX-XXXX-XXXX" recovery code).
    code: str | None = Field(default=None, min_length=6, max_length=64)


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


# --- Admin dashboard API (T13.1.1-T13.1.8) -----------------------------------------------
# Every response model below is aggregate/system state only (PHASE_PLANNING Phase 13
# "Privacy rule") -- never conversation content, an email address or any other value a
# client submitted verbatim (T13.1.7). A request model's own field (e.g. the target email
# on the erase-account action) is never echoed back in the response.


class FeatureHealthOut(BaseModel):
    id: str
    name: str
    phase: str
    status: str
    probe_status: str | None
    probe_detail: str | None
    metrics: dict[str, float | None]
    dev_only: bool
    azure_only: bool
    # T13.8.6 (13-G2): the registry's own docs/ADR reference, straight through -- lets the
    # dashboard's feature drawer show a docs link without a second, possibly-drifting lookup.
    docs: str


class AdminAlertOut(BaseModel):
    """T13.5.2: one banner condition, pre-computed server-side (never a client-side
    threshold) so the same business logic covers both live-update modes (SSE snapshot/
    heartbeat, local; the REST route below, Azure polling). ``severity`` is a small, fixed
    vocabulary -- never free text that could carry PII.

    T13.8.3: ``feature_ids`` names the registry feature(s) (``rag_app.features.REGISTRY``
    ids) this alert relates to, so the dashboard's side drawer for a feature can list its
    related alerts without a second lookup table."""

    code: str
    severity: str  # "amber" | "red"
    message: str
    feature_ids: list[str] = []


class AdminFeaturesResponse(BaseModel):
    features: list[FeatureHealthOut]
    # T13.4.1/T13.4.2 (TF14): the server-side decision ("sse" | "polling") the dashboard's
    # CSR code reads to pick its live-update strategy -- never a frontend build-time variable.
    live_mode: str
    # DA-13-E-2: the banner alerts computed from the SAME probe sweep as `features`, so the
    # Azure polling tick (and the SSR first load) never runs a second registry sweep.
    alerts: list[AdminAlertOut] = []


class AdminMetricsResponse(BaseModel):
    values: dict[str, float | None]


class StageLatencyOut(BaseModel):
    stage: str
    count: float
    avg_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None


class StepLatencyOut(BaseModel):
    """T13.8.2: one atomic step's latency (``secrag_chat_step_seconds``), nested under its
    UI-facing pipeline stage below -- see ``rag_app.metrics.STEP_ALLOWLIST``."""

    stage: str
    step: str
    count: float
    avg_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None


class PipelineStageLatencyOut(BaseModel):
    """One UI pipeline stage (classifying/retrieving/reranking/generating/checking/
    responding, in ``STEP_ALLOWLIST`` order) with its atomic steps, also in pipeline order --
    the data behind the Pipeline waterfall's expandable rows (13-G mockup). A DIFFERENT,
    higher-level grouping than ``StageLatencyOut.stage`` above (e.g. "retrieving" covers both
    the old "embed" and "hybrid_search" stage names)."""

    stage: str
    steps: list[StepLatencyOut]


class AdminLatencyResponse(BaseModel):
    stages: list[StageLatencyOut]
    # T13.8.2: additive -- `stages` (the pre-existing secrag_chat_stage_seconds view) is
    # unchanged; `pipeline` is the new per-atomic-step waterfall view.
    pipeline: list[PipelineStageLatencyOut] = []


class EvalMetricOut(BaseModel):
    key: str
    value: float | None
    floor: float | None
    baseline: float | None
    below_floor: bool
    regressed: bool


class AdminEvalResponse(BaseModel):
    last_run_at: dt.datetime | None
    metrics: list[EvalMetricOut]
    passed: bool


class ErasureRequestOut(BaseModel):
    id: str
    requested_at: dt.datetime
    age_seconds: float
    seconds_left: float
    status: str
    rows_remaining: dict[str, int]
    attempts: int
    last_error_class: str | None
    overdue: bool
    # T13.8.3: when the row is already `done`, its completion timestamp
    # (``DeletionRequest.completed_at``, written by ``rag_app.purger``/``rag_app.erasure``
    # when a request finishes) -- ``None`` while open (never set yet).
    completed_at: dt.datetime | None = None


class LastPurgerRunOut(BaseModel):
    started_at: dt.datetime
    finished_at: dt.datetime | None
    requests_processed: int
    errors: int


class AdminErasureResponse(BaseModel):
    requests: list[ErasureRequestOut]
    last_purger_run: LastPurgerRunOut | None


class ErasureActionResponse(BaseModel):
    detail: str
    request_id: str


class AdminAlertsResponse(BaseModel):
    alerts: list[AdminAlertOut]


class AdminEraseUserRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    # T13.1.8: a fixed typed phrase, not a re-typed email -- the email is already the one
    # field that identifies the target; requiring it twice would duplicate PII in the body
    # for no extra safety. Checked server-side, case-sensitive.
    confirmation: str = Field(min_length=1, max_length=32)


class AdminEraseUserResponse(BaseModel):
    detail: str
    request_id: str


# --- T13.3.5: defence lab (local only, DEC-028) --------------------------------------------


class DefenceProbeResultOut(BaseModel):
    probe_id: str
    expected_outcome: str
    observed_outcome: str
    passed: bool


class DefenceLabRunResponse(BaseModel):
    detail: str
    git_sha: str
    results: list[DefenceProbeResultOut]
    all_matched: bool


class DefenceRunOut(BaseModel):
    probe_id: str
    git_sha: str
    expected_outcome: str
    observed_outcome: str
    passed: bool
    run_at: dt.datetime


class DefenceRunsResponse(BaseModel):
    runs: list[DefenceRunOut]
