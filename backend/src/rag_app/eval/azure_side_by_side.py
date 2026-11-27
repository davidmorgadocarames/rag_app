"""Azure OpenAI gpt-4.1-mini vs gpt-5-mini live side-by-side check (14.7-migration-live, part 2b).

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
    ``rag_app.eval.runner.evaluate_dialogues``/``metrics``/``judge``. Token/EUR numbers for this
    phase are the PLAN's own per-question estimate (:data:`TOKENS_IN_PER_QUESTION`/
    :data:`TOKENS_OUT_PER_QUESTION`), not measured per-call ``usage`` the way phase (b) is --
    self-reported limitation: wiring real per-call usage through
    ``DialogueTurnResult``/``runner.py`` is out of this block's declared ``touches``.

Cost guard (:class:`CostGuard`): estimates EUR from ``status/azure_prices.json``'s
``datazone_standard`` tier for BOTH models (DEC-061's migration target deployment is
DataZoneStandard-only; pricing both deployments on that tier keeps the side-by-side comparison
apples-to-apples with the SKU the candidate will actually run on) BEFORE each reservation, adds it
to the running total, and raises :class:`CostCapExceeded` -- **no call is made** -- rather than
ever letting the running total exceed ``--max-eur`` (default 0.30, this block's
``PARALLEL_PLAN.md`` budget line). Retries: :func:`_call_with_single_retry` allows at most ONE
retry, only on HTTP 429, with a short fixed backoff -- never a retry loop.

Output discipline (never violated anywhere in this module): the report this module builds and
prints/writes contains only ids, call types, status codes, exception CLASS NAMES (never
``str(exc)`` -- an exception's string form can embed the request URL), token counts and EUR
figures. It never contains an endpoint, an API key, or full prompt/response text.

**Part 2c fix (worktree prices-file bug, F22-class).** ``status/`` is a symlink into the
private planning folder only in the MAIN repo clone (``~/proyectos/rag_app``); a `git worktree`
such as ``~/proyectos/wt/14-A1`` has no such symlink -- its own ``status/`` holds only the
tracked ``status/blocks/`` directory, never ``azure_prices.json``. Part 2b's
``DEFAULT_PRICES_PATH`` pointed unconditionally at ``REPO_ROOT/status/azure_prices.json``, which
is exactly this missing path when run from a worktree -- the cost guard would have crashed with
a raw ``FileNotFoundError`` before any Azure call, which is fail-safe by accident, not by design,
and the error message named no alternative. :func:`resolve_prices_path` now tries, in order,
stopping at the first path that exists: the explicit ``--prices`` CLI flag, the
``SECRAG_AZURE_PRICES`` env var, ``REPO_ROOT/status/azure_prices.json`` (works from the main repo
clone), then ``~/proyectos/rag_app/status/azure_prices.json`` (the hard-coded main-repo fallback,
so a worktree run finds the real file without the user having to pass anything). If none exists
it raises :class:`FileNotFoundError` naming every path it tried, BEFORE ``get_settings()`` is even
called (so before any possible Azure call) -- never "no cost guard, proceed anyway". The new
``--check-config`` flag resolves this path and the golden-set path and prints the
``gpt-4.1-mini``/``gpt-5-mini`` DataZone prices with zero env-file read and zero network call, so
the user can prove path resolution works from their own worktree before running anything live.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
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
from rag_app.llm import AzureOpenAIChat, Message, parse_azure_chat_response

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

# Per-question token arithmetic from status/blocks/14.7-migration.md §4 ("router ~500 in / ~200
# out, answer ~3400 in / ~400 out" per question, 2 LLM calls -> ~3900 in / ~600 out). Reused here
# as phase (c)'s cost-guard reservation AND -- absent real per-call usage plumbing through
# runner.py/DialogueTurnResult (out of this block's declared touches) -- as the reported
# token/EUR numbers themselves. Self-reported limitation: these are the PLAN's ESTIMATED tokens,
# not measured per-call usage the way phase (b) is.
TOKENS_IN_PER_QUESTION = 3900.0
TOKENS_OUT_PER_QUESTION = 600.0


# ---------------------------------------------------------------------------
# Cost guard
# ---------------------------------------------------------------------------


class CostCapExceeded(RuntimeError):
    """Raised by :meth:`CostGuard.reserve` BEFORE any call is made -- no partial charge, no
    call happens once this is raised."""


@dataclass
class CostGuard:
    """Tracks EUR spent (estimated) this run and refuses to let it exceed ``max_eur``.

    ``reserve()`` must be called before every group of Azure OpenAI calls a phase is about to
    make; it estimates the EUR cost of the given tokens from ``prices`` (never from a live call),
    and either commits the estimate to ``spent_eur`` and returns it, or raises
    :class:`CostCapExceeded` with the running total left untouched.
    """

    prices: dict[str, Any]
    max_eur: float
    spent_eur: float = 0.0
    log: list[dict[str, Any]] = field(default_factory=list)

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
        """Estimate BEFORE the call. Raises :class:`CostCapExceeded` (no call made, ``spent_eur``
        unchanged) if committing this estimate would push the running total over ``max_eur``;
        otherwise commits it and returns the estimate dict (also appended to ``self.log``)."""
        estimate = self.estimate(model=model, tokens_in=tokens_in, tokens_out=tokens_out)
        projected = self.spent_eur + estimate["eur"]
        if projected > self.max_eur:
            raise CostCapExceeded(
                f"{phase}: projected EUR {projected:.4f} would exceed --max-eur {self.max_eur:.4f} "
                f"(already spent {self.spent_eur:.4f} + this reservation's estimate "
                f"{estimate['eur']:.4f} for {model}); no call made"
            )
        self.spent_eur = projected
        entry = {"phase": phase, **estimate, "running_total_eur": round(self.spent_eur, 6)}
        self.log.append(entry)
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
    was at import time, before any monkeypatch exists."""
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


