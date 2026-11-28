"""Azure OpenAI gpt-4.1-mini vs gpt-5-mini live side-by-side check (14.7-migration-live, part 2b,
fixed in part 2d per DA-14.7-migration-live-2a-1/2a-2/2a-3).

**Built and unit-tested entirely OFFLINE** (mocked ``httpx``, see ``test_azure_side_by_side.py``)
by the senior-engineer agent -- this module makes **zero** network calls on import or by being
built. Every real Azure OpenAI call only happens when the USER runs
``scripts/azure/openai_live_check.sh`` in their own terminal, with the key sourced into THAT
shell's environment only (never on this module's argv, never logged). See F43 in
``status/failures.md`` and the "Part 2a"/"Part 2b" sections of
``status/blocks/14.7-migration-live.md`` for why this module cannot be run by an agent: the
permission system denies any agent command that so much as references
``~/.config/secrag/azure-openai.env`` (even a read-only existence check), by design -- the user's
own secret boundary.

Three phases, run in this order, each phase's Azure calls gated by :class:`CostGuard` BEFORE any
call is made (never after):

(a) **smoke** -- :meth:`AzureOpenAIChat.chat` + :meth:`AzureOpenAIChat.chat_json` (strict
    ``json_schema``) against both deployments: the current ``gpt-4.1-mini``
    (``reasoning_model=False``) and the candidate ``gpt-5-mini`` (``reasoning_model=True``). On an
    HTTP 400 for the reasoning deployment, :func:`probe_api_versions` tries
    :data:`GA_API_VERSION_PROBE_ORDER` in order and reports the first version that does not 400 --
    status code + error TYPE only, never a response body (which could echo request content back).
(b) **token budget** -- measures ``usage.completion_tokens_details.reasoning_tokens`` vs the
    visible completion on a handful of representative router/answer/groundedness-shaped prompts
    drawn from ``eval/golden_set_v2.jsonl`` (see :func:`representative_prompts`'s own
    self-reported scope limit), and prints a recommended ``max_tokens`` /
    ``GROUNDEDNESS_MAX_TOKENS`` bound compared to the 7e6b6f0 provisional 3072/512
    (DA-14.7-migration-2).
(c) **side-by-side eval** -- unless ``--smoke-only``, runs Option B
    (``status/blocks/14.7-migration.md`` §4: fresh baseline + candidate re-run on the same golden
    set v2 Azure subset) against both deployments, reusing
    ``rag_app.eval.runner.evaluate_dialogues``/``metrics``/``judge``, one DIALOGUE at a time (see
    :func:`run_side_by_side`'s own docstring for why this is behaviourally identical to one call
    over the whole subset, and what it buys: a per-dialogue progress count and real per-dialogue
    cost figures).

**Part 2d fix (DA-14.7-migration-live-2a-1, HIGH): per-CALL cost reservation, not per-phase.**
Part 2b's phase (c) reserved one flat, FIXED estimate
(``TOKENS_IN_PER_QUESTION``/``TOKENS_OUT_PER_QUESTION`` -- the original plan's arithmetic, ~600
output tokens/question) per deployment, upfront, for the WHOLE 30-question run -- this understated
``gpt-5-mini``'s real worst case by ~6x (up to 3584 output tokens in a single turn:
``settings.azure_openai_reasoning_max_tokens`` + ``...groundedness_max_tokens``, part 2a). Every
phase now routes its Azure HTTP calls through :func:`make_guarded_post`, which:

1. Reserves the real worst case for THAT ONE call, read straight off the request body's
   ``max_completion_tokens``/``max_tokens`` field -- this is already exactly
   ``settings.effective_max_tokens()`` for an answer call, ``effective_groundedness_max_tokens()``
   for a groundedness check, or the router's/chit-chat's/off-topic's/memory's/judge's own fixed
   bound for a ``chat_json`` or short reply call (every call site in ``generation.py``/
   ``assistant/*.py``/``eval/judge.py`` already passes its own real bound as ``max_tokens`` --
   reading it off the wire, rather than re-deriving it here, means this guard is correct for ANY
   call site, including ones this block never had to enumerate by hand).
2. Makes the call ONLY if that reservation fits under ``--max-eur``'s remaining budget (never a
   whole phase/deployment upfront) -- :class:`CostCapExceeded` is raised BEFORE the HTTP call if
   it would not fit.
3. **Settles** the reservation down (or up) to the REAL ``usage.completion_tokens`` the response
   reports once the call succeeds (reasoning tokens counted as part of the output total, since
   Azure's own ``completion_tokens`` already includes them) -- :meth:`CostGuard.settle`. A failed
   call (network error or non-2xx) settles to ZERO (nothing is billed for a rejected request).
4. Retries at most ONCE on HTTP 429 (:func:`_call_with_single_retry`, unchanged contract): the
   failed attempt's reservation is settled to zero BEFORE a FRESH reservation is made for the
   retry, so the two attempts are never double-counted and the retry's own worst case is checked
   against the remaining cap again, not assumed to still fit.
5. Reservation/settlement are protected by :class:`CostGuard`'s own lock, making them atomic --
   today every phase in this module makes its Azure calls strictly SEQUENTIALLY (no threads, no
   concurrency anywhere in this file), so the lock is pure insurance against a future caller that
   might not be, never a currently-exercised code path.

If a call's worst case would exceed the remaining budget, the guard stops BEFORE that call;
:func:`run_side_by_side` catches this at the per-DIALOGUE boundary (a dialogue's own in-memory
state is reset fresh by ``evaluate_dialogues`` for every new dialogue anyway, so replaying one at
a time is behaviourally identical to replaying the whole subset in one call) and reports exactly
how many of the subset's dialogues completed for EACH deployment, not just "blocked".

**Part 2d fix (DA-14.7-migration-live-2a-2, medium): every phase is exception-safe, and so is
``main()``.** Every raw HTTP call site in this module now sits inside a
``try/except (httpx.HTTPStatusError, httpx.HTTPError, ValueError)`` (the same three exception
types :func:`run_smoke` already caught -- ``ValueError`` covers
:func:`rag_app.llm.parse_azure_chat_response` on a malformed body, the other DA-14.7-migration-
live-2a-2 gap). Only ``status_code``/exception CLASS NAME are ever recorded or printed -- never
``str(exc)``, which for ``httpx.HTTPStatusError`` embeds the full request URL (the live endpoint).
``main()`` ALSO wraps its entire run in one more, outermost ``try/except Exception`` as a
last-resort safety net: if anything still escapes every phase's own handling, it is sanitized the
same way (exception class + HTTP status only) before being printed or written to the evidence
file, and the process exits non-zero -- never a raw Python traceback (which could otherwise embed
the endpoint via ``HTTPStatusError.__str__``) reaches the terminal or the evidence file.

**Part 2d fix (DA-14.7-migration-live-2a-3, medium): per-question ids + a flipped list.**
:func:`run_side_by_side` now keeps a ``per_turn`` list per deployment (``dialogue_id``,
``turn_index``, ``correct``, ``latency_s``, ``tokens_in``/``tokens_out``/``eur`` -- never
``reply_text``/prompt text) and a top-level ``flipped`` list: every ``(dialogue_id, turn_index)``
whose ``correct`` verdict differs between ``gpt-4.1-mini`` and ``gpt-5-mini``. Self-reported
boundary: real per-CALL token/EUR usage is not wired through ``DialogueTurnResult``/``runner.py``
(out of this block's declared ``touches``, unchanged from part 2b) -- the ``tokens_in``/
``tokens_out``/``eur`` figures on each ``per_turn`` row are the REAL per-DIALOGUE total the cost
guard measured (summed over however many calls that one dialogue actually made), divided EVENLY
across that dialogue's own turns. This is an honest approximation (a dialogue's turns can each
make a different number of LLM calls -- the exact fast path skips the router entirely), not a true
per-call attribution; it is still real measured usage, not the old flat plan estimate.

Cost guard (:class:`CostGuard`): estimates EUR from ``status/azure_prices.json``'s
``datazone_standard`` tier for BOTH models (DEC-061's migration target deployment is
DataZoneStandard-only; pricing both deployments on that tier keeps the side-by-side comparison
apples-to-apples with the SKU the candidate will actually run on) BEFORE each reservation, adds it
to the running total, and raises :class:`CostCapExceeded` -- **no call is made** -- rather than
ever letting the running total exceed ``--max-eur`` (default 0.30, this block's
``PARALLEL_PLAN.md`` budget line).

Output discipline (never violated anywhere in this module): the report this module builds and
prints/writes contains only ids, call types, status codes, exception CLASS NAMES (never
``str(exc)`` -- an exception's string form can embed the request URL), token counts and EUR
figures. It never contains an endpoint, an API key, or full prompt/response text.

**Part 2c fix (worktree prices-file bug, F22-class).** ``status/`` is a symlink into the
private planning folder only in the MAIN repo clone (``~/proyectos/rag_app``); a `git worktree`
such as ``~/proyectos/wt/14-A1`` has no such symlink -- its own ``status/`` holds only the
tracked ``status/blocks/`` directory, never ``azure_prices.json``. :func:`resolve_prices_path`
tries, in order, stopping at the first path that exists: the explicit ``--prices`` CLI flag, the
``SECRAG_AZURE_PRICES`` env var, ``REPO_ROOT/status/azure_prices.json`` (works from the main repo
clone), then ``~/proyectos/rag_app/status/azure_prices.json`` (the hard-coded main-repo fallback,
so a worktree run finds the real file without the user having to pass anything). If none exists
it raises :class:`FileNotFoundError` naming every path it tried, BEFORE ``get_settings()`` is even
called (so before any possible Azure call) -- never "no cost guard, proceed anyway". The
``--check-config`` flag resolves this path and the golden-set path and prints the
``gpt-4.1-mini``/``gpt-5-mini`` DataZone prices with zero env-file read and zero network call, so
the user can prove path resolution works from their own worktree before running anything live.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from rag_app.config import Settings, get_settings
from rag_app.eval.dataset import DEFAULT_GOLDEN_SET_V2, Dialogue, load_dialogues
from rag_app.eval.metrics import compute_dialogue_metrics
from rag_app.eval.runner import evaluate_dialogues
from rag_app.llm import (
    AzureOpenAIChat,
    AzureReasoningBudgetExhausted,
    Message,
    parse_azure_chat_response,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

# Part 2c (F22-class worktree bug): the env var an explicit override can use instead of --prices.
SECRAG_AZURE_PRICES_ENV = "SECRAG_AZURE_PRICES"
# The main repo clone's status/ IS the symlink into the private planning folder where the real
# azure_prices.json lives; this is the last-resort fallback resolve_prices_path() tries when run
# from a worktree (which has no status symlink of its own -- see the module docstring).
MAIN_REPO_PRICES_PATH = Path.home() / "proyectos" / "rag_app" / "status" / "azure_prices.json"


def resolve_prices_path(explicit: Path | None = None) -> Path:
    """Resolve ``azure_prices.json`` in this fixed order, returning the first candidate that
    **exists** on disk:

    1. ``explicit`` (the ``--prices`` CLI flag), if given.
    2. ``$SECRAG_AZURE_PRICES`` (the env var), if set.
    3. ``REPO_ROOT/status/azure_prices.json`` -- works when run from the main repo clone, where
       ``status/`` is the symlink into the private planning folder.
    4. ``~/proyectos/rag_app/status/azure_prices.json`` -- the main repo clone's own path,
       hard-coded as a fallback so a run from a WORKTREE (whose ``status/`` has no such symlink,
       F22-class) still finds the real file without the user passing anything.

    **Fails CLOSED**: raises :class:`FileNotFoundError` naming every path tried, in order, if
    none of them exists -- this function is always called BEFORE any Azure network call, never
    after, and never falls back to "no cost guard, proceed anyway".
    """
    candidates: list[tuple[str, Path]] = []
    if explicit is not None:
        candidates.append(("--prices", explicit))
    env_value = os.environ.get(SECRAG_AZURE_PRICES_ENV)
    if env_value:
        candidates.append((f"${SECRAG_AZURE_PRICES_ENV}", Path(env_value)))
    candidates.append(
        ("REPO_ROOT/status/azure_prices.json", REPO_ROOT / "status" / "azure_prices.json")
    )
    candidates.append(("~/proyectos/rag_app/status/azure_prices.json", MAIN_REPO_PRICES_PATH))

    for _label, candidate_path in candidates:
        if candidate_path.is_file():
            return candidate_path

    tried = "; ".join(f"{label} ({path})" for label, path in candidates)
    raise FileNotFoundError(
        "no Azure OpenAI prices file found -- refusing to proceed without a cost guard "
        f"(tried, in order: {tried})"
    )


BASELINE_DEPLOYMENT = "gpt-4.1-mini"
CANDIDATE_DEPLOYMENT = "gpt-5-mini"
# DEC-061: the migration target deployment is DataZoneStandard-only; price BOTH deployments on
# this tier so the side-by-side cost comparison matches the SKU the candidate actually runs on
# (not gpt-4.1-mini's existing GlobalStandard production deployment's own, slightly cheaper price).
PRICE_TIER = "datazone_standard"

# Documented GA (non-preview) Azure OpenAI API versions, probe order (current pinned default
# first). Sourced from this agent's training-time knowledge of Microsoft Learn's API version
# lifecycle page (cutoff: January 2026) -- a newer GA version may exist by the time this runs
# live. The live check reports the FIRST version that does not 400, not a theory about which
# "should" work (DA-14.7-migration-1).
GA_API_VERSION_PROBE_ORDER: tuple[str, ...] = (
    "2024-10-21",
    "2024-06-01",
    "2024-02-01",
)

# The "Azure subset" golden_set_v2.jsonl's dialogues eval against (status/blocks/
# 14.7-migration.md §4). No subset is defined anywhere else in code today -- per this block's own
# instructions, documented here as the first AZURE_SUBSET_SIZE dialogues in file order,
# deterministically (azure_subset()).
AZURE_SUBSET_SIZE = 30

# Crude chars/4 estimate for the INPUT side of a per-call reservation only (never the final,
# settled EUR figure) -- the same heuristic `rag_app.llm._estimate_prompt_tokens` uses for its own
# context-overflow warning, duplicated here rather than imported because that name is private to
# `rag_app.llm` and this module's declared `touches` do not include `llm.py`. Deliberately crude:
# it only sizes the WORST-CASE reservation (which the output bound already dominates -- every
# model's output price per token is several times its input price), never what gets billed.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _estimate_input_tokens(messages: list[Message]) -> float:
    return sum(len(m.get("content", "")) for m in messages) / _CHARS_PER_TOKEN_ESTIMATE


_DEPLOYMENT_URL_RE = re.compile(r"/openai/deployments/([^/]+)/chat/completions")


def _deployment_from_url(url: str) -> str | None:
    """Parse the Azure deployment name out of an Azure OpenAI Chat Completions URL.

    Returns ``None`` (DA-14.7-migration-live-2e-1, part 2e) -- never raises -- when ``url``
    is not an Azure OpenAI chat-completions call at all. ``_guarded`` (below) patches the
    GLOBAL ``httpx.post`` for the duration of phase (c)'s real pipeline run, which also
    calls Ollama's own ``/api/embed`` endpoint for retrieval (``OllamaEmbedder.embed``,
    always local/free regardless of ``LLM_PROVIDER`` -- embeddings and chat are independent
    settings) and any other non-Azure endpoint a future call site might add. This function
    used to raise a bare ``RuntimeError`` for exactly that case, which the FIRST real user
    run hit: the cost guard refused to "identify the deployment" for a plain Ollama
    embedding call, an unhandled ``RuntimeError`` propagated out of the whole script, and
    every phase's results (including the already-finished smoke/token-budget phases) were
    lost. ``None`` lets :func:`_guarded` recognise "not my concern" and pass the call
    straight through, unreserved -- this cost guard only ever tracks Azure OpenAI chat
    spend, never a local Ollama call's (zero) cost. Never returns or logs ``url`` itself
    (only the deployment NAME, never a secret) when it *does* match."""
    match = _DEPLOYMENT_URL_RE.search(url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Cost guard
# ---------------------------------------------------------------------------


class CostCapExceeded(RuntimeError):
    """Raised by :meth:`CostGuard.reserve` BEFORE any call is made -- no partial charge, no
    call happens once this is raised."""


# --- Diagnostics (part 2e) --------------------------------------------------------------
#
# Every error recorded anywhere in this module goes through these two helpers, so the shape
# is uniform and the no-secrets discipline is enforced in exactly one place rather than
# re-implemented at every call site.

# Exception types THIS module (or rag_app.llm, on its behalf) raises itself, from fixed,
# hard-coded literal text that never interpolates a request/response body, an endpoint or a
# key -- the only exception types whose own ``str()`` is ever safe to surface (DA-14.7-
# migration-live-2a-2's rule, extended in part 2e: never ``str()`` of a THIRD-PARTY exception
# such as ``httpx.HTTPStatusError``, whose ``__str__`` embeds the live request URL).
_SAFE_MESSAGE_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    CostCapExceeded,  # .__str__ is hand-built from phase/model/EUR floats only, see below
    AzureReasoningBudgetExhausted,  # fixed literal text, see rag_app.llm
)


def _azure_error_code(response: httpx.Response | None) -> str | None:
    """Extract ONLY the Azure error ``code`` field (a short, enum-like string such as
    ``"unsupported_parameter"``/``"content_filter"``) from a non-2xx response body -- never
    the ``message`` field (which can echo request content back) and never the raw body.
    Returns ``None`` for any shape this does not recognise (never raises)."""
    if response is None:
        return None
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - a malformed/non-JSON error body is simply "no code"
        return None
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _diagnostic_info(exc: BaseException) -> dict[str, Any]:
    """The ONLY facts ever recorded about a caught exception, anywhere in this module (part
    2e, used by every phase AND by ``main()``'s last-resort handler): exception class name,
    HTTP status code (if the exception carries an ``httpx.Response``), the Azure error
    ``code`` field (never ``message``), and -- ONLY for the explicitly allow-listed exception
    types above, built by THIS module from safe constants -- that exception's own message
    text. Never ``str(exc)`` for anything else (a third-party exception's ``__str__`` can
    embed the live endpoint, e.g. ``httpx.HTTPStatusError``), and never a traceback."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None
    info: dict[str, Any] = {
        "exception_class": type(exc).__name__,
        "status_code": status_code,
        "error_code": _azure_error_code(response),
    }
    if isinstance(exc, _SAFE_MESSAGE_EXCEPTION_TYPES):
        info["message"] = str(exc)
    return info


@dataclass
class CostGuard:
    """Tracks EUR spent (estimated, then settled to real usage) this run and refuses to let it
    exceed ``max_eur``.

    ``reserve()`` must be called before every SINGLE Azure OpenAI call (never once for a whole
    phase or deployment, DA-14.7-migration-live-2a-1) with that one call's real worst-case output
    bound; it estimates the EUR cost from ``prices`` (never from a live call), and either commits
    the estimate to ``spent_eur`` and returns the log entry, or raises :class:`CostCapExceeded`
    with the running total left untouched. ``settle()`` then trues up that same entry to the real
    usage the response reported once the call completes (or to zero if it failed). Both methods
    hold ``self._lock`` for their whole body: every phase in this module calls Azure strictly
    sequentially today, so this is defensive insurance for a concurrent caller that does not exist
    yet, not a currently-exercised code path.
    """

    prices: dict[str, Any]
    max_eur: float
    spent_eur: float = 0.0
    log: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_file(cls, max_eur: float, path: Path) -> CostGuard:
        """``path`` must already be a RESOLVED, existing path (see :func:`resolve_prices_path`)
        -- this method itself still fails closed with a plain, unambiguous
        ``FileNotFoundError``/``JSONDecodeError`` if ``path`` turns out not to be readable, but
        callers should resolve the path first so the error message names every path tried, not
        just the one that was finally passed in."""
        return cls(prices=json.loads(path.read_text(encoding="utf-8")), max_eur=max_eur)

    def estimate(self, *, model: str, tokens_in: float, tokens_out: float) -> dict[str, Any]:
        """EUR estimate for ``tokens_in``/``tokens_out`` against ``model``'s
        :data:`PRICE_TIER` price row in ``status/azure_prices.json["openai"]["models"]``.
        Never makes a network call; never reads a live usage figure."""
        models = self.prices.get("openai", {}).get("models", {})
        if model not in models:
            raise CostCapExceeded(
                f"unknown openai model {model!r} in prices file; known: {sorted(models)}"
            )
        cfg = models[model].get(PRICE_TIER)
        if cfg is None:
            raise CostCapExceeded(f"no {PRICE_TIER!r} price row for model {model!r}")
        usd = (tokens_in / 1_000_000.0) * cfg["input_per_1m_usd"] + (
            tokens_out / 1_000_000.0
        ) * cfg["output_per_1m_usd"]
        eur = usd * self.prices["fx_usd_to_eur"]
        return {
            "model": model,
            "tier": PRICE_TIER,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "eur": round(eur, 6),
        }

    def reserve(
        self, phase: str, *, model: str, tokens_in: float, tokens_out: float
    ) -> dict[str, Any]:
        """Estimate BEFORE the call -- for exactly ONE call, never a whole phase/deployment.
        Raises :class:`CostCapExceeded` (no call made, ``spent_eur`` unchanged) if committing
        this estimate would push the running total over ``max_eur``; otherwise commits it and
        returns the log entry (also appended to ``self.log``) that :meth:`settle` later mutates
        in place."""
        with self._lock:
            estimate = self.estimate(model=model, tokens_in=tokens_in, tokens_out=tokens_out)
            projected = self.spent_eur + estimate["eur"]
            if projected > self.max_eur:
                raise CostCapExceeded(
                    f"{phase}: projected EUR {projected:.4f} would exceed --max-eur "
                    f"{self.max_eur:.4f} (already spent {self.spent_eur:.4f} + this "
                    f"reservation's estimate {estimate['eur']:.4f} for {model}); no call made"
                )
            self.spent_eur = projected
            entry = {"phase": phase, **estimate, "running_total_eur": round(self.spent_eur, 6)}
            self.log.append(entry)
            return entry

    def settle(
        self, entry: dict[str, Any], *, tokens_in: float, tokens_out: float
    ) -> dict[str, Any]:
        """True up ``entry`` (the exact dict :meth:`reserve` returned for ONE call) to the REAL
        ``tokens_in``/``tokens_out`` that call used -- reasoning tokens counted as part of
        ``tokens_out`` (Azure's own ``completion_tokens`` already includes them). Adjusts
        ``spent_eur`` by the DIFFERENCE between the real cost and the worst-case estimate already
        committed -- never raises :class:`CostCapExceeded` here: the call already happened and
        cannot be undone, so a response that genuinely used more than its own worst-case bound
        (should not happen -- the bound IS the ceiling Azure was told not to exceed) simply grows
        ``spent_eur`` past ``max_eur``, visible in the next ``reserve()``'s own check, rather than
        being silently hidden."""
        with self._lock:
            actual = self.estimate(model=entry["model"], tokens_in=tokens_in, tokens_out=tokens_out)
            self.spent_eur = round(self.spent_eur + actual["eur"] - entry["eur"], 6)
            entry["actual_tokens_in"] = tokens_in
            entry["actual_tokens_out"] = tokens_out
            entry["actual_eur"] = actual["eur"]
            entry["running_total_eur"] = self.spent_eur
            return entry


def _call_with_single_retry(fn: Callable[[], Any], *, backoff_s: float = 2.0) -> Any:
    """At most ONE retry, only on HTTP 429, with a short fixed backoff. Never a retry loop:
    a second 429 (or any other error) propagates."""
    try:
        return fn()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            time.sleep(backoff_s)
            return fn()
        raise


def make_guarded_post(
    cost_guard: CostGuard,
    phase: str,
    *,
    post: Callable[..., httpx.Response] | None = None,
) -> Callable[..., httpx.Response]:
    """Return a drop-in replacement for ``httpx.post`` that reserves (and later settles) its OWN
    PER-CALL worst case from ``cost_guard`` around every single request it makes (DA-14.7-
    migration-live-2a-1) -- never a flat estimate for a whole phase or deployment.

    ``post`` (default ``None``) is resolved to the real ``httpx.post`` ONCE, right here, at
    construction time -- deliberately NOT a dynamic per-call lookup like
    :func:`probe_api_versions`'s own ``post`` parameter: the returned function is meant to be
    installed as the NEW value of ``httpx.post`` (see :func:`_patched_post`), so a dynamic
    ``httpx.post`` lookup inside the closure would recurse into itself. Resolving eagerly here,
    before any patching happens, captures the real (or test-faked, if a test already
    monkeypatched ``httpx.post`` before calling this function) underlying caller correctly.

    The returned function never calls ``raise_for_status()`` itself for a NON-429 error: it
    returns the raw ``Response`` so callers that inspect ``status_code`` manually
    (:func:`probe_api_versions`) see exactly what they already expect; callers that call
    ``raise_for_status()`` themselves (:class:`AzureOpenAIChat`, :func:`_raw_reasoning_chat_call`)
    still get the same exception they always did. A 429 IS raised here (as
    ``httpx.HTTPStatusError``) so :func:`_call_with_single_retry` can retry it with the cost
    guard settled-then-re-reserved correctly (see the module docstring's point 4).

    Refuses (``RuntimeError``, before any reservation) a call whose body carries no
    ``max_completion_tokens``/``max_tokens`` -- there would be no worst case to reserve against,
    which would silently bypass the cost guard entirely.

    **Passes straight through, unreserved** (DA-14.7-migration-live-2e-1, part 2e) a call
    whose URL is not an Azure OpenAI chat-completions call at all (:func:`_deployment_from_url`
    returns ``None``) -- this guard only prices Azure OpenAI chat spend; a local Ollama
    embeddings call made through the SAME globally-patched ``httpx.post`` while phase (c)'s
    real pipeline runs (see :func:`_patched_post`) is not this guard's concern and must never
    be refused.
    """
    real_post = post if post is not None else httpx.post

    def _guarded(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        body = json or {}
        deployment = _deployment_from_url(url)
        if deployment is None:
            # Not an Azure OpenAI chat-completions call (e.g. Ollama's /api/embed during
            # phase (c)'s real retrieval) -- not this guard's concern; pass straight through.
            return real_post(url, headers=headers, json=json, timeout=timeout, **kwargs)
        max_out = body.get("max_completion_tokens", body.get("max_tokens"))
        if max_out is None:
            raise RuntimeError(
                f"{phase}: refusing an Azure call with no max_tokens/max_completion_tokens -- "
                "there is no worst case to reserve against (DA-14.7-migration-live-2a-1)"
            )
        tokens_in = _estimate_input_tokens(body.get("messages", []))

        def _attempt() -> httpx.Response:
            reservation = cost_guard.reserve(
                phase, model=deployment, tokens_in=tokens_in, tokens_out=float(max_out)
            )
            try:
                response = real_post(url, headers=headers, json=json, timeout=timeout, **kwargs)
            except httpx.HTTPError:
                cost_guard.settle(reservation, tokens_in=0.0, tokens_out=0.0)
                raise
            if response.status_code == 429:
                cost_guard.settle(reservation, tokens_in=0.0, tokens_out=0.0)
                raise httpx.HTTPStatusError(
                    "429 Too Many Requests", request=response.request, response=response
                )
            if response.status_code >= 400:
                cost_guard.settle(reservation, tokens_in=0.0, tokens_out=0.0)
                return response
            try:
                usage = response.json().get("usage") or {}
                actual_in = float(usage.get("prompt_tokens", tokens_in))
                actual_out = float(usage.get("completion_tokens", max_out))
            except Exception:  # noqa: BLE001 - a malformed/missing usage body never blocks settle
                actual_in, actual_out = tokens_in, float(max_out)
            cost_guard.settle(reservation, tokens_in=actual_in, tokens_out=actual_out)
            return response

        response: httpx.Response = _call_with_single_retry(_attempt)
        return response

    return _guarded


@contextlib.contextmanager
def _patched_post(guarded: Callable[..., httpx.Response]) -> Any:
    """Temporarily replace the GLOBAL ``httpx.post`` with ``guarded`` so every call any code this
    process runs makes during the ``with`` block -- regardless of which function/module issued
    it -- goes through the per-call cost guard first. Restores the exact previous value on exit
    (success or exception), so nothing outside this block, no other test, and no later phase is
    ever affected. Only used around phase (a)/(c)'s calls, which go through
    :class:`AzureOpenAIChat` (no ``post=`` injection point of its own); phase (b) passes
    ``make_guarded_post``'s result as its own explicit ``post=`` argument instead."""
    original = httpx.post
    httpx.post = guarded
    try:
        yield
    finally:
        httpx.post = original


# ---------------------------------------------------------------------------
# Phase (a): smoke + API-version probe
# ---------------------------------------------------------------------------


class _SmokeSchema(BaseModel):
    ok: bool


def _azure_url(endpoint: str, deployment: str, api_version: str) -> str:
    base = endpoint.rstrip("/")
    return f"{base}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"


def _azure_headers(api_key: str) -> dict[str, str]:
    return {"api-key": api_key, "Content-Type": "application/json"}


def probe_api_versions(
    *,
    endpoint: str,
    api_key: str,
    deployment: str,
    candidates: tuple[str, ...] = GA_API_VERSION_PROBE_ORDER,
    post: Callable[..., httpx.Response] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Try each candidate API version (reasoning-model request shape: ``max_completion_tokens``,
    no ``temperature``) against ``deployment`` in order; return ``(first_working_version,
    attempts)``. ``attempts`` records only ``api_version``/``status_code``/``error_type`` per
    try -- never a response body. Raises ``RuntimeError`` (with the same sanitised attempts list)
    if every candidate fails.

    ``post`` defaults to ``None`` (resolved to ``httpx.post`` INSIDE the loop, not as a bound
    default-argument value) so a test's ``monkeypatch.setattr(module.httpx, "post", fake_post)``
    -- the same pattern every other test in this codebase uses -- takes effect here too; a bound
    default (``post: Callable = httpx.post``) would freeze the reference to whatever ``httpx.post``
    was at import time, before any monkeypatch exists. Callers running this through the cost
    guard (:func:`run_smoke`) pass the guarded post explicitly."""
    attempts: list[dict[str, Any]] = []
    body = {"messages": [{"role": "user", "content": "ping"}], "max_completion_tokens": 16}
    caller = post or httpx.post
    for version in candidates:
        url = _azure_url(endpoint, deployment, version)
        try:
            response = caller(url, headers=_azure_headers(api_key), json=body, timeout=30)
        except httpx.HTTPError as exc:
            attempts.append(
                {"api_version": version, "status_code": None, "error_type": type(exc).__name__}
            )
            continue
        if response.status_code < 400:
            attempts.append(
                {"api_version": version, "status_code": response.status_code, "error_type": None}
            )
            return version, attempts
        attempts.append(
            {
                "api_version": version,
                "status_code": response.status_code,
                "error_type": "HTTPStatusError",
            }
        )
    raise RuntimeError(f"no candidate API version succeeded for {deployment!r}: {attempts}")


# Non-reasoning smoke bounds (gpt-4.1-mini): tiny, unchanged since part 2b -- this deployment
# has no hidden reasoning tokens to absorb.
SMOKE_MAX_TOKENS = 16
SMOKE_JSON_MAX_TOKENS = 32
# Reasoning smoke bound floor (DA-14.7-migration-live-2e-2): the FIRST real user run showed
# SMOKE_JSON_MAX_TOKENS=32 was entirely consumed by gpt-5-mini's hidden reasoning tokens before
# any visible content was produced (empty content, finish_reason="length", two JSONDecodeErrors
# then a silent fallback) -- a reasoning deployment needs a much bigger bound even for a
# one-word smoke reply. Never smaller than this floor, and never smaller than the app's own
# already-tuned reasoning groundedness bound (part 2a) -- cheap at either model's price,
# generous enough to leave room for the hidden reasoning tokens a trivial prompt still spends.
SMOKE_MAX_TOKENS_REASONING_MIN = 1024


def _smoke_bound(settings: Settings, *, reasoning: bool, non_reasoning_bound: int) -> int:
    if not reasoning:
        return non_reasoning_bound
    return max(
        SMOKE_MAX_TOKENS_REASONING_MIN, settings.azure_openai_reasoning_groundedness_max_tokens
    )


def run_smoke(settings: Settings, cost_guard: CostGuard) -> dict[str, Any]:
    """Phase (a): one ``chat`` + one ``chat_json`` call against each deployment. Each call
    reserves (and settles) its OWN worst case from ``cost_guard`` -- via :func:`make_guarded_post`
    patched into the global ``httpx.post`` for the duration of this call -- instead of one flat
    estimate shared by both calls (DA-14.7-migration-live-2a-1).

    DA-14.7-migration-live-2e-2 fix: the reasoning deployment's ``chat``/``chat_json`` calls now
    use :func:`_smoke_bound` (>= 1024, never the tiny 16/32 non-reasoning bound) -- and a response
    whose hidden reasoning tokens STILL exhaust that bound (``AzureReasoningBudgetExhausted``,
    ``rag_app.llm``) is recorded as a distinct, explicit ``"reasoning_budget_exhausted"`` error
    type rather than surfacing as an opaque JSON-decode failure (or, for ``chat_json``, being
    silently swallowed by its own retry+fallback and wrongly reported as ``chat_json_ok=True``)."""
    results: dict[str, Any] = {}
    for deployment, reasoning in ((BASELINE_DEPLOYMENT, False), (CANDIDATE_DEPLOYMENT, True)):
        chat = AzureOpenAIChat(
            endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            deployment=deployment,
            reasoning_model=reasoning,
        )
        entry: dict[str, Any] = {"deployment": deployment, "reasoning_model": reasoning}
        guarded_post = make_guarded_post(cost_guard, "smoke")
        chat_bound = _smoke_bound(
            settings, reasoning=reasoning, non_reasoning_bound=SMOKE_MAX_TOKENS
        )
        json_bound = _smoke_bound(
            settings, reasoning=reasoning, non_reasoning_bound=SMOKE_JSON_MAX_TOKENS
        )

        with _patched_post(guarded_post):
            try:
                # `chat` default param binds THIS iteration's value explicitly (flake8-bugbear
                # B023: a closure over a loop variable without this would read whatever `chat` is
                # by the time the function actually runs -- harmless here since the call happens
                # synchronously in the same iteration, but explicit is safer and silences the
                # otherwise-valid-in-general lint).
                def _call_chat(chat: AzureOpenAIChat = chat, bound: int = chat_bound) -> str:
                    return chat.chat(
                        [{"role": "user", "content": "Reply with the single word: ok"}],
                        max_tokens=bound,
                        call_type="smoke",
                    )

                _call_chat()
                entry["chat_ok"] = True
                entry["api_version"] = chat.api_version
            except httpx.HTTPStatusError as exc:
                entry["chat_ok"] = False
                info = _diagnostic_info(exc)
                entry["chat_status_code"] = info["status_code"]
                entry["chat_error_type"] = info["exception_class"]
                entry["chat_error_code"] = info["error_code"]
                if reasoning and exc.response.status_code == 400:
                    try:
                        version, attempts = probe_api_versions(
                            endpoint=chat.endpoint,
                            api_key=chat.api_key,
                            deployment=deployment,
                            post=guarded_post,
                        )
                        entry["api_version_probe"] = attempts
                        entry["api_version_recommended"] = version
                    except CostCapExceeded:
                        raise
                    except RuntimeError as probe_exc:
                        entry["api_version_probe_failed"] = str(probe_exc)
            except AzureReasoningBudgetExhausted:
                # Distinct from a generic ValueError (DA-14.7-migration-live-2e-2): the call
                # succeeded on the wire, but the reasoning deployment's hidden tokens consumed
                # the whole `chat_bound` before any visible content -- raise the bound further.
                entry["chat_ok"] = False
                entry["chat_error_type"] = "reasoning_budget_exhausted"
                entry["chat_bound_used"] = chat_bound
            except (httpx.HTTPError, ValueError) as exc:
                entry["chat_ok"] = False
                entry["chat_error_type"] = _diagnostic_info(exc)["exception_class"]

            try:

                def _call_chat_json(
                    chat: AzureOpenAIChat = chat, bound: int = json_bound
                ) -> _SmokeSchema:
                    return chat.chat_json(
                        [{"role": "user", "content": 'Reply with JSON {"ok": true}.'}],
                        _SmokeSchema,
                        fallback=_SmokeSchema(ok=False),
                        max_tokens=bound,
                        call_type="smoke",
                    )

                _call_chat_json()
                entry["chat_json_ok"] = True
            except httpx.HTTPStatusError as exc:
                entry["chat_json_ok"] = False
                info = _diagnostic_info(exc)
                entry["chat_json_status_code"] = info["status_code"]
                entry["chat_json_error_type"] = info["exception_class"]
                entry["chat_json_error_code"] = info["error_code"]
            except AzureReasoningBudgetExhausted:
                entry["chat_json_ok"] = False
                entry["chat_json_error_type"] = "reasoning_budget_exhausted"
                entry["chat_json_bound_used"] = json_bound
            except (httpx.HTTPError, ValueError) as exc:
                entry["chat_json_ok"] = False
                entry["chat_json_error_type"] = _diagnostic_info(exc)["exception_class"]

        results[deployment] = entry
    return results


# ---------------------------------------------------------------------------
# Phase (b): reasoning vs visible token budget
# ---------------------------------------------------------------------------


def representative_prompts(
    dialogues: list[Dialogue], n: int = 3
) -> list[tuple[str, list[Message]]]:
    """Up to ``n`` ``(call_type, messages)`` pairs built from real golden-set v2 text: for each
    of up to ``n`` answerable "security" turns, a router-shaped prompt (the raw message), an
    answer-shaped prompt (system + the question, no retrieved chunks) and a groundedness-shaped
    prompt (question + ground truth as the "claim" to check).

    Self-reported scope limit: these are representative SHAPES, not the exact prompts
    ``generation.py`` builds (which need real retrieved chunks from a live DB) -- good enough to
    see the reasoning/visible token RATIO on this app's own text, not a substitute for phase
    (c)'s full-pipeline eval."""
    turns = [
        turn
        for dialogue in dialogues
        for turn in dialogue.turns
        if turn.expected_intent == "security" and turn.answerable and turn.ground_truth
    ][:n]
    prompts: list[tuple[str, list[Message]]] = []
    for turn in turns:
        prompts.append(("router", [{"role": "user", "content": turn.message}]))
        prompts.append(
            (
                "answer",
                [
                    {"role": "system", "content": "Answer using only the provided context."},
                    {"role": "user", "content": turn.message},
                ],
            )
        )
        prompts.append(
            (
                "groundedness",
                [
                    {
                        "role": "user",
                        "content": (
                            f"Question: {turn.message}\nClaim: {turn.ground_truth}\n"
                            "Is the claim supported by the question's topic? yes/no"
                        ),
                    }
                ],
            )
        )
    return prompts


def _raw_reasoning_chat_call(
    *,
    endpoint: str,
    api_key: str,
    deployment: str,
    api_version: str,
    messages: list[Message],
    max_tokens: int,
    post: Callable[..., httpx.Response] | None = None,
) -> dict[str, Any]:
    """Direct HTTP call (bypassing ``AzureOpenAIChat.chat``, which discards ``usage``) so phase
    (b) can read ``usage.completion_tokens_details.reasoning_tokens``. Always sends the
    reasoning-model shape (``max_completion_tokens``, no ``temperature``) -- this phase only ever
    measures the candidate reasoning deployment. ``post`` resolves to ``httpx.post`` INSIDE the
    call (see :func:`probe_api_versions`'s docstring for why -- same monkeypatch-friendliness
    reason)."""
    caller = post or httpx.post
    body = {"messages": messages, "stream": False, "max_completion_tokens": max_tokens}
    response = caller(
        _azure_url(endpoint, deployment, api_version),
        headers=_azure_headers(api_key),
        json=body,
        timeout=180,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    try:
        parse_azure_chat_response(data)  # validate the shape; raises ValueError on a malformed body
    except AzureReasoningBudgetExhausted:
        # Expected/measured outcome for THIS phase, not a failure: phase (b)'s whole purpose is
        # to detect exactly this (hidden reasoning tokens consuming the entire `max_tokens`
        # bound, leaving no visible content) -- `usage.completion_tokens_details.reasoning_tokens`
        # below is what this function actually reads, not the content itself.
        pass
    return data


def measure_token_budget(
    settings: Settings,
    cost_guard: CostGuard,
    dialogues: list[Dialogue],
    *,
    post: Callable[..., httpx.Response] | None = None,
) -> dict[str, Any]:
    """Phase (b): per representative prompt, how many of the completion tokens were hidden
    "reasoning tokens" vs the visible answer -- never stores the prompt/response text itself,
    only ids (``call_type``) and numeric counts.

    DA-14.7-migration-live-2a-2 fix: each raw call now sits inside its own
    ``try/except (httpx.HTTPStatusError, httpx.HTTPError, ValueError)``, exactly like
    :func:`run_smoke`'s calls -- previously this phase had NO such wrapping, so an HTTP error or
    a malformed-body ``ValueError`` propagated uncaught all the way to ``main()``'s entry point, a
    raw Python traceback whose ``HTTPStatusError.__str__`` embeds the live endpoint. A single
    failed prompt now records ``error_type``/``status_code`` and this phase continues measuring
    the REMAINING representative prompts.

    DA-14.7-migration-live-2a-1 fix: routed through the SAME :func:`make_guarded_post` every
    other phase uses -- each call's reservation is still the real per-call worst case
    (``bound`` == ``settings.azure_openai_reasoning_max_tokens``/``...groundedness_max_tokens``,
    exactly what phase (b) already reserved correctly in part 2b), now also SETTLED down to the
    real ``usage.completion_tokens`` the response reports.
    """
    measurements: list[dict[str, Any]] = []
    max_visible = 0
    guarded_post = make_guarded_post(cost_guard, "token-budget", post=post)
    for call_type, messages in representative_prompts(dialogues):
        bound = (
            settings.azure_openai_reasoning_groundedness_max_tokens
            if call_type == "groundedness"
            else settings.azure_openai_reasoning_max_tokens
        )

        def _call_raw(messages: list[Message] = messages, bound: int = bound) -> dict[str, Any]:
            return _raw_reasoning_chat_call(
                endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                deployment=CANDIDATE_DEPLOYMENT,
                api_version=settings.azure_openai_reasoning_api_version,
                messages=messages,
                max_tokens=bound,
                post=guarded_post,
            )

        try:
            data = _call_raw()
        except (httpx.HTTPStatusError, httpx.HTTPError, ValueError) as exc:
            info = _diagnostic_info(exc)
            measurements.append(
                {
                    "call_type": call_type,
                    "bound": bound,
                    "completion_tokens": None,
                    "reasoning_tokens": None,
                    "visible_tokens": None,
                    "error_type": info["exception_class"],
                    "status_code": info["status_code"],
                    "error_code": info["error_code"],
                }
            )
            continue
        usage = data.get("usage", {})
        completion_tokens = int(usage.get("completion_tokens", 0))
        reasoning_tokens = int(
            usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        )
        visible_tokens = max(completion_tokens - reasoning_tokens, 0)
        max_visible = max(max_visible, completion_tokens)
        measurements.append(
            {
                "call_type": call_type,
                "bound": bound,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "visible_tokens": visible_tokens,
            }
        )
    # A generous safety margin over the worst observed total (reasoning + visible), rounded up
    # to the next 256 -- compared against the 7e6b6f0 provisional defaults (3072/512).
    recommended = ((int(max_visible * 1.5) // 256) + 1) * 256 if max_visible else None
    return {
        "measurements": measurements,
        "provisional_max_tokens": settings.azure_openai_reasoning_max_tokens,
        "provisional_groundedness_max_tokens": (
            settings.azure_openai_reasoning_groundedness_max_tokens
        ),
        "recommended_max_tokens": recommended,
    }


# ---------------------------------------------------------------------------
# Phase (c): side-by-side 30-question eval (Option B)
# ---------------------------------------------------------------------------


def azure_subset(dialogues: list[Dialogue], size: int = AZURE_SUBSET_SIZE) -> list[Dialogue]:
    """The "Azure subset" ``status/blocks/14.7-migration.md`` §4 budgets the eval for. No subset
    is defined anywhere else in code today -- documented here, per this block's own
    instructions, as the first ``size`` dialogues in ``eval/golden_set_v2.jsonl``'s own file
    order, deterministically."""
    return dialogues[:size]


def _flipped_questions(results: dict[str, Any]) -> list[dict[str, Any]]:
    """DA-14.7-migration-live-2a-3: every ``(dialogue_id, turn_index)`` whose judge verdict
    (``correct``) differs between :data:`BASELINE_DEPLOYMENT` and :data:`CANDIDATE_DEPLOYMENT` --
    never includes ``reply_text``/prompt text, only ids + the two verdicts."""
    baseline = {
        (row["dialogue_id"], row["turn_index"]): row["correct"]
        for row in results.get(BASELINE_DEPLOYMENT, {}).get("per_turn", [])
    }
    candidate = {
        (row["dialogue_id"], row["turn_index"]): row["correct"]
        for row in results.get(CANDIDATE_DEPLOYMENT, {}).get("per_turn", [])
    }
    flipped: list[dict[str, Any]] = []
    for key in sorted(set(baseline) & set(candidate)):
        if baseline[key] != candidate[key]:
            dialogue_id, turn_index = key
            flipped.append(
                {
                    "dialogue_id": dialogue_id,
                    "turn_index": turn_index,
                    f"{BASELINE_DEPLOYMENT}_correct": baseline[key],
                    f"{CANDIDATE_DEPLOYMENT}_correct": candidate[key],
                }
            )
    return flipped


def run_side_by_side(
    settings: Settings,
    cost_guard: CostGuard,
    session_factory: Callable[[], Any],
    subset: list[Dialogue],
) -> dict[str, Any]:
    """Phase (c): Option B -- re-run BOTH deployments on the identical subset, reusing the real
    pipeline (``evaluate_dialogues``) so the comparison exercises router/memory/retrieval exactly
    like production, not a synthetic shortcut.

    DA-14.7-migration-live-2a-1 fix: no more flat per-deployment reservation. Every Azure HTTP
    call ``evaluate_dialogues`` makes (router, chit-chat/off-topic/abstention reply, memory
    summary, answer, groundedness, judge -- all through the SAME ``chat`` client under test) is
    routed through :func:`make_guarded_post`, patched into the global ``httpx.post`` for this
    deployment's run. ``subset`` is replayed ONE DIALOGUE AT A TIME (not the whole list in one
    ``evaluate_dialogues`` call): a dialogue's own in-memory conversation state is reset fresh by
    ``evaluate_dialogues`` for every new dialogue regardless (see its own docstring), so this is
    behaviourally IDENTICAL to one call over the whole subset -- it just gives this function a
    boundary at which to catch :class:`CostCapExceeded`, count how many dialogues completed, and
    read the REAL per-dialogue tokens/EUR the cost guard just measured off ``cost_guard.log``.

    If the guard stops partway through a deployment's run, this function attaches
    ``exc.partial_results`` (everything built so far, for BOTH deployments) to the SAME
    :class:`CostCapExceeded` and re-raises it, so ``main()``'s existing ``_fail`` handling can
    report exactly how many of ``subset``'s dialogues completed per deployment, not just
    "blocked".

    DA-14.7-migration-live-2a-3 fix: each deployment's entry gets a ``per_turn`` list
    (``dialogue_id``, ``turn_index``, ``correct``, ``latency_s``, ``tokens_in``/``tokens_out``/
    ``eur`` -- never ``reply_text``); the top-level ``flipped`` list (:func:`_flipped_questions`)
    is only computed once BOTH deployments finish without being stopped early.

    DA-14.7-migration-live-2e-3 fix: one dialogue failing for a reason OTHER than the cost cap
    (an Azure HTTP error, a malformed response, anything else unexpected) no longer aborts the
    whole deployment's run -- it is recorded in that deployment's ``failures`` list
    (``dialogue_id`` + :func:`_diagnostic_info`) and the loop continues to the NEXT dialogue,
    exactly like :func:`run_smoke`/:func:`measure_token_budget` already isolate failures per
    call. Only :class:`CostCapExceeded` (a deliberate budget stop, never a per-call bug) still
    breaks the loop early.
    """
    results: dict[str, Any] = {}
    n = len(subset)
    for deployment, reasoning in ((BASELINE_DEPLOYMENT, False), (CANDIDATE_DEPLOYMENT, True)):
        chat = AzureOpenAIChat(
            endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            deployment=deployment,
            reasoning_model=reasoning,
        )
        guarded_post = make_guarded_post(cost_guard, f"side-by-side:{deployment}")
        turn_results: list[Any] = []
        per_turn: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        dialogues_completed = 0
        stopped: CostCapExceeded | None = None
        start = time.perf_counter()
        with session_factory() as session, _patched_post(guarded_post):
            for dialogue in subset:
                log_start = len(cost_guard.log)
                try:
                    dialogue_turns = evaluate_dialogues(session, [dialogue], chat=chat)
                except CostCapExceeded as exc:
                    stopped = exc
                    break
                except Exception as exc:  # noqa: BLE001 - 2e-3: one dialogue's failure must
                    # never lose the rest of this deployment's run (or the OTHER deployment's,
                    # which still runs normally afterwards) -- record and move on.
                    failures.append({"dialogue_id": dialogue.id, **_diagnostic_info(exc)})
                    continue
                dialogue_calls = cost_guard.log[log_start:]
                dialogue_eur = sum(c.get("actual_eur", c["eur"]) for c in dialogue_calls)
                dialogue_tokens_in = sum(
                    c.get("actual_tokens_in", c["tokens_in"]) for c in dialogue_calls
                )
                dialogue_tokens_out = sum(
                    c.get("actual_tokens_out", c["tokens_out"]) for c in dialogue_calls
                )
                n_turns_in_dialogue = len(dialogue_turns) or 1
                for turn in dialogue_turns:
                    turn_results.append(turn)
                    per_turn.append(
                        {
                            "dialogue_id": turn.dialogue_id,
                            "turn_index": turn.turn_index,
                            "correct": turn.correct,
                            "latency_s": round(turn.latency_s, 3),
                            "tokens_in": round(dialogue_tokens_in / n_turns_in_dialogue, 1),
                            "tokens_out": round(dialogue_tokens_out / n_turns_in_dialogue, 1),
                            "eur": round(dialogue_eur / n_turns_in_dialogue, 6),
                        }
                    )
                dialogues_completed += 1
        elapsed_s = time.perf_counter() - start
        metrics = compute_dialogue_metrics(turn_results)
        results[deployment] = {
            "metrics": metrics.as_dict(),
            "n_turns": len(turn_results),
            "wall_time_s": round(elapsed_s, 1),
            "dialogues_completed": dialogues_completed,
            "dialogues_total": n,
            "per_turn": per_turn,
            "failures": failures,
        }
        if stopped is not None:
            stopped.partial_results = results  # type: ignore[attr-defined]
            raise stopped
    results["flipped"] = _flipped_questions(results)
    return results


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------


def summarize(report: dict[str, Any]) -> str:
    """Short human summary (this is what the user pastes back) -- same no-secrets discipline as
    the full report: ids, status codes, error types, token usage, scores, EUR only."""
    lines = [
        f"Azure OpenAI side-by-side check -- total estimated EUR {report.get('total_eur', 0.0):.4f}"
    ]
    smoke = report.get("smoke")
    if smoke:
        lines.append("smoke:")
        for deployment, entry in smoke.items():
            chat_status = "ok" if entry.get("chat_ok") else f"FAIL ({entry.get('chat_error_type')})"
            json_status = (
                "ok" if entry.get("chat_json_ok") else f"FAIL ({entry.get('chat_json_error_type')})"
            )
            lines.append(f"  {deployment}: chat={chat_status} chat_json={json_status}")
            if entry.get("api_version_recommended"):
                lines.append(f"    -> recommended api-version: {entry['api_version_recommended']}")
    token_budget = report.get("token_budget")
    if token_budget:
        lines.append(
            "token budget: provisional max_tokens="
            f"{token_budget.get('provisional_max_tokens')} -> recommended "
            f"{token_budget.get('recommended_max_tokens')}"
        )
    side_by_side = report.get("side_by_side")
    if side_by_side:
        lines.append("side-by-side:")
        for deployment, entry in side_by_side.items():
            if deployment == "flipped":
                continue
            lines.append(
                f"  {deployment}: dialogues={entry.get('dialogues_completed')}/"
                f"{entry.get('dialogues_total')} n_turns={entry['n_turns']} "
                f"wall_time_s={entry['wall_time_s']} metrics={entry['metrics']}"
            )
            failures = entry.get("failures") or []
            if failures:
                lines.append(f"    failures: {len(failures)} dialogue(s) failed and were skipped")
        flipped = side_by_side.get("flipped")
        if flipped is not None:
            lines.append(f"  flipped questions (baseline vs candidate): {len(flipped)}")
    for blocked_key in ("smoke_blocked", "token_budget_blocked", "side_by_side_blocked"):
        if report.get(blocked_key):
            lines.append(f"COST GUARD STOPPED {blocked_key}: {report[blocked_key]}")
    # DA-14.7-migration-live-2e-2/2e-3: a phase that failed for a reason OTHER than the cost cap
    # still gets a line here -- the OTHER phases' results above are never erased by this.
    for error_key in ("smoke_error", "token_budget_error", "side_by_side_error"):
        info = report.get(error_key)
        if info:
            lines.append(
                f"PHASE ERROR {error_key}: {info.get('exception_class')} "
                f"(status_code={info.get('status_code')}, error_code={info.get('error_code')})"
            )
    return "\n".join(lines)


def _write_report(report: dict[str, Any], output_dir: Path | None) -> None:
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output_dir / "summary.md").write_text(summarize(report) + "\n", encoding="utf-8")


def check_config_lines(explicit_prices: Path | None = None) -> list[str]:
    """Part 2c: resolve the prices file + golden-set path and format the
    ``gpt-4.1-mini``/``gpt-5-mini`` :data:`PRICE_TIER` prices, exactly what ``--check-config``
    prints. **Zero network call, zero env-file read** (never calls :func:`get_settings`) -- only
    path resolution and a local JSON read, so this is safe to run from any worktree to PROVE the
    real ``azure_prices.json`` is found before anything that could cost money. Raises
    :class:`FileNotFoundError` (unchanged, naming every path tried) if resolution fails -- the
    caller is responsible for turning that into a clean non-zero exit, never a stack trace to the
    user."""
    prices_path = resolve_prices_path(explicit_prices)
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    lines = [f"prices file: {prices_path}"]
    for model in (BASELINE_DEPLOYMENT, CANDIDATE_DEPLOYMENT):
        row = prices.get("openai", {}).get("models", {}).get(model, {}).get(PRICE_TIER)
        if row is None:
            lines.append(f"{model} ({PRICE_TIER}): MISSING from prices file")
            continue
        lines.append(
            f"{model} ({PRICE_TIER}): input={row['input_per_1m_usd']} USD/1M tokens "
            f"output={row['output_per_1m_usd']} USD/1M tokens"
        )
    lines.append(f"golden set v2: {DEFAULT_GOLDEN_SET_V2} exists={DEFAULT_GOLDEN_SET_V2.is_file()}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Azure OpenAI gpt-4.1-mini vs gpt-5-mini live side-by-side check (part 2b)."
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="run only phase (a) + (b), skip the 30-question eval",
    )
    parser.add_argument(
        "--max-eur", type=float, default=0.30, help="hard cost cap for this run (EUR)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="where to write results.json/summary.md"
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=None,
        help=(
            "explicit path to azure_prices.json (overrides $SECRAG_AZURE_PRICES and the "
            "repo/main-repo fallback -- see resolve_prices_path())"
        ),
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help=(
            "resolve the prices/golden-set paths and print the gpt-4.1-mini/gpt-5-mini DataZone "
            "prices, then exit -- no env file read, no network call, no cost guard run. Use this "
            "FIRST from a new worktree to prove path resolution works."
        ),
    )
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - DA-14.7-migration-live-2a-2's last-resort safety
        # net: if anything still escapes every phase's own exception handling above, sanitize it
        # the same way (exception CLASS + HTTP status only -- never str(exc), never a traceback,
        # which could otherwise embed the live endpoint via httpx.HTTPStatusError.__str__) before
        # it reaches the terminal or the evidence file, and exit non-zero. Part 2e: with every
        # phase in ``_run`` now isolating its own exceptions (see that function's ``_checkpoint``),
        # this should only ever fire for a failure BEFORE any phase starts (e.g. ``get_settings``/
        # ``CostGuard.from_file`` itself) -- there is no partial report to lose at that point.
        info = _diagnostic_info(exc)
        print(
            f"ERROR: unhandled {info['exception_class']} (status_code={info['status_code']})",
            file=sys.stderr,
        )
        if args.output_dir is not None:
            try:
                args.output_dir.mkdir(parents=True, exist_ok=True)
                (args.output_dir / "summary.md").write_text(
                    f"UNHANDLED ERROR: {info['exception_class']} "
                    f"(status_code={info['status_code']})\n",
                    encoding="utf-8",
                )
                (args.output_dir / "results.json").write_text(
                    json.dumps({"unhandled_error": info}, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        return 1


def _run(args: argparse.Namespace) -> int:
    if args.check_config:
        try:
            for line in check_config_lines(args.prices):
                print(line)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        prices_path = resolve_prices_path(args.prices)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    settings = get_settings()
    cost_guard = CostGuard.from_file(args.max_eur, path=prices_path)
    report: dict[str, Any] = {
        "generated_at_utc": _dt.datetime.now(_dt.UTC).isoformat(),
        "max_eur": args.max_eur,
    }

    def _checkpoint() -> None:
        """DA-14.7-migration-live-2e-2/2e-3: write whatever ``report`` holds AT EVERY phase
        boundary (success, cost-guard stop, or an isolated phase error alike). The FIRST real
        user run lost every phase's already-computed results to one unhandled exception in the
        LAST phase it reached -- this ensures a failure in phase N+1 never erases phase N's
        already-written evidence."""
        report["cost_log"] = cost_guard.log
        report["total_eur"] = round(cost_guard.spent_eur, 6)
        _write_report(report, args.output_dir)

    def _fail(key: str, exc: CostCapExceeded) -> int:
        report[key] = str(exc)
        partial = getattr(exc, "partial_results", None)
        if partial is not None:
            report["side_by_side"] = partial
        _checkpoint()
        print(summarize(report))
        return 2

    try:
        report["smoke"] = run_smoke(settings, cost_guard)
    except CostCapExceeded as exc:
        return _fail("smoke_blocked", exc)
    except Exception as exc:  # noqa: BLE001 - 2e: phase isolation -- see this function's
        # ``_checkpoint`` and the module docstring. A failure in THIS phase must not stop the
        # REST of the run; it is recorded and phase (b) still runs.
        report["smoke_error"] = _diagnostic_info(exc)
    _checkpoint()

    dialogues = load_dialogues()
    try:
        report["token_budget"] = measure_token_budget(settings, cost_guard, dialogues)
    except CostCapExceeded as exc:
        return _fail("token_budget_blocked", exc)
    except Exception as exc:  # noqa: BLE001 - 2e: phase isolation, see above.
        report["token_budget_error"] = _diagnostic_info(exc)
    _checkpoint()

    if not args.smoke_only:
        from rag_app.db.session import make_session_factory

        subset = azure_subset(dialogues)
        session_factory = make_session_factory()
        try:
            report["side_by_side"] = run_side_by_side(settings, cost_guard, session_factory, subset)
        except CostCapExceeded as exc:
            return _fail("side_by_side_blocked", exc)
        except Exception as exc:  # noqa: BLE001 - 2e: phase isolation, see above.
            report["side_by_side_error"] = _diagnostic_info(exc)
        _checkpoint()

    print(summarize(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
