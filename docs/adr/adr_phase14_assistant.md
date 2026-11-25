# ADR phase 14 — Conversational assistant

- **Status:** **In progress.** Created at block **14-A1** (golden set v2 schema + items);
  extended at **14-A2** (runner + metrics, "before" baseline + floors) and **14-B1**
  (`chat_json` structured output). The router/rewriting/memory, conversational-answer,
  visible-plan, latency/cost/dashboard and Azure-migration decisions (blocks 14-B2, 14-C1/C2,
  14-D1/D2/D3, 14.7/14.7-migration) are added as those blocks land. Do not read this file as a
  complete phase-14 ADR until the "Docs + Azure promotion" block (14.6) closes it out.
- **Branch:** `phase-14-assistant`.
- **Related:** `docs/TRD.md` §6.1 (golden set v2, same decision, shorter); `docs/PRD.md`
  (assistant behaviour, added at a later block); DEBT-006 (reranker truncation edge probe),
  DEBT-020 (Azure OpenAI data-zone quota re-check, re-checked separately at 14.7-migration),
  DEBT-049 (misspelling-tolerance variants, re-verified against this golden set).

## Context

The pre-14 pipeline classifies intent by regex and defaults to `security` for anything that is
not an exact greeting, so a single typo ("hol") routes straight into RAG and abstains; the SSE
stream handler forwards only `request.question`, so a follow-up ("What about OS commands?") loses
its subject; and the 14-item v1 golden set (English, single-turn, no chit-chat/typo/follow-up
coverage) cannot measure any of this (findings F14.1–F14.7 from the Phase 14 design review). Fixing the
pipeline (later blocks) without first being able to measure it would mean shipping on faith.

## Decision — golden set v2 dialogue schema (T14.0.2/T14.0.3)

Add a **dialogue** schema that is a strict superset of the existing flat schema, rather than
replacing it or forking a parallel format:

- A `Dialogue` is `{"id", "turns": [...]}`; each `Turn` carries `message`, `expected_intent`
  (`security | chitchat | followup | ambiguous | off_topic`), `language` (`en | es`) and, only for
  turns the pipeline is expected to retrieve/answer, `expected_doc` / `answerable` /
  `ground_truth`.
- **Single-turn items are one-turn dialogues.** The v1 loader (`GoldenItem`/`load_golden_set`,
  reading `eval/golden_set.jsonl`) is untouched — zero lines changed — so the current runner and
  every test that exercises it keeps passing exactly as before. The new loader (`load_dialogues`)
  additionally accepts a bare v1 record (no `"turns"` key) and wraps it into a one-turn `Dialogue`
  with the only two values every v1 item actually has (`expected_intent="security"`,
  `language="en"`), so v1 files are loadable through the v2 loader too without being rewritten.
- `eval/golden_set_v2.jsonl` (new file, 48 items: 38 single-turn + 10 dialogues) is the first
  draft of golden set v2's content (P14-D4: "Claude drafts, user reviews"), covering every
  category T14.0.3 named: misspellings (a lone `"hol"`/`"hell"` as a typo greeting vs. `"hell"`
  inside a real sentence, `"injecion"`, `"preparde statements"`), Spanish questions, chit-chat
  (en/es), follow-ups resolved 1 turn back and 3+ turns back (en/es), ambiguous turns, off-topic
  turns, and abstention in both languages — plus one DEBT-006 probe item.

### Alternatives considered

1. **Replace the v1 file/loader outright.** Rejected: breaks "the existing runner/eval still
   passes unchanged" (this block's explicit constraint) for no benefit — the runner migration is
   a separate block (14-A2) that needs v1 and v2 to coexist during the transition anyway.
2. **A parallel, unrelated multi-turn format with no backward-compat wrapping.** Rejected: 14-A2's
   runner would need two entirely separate code paths (one per schema) instead of one loader that
   returns `Dialogue` objects uniformly; the wrapping costs ~15 lines and removes that duplication.

## Known limitations (self-reported, not just design) — 14-A1

- **Narrow corpus.** The fixture's `expected_doc`s are drawn from a single-topic corpus (OWASP
  injection + broken-access-control cheat sheets and Top-10 pages, `docs/TRD.md` §3) — it cannot
  exercise retrieval across a broad corpus, and every Spanish item is hand-written (the corpus
  itself is English-only), not a translated real document.