def run_smoke(settings: Settings, cost_guard: CostGuard) -> dict[str, Any]:
    """Phase (a): one ``chat`` + one ``chat_json`` call against each deployment. Reserves its
    own (small, fixed) token estimate from ``cost_guard`` per deployment before making either
    call for that deployment."""
    results: dict[str, Any] = {}
    for deployment, reasoning in ((BASELINE_DEPLOYMENT, False), (CANDIDATE_DEPLOYMENT, True)):
        cost_guard.reserve("smoke", model=deployment, tokens_in=150.0, tokens_out=80.0)
        chat = AzureOpenAIChat(
            endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            deployment=deployment,
            reasoning_model=reasoning,
        )
        entry: dict[str, Any] = {"deployment": deployment, "reasoning_model": reasoning}

        try:
            # `chat`/`messages` default params bind THIS iteration's value explicitly
            # (flake8-bugbear B023: a closure over a loop variable without this would read
            # whatever `chat` is by the time the function actually runs -- harmless here since
            # `_call_with_single_retry` calls `fn()` synchronously in the same iteration, but
            # explicit is safer and silences the (otherwise valid-in-general) lint).
            def _call_chat(chat: AzureOpenAIChat = chat) -> str:
                return chat.chat(
                    [{"role": "user", "content": "Reply with the single word: ok"}],
                    max_tokens=16,
                    call_type="smoke",
                )

            _call_with_single_retry(_call_chat)
            entry["chat_ok"] = True
            entry["api_version"] = chat.api_version
        except httpx.HTTPStatusError as exc:
            entry["chat_ok"] = False
            entry["chat_status_code"] = exc.response.status_code
            entry["chat_error_type"] = type(exc).__name__
            if reasoning and exc.response.status_code == 400:
                try:
                    version, attempts = probe_api_versions(
                        endpoint=chat.endpoint, api_key=chat.api_key, deployment=deployment
                    )
                    entry["api_version_probe"] = attempts
                    entry["api_version_recommended"] = version
                except RuntimeError as probe_exc:
                    entry["api_version_probe_failed"] = str(probe_exc)
        except httpx.HTTPError as exc:
            entry["chat_ok"] = False
            entry["chat_error_type"] = type(exc).__name__

        try:

            def _call_chat_json(chat: AzureOpenAIChat = chat) -> _SmokeSchema:
                return chat.chat_json(
                    [{"role": "user", "content": 'Reply with JSON {"ok": true}.'}],
                    _SmokeSchema,
                    fallback=_SmokeSchema(ok=False),
                    max_tokens=32,
                    call_type="smoke",
                )

            _call_with_single_retry(_call_chat_json)
            entry["chat_json_ok"] = True
        except httpx.HTTPStatusError as exc:
            entry["chat_json_ok"] = False
            entry["chat_json_status_code"] = exc.response.status_code
            entry["chat_json_error_type"] = type(exc).__name__
        except httpx.HTTPError as exc:
            entry["chat_json_ok"] = False
            entry["chat_json_error_type"] = type(exc).__name__

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
    parse_azure_chat_response(data)  # validate the shape; raises ValueError on a malformed body
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
    only ids (``call_type``) and numeric counts."""
    measurements: list[dict[str, Any]] = []
    max_visible = 0
    for call_type, messages in representative_prompts(dialogues):
        bound = (
            settings.azure_openai_reasoning_groundedness_max_tokens
            if call_type == "groundedness"
            else settings.azure_openai_reasoning_max_tokens
        )
        cost_guard.reserve(
            f"token-budget:{call_type}",
            model=CANDIDATE_DEPLOYMENT,
            tokens_in=200.0,
            tokens_out=float(bound),
        )

        def _call_raw(messages: list[Message] = messages, bound: int = bound) -> dict[str, Any]:
            return _raw_reasoning_chat_call(
                endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                deployment=CANDIDATE_DEPLOYMENT,
                api_version=settings.azure_openai_reasoning_api_version,
                messages=messages,
                max_tokens=bound,
                post=post,
            )

        data = _call_with_single_retry(_call_raw)
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


def run_side_by_side(
    settings: Settings,
    cost_guard: CostGuard,
    session_factory: Callable[[], Any],
    subset: list[Dialogue],
) -> dict[str, Any]:
    """Phase (c): Option B -- re-run BOTH deployments on the identical subset, reusing the real
    pipeline (``evaluate_dialogues``) so the comparison exercises router/memory/retrieval exactly
    like production, not a synthetic shortcut."""
    results: dict[str, Any] = {}
    n = len(subset)
    for deployment, reasoning in ((BASELINE_DEPLOYMENT, False), (CANDIDATE_DEPLOYMENT, True)):
        cost_guard.reserve(
            f"side-by-side:{deployment}",
            model=deployment,
            tokens_in=TOKENS_IN_PER_QUESTION * n,
            tokens_out=TOKENS_OUT_PER_QUESTION * n,
        )
        chat = AzureOpenAIChat(
            endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            deployment=deployment,
            reasoning_model=reasoning,
        )
        start = time.perf_counter()
        with session_factory() as session:
            turn_results = evaluate_dialogues(session, subset, chat=chat)
        elapsed_s = time.perf_counter() - start
        metrics = compute_dialogue_metrics(turn_results)
        results[deployment] = {
            "metrics": metrics.as_dict(),
            "n_turns": len(turn_results),
            "wall_time_s": round(elapsed_s, 1),
            "estimated_tokens_in": TOKENS_IN_PER_QUESTION * n,
            "estimated_tokens_out": TOKENS_OUT_PER_QUESTION * n,
        }
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
            lines.append(
                f"  {deployment}: n_turns={entry['n_turns']} wall_time_s={entry['wall_time_s']} "
                f"metrics={entry['metrics']}"
            )
    for blocked_key in ("smoke_blocked", "token_budget_blocked", "side_by_side_blocked"):
        if report.get(blocked_key):
            lines.append(f"COST GUARD STOPPED {blocked_key}: {report[blocked_key]}")
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

    def _fail(key: str, exc: CostCapExceeded) -> int:
        report[key] = str(exc)
        report["cost_log"] = cost_guard.log
        report["total_eur"] = round(cost_guard.spent_eur, 6)
        _write_report(report, args.output_dir)
        print(summarize(report))
        return 2

    try:
        report["smoke"] = run_smoke(settings, cost_guard)
    except CostCapExceeded as exc:
        return _fail("smoke_blocked", exc)

    dialogues = load_dialogues()
    try:
        report["token_budget"] = measure_token_budget(settings, cost_guard, dialogues)
    except CostCapExceeded as exc:
        return _fail("token_budget_blocked", exc)

    if not args.smoke_only:
        from rag_app.db.session import make_session_factory

        subset = azure_subset(dialogues)
        session_factory = make_session_factory()
        try:
            report["side_by_side"] = run_side_by_side(settings, cost_guard, session_factory, subset)
        except CostCapExceeded as exc:
            return _fail("side_by_side_blocked", exc)

    report["cost_log"] = cost_guard.log
    report["total_eur"] = round(cost_guard.spent_eur, 6)
    _write_report(report, args.output_dir)
    print(summarize(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
