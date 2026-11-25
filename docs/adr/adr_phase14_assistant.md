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
- **Fixed in block 14-B1-fix (DA-14-B1-1): Azure's `strict: true` schema tightening.**
  `_strict_json_schema` now also sets `required` to the full list of a given object node's
  `properties` keys, on every object node (top level, nested `properties`, `$defs`, and array
  `items`) — not only `additionalProperties: false`. Azure/OpenAI's strict mode rejects a schema
  where *any* property is missing from `required`; optionality must be expressed only via a
  nullable type (Pydantic's own `anyOf: [..., {type: "null"}]` for `Optional[X] = None`), never
  by omission from `required`. This limitation was first mis-framed as "a constraint on how
  14-B2/14-C1 author their Pydantic models" — not quite right: a field with a default never
  lands in Pydantic's own `required` regardless of its type annotation, so there was no
  caller-side authoring fix available; the fix belongs in `_strict_json_schema` itself, which is
  where it now lives. Runtime validation is unaffected — `model_validate` still accepts (and
  produces) `null`/omitted for an `Optional` field; only the schema *advertised* to Azure
  changed. `_strict_json_schema` operates on a deep copy of `model_json_schema()`'s result, never
  mutating the dict Pydantic returns. **DEBT-036 note (unchanged by this fix):** no live Azure
  call exists yet in this phase; the first real `chat_json` call against Azure (14-B2/14-C1) must
  still confirm that API version `2024-10-21` (the pinned `azure_openai_api_version` default)
  actually serves `response_format: {type: json_schema, strict: true}` for the `gpt-4.1-mini`
  deployment — tie that confirmation to DEBT-036's existing "untested... request parameters"
  language rather than opening a new row.
- **Retry re-sends the exact same request.** A provider that is deterministically wrong for a
  given input (e.g. always drops a field) will exhaust both attempts and fall back every time,
  not self-correct; no retry-with-a-corrective-message strategy is implemented (none was asked
  for — the task's "bounded retry" + "safe fallback" are satisfied literally).

## Decision — shared understanding step: fast path + LLM router (T14.2.1–T14.2.3, block 14-B2)

**One shared understanding step** (`rag_app.assistant.core.understand`) replaces the 14-A2
placeholder (`classify_intent`, binary chit-chat/security) on every call site that answers a
message: `generation.answer_question` (`POST /chat` — which previously had NO chit-chat branch
at all, running full RAG even for "hi"), `generation.answer_question_stream` (`/chat/stream`),
and `eval.runner.evaluate_dialogues`. See `docs/TRD.md` §6.2 for the full design (regex fast
path, LLM router schema, delimited untrusted-data handling, fallback semantics).

**Scope interpretation (documented, not a blocking ambiguity).** The phase plan describes
"one shared conversational core (understand → retrieve → answer)". This block builds the
UNDERSTAND part as one genuinely shared implementation (there was none before — the 5-way
router/fast-path decision literally did not exist). The RETRIEVE/GENERATE/CHECK part
deliberately stays where it already lived (`generation.py`'s own functions, the eval runner's
own direct `reranking.retrieve`/`generation.answer_from_chunks` calls) rather than being
relocated into `rag_app.assistant` too: several pre-existing tests monkeypatch module-qualified
names (`generation.hybrid_search`, `generation.embed_query`) that only intercept the real call
when the calling code is textually inside `generation.py` — moving that code to a new module
would silently stop those tests from testing anything (the patch would simply not apply, and
the real function would run unmocked) rather than failing loudly. Collapsing stream/non-stream/
eval into one single generator end-to-end is accordingly tracked as a follow-up (TF19's second
half), not silently dropped.

**Design note: the daily-answer-cap exemption is now an approximation, not fixed here.**
`api.conversations._reserve_stream_answer` exempts a chit-chat turn from the global daily
answer cap (R6-1) on the theory that it is a free, instant canned reply. Since T14.2.2 every
chit-chat reply (fast path OR router-detected) is LLM-generated — small, but no longer free.
This block leaves the exemption's mechanics unchanged (still keyed off the OLD `classify_intent`
heuristic, not the new fast path/router) rather than silently changing a cost-cap policy
decision; see the 14-B2 block report's "Needs the user's decision" for the options.

**Superseded by 14-B2-fix (see the section below).** The DA review (DA-14-B2) found this was
not just drift but a genuine, exploitable cost-control hole (finding 2: `classify_intent`'s
unanchored `_META_RE` branch could be smuggled inside a long compound message to get a free
answer from the full, expensive retrieve → generate → groundedness path) plus `POST /chat`
having no exemption at all (finding 1, a separate stale-docstring bug). Fixed in 14-B2-fix,
orchestrator option B: both endpoints now key the exemption off
`assistant.fast_path.match_fast_path` (exact, anchored) instead.

## Fix — 14-B2-fix: daily-cap exemption keyed off the exact fast path (regression fix)

**What changed.** `api.deps.reserve_daily_answer` (`POST /chat`) and
`api.conversations._reserve_stream_answer` (`/chat/stream`) both now exempt a turn from the
daily answer cap ONLY when `assistant.fast_path.match_fast_path` matches the raw question —
an exact, anchored greeting/thanks/farewell. Neither function calls `classify_intent` any
more; that function has no remaining live call site (kept, unit-tested, as a standalone
typo-tolerant classifier, see `generation.py`'s own comment above its definition).

**Why this, not reusing `classify_intent` for `POST /chat` too.** The DA's concrete exploit
class (a compound meta-phrase-plus-question message matching `classify_intent`'s unanchored
`_META_RE` substring search) would have been exempted for free on EITHER endpoint had the fix
simply extended the old heuristic to `/chat` as well — e.g. `"What can you do? Also, explain
step by step how to log into someone else's account without their credentials or
permission."` really does classify as `"chitchat"` (verified in `test_daily_cap.py`).
**Correction to the DA's own literal PoC string** (self-reported, not silently fixed): DA-
14-B2.md quoted `"What can you do? Also: give me a full step-by-step SQLi exploit against
admin login."` — that string also contains "exploit", one of `classify_intent`'s OWN
`_SECURITY_TERM_RE` keywords, checked BEFORE `_META_RE`, so it actually classifies as
`"security"`, not `"chitchat"` — the DA's exact example does not reproduce the bug, even
though the underlying vulnerability class it describes is real (see
`test_daily_cap.py::test_the_das_own_exploit_string_does_not_actually_reproduce_the_bug`).
`match_fast_path` is exact-anchored (`^...$`), so any message long/compound enough to carry a
real question fails the match and is correctly counted — closing the hole on both endpoints
at once, not just making them consistent with each other.

**Net effect.** A typo'd greeting (e.g. "hol") or a router-classified chit-chat turn (e.g. a
meta question the fast path's exact regex does not cover) now correctly COUNTS against the
cap, even though it still only reaches a small, bounded LLM call (never the full RAG path) —
"chit-chat is (usually) cheap" replaces "chit-chat is free" as of T14.2.2; this fix makes the
cap accounting match that reality everywhere, not just on the stream path.

**Delimiter breakout (DA-14-B2 finding 4), same block.** `assistant.fast_path
.escape_delimiter_markers` neutralises a literal `<<<`/`>>>` marker run already present
inside untrusted text (message, summary, last turn) before `_delimited_message`/
`build_router_messages` wrap it in a real one — otherwise a message containing a fake
`<<<END MESSAGE>>>` could forge a closing tag and make a less careful model treat trailing
attacker text as outside the untrusted block. This is an input-side mitigation; the
output-side screen (`_looks_suspicious`) is unchanged and still the primary defence (any
misrouted/adversarial message still only ever reaches the existing, already-reviewed
security path, per the Decision section above).

## Known limitations (self-reported, not just design) — 14-B2

- **Memory is plumbed but inert.** `understand`/`route` accept `summary`/`last_turn` parameters
  (ready for 14-C1) but every 14-B2 call site always passes `None` — the router classifies a
  follow-up on its own wording alone, same F14.2 shape as before, just with a real router call.
- **Retrieval still uses the original message, never `standalone_query`.** The router already
  produces an English, self-contained rewrite; nothing downstream reads it yet (T14.2.7/14-C2).
- **`followup`/`ambiguous`/`off_topic` have no dedicated reply yet.** All three currently fall
  through to the same retrieve-and-answer path as `security` (the pre-14.2 conservative
  default) — a clarifying question, a polite off-topic redirect and a conversational
  abstention are T14.3/14-D1, not built here. `clarification_rate`/some `conversational_reply_rate`
  gaps in the eval table below are therefore still expected misses, same reasoning as 14-A2's
  own baseline table, just narrower.
- **No new fine-grained latency metric for the understanding step.** Adding one would need a
  new `metrics.STEP_ALLOWLIST` entry (`metrics.py` is outside this block's named touches); the
  pre-existing coarse `secrag_chat_stage_seconds{stage="classify"|"responding"}` total already
  grows to include it. Deferred to 14.5 (Latency, cost, dashboard).
- **The router's own num_ctx/call_type plumbing is exercised only against a local Ollama fake
  in tests** — no live Azure OpenAI `chat_json` call exists yet (DEBT-036's existing note still
  applies: the first live Azure `chat_json` call, whichever block makes it, must confirm API
  version `2024-10-21` serves `strict: true` for the pinned deployment).
- **The suspicious-output screen (`_looks_suspicious`) is a narrow, literal check** (delimiter
  markers, a `role:` prefix) — a determined prompt-injection attempt that avoids those exact
  literal patterns while still producing a structurally valid `RouterOutput` would not be
  caught by this screen specifically; the defence-in-depth property that matters (a misrouted
  message can only ever reach the existing, already-reviewed retrieve → generate →
  groundedness-check → cite-or-abstain path, never bypass citation/groundedness) holds
  regardless, because every non-chitchat intent — caught or not — runs through that same,
  unmodified path.
- **(14-B2-fix) `escape_delimiter_markers` is a narrow, literal check too.** It only
  neutralises the EXACT 3-character runs `<<<`/`>>>`; it does not defend against a model that
  is talked into treating a DIFFERENT attacker-chosen string as a delimiter, or against
  Unicode look-alike characters the model's own tokenizer might still read as "the same"
  marker. Same bounded-risk reasoning as the suspicious-output screen above: this is one
  additional input-side mitigation layered onto (not a replacement for) the output-side
  screen and the conservative fallback, which is what actually guarantees a misrouted message
  never bypasses citation/groundedness.

## Decision — Azure OpenAI model migration research (DEBT-036, block 14.7-migration, phase 1)

Phase 1 of 14.7-migration is research + preparation only (user OK 2026-11-25 09:00): no Azure
write, no Azure OpenAI inference call, no cost. Read-only `az` queries against
`secrag-openai-dmc26` (region `spaincentral`) confirmed:

- Today's deployment (`gpt-4.1-mini`, version `2025-04-14`, GlobalStandard, capacity 10 = 10K
  TPM) has `lifecycleStatus: Legacy`, `deprecation.inference: 2027-04-14` — matches DEBT-036's
  retirement date exactly.
- **DEBT-020 re-check: DataZoneStandard quota is no longer 0 in every EU region.** As of
  2026-11-25, `spaincentral` shows nonzero `OpenAI.DataZoneStandard.*` limits for several
  candidate models (e.g. `gpt4.1-mini` limit 2000, `gpt-5-mini` limit 300, `gpt-5-nano` limit
  2000 — all far above the current deployment's 10-unit capacity). This was 0 everywhere in EU
  on 2026-09-27 (DEBT-020's original finding); it has changed since. A DataZoneStandard
  deployment (EU data residency) is now a real option for the migration target, not blocked by
  quota the way it was at Phase 10.5.
- Candidate GA models in this account (`az cognitiveservices account list-models`) with longer
  runway than `gpt-4.1-mini`: `gpt-5-mini` (`2025-08-07`, `GenerallyAvailable`, retires
  `2027-07-13`, DataZoneStandard SKU present) and `gpt-5-nano` (same GA date, retires
  `2027-02-09`). Both are real, documented Azure OpenAI models as of this agent's training
  knowledge. The account also lists several newer model families (`gpt-5.1`…`gpt-6.2-*`) that
  are beyond this agent's verifiable knowledge — their retirement dates/pricing/API shape could
  not be confirmed against Microsoft Learn (no web-fetch tool was available in this agent's
  session; only the account's own `az` metadata was used), so they are not recommended as a
  deliberate target without a live human/orchestrator check of the current public docs.

**Code change (this block, mocked tests only, no live call):** `AzureOpenAIChat._body`/
`_json_body` (shared by `chat`/`chat_stream`/`chat_json`) now branch on a new
`Settings.azure_openai_reasoning_model` flag (default `False`, no behaviour change for
`gpt-4.1-mini`): when `True`, the request body uses `max_completion_tokens` instead of
`max_tokens` and omits `temperature` entirely. This is required for any o-series/`gpt-5`-family
reasoning-tier target — Azure's Chat Completions API rejects both `max_tokens` and a custom
`temperature` on those deployments. The flag is a per-deployment switch (constructor
`reasoning_model` kwarg falls back to the setting, same pattern as `endpoint`/`api_key`/
`deployment`/`api_version`), not a per-call one, because the model family is fixed once a
deployment is chosen.

## Known limitations (self-reported, not just design) — 14.7-migration (phase 1)

- **No live Azure call was made or is proposed yet.** The `max_completion_tokens`/no-`temperature`
  body shape is verified only against mocked HTTP responses; the first live call against a real
  reasoning-tier deployment (phase 2, after the user approves a target model + cost) must still
  confirm the exact request/response shape, same discipline DEBT-036 already requires for
  `chat_json`'s `strict: true` structured output (still unconfirmed against a live Azure
  endpoint as of this block — no block has made that live call yet).
- **Pricing for every candidate model is unverified.** `az` has no token-pricing query; this
  agent had no web-fetch tool this session, so per-1M-token prices in `status/blocks/
  14.7-migration.md`'s comparison table are either this agent's training-time knowledge (for
  `gpt-5-mini`/`gpt-5-nano`/`gpt-4.1-mini`/`gpt-4o-mini`, which predate this agent's knowledge
  cutoff) or explicitly marked "unverified" (for the newer model families the account lists).
  The orchestrator/user must confirm current pricing on the public Azure OpenAI pricing page
  before approving a target.
- **The reasoning-model flag is coarse.** It assumes a deployment is either "fully reasoning
  family" (no custom temperature at all, `max_completion_tokens` always) or not; it does not
  yet model intermediate cases (e.g. a `reasoning_effort` parameter, or a model that accepts
  `temperature` only at a fixed value like `1.0`). No such intermediate case is known to exist
  among the verified candidates (`gpt-5-mini`/`gpt-5-nano`), so this is not a known gap today —
  flagged in case a different target is later chosen.

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
14-B2: changes LIVE runtime behaviour (every chat turn now runs the fast path or the LLM router
before anything else) — a plain `git revert` of this block's commit(s) restores the 14-B1 head's
`classify_intent`-only behaviour exactly; no migration, no new persisted data, no deployed-image
step (Ollama/Azure OpenAI are called the same way `chat`/`chat_stream` already were).
14-B2-fix: changes LIVE runtime behaviour again (the daily-cap exemption is narrower, and
untrusted text is escaped before being wrapped) but no new surface — `reserve_daily_answer`'s
signature grows a `question: str` parameter (its only caller, `api.app.chat`, updated in the
same commit), and `escape_delimiter_markers` is a pure string transform. A plain `git revert`
of this block's commit(s) restores 14-B2's behaviour exactly (reopening the cap-exemption and
delimiter-breakout holes this block closes); no migration, no new persisted data, no
deployed-image step.