- **DEBT-006 probe is unverified at write time.** `trunc-sqli-error-messages` targets a detail
  expected to sit late in a long cheat-sheet section (reranker `max_length=256` truncates the
  chunk's tail, `docs/TRD.md` §3); confirmed (or not) by the 14-A2 baseline run below.
  **14-A2 result:** recall did NOT fail — `cs-sql-injection-prevention` is retrieved and the item
  is judged `correct: true` — but the generated answer text is anomalous: a bare `"[1]"` citation
  marker with no prose at all. The judge still called it CORRECT, which is itself a small,
  separate finding (a near-empty, citation-only "answer" should probably not pass a correctness
  check) — recorded for the orchestrator in `status/blocks/14-A2.md` rather than acted on here
  (out of this block's scope: T14.0.4/T14.0.5 build the measurement, not fix `judge_correctness`
  or the prompt that produced the thin answer).
- **Item count is a first pass, not a ceiling.** The Phase 14.0 plan calls for roughly 60
  single-turn items + 10 dialogues for the whole sub-phase; this block ships 38 + 10 = 48
  (DEC-052 = A, user 2026-11-25: accepted as the first draft, add items only if the baseline
  looks noisy — see the table below).

## Decision — golden-set v2 runner + "before" baseline (T14.0.4/T14.0.5, block 14-A2)

`rag_app.eval.runner.evaluate_dialogues` replays every dialogue turn through TODAY's placeholder
core (`classify_intent` chit-chat fast path, or retrieve + `answer_from_chunks`), **standalone per
turn** — no shared conversational core, router or memory exists yet (T14.2.1/T14.2.2, block
14-B2), so a follow-up turn gets no context from earlier turns, exactly like the live stream
handler does today (F14.2: it only ever forwards `request.question`). This is deliberate: it is
the "before" measurement the plan's floors are compared against, not a simulation of the future
router — several floors are expected to be missed here precisely because the feature they
measure does not exist until later blocks.

`rag_app.eval.metrics.compute_dialogue_metrics` aggregates `intent_accuracy`, `noisy_recall`
(turns from a `typo-*`-prefixed dialogue id — a naming convention, not a schema field),
`follow_up_resolution`, `clarification_rate` (`looks_like_clarifying_question` heuristic — no
clarification code path exists before 14.3, so this is 0 today by construction, never
hard-coded), `conversational_reply_rate`, `abstention_no_claims_rate`
(`rag_app.eval.judge.judge_no_claims`, the validated-judge mechanism named in the task) and
`greeting_reply_p95_s` (measurable today); `router_p95_s` stays `None` (no router call exists to
time).

### Floor-enforcement decision

The Phase 14.0 plan lists the floors next to "baseline on current code" with no explicit
enforcement date; §14.2's own Gate line reads **"floors from 14.0"**, read here as "14.0 records
them, 14.2 (once the router/memory it measures exists) is where they start being enforced."
`eval/golden_set_v2_floors.json` carries `enforce_now: false` accordingly — the `eval` gate step
prints every value-vs-floor comparison every run, but never fails on a v2 metric yet, so
`gate.sh --full` stays green on this "before" baseline as required. Alternatives: enforce now
(would fail `--full` until the router exists) or enforce only the one floor arguably already met
(`conversational_reply_rate`, predates Phase 14) — recorded as a deviation for the
orchestrator/user in `status/blocks/14-A2.md`, not applied here.

### "Before" baseline (measured, golden set v2, 48 items / 63 turns)

Real `--only eval` run against the gate stack (seeded corpus, native Ollama qwen + bge-m3),
`eval/golden_set_v2_baseline.json`, gate log `gate-20261125T000456Z-1128878.log`:

| Metric | Measured | Floor | Met now? | Enforced now? |
|---|---|---|---|---|
| intent_accuracy | 0.571 | ≥ 0.90 | **no** | no (14.2) |
| noisy_recall | 1.000 | ≥ 0.90 | yes | no (14.2) |
| follow_up_resolution | 0.500 | ≥ 0.85 | **no** | no (14.2) |
| clarification_rate | 0.000 | (not floored) | — | no |
| conversational_reply_rate | 0.562 | 1.00 | **no** | no (14.2) |
| abstention_no_claims_rate | 1.000 | (not floored) | — | no |
| greeting_reply_p95_s | ~0.00003 s | ≤ 2.0 s | yes | no (14.2) |
| router_p95_s | N/A (no router yet) | ≤ 1.5 s local / ≤ 1 s Azure | N/A | no (14.2) |

(v1 golden set, unaffected: `retrieval_recall`/`faithfulness`/`correctness`/`correct_abstention`
all 1.0, same as before this block — the v1 `eval` gate path is untouched.)

**Reading the gaps (expected, this is the point of a "before" baseline):**
- `noisy_recall` and `greeting_reply_p95_s` already clear their floor — both ride on features that
  predate Phase 14 (hybrid retrieval's existing typo tolerance; the chit-chat fast path's
  near-zero-cost template reply, T12.3.0c).
- `intent_accuracy` (36/63 overall) breaks down as `security` 27/27 (classify_intent's own
  default, trivially "correct" whenever the turn is in fact security) and `chitchat` 9/16, but
  **0/6 `followup`, 0/8 `off_topic`, 0/6 `ambiguous`** — the placeholder's binary classifier
  cannot produce any of those three labels at all, so every such turn is a guaranteed miss until
  the real router exists (T14.2.1). The `chitchat` shortfall (7/16 missed) is a second, smaller
  gap: general phrasing outside the greeting/farewell/thanks lexicon ("How are you?", "What's
  your name?", "Tell me a joke" and their Spanish equivalents) — exactly F14.1's gap, now
  quantified.
- `follow_up_resolution` (3/6) fails whenever the follow-up message has no resolvable subject on
  its own (*"Where does it rank…?"*, *"What about the 2025 edition?"*) and succeeds when the raw
  follow-up text happens to be self-contained enough for hybrid search alone (*"What about OS
  commands?"*, *"¿Y los comandos del sistema operativo?"*, *"what about least privilege db
  accounts"*) — i.e. recall degrades exactly where memory/rewriting (14.2) is needed, not
  uniformly.
- `conversational_reply_rate` (9/16) fails on the same general-chit-chat gap as `intent_accuracy`
  above (`chat-en-1/4/5`, `chat-es-1/4/5`, `dlg-chitchat-then-security` turn 0) — every exact
  greeting/farewell/thanks and every typo of one ("hol", "hell") already passes.
- `abstention_no_claims_rate` is 1.0 because every abstention today is the fixed sentinel
  `_ABSTENTION_TEXT`, which trivially makes no claim — this floor becomes meaningful once 14.3
  introduces LLM-phrased conversational abstentions that could, in principle, slip in a claim.

**N=38 single-turn items (DEC-052 = A):** none of the ratios above show obvious instability from
sample size alone (the failing metrics fail on a clear, explainable majority of their own
sub-population, not a 1-item swing) — no evidence yet that more items are needed before 14.2; if
intent_accuracy/follow_up_resolution still look marginal once the router exists, revisit then.

## Known limitations (self-reported, not just design) — 14-A2

- **Noisy-turn detection is a naming convention, not a schema field.** `is_noisy` is derived from
  the dialogue id's `typo-` prefix (every current noisy item is single-turn); a future noisy item
  placed inside a multi-turn dialogue at a turn other than the first would need a real per-turn
  flag.
- **`judge_no_claims` is not independently kappa-validated.** It reuses the same validated
  judge model/prompt style as `judge_correctness` (temperature 0, bounded tokens), but this
  specific question has not itself been scored against a labeled set the way
  `validate_judge.py` does for correctness.
- **`clarification_rate`/`router_p95_s` are placeholders by necessity**, not incomplete work:
  neither a clarification code path nor a router call exists yet to measure for real.
- **N=38 single-turn items (DEC-052 = A).** If any floor's measurement above looks unstable at
  this sample size, the table/log says so explicitly rather than reporting a single confident
  number from too few items.

## Decision — `chat_json` structured output (T14.1.1/T14.1.2, block 14-B1)

Both `OllamaChat` and `AzureOpenAIChat` (`backend/src/rag_app/llm.py`) gain a `chat_json(messages,
schema, *, fallback, ...)` method with the same `max_tokens`/`num_ctx`/`call_type` shape as
`chat`/`chat_stream`, so a future router (T14.2.3) or summary (T14.2.6) call site is a drop-in:

- **Ollama**: the request's top-level `format` field is set to `schema.model_json_schema()`
  (Ollama's own structured-output mechanism — a JSON-schema object, not the provider's `max_tokens`/
  `num_ctx`/`options` shape, which is unchanged).
- **Azure OpenAI**: `response_format: {"type": "json_schema", "json_schema": {"name":
  schema.__name__, "schema": ..., "strict": true}}`. Azure's `strict: true` mode requires
  `additionalProperties: false` on every object sub-schema (top level and any nested `$defs`),
  which Pydantic's `model_json_schema()` does not set on its own — a small recursive helper
  (`_strict_json_schema`) adds it without needing a second schema-authoring mechanism.
- **Validate, one bounded retry, then the caller's `fallback`** (T14.1.2), shared by both
  providers through one module-level `_chat_json_with_retry` helper so the retry/fallback policy
  cannot drift between them: parse the reply as JSON, validate it against the Pydantic `schema`
  (`model_validate`); on `JSONDecodeError`/`ValidationError`, retry once (two HTTP calls total,
  never more); a second failure returns `fallback` (the caller's own safe default, e.g. a router
  output that routes to today's "security" intent) rather than raising — a malformed router/
  summary reply must never break the chat flow for the user. No prompt/response content is
  logged on failure, only the call type, schema class name and error class (same PII discipline as
  the rest of this module).
- `num_ctx`/`call_type` on `chat_json` are accepted as a passthrough exactly like
  `chat`/`chat_stream` (ignored by Azure, pinned explicitly for Ollama); this block does **not**
  add `num_ctx_router`/`num_ctx_summary` constants to `config.py` — there is no real router/summary
  call site yet to pin them from (T14.2.3/T14.2.6, blocks 14-B2/14-C1). Those blocks follow the
  pattern `test_llm_bounds.py::test_router_and_judge_calls_now_pin_num_ctx_to_match_answer_
  groundedness` already enforces for the agentic rewrite and the eval judge: once the router/
  summary calls can share the answer/groundedness Ollama process (same keep-alive window), they
  must also pin `num_ctx_answer` to avoid a mid-request model reload (config.py's existing
  `num_ctx_answer` comment already flags this as a "future conversation-summary call" risk).
- `chat_json` is added to `test_llm_bounds.py`'s static `_LLM_METHODS` check (alongside
  `chat`/`chat_stream`) so a real future call site missing `max_tokens` fails the same way the
  existing calls already do — proactive, even though no such call site exists in this block.

**Tests (mocked only, no live Azure OpenAI call — cost):** `backend/tests/test_llm_json.py` (new)
covers `_strict_json_schema` (top-level and nested `$defs`), both providers' happy path, the
request-body shape for Azure (`response_format`/`strict`/`additionalProperties`), the retry
actually performing a second HTTP call (not a local no-op), invalid JSON and a schema violation
both falling back after exactly two attempts, and Azure's `num_ctx` passthrough-and-ignore matching
`chat`'s existing behaviour. A manual, uncommitted smoke against the local Ollama/qwen model
confirmed the live request/response shape end-to-end (schema-valid JSON came back for a Spanish
greeting) — not part of the gate (cost/determinism), reported for confidence only.

## Known limitations (self-reported, not just design) — 14-B1

- **No real router/summary call site yet.** `chat_json`'s `num_ctx`/`call_type` plumbing is
  built and tested in isolation; whether the router (14-B2) and summary (14-C1) calls actually
  need to pin `num_ctx_answer` to share Ollama's keep-alive window (as the agentic rewrite and
  the eval judge already do) is a decision for those blocks, not verified here.
  `test_router_and_judge_calls_now_pin_num_ctx_to_match_answer_groundedness` does not yet cover
  `llm.chat_json` call sites because none exist.
  - Explore `test_router_and_judge_calls_now_pin_num_ctx_to_match_answer_groundedness` once
    14-B2/14-C1 land.
- **Azure's `strict: true` schema tightening (`_strict_json_schema`) only sets
  `additionalProperties: false`.** OpenAI's strict mode also requires every property to be
  listed in `required` (optional fields must be modelled as nullable, not merely absent from
  `required`) — this is a constraint on how 14-B2/14-C1 author their Pydantic models, not
  something `chat_json` enforces or can enforce generically; a schema that violates it would
  fail at the real Azure API call, not locally (no live Azure call in this block to catch it).
- **Retry re-sends the exact same request.** A provider that is deterministically wrong for a
  given input (e.g. always drops a field) will exhaust both attempts and fall back every time,
  not self-correct; no retry-with-a-corrective-message strategy is implemented (none was asked
  for — the task's "bounded retry" + "safe fallback" are satisfied literally).

## Rollback

14-A1: no runtime behaviour changes (`eval/golden_set_v2.jsonl` and the new
`Turn`/`Dialogue`/`load_dialogues` code were inert until this block). 14-A2: `evaluate_dialogues`/
`compute_dialogue_metrics`/`judge_no_claims` are new, additive functions reached only from the
`eval` gate step's own extra code path and from tests — reverting this block's commit(s) is a
plain revert with no migration, no deployed-image, and no API impact (the v1 `eval` gate path is
untouched either way). 14-B1: `chat_json` is new, additive surface on both `ChatClient`
implementations; no existing caller is touched (`chat`/`chat_stream` signatures and bodies are
byte-identical, proven by the full pre-existing `test_llm.py`/`test_llm_bounds.py` suite staying
green) and nothing in `generation.py`/the API routes calls `chat_json` yet — reverting this
block's commit(s) is a plain revert with no migration, no deployed-image, and no API impact.
