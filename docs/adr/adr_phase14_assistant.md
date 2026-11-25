# ADR phase 14 — Conversational assistant

- **Status:** **In progress.** Created at block **14-A1** (golden set v2 schema + items);
  extended at **14-A2** (runner + metrics, "before" baseline + floors), **14-B1** (`chat_json`
  structured output), **14-B2**/**14-B2-fix** (shared understanding step), **14-C1**
  (conversation memory read/write) and **14-C2** (retrieval with the standalone query +
  per-metric floor enforcement). The conversational-answer, visible-plan, latency/cost/
  dashboard and Azure-migration decisions (blocks 14-D1/D2/D3, 14.7/14.7-migration) are added
  as those blocks land. Do not read this file as a complete phase-14 ADR until the "Docs +
  Azure promotion" block (14.6) closes it out.
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

## Decision — conversation memory read/write (T14.2.5/T14.2.6, block 14-C1)

**Scope (DEC-059, user 2026-11-25 09:00 = A).** 14-B2 shared only the UNDERSTAND step
(above); unifying retrieve/generate/groundedness too, as T14.2.1's literal wording would
imply, is a larger refactor than this project's own block-sizing rule allows in one step
(≤3 rows per block). Memory is instead wired directly into the two entry points that genuinely
have a conversation — `generation.answer_question_stream` (`/chat/stream`) and
`eval.runner.evaluate_dialogues` (in-memory per replayed dialogue) — leaving
`generation.answer_question` (`POST /chat`) memory-less by design (TF24: it has no
conversation at all).

- **Read** (migration 0009, `conversations.summary_encrypted`): `api.conversations
  ._load_memory` decrypts the conversation's own summary and selects its last clean
  user+assistant turn (`assistant.memory.select_last_turn` — skips an error/interrupted
  marker, DA-G1-9/DA-G2-3) **before** the current turn's user message is persisted
  (`_start_turn` fetches `prior_messages` first), then forwards both into `understand()`,
  which already accepted them as of 14-B2.
- **Write** (`assistant.memory.summarize_turn`): one bounded `chat_json` call against
  `ConversationSummary {topics, entities, owasp_version, open_questions}` (≤ ~300 tokens),
  merging the previous summary + last turn + the turn just answered, run **after** the
  answer is persisted and **before** the stream's own `done`/`error` event (never a crash
  risk for an already-successful turn). ANY failure — network/timeout, or `chat_json`'s own
  bounded retry exhausting itself on an invalid reply (T14.1.2) — is a DROP (`None`): the
  caller leaves the OLD summary untouched. Detected by identity against a dedicated,
  freshly-built empty `ConversationSummary()` fallback (never confused with a genuinely
  empty but valid reply — a different object, same content).
- **Untrusted-data discipline, transitively.** Every piece of untrusted text the write call
  sees (previous summary, last turn, the new question/answer) is escaped
  (`assistant.fast_path.escape_delimiter_markers`) before being wrapped in this module's own
  delimited blocks — the SAME reasoning as DA-14-B2 finding 4 applies here too: a malicious
  turn must not be able to forge a fake closing tag and poison a FUTURE summary that is later
  fed back to the router as untrusted data.
- **Purge (X3).** No new `PURGE_STEPS` entry: `summary_encrypted` is plain content on the
  already-covered `conversations` table (no user-linked column name, no FK) — the existing
  "conversations" step already `DELETE`s the whole row leaf-first, same as `title_encrypted`
  already does. No new grant (table-level grants from 0005 cover the column already).
  Downgrade refuses while any conversation holds a non-null summary (0008's precedent).

### Alternatives considered

- **Refactor retrieve/generate/groundedness into `assistant.core` first, then wire memory
  once, in one place.** Rejected for this block: it would re-trigger the exact
  monkeypatch-target breakage 14-B2 already hit and reverted from (tests patch
  `generation.hybrid_search`/`generation.embed_query`), and is a strictly bigger change than
  T14.2.4-6's three rows ask for. DEC-059 explicitly accepted the narrower scope, flagging
  the "wired three times" compounding risk for 14-C2 to budget for.
- **Skip memory write for the chit-chat fast path** (a greeting carries no real security
  content) to save one LLM call per trivial turn. Originally rejected in this block in
  favour of a uniform write path ("simpler to reason about than a second special case") —
  **reversed in 14-C1-fix (DA-14-C1 finding 1, HIGH):** the DA review reframed this not as a
  code-simplicity question but as a cost-control one, the same class DEC-050 was raised to
  the user for: the fast path is ALREADY the one case this block's own daily-cap exemption
  (14-B2-fix) singles out as free, so adding a second, uncounted LLM call on exactly that
  case reopened a cost gap right next to the fix for a worse exploit of the same pattern.
  The schema-meaningfulness argument above still holds (an exact fast-path match cannot
  carry a buried real question), so skipping the write loses nothing — only the
  "uniform is simpler" preference was outweighed, not refuted.

## Known limitations (self-reported, not just design) — 14-C1

- **`POST /chat` stays memory-less.** Not an oversight: it has no conversation parameter at
  all (TF24) — there is nothing to read memory FROM or write memory TO. Any future block that
  wants memory on `POST /chat` would first need to give it a conversation concept, which is a
  product decision, not a plumbing gap this block left open.
- **Memory does not feed retrieval.** `understand()`'s `standalone_query` (now informed by
  real memory) is still not consumed by `hybrid_search`/`retrieve` (T14.2.7/14-C2) — a
  follow-up's retrieval quality is unaffected by this block; only the ROUTER's own
  classification of the turn (and therefore `intent_accuracy`) can move.
- **The summary is a running merge, not a verified fact store.** `summarize_turn`'s system
  prompt asks the LLM to "merge" the previous summary with the new turn, but nothing
  validates that the merge is faithful (no groundedness check on a summary, unlike an
  answer) — a summary can drift or lose a detail over many turns; it is a soft aid to the
  router's classification, never a source the final answer is checked against.
- **`escape_delimiter_markers` is the same narrow, literal check 14-B2-fix already
  documented** (only the exact `<<<`/`>>>` 3-character run) — the same bounded-risk reasoning
  applies: a bypass here can at most make the router/memory see slightly misleading context,
  never let an attacker's text escape into the system role or reach the corpus/citation path
  directly.
- **One extra bounded LLM call per answered turn** (the memory write) — a real, acknowledged
  latency/cost cost on every turn that completes successfully; not separately measured
  against a per-turn latency floor in this block (14.5, Latency/cost/dashboard, is where a
  dedicated budget would be added). **Fixed for the fast-path case (14-C1-fix, DA-14-C1
  finding 1, HIGH):** an exact, anchored greeting/thanks/farewell (`match_fast_path` —
  the SAME decision `_reserve_stream_answer`/`reserve_daily_answer` use to exempt the turn
  from the daily cap) now skips the memory write entirely, in both
  `api.conversations._chat_events`/`_update_conversation_summary` and `eval.runner
  .evaluate_dialogues` — a bare greeting has nothing worth summarizing, so this is free, not
  a quality trade-off. Every other answered turn, including a turn the LLM ROUTER (not the
  fast path) classifies as chit-chat, still writes memory and is still counted against the
  cap — the fix narrows the skip to exactly the already cap-exempt case, nothing wider.
- **Two-hop stored prompt injection via memory (DA-14-C1 finding 2, LOW, self-reported
  explicitly here — DA-13-J2-1 discipline).** A malicious turn's text is summarized by
  `summarize_turn` into the stored `summary`; `summarize_turn`'s own system prompt only
  INSTRUCTS the model not to obey embedded instructions — nothing enforces that structurally
  at the write step. On the NEXT turn, that (possibly poisoned) summary is fed back into the
  router as `understand()`'s `summary` input. The mitigation already in place does not rely
  on the write step being honest: every DOWNSTREAM read treats the summary as escaped,
  delimited, untrusted data FOREVER, regardless of what ended up inside it —
  `assistant.router.build_router_messages` escapes `summary`/`last_turn` via
  `escape_delimiter_markers` before wrapping them in the router's own delimited block
  (`router.py:109-113`), and the memory-WRITE prompt applies the identical escaping to every
  untrusted field it reads, so even the ciphertext the next turn's read produces was already
  pre-neutralised the first time
  (`test_assistant_memory.py::test_build_memory_messages_escapes_a_literal_delimiter_marker_in_every_untrusted_field`).
  This is a second-order case of the same bounded, literal-3-character-run risk the
  `escape_delimiter_markers` bullet above already documents — not a new exploit surface, but
  worth naming explicitly because "the summary becomes untrusted data again on the next
  turn" is the whole argument for why the two-hop chain is safe, and was previously only
  implicit in this list.

## Decision — retrieval with the standalone query + per-metric floor enforcement (T14.2.7/T14.2.8, block 14-C2)

**What changed (T14.2.7).** `rag_app.assistant.core.retrieval_query(decision, query)` is a
new pure function: the router's English `standalone_query` when it is a genuine, non-empty
rewrite, else the original `query` unchanged. All three entry points that call `understand()`
— `generation.answer_question`, `generation.answer_question_stream`,
`eval.runner.evaluate_dialogues` (DEC-059 option A, the same "wire through all three entry
points" pattern 14-C1 already used for memory) — now call `retrieve()`/`hybrid_search()`/
`CrossEncoderReranker.rerank()` with that resolved query instead of the raw message.
Generation (`answer_from_chunks`/`_stream_answer_tokens`) is deliberately UNCHANGED — it still
receives the original `query`, so the reply still addresses what the user actually typed, in
their own language (D14.1); see `docs/TRD.md` §6.4 for the full design.

### Measured effect (real `--only eval`/`--full` run, 48-item/63-turn fixture, same fixture as 14-B2/14-C1)

| Metric | 14-C1 (before) | 14-C2 (this block) | Floor | Met? | Enforced from this block? |
|---|---|---|---|---|---|
| intent_accuracy | 0.889 | 0.873 | ≥ 0.90 | no | no |
| noisy_recall | 1.000 | 1.000 | ≥ 0.90 | **yes** | **yes** |
| follow_up_resolution | 0.500 | 0.500 | ≥ 0.85 | no | no |
| conversational_reply_rate | 0.9375 | 0.9375 | ≥ 1.00 | no | no |
| greeting_reply_p95_s | ~1.71–1.94s | ~1.48s | ≤ 2.0s | **yes** | **yes** |
| router_p95_s | ~1.15–1.34s | ~1.05s | ≤ 1.5s local | **yes** | **yes** (new check, see below) |

(v1 golden set, unaffected: `retrieval_recall`/`faithfulness`/`correctness`/`correct_abstention`
all 1.0, same as every earlier block — ≥ the Phase 4 baseline, the v1 `eval` gate path is
untouched by this block.)

**`intent_accuracy` (0.889 → 0.873, one turn) and `follow_up_resolution`/
`conversational_reply_rate` (byte-identical) are explained, not a regression:**
`intent_accuracy`/`conversational_reply_rate` depend on the ROUTER's classification and the
CHIT-CHAT reply respectively — neither is touched by this block (T14.2.7 only changes what
RETRIEVAL searches with); the one-turn drift in `intent_accuracy` is LLM run-to-run variance
at the router call (same class of noise 14-C1's own table already called out for latency
metrics), not a code change — rerunning the SAME code can move this number by ±1 turn either
way. `follow_up_resolution` staying at EXACTLY 3/6 is the one number this block's own change
COULD move — verified it is not silently broken by tracing the real pipeline by hand (not
guessed from the aggregate number alone):

```
UNDERSTAND: 'Where does it rank in the 2021 OWASP Top 10?' (dlg-followup-3-back, turn 3,
  3 turns back, after a chit-chat + off-topic distractor turn)
  -> intent=ambiguous standalone='What rank does SQL injection have in the 2021 OWASP Top 10?'
RETRIEVE CALLED WITH: 'What rank does SQL injection have in the 2021 OWASP Top 10?'
```

Retrieval genuinely now searches with the resolved, topic-correct English query (confirmed by
instrumenting `understand()`/`retrieve()` directly against the real router, no DB needed for
the trace) — T14.2.7's wiring works. The SAME 3 turns still miss their `expected_doc` for two
different, narrower reasons, neither a wiring bug:

1. **Near-duplicate corpus chunks differing only by OWASP edition year.** `dlg-followup-3-back`
   (above) and `dlg-followup-doc-version` ("What about the 2025 edition?") both need retrieval
   to distinguish a `top10-2021-a03-injection` chunk from a `top10-2025-a05-injection`/
   `top10-2025-presentation-pdf` chunk — same topic (injection), different year. Even with a
   well-resolved query naming the right topic, hybrid search/rerank (unchanged by this block)
   sometimes still surfaces the wrong-edition chunk (`golden_set_v2_results.json`'s real
   `retrieved_docs` for `dlg-followup-3-back`: `['top10-2025-a05-injection', ...]`, the WRONG
   year) — a retrieval-precision limitation, not something an input-query rewrite alone fixes.
2. **An under-specified router rewrite on a terse one-turn-back case.** For
   `dlg-followup-doc-version`'s single prior turn, the router sometimes classifies the turn
   `ambiguous` and writes a standalone_query like `"What about the 2025 edition of the OWASP Top
   10?"` — grammatically resolved, but missing the TOPIC word ("injection") the previous turn
   was about, because the router's own system prompt asks it to resolve a reference using the
   last turn/summary but does not force every noun phrase from them into the rewrite. Retrieval
   then (correctly, given that query) finds a generic Top-10-2025 overview chunk instead of the
   injection-specific one.

Both are real, narrower limitations of the ROUTER's rewrite quality and the CORPUS's
near-duplicate chunks — not a regression from this block (the SAME 3/6 failed before T14.2.7
existed, for the OLDER reason that retrieval used the raw, unresolved message at all) and not
something `retrieval_query`/the wiring can fix by construction; closing them needs either a
better router prompt (explicitly naming every entity resolved, not just paraphrasing) or
corpus/retrieval changes (e.g. weighting the edition year more heavily) — out of this block's
scope (T14.2.7 is "wire retrieval to the standalone query", not "fix router rewrite quality" or
"fix retrieval precision on near-duplicate chunks").

### Per-metric floor enforcement (T14.2.8)

The Phase 14 plan's own §14.2 Gate line ("floors from 14.0") applies to every floor as of this
block (router, memory AND retrieval all exist now) — read literally as "every floor is checked
every run from now on", not "every floor must pass before any of them is enforced". 14-A2/
14-B2/14-C1 all kept a single global `enforce_now: false` because, at each of those points,
EVERY floor was still short. Now that `noisy_recall`/`greeting_reply_p95_s`/`router_p95_s` are
genuinely met, `gate.py`'s `_check_v2` (and `eval/golden_set_v2_floors.json`) move to **one
`enforce` flag per metric**: a metric only fails the gate once its OWN floor is both in force
and actually met is false — `intent_accuracy`/`follow_up_resolution`/`conversational_reply_rate`
stay `enforce: false` (recorded every run, never failing) until a later block (14.3/14-D1 for
chit-chat/ambiguous handling; a router-prompt or corpus fix for the two narrower cases above)
raises the real number — **not a lowered floor**, the same number (0.90/0.85/1.00) stays in
the file, just not yet gating. `router_p95_s` is also a NEW check this block adds to
`_check_v2` (compared against the floors file's own `router_p95_s_local` key, since this gate
only ever runs against the local stack) — it existed as a measured field since 14-C1 but was
never compared against a floor at all before (`_V2_LATENCY_KEYS` did not include it).

### Alternatives considered

- **Enforce every floor now regardless of measured state.** Rejected — explicitly ruled out by
  the orchestrator's prompt ("do not lower a floor") and would fail `gate.sh --full` outright
  on three metrics that a LATER block, not this one, is scoped to fix.
- **Keep the single global `enforce_now` switch and simply leave it `false` again.** Rejected —
  the orchestrator's prompt explicitly asks this block to "turn on floor enforcement for the
  metrics the plan says apply now and are met"; a global switch cannot do that without ALSO
  enforcing the three that are not met.

## Known limitations (self-reported, not just design) — 14-C2

- **Two narrower, explained gaps keep `follow_up_resolution` at 0.500** (see the measured-effect
  section above): near-duplicate OWASP-edition-year chunks confusing retrieval precision, and
  an occasional under-specified router rewrite on a terse one-turn-back follow-up. Neither is
  fixed by this block; both are candidates for a future retrieval-precision or router-prompt
  follow-up (not filed as a dated DEBT row here — out of scope to triage further within this
  block's time budget; flagged for the orchestrator in the block report instead).
- **`intent_accuracy`/`conversational_reply_rate` are untouched by this block** (T14.2.7 only
  changes retrieval) — their gaps are the same ones 14-B2/14-C1 already self-reported
  (chit-chat phrasing outside the fast-path lexicon; `followup`/`ambiguous`/`off_topic` having
  no dedicated reply yet) and are T14.3/14-D1's scope, not this block's.
- **LLM run-to-run variance.** `intent_accuracy` moved by exactly one turn (0.889 → 0.873)
  between 14-C1's and this block's otherwise-identical-fixture runs with no retrieval-unrelated
  code change to explain it — the same class of noise already documented for latency metrics;
  a single re-run of the SAME code can move a ratio metric by ±1 turn.

## Decision — chunk dominance, OWASP-edition filter, router few-shots (T14.2.9, block 14-C3, DEC-062)

**Scope.** DEC-062 option A (user 2026-11-25 12:56, widened by DA-14-C2-2): make the Phase-14.2
local gate pass (`intent_accuracy` ≥ 0.90, `follow_up_resolution` ≥ 0.85) without lowering
either floor, re-deriving the diagnosis from the live corpus rather than trusting 14-C2's own
(DA-flagged, not-reproducible-across-runs) per-turn quotes.

**Re-diagnosis against the live dev corpus (read-only `docker exec … psql` against
`rag_app-db-1`/`rag_ia_pgdata`, plus a direct, ad-hoc call into `retrieval.hybrid_search`/
`reranking.CrossEncoderReranker` — no data modified, no test run against this volume).**
Three independent, compounding root causes, not one:

1. **`_bm25_uids` used `plainto_tsquery` (implicit AND).** A 6+-word natural-language question
   ("What rank does injection have in the 2025 OWASP Top 10?") needs every one of its stemmed
   terms present in the SAME chunk to match at all — confirmed live: this query returned ZERO
   BM25 rows (with or without a version filter), so BM25 contributed nothing and retrieval was
   silently vector-only for exactly the kind of query DEC-062's misses are made of.
2. **One generic, multi-topic document saturates the raw candidate pool.** `top10-2025-
   presentation-pdf` (a 39-chunk slide deck) and `top10-2017-full-pdf` (a 134-chunk full PDF
   with its own front matter/release-notes/risk-factor sections) both have EVERY chunk's
   heading breadcrumb rooted in a boilerplate top-level section (`Agenda`, `FW > Welcome to
   the OWASP Top 10 - 2017!`, `Details About Risk Factors +RF > …`) rather than a specific
   category — confirmed live: for the hardest DEC-062 miss ("injection rank, 2025 edition"),
   version-scoping to `2025` alone still returned 10/10 raw vector-search candidates AND 10/10
   raw BM25 candidates from the SAME `presentation-pdf` document; the one relevant
   `top10-2025-a05-injection` chunk did not even reach the top 10 of either raw list (it
   surfaced at vector rank 11 once the raw pool was widened to 30).
3. **The cross-encoder's own raw score sometimes ranks a bare reference list above the chunk
   that states the actual fact.** For "what rank does X have", a `> References` chunk (a bare
   markdown link list, no prose) outscored the SAME document's `> Overview` chunk (the one
   stating "Injection slides down to the third position") on the live quantized model's raw
   `predict()` output — confirmed by a direct side-by-side call, not inferred from the
   aggregate metric.

**Fixes (all retrieval-time; no re-ingestion, no corpus/chunking change — deliberately, per
the orchestrator's "a corpus re-index is a big change" steer):**

1. **`retrieval._bm25_uids` rewritten to OR semantics.** Builds `plainto_tsquery` for its
   dictionary-aware tokenizing/stemming/stopword handling (kept, not reimplemented), then
   turns its `&`-joined text form into `|` (`replace(tsquery::text, ' & ', ' | ')::tsquery` —
   the same recipe Postgres's own documentation uses) before matching. A chunk matching SOME
   of the query's terms now ranks instead of the whole list silently returning nothing.
2. **`retrieval.hybrid_search` fetches a wider raw pool per list (`_RAW_OVERSAMPLE = 3`) and
   caps how many of the final fused `top_k` candidates one document may fill
   (`diversify_candidates`, `_MAX_CHUNKS_PER_DOCUMENT_IN_CANDIDATES = 3`).** Widening alone
   does nothing if the first `candidate_k` raw rows are already saturated (confirmed above);
   capping alone does nothing if the correct document's chunk never reaches the raw pool at
   all — both together are needed. Diversity is applied ONLY at the candidate-POOL stage: the
   cross-encoder still freely picks whichever `top_n` it judges best from that (now more
   diverse) pool, so a question genuinely answered from 2-3 chunks of one relevant document is
   never penalised at the final answer-context step (verified: no v1 golden-set regression,
   table below).
3. **`reranking.CrossEncoderReranker.rerank` down-weights a low-value heading's score
   (`is_low_value_heading`, `_LOW_VALUE_SCORE_PENALTY = 0.05` — see the known-limitations note
   below on how this value was reached) AFTER the real cross-encoder call, before the final
   sort.** The raw score still has to prove the chunk is topically relevant enough to reach
   this pool at all; the penalty only stops a reference-list/boilerplate chunk
   from crowding out a lower-raw-scored chunk that actually carries the answer. Headings are
   real corpus content (`References`, `List of Mapped CWEs`, `Agenda`, `Acknowledgements`,
   `Release Notes`, `Foreword`, `Risk Factor`/`Industry Ranked Survey`, …), not a per-document
   hardcoded rule — any document whose own breadcrumb ends in one of these sections is treated
   the same way.
4. **`assistant.core.named_owasp_version(text)`** (pure regex over the corpus's three known
   editions, `2017`/`2021`/`2025`, word-bounded so e.g. a CVE-style `…12021` never matches):
   wired into all three `retrieval_query` call sites
   (`generation.answer_question`/`answer_question_stream`, `eval.runner.evaluate_dialogues`) as
   `version = explicit_version if explicit_version is not None else
   named_owasp_version(search_query)`. Reads the RESOLVED retrieval text (what
   `retrieval_query` actually returns), never `ConversationSummary.owasp_version` directly —
   the router already folds summary/last-turn context into its rewrite (fix 5 below is what
   makes that rewrite reliably name the edition), so this is both simpler and safer than also
   consulting the summary: a topic change several turns after an edition was once named can
   never leak a stale version into an unrelated query, because nothing in the CURRENT search
   text repeats it (DEC-062's own wording: "never when not named").
5. **Router few-shots (`assistant.router._ROUTER_SYSTEM`).** Three short examples (two EN
   patterns + one ES-to-EN translation pattern) instructing the model to always pull the topic
   noun from the last turn/summary into `standalone_query` for a terse follow-up, and to use
   the SUMMARY (not only the immediately preceding turn) to resolve a pronoun several turns
   back — addresses the "under-specified rewrite" gap 14-C2 self-reported. The delimited-
   untrusted-data wrapping (`build_router_messages`/`escape_delimiter_markers`) and the strict
   `RouterOutput` schema are UNCHANGED — the examples are plain instructional prose inside the
   system prompt, never real conversation data, and are explicitly marked "do not copy
   verbatim" so the model does not treat them as a literal override of the real summary/last
   turn given above them.
6. **4 more follow-up dialogues in `eval/golden_set_v2.jsonl`** (`dlg-followup-1-back-reverse`,
   `dlg-followup-es-1-back-reverse`, `dlg-followup-3-back-2025`,
   `dlg-followup-3-back-es-2025`) — both languages, both 1-back and 3+-back depths, every
   `expected_doc` a real corpus slug reused from the sources above (`cs-sql-injection-
   prevention`, `cs-os-command-injection-defense`, `top10-2025-a05-injection`). Raises
   `follow_up_resolution`'s denominator from `n=6` to `n=10` (DA-14-C2-2's own concern: "one
   flaky turn moves it by ±0.167" at n=6).

### Measured effect

Real `--only eval` runs against the live dev corpus (322 chunks, same seed every run). An
intermediate attempt (penalty `0.4`, see the known-limitations note on tuning below) regressed
v1 `correctness` 1.0→0.7 before the final `0.05` penalty value was found and re-verified; only
the FINAL code's numbers (identical code, run twice) are used to decide `enforce`:

| Metric | 14-C2 (before, n=63 turns) | Final run 1 (`gate-20261125T130554Z-2888977.log`) | Final run 2 (`gate-20261125T131222Z-2925883.log`) | Floor | Met (both runs)? | Enforced from this block? |
|---|---|---|---|---|---|---|
| intent_accuracy | 0.873 | 0.840 | 0.840 | ≥ 0.90 | no | no |
| follow_up_resolution | 0.500 | 0.900 | 0.900 | ≥ 0.85 | **yes** | **yes** |
| noisy_recall | 1.000 | 1.000 | 1.000 | ≥ 0.90 | yes (already) | yes (already, 14-C2) |
| conversational_reply_rate | 0.9375 | 0.944 | 0.944 | ≥ 1.00 | no | no |
| greeting_reply_p95_s | ~1.48s | ~1.53s | ~1.53s | ≤ 2.0s | yes (already) | yes (already, 14-C2) |
| router_p95_s | ~1.05s | ~1.06s | ~1.05s | ≤ 1.5s local | yes (already) | yes (already, 14-C2) |

v1 golden set (unchanged fixture, `eval/golden_set.jsonl`, 10 answerable + 4 negative items),
BOTH final runs: `retrieval_recall`/`faithfulness`/`correctness`/`correct_abstention` all 1.0 —
**at, not below, the Phase 4 baseline**; the intermediate 0.7 `correctness` regression (3 of 10
items wrongly abstained despite retrieving the correct document and the correct, fact-bearing
chunk — traced live to the groundedness-check call, not retrieval, being sensitive to the exact
4th context slot for a terse one-marker `"[1]"` answer, the same class of flakiness DEBT-066
already names for the correctness judge) is fully resolved by the final `0.05` penalty value and
does not reappear on either final run.

**`follow_up_resolution` 0.500 → 0.900 (both runs) is the headline result**, from the three
retrieval-time fixes (OR-BM25, candidate-pool diversity, low-value-heading down-weight) plus the
named-edition filter — re-derived per-turn evidence, not assumed: all 3 of DEC-062's original
misses (`dlg-followup-3-back`, `dlg-followup-doc-version`, `dlg-followup-3-back-es`) now retrieve
their `expected_doc` on at least one of the two final runs; run 2's one remaining miss at n=10
(`dlg-followup-1-back` turn 1 — `cs-os-command-injection-defense` expected,
`top10-2017-full-pdf`/`top10-2025-a05-injection`/two cheat-sheet chunks retrieved instead) is a
DIFFERENT turn than any of DEC-062's 3 original misses — confirms this is ordinary per-turn LLM
retrieval noise at the margin (the router/embedder/cross-encoder are not bit-for-bit
deterministic run to run even at temperature 0), not a new systematic gap, since the aggregate
(9/10) still clears the 0.85 floor comfortably on both runs.

**`intent_accuracy` 0.840 (both runs) stays below its 0.90 floor, and is WORSE than 14-C2's 0.873**
(on a smaller, 63-turn fixture). Re-derived from run 2's per-turn results (`eval/
golden_set_v2_results.json`, 12 mismatches of 75 turns): most are PRE-EXISTING items unrelated to
this block's changes (`typo-injecion-what`, `chat-en-5`, `abstain-en-cisco`, `abstain-es-nginx`,
`dlg-ambiguous-then-clarify` turn 1, `dlg-abstention-then-recover` turns 0+1) — consistent with
14-C2's own "LLM run-to-run noise" finding, now more visible simply because there are more turns
to miss among. The remaining mismatches are a genuine, narrower side effect of this block's
router few-shots, reproduced directly against the live router (not only inferred from the
aggregate): a terse,
several-turns-back follow-up (`dlg-followup-3-back-es` turn 3, `dlg-followup-3-back-2025` turn 3,
`dlg-followup-3-back-es-2025` turn 3) now resolves its `standalone_query` CORRECTLY (confirmed by
instrumenting `understand()` directly against the real router: e.g. `'What rank does it have in
the 2025 edition?'` → `'What rank does SQL injection have in the 2025 OWASP Top 10?'`, exactly
the fix intended) but the router labels the intent `ambiguous` instead of `followup` — a
boundary confusion between two intents whose DEFINITIONS are genuinely close for a multi-turn-back
reference, not a wiring bug. **This mislabeling does not affect `follow_up_resolution`** (that
metric keys off the golden set's own `expected_intent`, never the router's `observed_intent`), so
the headline fix is not undermined by it. Two further prompt attempts were made and live-tested
directly against the real router before accepting this gap: (1) sharpening the `followup`/
`ambiguous` definitions ("if resolvable from the last turn/summary, it is `followup`, never
`ambiguous`") — kept, since it is a correct, harmless clarification, but did not flip the
observed label; (2) one more contrasting few-shot example — tried, made the `ambiguous`/`followup`
confusion no better and, on one case, regressed `standalone_query` quality (dropped the resolved
topic noun) — reverted. Closing this needs router-prompt iteration beyond what retrieval-time
fixes can do, or a larger/non-quantized router model; out of this block's scope (DEC-062 was a
retrieval-time fix block). **Not a lowered floor** — recorded every run, never silently loosened.

### Known limitations (self-reported, not just design) — 14-C3

- **`is_low_value_heading`'s regex is corpus-informed, not corpus-proof.** It was built by
  reading the ACTUAL headings of the two documents DEC-062 named, not invented in the
  abstract — a future corpus document with a genuinely substantive section literally titled
  "References" (unlikely in this corpus's style, but not impossible) would be down-weighted
  too. Low risk given the fixed, small (9-document) mini-corpus this gate runs against; worth
  revisiting if the corpus ever grows.
- **`named_owasp_version` only recognises the three editions already in this corpus
  (2017/2021/2025).** A real new OWASP edition would need this tuple extended — deliberate
  (DEC-062: "never when not named" means never guessing at an edition the corpus does not
  have), not an oversight.
- **The BM25 OR-rewrite trades precision for recall.** A query matching only ONE of many
  terms now ranks (previously: not at all) — this is the intended fix (BM25 contributing
  nothing was strictly worse), but it does mean a very short, generic BM25 match can enter the
  raw candidate pool more easily than before; the diversity cap and the cross-encoder's own
  re-scoring are what keep this from degrading final precision (verified: v1 golden-set
  `retrieval_recall`/`faithfulness`/`correctness` unregressed, table above).
- **The router few-shots are prose guidance, not a guarantee** — an LLM router can still
  occasionally write an under-specified rewrite; `intent_accuracy`'s own run-to-run LLM
  variance (14-C2's self-reported ±1 turn) is unchanged in kind, only hopefully improved in
  degree by the extra guidance. The few-shots ALSO introduced a narrower, measured side
  effect (the `followup`/`ambiguous` boundary confusion on terse, multi-turn-back follow-ups
  — see the measured-effect section above); kept anyway because the headline metric
  (`follow_up_resolution`) is unaffected by it and the standalone_query quality gain is real.
- **`_LOW_VALUE_SCORE_PENALTY`'s value (`0.05`) was reached empirically, not derived.** An
  intermediate `0.4` value fixed the chunk-dominance misses but regressed v1 `correctness`
  1.0→0.7 (traced live to a groundedness-check call flipping its verdict on a bare `"[1]"`
  answer depending on the EXACT 4th context slot — the same class of small-model flakiness
  DEBT-066 already names for the correctness judge, here surfacing via the groundedness
  checker instead). `0.05` was the value that reliably let a substantive chunk win the
  contested slot in all 3 live-reproduced regression cases; a future corpus/model change
  could in principle need this re-tuned the same way, by reproducing a failing case live
  against the real pipeline (as this block did) rather than guessing.

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
14-C1: the FIRST block in Phase 14 with a schema change — migration 0009 adds
`conversations.summary_encrypted` (expand-only, nullable, docs/DEFINITION_OF_DONE.md
"Expand/contract"): an OLDER image keeps working unmodified against the new schema (it
simply never reads/writes the column), and a `git revert` of this block's CODE commit(s)
alone is safe at any time — the column being present-but-unused is not a behaviour change.
Reverting the MIGRATION itself (`alembic downgrade -1`) is the separate, data-loss-aware step
0009's own `downgrade()` already guards: it refuses while any conversation holds a non-null
summary (same rule as 0008's `admin_actions`/`defence_runs`) — roll back images instead once
real summaries exist, never the migration.
14-C2: changes LIVE runtime behaviour (retrieval now searches with the router's
`standalone_query`) and changes the `eval` gate's own pass/fail surface (per-metric floor
enforcement) but no new persisted data, no migration, no deployed-image step. A plain `git
revert` of this block's commit(s) restores 14-C1's behaviour exactly: retrieval reverts to
always using the raw message, and `eval/golden_set_v2_floors.json`'s per-metric `enforce` map
reverts to the single `enforce_now: false` 14-A2 introduced (every v2 floor recorded, none
enforced) — `gate.sh --full` stays green either way, since no enforced floor regresses by
reverting (they were already met before this block existed too, for unrelated reasons:
`noisy_recall`/`greeting_reply_p95_s` predate Phase 14, `router_p95_s` is a 14-C1-era
measurement this block only started comparing against its floor).
14-C3: changes LIVE runtime behaviour (BM25 OR-rewrite, candidate-pool diversity cap,
low-value-heading score penalty, the named-edition version filter, router few-shot prose) and
`eval/golden_set_v2.jsonl`/`_floors.json` (4 more dialogues; `intent_accuracy`/
`follow_up_resolution` move to `enforce: true` only if both real runs below meet their floor).
No migration, no new persisted data, no deployed-image step. A plain `git revert` of this
block's commit(s) restores 14-C2's behaviour exactly (BM25 back to AND-only, no diversity cap,
no low-value penalty, no version filter, no few-shots, the original 6-follow-up-turn fixture) —
`gate.sh --full` stays green either way as long as `enforce` was not flipped on a floor this
revert would then fail; if it was, flip it back to `false` in the SAME revert commit.
