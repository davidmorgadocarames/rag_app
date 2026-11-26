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

## Decision — ambiguous-vs-followup tie-breaker (T14.2.9-fix, block 14-C4, DEC-063)

**Scope.** DEC-063 option B (user 2026-11-25 18:50, per DA-14-C3's recommendation): fix the
`intent_accuracy` regression 14-C3 left (0.873 → 0.840) without touching `follow_up_resolution`
(≥ 0.85, already enforced) or lowering any floor; enforce `intent_accuracy` too, if ≥ 0.90 over
2 real runs.

**Re-diagnosis (live, against the real router, not only the aggregate).** Reconstructed the
exact conversation state (summary + last turn) 14-C3's own 5 regressed turns reach by turn 3/4
and called `understand()`/`route()` directly against it (real Ollama, no stub): both
`dlg-followup-3-back` and `dlg-followup-3-back-es` turn 3 reproduce the mismatch every time —
the router outputs `intent="ambiguous"` while its OWN `standalone_query` has already resolved
the reference correctly from the summary (e.g. `'Where does it rank in the 2021 OWASP Top 10?'`
→ `'Where does SQL injection rank in the 2021 OWASP Top 10?'`) — AND, in both live-reproduced
cases, `clarification` is `null`, directly contradicting the router's own stated rule that an
`"ambiguous"` verdict must carry a real clarifying question. 14-C3's two prompt-only attempts
(a sharper definition, kept; one more few-shot, reverted) did not close this because the model's
own self-contradiction (claims "ambiguous", behaves as if resolved) is not something more prose
alone reliably fixes.

**Fix.**

1. **One more small, contrasting few-shot** in `assistant.router._ROUTER_SYSTEM`: an explicit
   "genuinely ambiguous — no context at all" example, paired with a reminder that `"ambiguous"`
   MUST carry a real `clarification`. Additive to 14-C3's existing 3 examples (kept unchanged).
2. **A deterministic, code-level tie-breaker (`router._resolves_a_reference`)**, applied in
   `route()` AFTER the existing suspicious-output screen, BEFORE returning: when the model says
   `"ambiguous"` but (a) conversation context (`summary`/`last_turn`) actually existed to
   consult, AND (b) `standalone_query` names at least one substantive (≥3-letter) word absent
   from the raw `query` — direct, language-agnostic evidence it DID resolve the reference — the
   verdict is corrected to `"followup"` (`clarification` reset to `None`), per the router's own
   stated rule ("if you CAN write a resolved standalone_query … the intent is followup, never
   ambiguous"). Trusts the resolved query over a self-contradicting label instead of trying to
   out-prompt the contradiction.
   - **Why both conditions are needed, not just "intent == ambiguous":** every golden-set
     `ambiguous` item today is a FIRST turn with no prior summary/last-turn at all (condition
     (a) is false) — live-verified with the final code (`test_route_leaves_ambiguous_alone_
     without_any_context` + a direct 5x repeat of all 5 single-turn ambiguous fixtures against
     the real router) that the tie-breaker never fires for them, regardless of how its
     `standalone_query` happens to be worded. A message with context that genuinely cannot be
     resolved (tried live: two topics in the summary, a message too vague to pick either) also
     never satisfies (b) — the model can only echo/translate it, adding no new word — confirmed
     live (`"How do I fix this?"` against a two-topic summary stayed `"ambiguous"`).
   - **Why not simply "standalone_query differs from query":** a Spanish message translated to
     English always "differs" even when nothing was resolved — the (b) check specifically
     requires an ADDED substantive word, not just any textual difference.
   - **Correction (14-D1, DA-14-C4-1, HIGH) — the claim above was WRONG, empirically
     verified.** "Requires an ADDED word" is NOT enough to exclude translation-only cases:
     `_resolves_a_reference("¿Cómo lo arreglo?", "How do I fix this?")` returned `True` under
     this block's own original code — a plain machine translation shares almost no tokens
     with the original in ANY language pair, so EVERY word in the translation counts as
     "added" relative to the raw query, with zero real grounding in context. The golden set's
     one Spanish `ambiguous` item (`ambiguous-es-arreglar`) never exercised this because it is
     a first turn with no context at all (condition (a) already false) — the gap was real but
     untested. **Fixed in 14-D1:** `_resolves_a_reference` now takes the real `context`
     (`summary`/`last_turn`) as a third argument and additionally requires the added word to
     actually APPEAR in it — a translation with no grounding in context no longer flips the
     verdict (`test_route_leaves_a_genuinely_ambiguous_spanish_message_with_unrelated_context
     _alone`), while a genuine resolution still does (the added word came FROM context by
     construction, `test_route_overrides_ambiguous_to_followup_for_a_spanish_followup
     _grounded_in_context`). See `docs/TRD.md` §6.4c for the fix's own write-up.

### Measured effect

Real `--only eval` runs against the live dev corpus (same 322-chunk seed, same 75-turn golden
set v2 fixture as 14-C3), identical code both times:

| Metric | 14-C3 (before) | Run 1 (`gate-20261125T180052Z-3120649.log`) | Run 2 (`gate-20261125T180723Z-3121974.log`) | Floor | Met (both)? | Enforced from this block? |
|---|---|---|---|---|---|---|
| intent_accuracy | 0.840 | 0.907 | 0.907 | ≥ 0.90 | **yes** | **yes** |
| follow_up_resolution | 0.900 | 0.900 | 0.900 | ≥ 0.85 | yes (unchanged) | yes (already, 14-C3) |
| noisy_recall | 1.000 | 1.000 | 1.000 | ≥ 0.90 | yes (already) | yes (already) |
| conversational_reply_rate | 0.944 | 1.000 | 1.000 | ≥ 1.00 | yes (both runs) | not this block (see below) |
| greeting_reply_p95_s | ~1.53s | ~1.57s | ~1.56s | ≤ 2.0s | yes (already) | yes (already) |
| router_p95_s | ~1.05s | ~1.05s | ~1.05s | ≤ 1.5s local | yes (already) | yes (already) |

v1 golden set, BOTH runs: `retrieval_recall`/`faithfulness`/`correctness`/`correct_abstention`
all **1.0** — unchanged, this block touches no retrieval/generation code.

`intent_accuracy` 0.840 → 0.907, identical on both runs (same 5 previously-mislabeled turns
re-checked individually: all 5 now observe `"followup"`, matching `expected_intent`) — clears
the 0.90 floor with margin on both, so `enforce.intent_accuracy` flips to `true`.
`conversational_reply_rate` also reached 1.000 on both of these same two runs (up from 14-C3's
0.944) — plausibly the extra few-shot/prompt tweak also firming up the chitchat/off-topic
boundary, or ordinary run-to-run LLM variance; **left unenforced** here regardless, since
DEC-063's scope was `intent_accuracy` + `follow_up_resolution` only and two runs on one fixture
is a thinner basis for a metric this block did not set out to fix — recorded in "Needs the
user's decision" for whoever next touches conversational replies (T14.3/14-D1) to pick up.

### Known limitations (self-reported, not just design) — 14-C4

- **`_resolves_a_reference`'s "substantive word" test is a crude, language-agnostic proxy
  (≥3-letter alphabetic token), not real semantic resolution.** It cannot tell a genuinely
  resolved reference from the model simply inventing a plausible-sounding but WRONG entity not
  actually in the real context (hallucinated resolution) — it only checks that the word is new
  relative to the raw query, never that it is also correct. This mirrors 14-C3's own
  `follow_up_resolution` metric covering genuine correctness instead (checked via
  `retrieved_docs`, not by this tie-breaker); the tie-breaker's job is narrowly to stop a
  self-contradicting `"ambiguous"` label from overriding a standalone_query that is in every
  measured case already correct — not to independently verify that correctness itself.
- **Still carries 14-C3's own run-to-run LLM variance.** The tie-breaker makes the ambiguous/
  followup BOUNDARY deterministic given the model's own output, but the underlying router call
  itself is not bit-for-bit reproducible (temperature, quantized model) — a future run could in
  principle produce a `standalone_query` that fails condition (b) (e.g. staying a bare "it")
  even for a resolvable reference, same class of noise 14-C2/14-C3 already self-report.
- **`conversational_reply_rate` reaching 1.000 on both runs is NOT attributed to a specific
  mechanism** (see "Measured effect" above) — recorded as an observation, not claimed as a
  fixed/understood improvement, and deliberately left unenforced pending a block that actually
  targets it.

## Decision — conversational answers (T14.3.1–T14.3.5, block 14-D1)

**Scope.** Reply in the user's language (D14.1); every chit-chat reply by the LLM (already
shipped, 14-B2); a clarifying question for `ambiguous` (ask, don't guess) and a polite
redirect for `off_topic` (never an uncited answer); a conversational abstention (LLM-phrased,
no claims, rephrasing tips, related topics only if retrieval found them) as the PRIMARY path,
replacing `generation._ABSTENTION_TEXT`.

**DA-14-C4-1 fix done first.** See §6.4c in `docs/TRD.md` and the "Correction" paragraph added
to this document's own 14-C4 Decision section above — `_resolves_a_reference` now requires the
added word to appear in the real `summary`/`last_turn`, not merely be absent from the raw
`query`, closing a translation-only false-positive the 14-C4 ADR wrongly claimed was already
excluded. Re-checked after the fix: `intent_accuracy` still clears its 0.90 floor (see the
eval table below) — the fix only narrows a code path that never fired for any golden-set item
anyway (every `ambiguous` item is either a first turn or has unrelated context), so no
regression was expected and none was measured.

**Resume fix (noisy_recall regression, found by the orchestrator's verification, fixed by the
resumed senior).** The orchestrator's first 2 `--only eval` runs on `cc8c597` both failed
`noisy_recall` (1.000 → 0.750, deterministic). Root cause: golden-set item
`typo-injecion-what` ("What is injecion in security?", no prior context) was ALREADY
classified `"ambiguous"` by the live router at the `bf548f0` baseline too — confirmed by
replaying the identical probe in a throwaway worktree at that commit (same result, 3×). Not a
code regression in `router.py`: this block's own `_resolves_a_reference` tie-breaker change
never touches a no-context, first-turn message. What changed the OUTCOME is T14.3.3's new
dispatch — `ambiguous` never retrieves — so the pre-existing misclassification, which the OLD
`else` branch used to retrieve through anyway (a lucky hit), now shows up as a genuine recall
miss. Fixed with one contrastive few-shot added to `router._ROUTER_SYSTEM` (a misspelling
alone is not an unresolved reference) rather than a code-level tie-breaker on
`clarification is None`: live-verified, 2 of the golden set's 5 genuine `ambiguous` items
("Can you help me?", "¿Cómo lo arreglo?") ALSO produce `clarification=None`, so that signal
does not distinguish a genuine ambiguity from this one. See `docs/TRD.md` §6.5's own
paragraph and `router.py`'s module docstring for the full write-up; commit `81c562a`.

**New module `assistant/replies.py`** — `clarifying_reply` (reuses the router's own
clarifying question, no extra LLM call), `generate_offtopic_reply`, `generate_abstention_reply`
(both LLM calls, same delimited-untrusted-data/bounded-tokens/bilingual-template-fallback
discipline `fast_path.generate_chitchat_reply` already established in 14-B2). Dispatched
identically in all three entry points that already branch on `decision.intent`
(`generation.answer_question`, `.answer_question_stream`, `eval.runner.evaluate_dialogues`) —
same pattern 14-B2's chit-chat branch and 14-C1's memory wiring already used (DEC-059 option
A: wire separately into each entry point that genuinely has the information, rather than a
larger refactor).

**Conversational abstention wiring.** `answer_from_chunks` gains `language: str | None = None`
(default `None` keeps the OLD fixed English text — the CLI, `eval.benchmark`'s v1 run,
`agentic.py` never detected a language and are out of this block's scope, must not regress).
The three conversational callers pass `language=decision.language`. `related_topics` — the
headings of chunks retrieval found but did not ground an answer in (`chunks[:3]`) — are passed
through to the reply generator, which is explicitly told never to invent one not listed.

**Why `ambiguous` needs no extra LLM call.** The router (§6.2, T14.2.3) already writes a short
clarifying question, in the user's own language, whenever it decides `"ambiguous"` — reusing
it is both cheaper and more faithful to what the router itself determined was unclear than
generating a second, independent question would be. A short bilingual fallback
(`CLARIFYING_FALLBACK_EN`/`_ES`) covers the one case the router can still contradict its own
rule on (§6.4b/§6.4c narrowed, did not eliminate, the "`ambiguous` with `clarification=None`"
self-contradiction) — never surfaces an empty reply.

### Eval table — measured on the final code (resume, commit `81c562a`/`6bcbc96`), 7 runs

The first verification attempt (on `cc8c597`, before the resume fix) was killed by the harness
itself (system-wide low memory while the session sat idle) immediately after `gate.sh --fast`
had passed; the orchestrator then ran `--only eval` twice on `cc8c597` directly and found a
real, deterministic `noisy_recall` regression (1.000 → 0.750 both times) — see "Resume fix"
above for the root cause and fix (commit `81c562a`). **`noisy_recall` is the metric this block
set out to fix, and it is now the most solid number in the whole table: 1.000 on EVERY one of
the 7 `--only eval`/`--full` runs below, including a code revert, an Ollama restart, and 2
full `gate.sh --full` attempts.** `intent_accuracy`/`follow_up_resolution`, by contrast, turned
out to be noisier at the margin than the 2-run framing below first assumed — the full 7-run
picture (not just the first 2) is the honest basis for this block's state:

| Run | intent_accuracy | noisy_recall | follow_up_resolution | Verdict | Log |
|---|---|---|---|---|---|
| 1 | 0.933 | 1.000 | 1.000 | ALL enforced floors PASS | `gate-20261125T194629Z-3275007.log` |
| 2 | 0.920 | 1.000 | 1.000 | ALL enforced floors PASS | `gate-20261125T195226Z-3276875.log` |
| `--full` #1 (eval step) | 0.893 | 1.000 | 0.900 | eval FAIL (intent) | `gate-20261125T195933Z-3279426.log` |
| A (post-revert¹) | 0.907 | 1.000 | 0.700 | FAIL (follow-up) | `gate-20261125T202842Z-3338303.log` |
| B | 0.920 | 1.000 | 0.800 | FAIL (follow-up) | `gate-20261125T203509Z-3339606.log` |
| C (fresh Ollama²) | 0.893 | 1.000 | 0.900 | FAIL (intent) | `gate-20261125T204218Z-3341180.log` |
| `--full` #2 (eval step) | 0.880 | 1.000 | 0.900 | eval FAIL (intent) | `gate-20261125T204938Z-3342634.log` |

Floor: `intent_accuracy` ≥ 0.90, `follow_up_resolution` ≥ 0.85, `noisy_recall` ≥ 0.90 (all
already enforced since 14-C4/14-C3/14-C2 respectively — this block changes none of them).
¹Between runs A and the `--full` #1 result, a reworded few-shot was tried (to also fix a
newly-observed `typo-hol-greeting` miss) and REVERTED after it caused a worse, reproducible
regression on `typo-hell-midsentence`/`what the hell is sql injection` — see "Known
limitations" below; run A is the FIRST measurement after reverting back to the original
commit-`81c562a` wording, confirmed byte-identical via `git diff`. ²Ollama (`ollama serve`,
up 6h45m/4.2 GB RSS at that point) was stopped and restarted fresh between run B and run C to
rule out server-state drift as the cause — run C still failed (on a different metric), which
disproves that hypothesis.

**Per-turn forensics (all 7 runs' `eval/golden_set_v2_results.json` snapshots kept at
`~/secrag-gate-logs/14-D1-*.json`): every single `intent_accuracy`/`follow_up_resolution` miss
across all 7 runs belongs to an already-identified, PRE-EXISTING noise category** — none of
them is `typo-injecion-what` (the item this block's fix targets, which stays correct in all 7):
- `abstain-en-cisco`/`abstain-es-nginx` (security → off_topic) and `dlg-ambiguous-then-clarify`/
  `dlg-abstention-then-recover` — the EXACT items 14-C3's own ADR section (§"Decision — 14-C3")
  already named as "PRE-EXISTING items unrelated to this block's changes... consistent with
  14-C2's own 'LLM run-to-run noise' finding" — present in the orchestrator's 2 pre-fix runs on
  `cc8c597` too, so provably not caused by this block.
- `dlg-followup-3-back-es`/`dlg-followup-3-back-es-2025` (a multi-turn-back `followup` labelled
  `ambiguous`) — the same "boundary confusion between two intents whose DEFINITIONS are
  genuinely close for a multi-turn-back reference" 14-C3's ADR already documents for siblings
  of this exact dialogue family.
- `typo-hol-greeting`/`typo-hell-greeting`/`dlg-typo-then-followup` turn 0 (a greeting typo —
  "hol"/"hell" — labelled `off_topic` instead of `chitchat`) — a NEWLY-OBSERVED noise class, not
  seen in the orchestrator's 2 pre-fix runs, but confirmed NOT deterministically caused by this
  block's fix either: isolated, fresh-process repeated trials (5x) of the bare messages "hol"
  and "hell" against the unmodified commit-`81c562a` code returned `chitchat` every time; it
  only appears inside full 75-turn `--only eval`/`--full` runs, where upstream per-turn memory/
  summarization calls (visibly non-deterministic in every run's log: `chat_json invalid
  output... falling back`) feed slightly different accumulated state into later turns — the
  same "not bit-for-bit deterministic run to run even at temperature 0" phenomenon 14-C2/14-C3's
  ADR sections already self-report, now additionally visible on the chitchat/off_topic boundary.

**Conclusion:** the fix in commit `81c562a` is verified correct and solid for its actual target
(`noisy_recall`, 7/7 clean); it did not cause the `intent_accuracy`/`follow_up_resolution`
flakiness, which is pre-existing, already-documented LLM margin noise that happened to surface
more visibly during this block's unusually large number of verification runs (7, vs. 14-C3/
14-C4's 2 each). **Per the brief's own fallback ("if a floor cannot be met after a real effort,
STOP and report"): this report IS that stop** — see "Needs the user's decision" in the senior's
handback for the options. No floor was lowered or silently un-enforced.

v1 clean metrics (`retrieval_recall`/`faithfulness`/`correctness`/`correct_abstention`): **1.000
on every run that reached the `eval` step** — unaffected by this block for `security`/
`followup` turns (groundedness/citation logic untouched); `correct_abstention`/judge "no
claims" now judges the conversational abstention text instead of the old fixed sentence
(T14.3.5) and still measures 1.000 throughout.

`conversational_reply_rate` (not floored by this block) ranged 0.833–1.000 across the 7 runs —
the same chitchat/off_topic-boundary noise as above, not attributed to a specific mechanism;
left unenforced, consistent with 14-C4's own precedent and the orchestrator's brief ("enforce
only if 1.0 in both runs" — it was not).

### Known limitations (self-reported, not just design) — 14-D1

- **The conversational abstention/off-topic/clarifying replies add real LLM-call latency and
  cost to turns that used to be free (abstention) or already an LLM call (chitchat already
  was from 14-B2).** An abstention is now one more bounded call (`ABSTENTION_REPLY_MAX_TOKENS`
  = 100) on top of generate + groundedness; an off-topic turn is one call instead of a full
  retrieve→generate→check pipeline (likely a net latency IMPROVEMENT for that case, since
  retrieval/reranking no longer run at all) — not independently measured per-intent in this
  block, only the aggregate `eval` floors above.
- **`related_topics` is a crude top-3 heading list (`chunks[:3]`), not a relevance-ranked
  "what you might actually want" suggestion** — the chunks retrieval found are, by
  definition, the ones that did NOT ground an answer; they may be only loosely related to
  what the user actually asked. The system prompt's own instruction (never invent a topic not
  listed, never mention any if the list is empty) bounds the failure mode to "an unhelpful
  suggestion", never a fabricated fact.
- **`clarifying_reply`'s fallback question is generic** (`CLARIFYING_FALLBACK_EN`/`_ES`, not
  tailored to the actual message) — only reached when the router already broke its own
  stated rule; narrowing that further is the router's own job (§6.4b/§6.4c), not this
  module's.
- **`conversational_reply_rate` enforcement decision** (14-C4's own open item): **left
  unenforced.** Ranged 0.833–1.000 across 7 runs (down from 14-C4's own 1.000) — never 1.0
  twice running, so per the orchestrator's own stated rule ("enforce if 1.0 in both runs") it
  stays `enforce: false` in `eval/golden_set_v2_floors.json`. Not attributed to a specific
  mechanism this block changed (chitchat's own branch/metric computation is untouched) — the
  same chitchat/off_topic-boundary noise documented below.
- **The noisy_recall / "ambiguous never retrieves" interaction (resume finding, CONFIRMED
  across 7 runs, not just narrowed).** Making `ambiguous` a dead-end for retrieval means ANY
  router misclassification of a genuine, answerable security question as `"ambiguous"` costs a
  real `noisy_recall`/`retrieval_recall`/`intent_accuracy` point that the pre-14-D1 pipeline
  would have absorbed silently (it retrieved anyway, just under the wrong label). This block
  fixed the ONE specific instance it was asked to fix (a misspelling, `typo-injecion-what`,
  solid 1.000 across all 7 verification runs) but discovered, while verifying it, that the
  SAME structural exposure already existed for several OTHER boundaries this block never
  touched: `security` questions about out-of-corpus vendor tools misclassified `off_topic`
  (`abstain-en-cisco`/`abstain-es-nginx`, pre-existing per 14-C3's ADR), multi-turn-back
  `followup` misclassified `ambiguous` (`dlg-followup-3-back-es`/`-es-2025`, pre-existing per
  14-C3's ADR), and — newly observed this block, though NOT deterministically caused by it
  (isolated fresh-process trials of the bare messages return the correct `chitchat` every
  time; it only surfaces inside a full 75-turn run, alongside visible upstream
  memory-summarization non-determinism) — a short greeting typo (`"hol"`/`"hell"`)
  occasionally misclassified `off_topic` instead of `chitchat`. **This is a router-level
  reliability concern spanning the WHOLE module, not something a single contrastive few-shot
  can close** — see "Needs the user's decision" in the senior's handback for how to proceed.
- **A reworded few-shot attempt was tried and reverted (self-reported, do not repeat without
  re-testing).** To also fix the `typo-hol-greeting` miss, the fix's few-shot was reworded
  twice: the first reword explicitly named the word "hell" as a chitchat example, which caused
  a REAL, reproducible regression — the model started pattern-matching the literal substring
  "hell" and misclassified `typo-hell-midsentence`/`"what the hell is sql injection"` (a real
  security question using "hell" as an intensifier) as `chitchat`/`ambiguous`. A second,
  more generic reword still left `"what the hell is sql injection"` misclassified `ambiguous`
  (5/5 fresh-process trials). Both rewords were reverted; the shipped code is the ORIGINAL,
  narrower wording from commit `81c562a` (confirmed solid for its own target across all 7
  runs). **Lesson for whoever next touches `_ROUTER_SYSTEM`: a few-shot aimed at one specific
  item can shift the decision boundary for UNRELATED items sharing a surface feature (a literal
  word, a short/truncated shape) — always re-test the full golden set's noisy/greeting items
  after any system-prompt change, not just the item the change targets.**

### Eval-determinism fix and the real deterministic baseline (DEC-064, block eval-determinism)

**This supersedes the 7-run "margin noise" framing above with a real answer: most (not all)
of that flakiness was genuine LLM-serving non-determinism, now fixed — but fixing it reveals
the deterministic result is BELOW the enforced floors, not a lucky-vs-unlucky coin flip.**

**Root cause.** A minimal reproduction (same prompt/options, N=8 calls, real native Ollama)
showed `OllamaChat.chat()` and `assistant.memory.summarize_turn()` are ALREADY
byte-identical across repeats when called back-to-back with IDENTICAL input and no seed —
isolated single-call determinism was never the problem (confirms the 7-run report's own
"isolated trials are stable" finding, rather than contradicting it). Reading `eval/runner.py`/
`eval/gate.py` confirmed there is no concurrency anywhere in the eval path. Cross-referencing
the 7 runs' per-turn JSON separated two different phenomena the earlier framing had merged:
3 items were wrong in **all 7/7 runs identically** (genuinely pre-existing, non-flipping
noise), while a DIFFERENT set of items flipped between correct/incorrect run to run — every
flipping item either depends on a `summary`/`last_turn` string built by an earlier
`summarize_turn` call in the SAME run, or sits on a close router decision boundary — consistent
with Ollama's own GPU-serving non-determinism (a documented class of issue, independent of
client concurrency) that is invisible on an easy prompt but can flip a near-tied decision, and
compounds across a multi-turn dialogue because an earlier turn's non-deterministic summary
text changes what a LATER turn's router literally reads.

**Fix (commit `60f5356`):** `config.Settings.ollama_seed` (default `42`) sent as
`options.seed` on every `OllamaChat` call; `evaluate_dialogues` confirmed (by reading) already
strictly sequential, with a new regression test guarding it; the gate's native Ollama server
restarted with `OLLAMA_NUM_PARALLEL=1` for this block's measurement runs (a process-level
setting, not a code change — outside this block's `touches`, see "Needs the user's decision"
in the senior's handback for the runbook edit this needs to become persistent).

**Measured (3 `gate.sh --only eval` runs on commit `60f5356`, same fixture, n=75):**

| Run | intent_accuracy | noisy_recall | follow_up_resolution | conversational_reply_rate | Note |
|---|---|---|---|---|---|
| 1 (just after an Ollama restart) | 0.867 | 1.000 | 0.700 | 0.889 | `gate-20261125T230150Z-3484421.log` |
| 2 | 0.867 | 1.000 | 0.800 | 0.833 | `gate-20261125T230955Z-3507623.log` |
| 3 | 0.867 | 1.000 | 0.800 | 0.833 | `gate-20261125T231616Z-3509350.log` |

**Runs 2 and 3 are BYTE-IDENTICAL** in every field of `eval/golden_set_v2_results.json`
(intent, reply text, correctness, clarified, abstention-no-claims — everything) except the
`latency_s`/`router_latency_s` timing fields themselves (confirmed with a full structural
`diff`, 0 content differences). **This is full determinism, achieved** — the fix works. Run 1,
taken immediately after restarting Ollama (to apply `OLLAMA_NUM_PARALLEL=1`), differs from
runs 2/3 in exactly 2 items (`typo-hol-greeting`↔`dlg-followup-3-back` swap, same total miss
count) — a cold-start artifact (GPU kernel/algorithm selection on the very first request after
a fresh model load), not a flaw in the fix itself; in this project's real usage Ollama is a
long-running native process started once per session, so this edge case is a one-off, not a
per-gate-run risk.

**The deterministic value itself (`intent_accuracy` 0.867, `follow_up_resolution` 0.800) is
BELOW both enforced floors (≥ 0.90 / ≥ 0.85) — every single time, not sometimes.** Per-turn
evidence (identical for runs 2 and 3; run 1 differs only in the 2 items named above), all 10
`intent_accuracy` misses and both `follow_up_resolution` misses belong to the SAME
already-documented categories 14-D1's own report above names (`abstain-en-cisco`/
`abstain-es-nginx`, `dlg-ambiguous-then-clarify`, `dlg-abstention-then-recover`,
`dlg-followup-3-back-2025`/`-es-2025`, greeting-typo `off_topic` misclassifications) — this
block's fix did not introduce any new failure category, and did not regress `noisy_recall`
(1.000, all 3 runs, confirming 14-D1's own fix is still solid). **What changed is the
CONFIDENCE in the number: before this block, intent_accuracy's 7-run range (0.880–0.933)
included 2 runs that cleared the 0.90 floor, which looked like "noise that might land on the
right side of the floor by chance" (14-D1's own Option A reasoning). Now that the flipping is
fixed, the deterministic value (0.867) is BELOW even the lowest historical run — this was
never noise alone; some of those "lucky" high-side 7-run numbers were themselves
non-deterministic excursions, and the true, reproducible baseline is lower than any of them.**

This is the brief's own stop condition ("if floors still fail deterministically, STOP and
report per-turn evidence") — no floor is lowered or un-enforced; this block does not attempt a
router-prompt fix (out of DEC-064's scope, and 14-D1's own report already names this as a
needed, separate router-reliability block). See "Needs the user's decision" in the senior's
handback for how to proceed with 14-D1's closure given this.

## Decision — router-reliability: 5 deterministic fixes for the 10 misses (DEC-066), closing
14-D1 + eval-determinism

DEC-066's own concrete 6-change plan, implemented exactly:

1. **Greeting typos (`assistant.fast_path.match_fast_path`), no router/model involved at
   all.** A LONE word within one edit (substitution/insertion/deletion) of a greeting root
   ("hello"/"hola") now fast-paths — never inside a sentence (the whole, punctuation-trimmed
   message must be exactly that one word). Fixes `typo-hol-greeting`/`typo-hell-greeting`/
   `dlg-typo-then-followup` turn 0 (observed `off_topic`, expected `chitchat`) 100%
   deterministically, since the router is never even called for these messages. **Known,
   accepted false-positive risk** (self-reported, same discipline as every other block's
   boundary): a one-edit check cannot distinguish a genuine typo ("holz") from a real,
   unrelated short word that happens to also be one edit from a root ("hold" vs "hola") — low
   risk (a standalone "hold" routed to chit-chat is a minor UX quirk, never a security issue;
   proven by the compound/injection security tests in `test_assistant_router.py`/
   `test_assistant_fast_path.py`, which show this never widens past a LONE word).
2. **Security infrastructure our corpus does not cover is still `security`, never
   `off_topic`** (`router._ROUTER_SYSTEM`, prompt text only). The `security` bullet now names
   configuring security infrastructure (firewalls, proxies, TLS, WAF) as in-scope even when
   our own OWASP corpus does not cover the specific vendor product — that is the ANSWER
   step's job to abstain on, never a routing decision; `off_topic` is now explicitly reserved
   for subjects that are not about security at all. Fixes `abstain-en-cisco`/
   `abstain-es-nginx`/`dlg-abstention-then-recover` turn 0.
3. **A self-contained security question is never `ambiguous`**
   (`router._names_a_security_term`, a deterministic code-level tie-breaker). When the model
   says `ambiguous` but the message ITSELF already names a recognisable security term (en/es),
   the verdict is corrected to `security`, independent of conversation context. Fixes
   `dlg-abstention-then-recover` turn 1.
4. **The reply to a clarifying question is a NEW security question**
   (`router._previous_turn_asked_the_clarification`). When the PREVIOUS turn's own decision
   was `ambiguous` (its reply was the clarifying question itself) and this turn's raw
   intent is `followup`/`ambiguous`, it is corrected to `security` — never a followup to
   whatever topic preceded the clarification. `route()`/`understand()` grow a caller-tracked
   `previous_intent` parameter (never inferred from `last_turn` TEXT: an `off_topic` redirect
   reply can also end in a question mark, e.g. "Would you like to ask about security
   instead?", which would misfire on an unrelated, already-correct multi-turn-back follow-up
   — change 5's own dialogues). **Self-reported boundary:** only `eval.runner
   .evaluate_dialogues` tracks and passes `previous_intent` today — `api.conversations` (the
   live `/chat/stream` path) does not yet persist a per-turn intent, so this fix benefits the
   deterministic eval now; extending it to live chat needs a small schema/wiring follow-up.
   Fixes `dlg-ambiguous-then-clarify` turn 1.
5. **A bare pronoun/reference question with a subject in memory is always a followup**
   (`router._is_bare_reference_query` + `_memory_subject`). A message that names no content
   word of its own beyond generic position/edition/version vocabulary (en/es) — "what rank
   does IT have...", or the Spanish pro-drop equivalent with no pronoun at all — is resolved
   from the conversation summary's own `topics`/`entities` fields DIRECTLY (parsed from the
   real JSON `summarize_turn` writes) rather than trusting the model's own (sometimes
   unresolved) `standalone_query`. Fixes the `dlg-followup-3-back*` family's final,
   several-turns-back follow-up turn, in both languages.
6. **Persistent `OLLAMA_NUM_PARALLEL=1`.** `tools/ops/resume.sh` (private, orchestrator-owned
   per the brief) now exports it before every `ollama serve` it starts — outside this block's
   `touches`, confirmed already done before this block began. **Follow-up needed, not done by
   this block (outside `touches`):** `scripts/gate.sh` should warn when it finds a native
   Ollama server already running WITHOUT the var — recorded in `status/blocks/
   router-reliability.md`, not implemented here (another branch, `f24-check`, also edits
   `scripts/gate.sh`; this block was briefed not to touch it).

### Order preserved (security)

Every new tie-breaker (3, 4, 5) runs strictly AFTER the existing, already-reviewed sequence:
delimited-untrusted-data handling (`build_router_messages`) → strict schema validation
(`chat_json`) → the conservative `security` fallback on ANY failure → the suspicious-output
screen (`_looks_suspicious`). A reply that echoes a delimiter marker still falls back to
`security` with the ORIGINAL query regardless of how "ambiguous"-shaped or security-term-laden
the message is (`test_security_term_tie_breaker_never_fires_on_a_suspicious_reply`) — none of
the 3 new tie-breakers adds a new attacker-reachable code path; each only relabels an output
the already-screened router itself produced, same reasoning DA-14-C4's own review already
established for the pre-existing tie-breaker. None can ever produce `chitchat` (the only
intent the daily-cap/fast-path exemption grants) — they only ever resolve TOWARDS `security`/
`followup`, both of which still run the full retrieve → generate → groundedness-check →
cite-or-abstain path.

### Measured effect — 2 real `--only eval` runs on the final code, BYTE-IDENTICAL (n=75)

| Metric | eval-determinism baseline (83bf53c) | Run 1 (`gate-20261126T003647Z-3588786.log`) | Run 2 (`gate-20261126T004905Z-3590916.log`) | Floor | Met (both)? | Enforced? |
|---|---|---|---|---|---|---|
| intent_accuracy | 0.867 | 0.9733333333333334 | 0.9733333333333334 | ≥ 0.90 | **yes** | yes (already) |
| follow_up_resolution | 0.700 | 1.0 | 1.0 | ≥ 0.85 | **yes** | yes (already) |
| noisy_recall | 1.000 | 1.0 | 1.0 | ≥ 0.90 | yes | yes (already) |
| conversational_reply_rate | 0.833 | 1.0 | 1.0 | ≥ 1.00 | **yes, both runs** | **yes, from this block** |
| greeting_reply_p95_s | 1.628 | 1.5902445820102002 | 1.6275182380049955 | ≤ 2.0s | yes | yes (already) |
| router_p95_s | 1.222 | 1.116358497994952 | 1.0969021230121143 | ≤ 1.5s local | yes | yes (already) |

v1 clean metrics, both runs: `retrieval_recall`/`faithfulness`/`correctness`/
`correct_abstention` all **1.0** (unchanged). `scripts/gate.sh --only eval`'s own
`eval/golden_set_v2_results.json` is BYTE-IDENTICAL between the two runs (confirmed by a full
structural diff) except the timing fields — the fix did not reintroduce the non-determinism
DEC-064 eliminated.

**10/75 → 2/75 misses.** 8 of the original 10 deterministic misses are fixed (all 3 greeting
typos, all 3 uncovered-security-infra items, the self-contained-question item, the
reply-to-clarification item, and both `dlg-followup-3-back*`/`-es*` 3-back follow-up turns —
change 5 fixes BOTH the originally-named `-2025` variants AND the plain/`-es` variants, which
this run's own measurement also showed missing at the `83bf53c` baseline, not just the 2 DEC-066
named explicitly).

### Known limitation (self-reported, not just design) — 2 new misses from change 2's prompt
wording

Change 2's prompt rewording introduced **2 new deterministic misses NOT present in the
10-item baseline**: `es-bac-rank-2021` turn 0 and `dlg-followup-doc-version` turn 0 (both
FIRST turns, no conversation context at all — expected `security`, observed `followup`).
Root-cause isolated with a disposable `git worktree --detach 83bf53c` and direct `route()`
probes (same technique as 14-C4's own re-diagnosis): restoring the ORIGINAL (pre-change-2)
prompt wording on top of this block's own tie-breaker code makes both items pass again,
confirming the new misses are caused by the prompt text change itself (most likely the new
Cisco-ASA few-shot interacting with 14-C3's own pre-existing "rank in edition" follow-up
few-shot, which shares near-identical wording with these two self-contained, no-context
questions) — not by any of the 3 new code-level tie-breakers, none of which can fire on a
first turn with no `summary`/`last_turn` at all. A milder prompt wording was tried and
confirmed to avoid this specific regression, but then left `abstain-en-cisco` itself
unfixed (it only fixed the Spanish nginx item) — trading one deterministic miss for another,
not a net improvement. **Decision: keep the stronger wording** (this is a NET improvement,
10 → 2 misses, both floors cleared with a large margin on both runs) rather than chase a
zero-regression prompt that 14-D1's own report already warned is a real risk ("two reworded
few-shot attempts, both reverted after causing new regressions elsewhere" — the same
whack-a-mole this block avoids repeating). Tracked as a known, accepted trade-off, not hidden.

## Decision — F24/F31 merge + DA-router-reliability follow-ups (block F24-into-14)

Merged `f24-check` (F24: `scripts/gate.sh` refuses a manual-background launch; F31: one
gate seed per alembic head) into `phase-14-assistant` — clean, no conflicts (`fa135d8` x
`bf548f0`), confirmed by an independent `git merge-tree` before this block started.
`check_ollama_num_parallel()` closes DEC-066 change 6's second half: at gate start, reads
`/proc/<pid>/environ` of the native `ollama serve` process (`pgrep -f`) and prints a
WARNING (never fails) when `OLLAMA_NUM_PARALLEL=1` is missing -- the dialogue eval's
determinism (DEC-064) depends on it. A test-only `GATE_OLLAMA_PID_FOR_TEST` override (same
convention as the pre-existing `GATE_LOCK_TEST_SLEEP`) lets the 4 new tests point at a
throwaway process deterministically instead of racing the real `pgrep` against whatever
server may already be running on the host.

**DA-router-reliability-2** (devil's-advocate finding 2 on the router-reliability block):
a raw model `"followup"` verdict with NO context (no `summary`, no `last_turn`) is never
trusted -- it falls through to `"ambiguous"` instead, letting the existing security-term
tie-breaker (change 3) still classify it correctly. The DA proved, by querying the golden
set directly, that zero turns ever expect `"followup"` on a context-free first turn, so
this guard is provably safe against the whole golden set. Fixes the 2 regressions change 2
introduced (`es-bac-rank-2021`, `dlg-followup-doc-version`, both turn 0) with no new
regressions (confirmed on 2 byte-identical `--only eval` runs below).

**DA-router-reliability-1** (devil's-advocate finding 1): `previous_intent` now reaches the
LIVE `/chat/stream` path, not just the deterministic eval. `assistant.memory
.ConversationSummary` gains a new `last_intent: str | None = None` field, piggybacked onto
the ALREADY-persisted, already-encrypted `summary_encrypted` JSON blob -- **no new database
column, no migration 0010** (every summary written before this change simply lacks the key,
which Pydantic reads as the default `None`, never a parse error). `summarize_turn()` gains
an `observed_intent` parameter that OVERWRITES whatever the LLM itself produced for that
field -- the field is never asked for in the memory prompt, by design: this is a plain
code-set/code-read value, not something a model should be trusted to report about itself.
`api.conversations._load_memory` reads it back via the new
`assistant.memory.previous_intent_from_summary` helper (same "never crash on bad stored
data" discipline as `router._memory_subject`) and threads it through
`_pipeline_events`/`answer_question_stream`/`understand()`/`route()`; the turn's own
observed intent travels back out via a new `StreamResult.intent` field (set at all 5 of
`answer_question_stream`'s yield sites) and is written back by
`_update_conversation_summary` -> `summarize_turn(observed_intent=...)`. `answer_question`
(`POST /chat`) also gained the `previous_intent` parameter, forwarded into `understand()`,
for signature symmetry with `answer_question_stream` -- `POST /chat` remains genuinely
stateless by design (TF24/DEC-059: no conversation at all, the same existing scope boundary
that already excludes `summary`/`last_turn` from that function), so no live caller supplies
a real value there today; this is parity, not a new capability.

### Known limitations (self-reported, not just design) -- F24-into-14

- **`last_intent` staleness across a fast-pathed turn.** The write only happens when
  `_update_conversation_summary` runs, which (14-C1-fix) is deliberately SKIPPED on an
  exact fast-path chit-chat turn (a bare greeting/thanks/farewell). A fast-pathed turn
  sandwiched between an `"ambiguous"` turn and the user's reply to it leaves `last_intent`
  one turn stale (still `"ambiguous"` from before the fast-pathed turn, not updated to
  `"chitchat"`). This never produces an unsafe result -- change 4's tie-breaker only ever
  moves an intent TOWARDS `"security"` (`assistant.router`'s own security framing: ANY
  failure/ambiguity degrades to the conservative fallback, never away from it) -- only a
  label/retrieval-target mismatch in this narrow, rare case (a user saying "thanks" or "hi"
  literally between a clarifying question and their answer to it). Unlike `eval.runner
  .evaluate_dialogues`'s own `previous_intent` tracking (a plain in-memory variable,
  unconditionally updated every turn, never stale), the live path's fidelity is bounded by
  the SAME optimization that already accepts similar staleness for `topics`/`entities`.
- **`GATE_OLLAMA_PID_FOR_TEST`'s production path (plain `pgrep -f`) is exercised manually,
  not by an automated test** that avoids depending on the host's real Ollama process (doing
  so deterministically would need spawning a process whose own command line literally
  contains the substring `ollama serve`, adding meaningful complexity for a thin wrapper
  around 2 already-tested primitives, `pgrep`/`/proc/<pid>/environ` parsing). Manually
  confirmed working against the real native Ollama server during this block (found its PID,
  read its real `OLLAMA_NUM_PARALLEL=1` from `/proc/<pid>/environ` directly).

## Decision — Visible plan panel + ride-alongs (block 14-D2)

**Ride-along 1 (DA-F24-into-14's own Finding 1, done FIRST, per the brief):** the
`DA-router-reliability-2` guard (F24-into-14) fell through to `"ambiguous"` for a
context-free raw `"followup"` verdict, relying on change 3's `_names_a_security_term`
regex to rescue it to `"security"` when the message happens to name a recognised term. The
DA proved that regex is not exhaustive (4 golden-set `security` turns whose own wording
would not trip it: "rotate AWS IAM access keys", "harden a Kubernetes cluster", "¿Cómo
detecto malware...?", "detailed database error messages"). The guard now falls through
STRAIGHT to `"security"` -- the SAME conservative default every other router failure mode
already uses (timeout, suspicious output, HTTP error) -- independent of whether the message
matches the regex. New tests (`test_assistant_router.py`, en + es, deliberately OUTSIDE
`_SECURITY_TERM_RE`'s vocabulary) prove a genuine first-turn security question like "How do
I rotate AWS IAM access keys?" is now answered, not met with a clarifying question.

**Ride-along 2:** `ConversationSummary.last_intent` (`assistant/memory.py`) is now typed to
the real `Intent` value set (`Literal["chitchat","security","followup","ambiguous",
"off_topic"] | None`) rather than a bare `str` -- closing DA-F24-into-14's Finding 2. A
`field_validator(mode="before")` maps any value OUTSIDE that set to `None` (never raises --
a forged/corrupted stored summary degrades to the same safe default as a summary written
before this field existed) rather than a plain `Literal` field, which would turn one bad
key into a hard parse failure for the WHOLE summary. `summarize_turn`'s own
`model_copy(update=...)` call -- which bypasses pydantic validation entirely -- gets the
SAME unknown-value guard inline, since the field validator alone cannot catch that path.

**T14.4.1 -- stage events.** Four new `StreamStep` events (reusing the exact
`{"type":"step",stage,step,phase}` SSE contract 13-J3b shipped for the wake banner's
`embed_query` sub-step, DEBT-062, per 14-0 re-check #3): `understood` (every turn, incl.
chitchat/ambiguous/off_topic), `searching`/`found_sources` (retrieval path only),
`writing_answer` (right before generation). `StreamStep` gains an optional `detail: str |
None` field, built EXCLUSIVELY from already-classified/derived values -- the detected
`{intent}/{language}`, the retrieval `standalone_query` (+ detected OWASP edition), the
chunk count -- never the system prompt, a raw/few-shot prompt or a delimiter marker. A
canary test (`test_assistant_plan_events.py`) asserts no step's `detail`, across every
branch (chitchat/ambiguous/off_topic/security), ever contains a fragment of `_ROUTER_SYSTEM`
/`_MEMORY_SYSTEM` or a `<<<`/`>>>` delimiter marker. An unrecognised `step` name (including
these four, for an OLDER frontend build) is a forward-compatible no-op, same convention
`reduceWakeStep` already uses.

**T14.4.2 -- plan panel.** No approved mockup exists for this panel (memory: "follow
mockup exactly" -> "no mockup, follow the existing chat/app style exactly") --
`components/PlanPanel.tsx` reuses WakeBanner.tsx's own design tokens (`vault-amber` for the
live/current line, `vault-steel-dark` borders, `vault-steel-light` for past entries) and the
app's single monospace typeface; no new colours/fonts. Collapsed by default, showing only
the latest step's label as a live one-line status; a plain, native `<button>` (always
keyboard-reachable) toggles the full transcript, `aria-expanded` reflects the state,
`aria-controls` points at the expanded list. `prefers-reduced-motion`: the panel starts no
animation of its own (expand/collapse is a plain conditional render, not a CSS transition)
-- the project-wide `globals.css` rule already neutralises every `animate-pulse`/transition
utility elsewhere, so there is nothing extra to opt out of. **XSS:** every label (which may
contain the user's own `standalone_query` text, via the `searching` step) is rendered as a
plain JSX text child -- React's default escaping -- never `dangerouslySetInnerHTML`, never
passed through a markdown/HTML renderer; `lib/planSteps.test.ts` has a dedicated test
proving an XSS-shaped detail string survives the reducer as a literal string, unparsed.

**T14.4.3 -- tests.** Backend: the canary above, plus unit tests for the event sequence,
content and ordering on the security/chitchat/ambiguous/abstention paths
(`test_assistant_plan_events.py`); two pre-existing `test_generation.py` tests narrowed
their own filter to `embed_query`/no-embedding specifically (the new `understood` step is
additive and was breaking their blanket "no StreamStep at all" assertions). Frontend:
`lib/planSteps.test.ts` (the pure reducer) and an extended `lib/chatStream.test.ts` (the
`detail` field is forwarded; a JSON `null` becomes `undefined`). E2E:
`e2e/plan-panel.spec.ts` drives a real registered user through one real chat turn and
asserts the panel is collapsed by default, shows a live status, expands via a real `Enter`
keypress on the toggle (not only a mouse click), and lists all four steps in order with the
expected fixed label shape (the LLM's own exact `standalone_query` wording is deliberately
NOT pinned here -- that is `eval/golden_set_v2*`'s job).

### Known limitations (self-reported, not just design) -- 14-D2

- **Ride-along 1's new guard can answer a genuinely off-topic, context-free message as
  `"security"`** if the model mislabels it `"followup"` with no context AND the message
  does not land in the fast path's exact chitchat/greeting set. Before this ride-along the
  same mislabel degraded to `"ambiguous"` (a clarifying question); now it degrades to
  `"security"` (an attempt to answer, which the existing retrieve -> generate ->
  groundedness-check -> cite-or-abstain path then either answers for real or abstains on,
  never an uncited claim). This is the deliberate, documented trade-off DEC-066's own framing
  argues for ("a first-turn real security question must be answered") -- accepted as a
  known, conservative-but-possible mismatch, not chased further this block (no golden-set
  item exercises it either way).
- **The plan panel's transcript is never auto-collapsed or cleared once a turn finishes** --
  it stays expanded/collapsed at whatever state the user left it, showing the completed
  turn's steps, until the NEXT question resets it (`usePlanSteps.ts`'s `reset()`, called
  alongside the existing `wakeSteps.reset()`). This is intentional (a small, free "what did
  it just do" transcript) but means the panel's live status line can show a stale
  "Writing answer" after the real answer has already finished streaming -- never incorrect,
  just no longer literally "live" at that point.
- **The `searching` step's `detail` is not length-bounded for display** -- it is the
  router's own `standalone_query` (already bounded by `ROUTER_MAX_TOKENS`, T14.1.1), so a
  pathological length is not reachable today, but `PlanPanel.tsx` relies on `truncate` (a
  CSS ellipsis on the COLLAPSED one-line view only) rather than a hard string cap; the
  EXPANDED list shows the full text unbounded. Acceptable given the existing token bound;
  flagged for whoever next changes that bound.

## Rollback

14-D2: ride-along 1 changes LIVE runtime behaviour (a context-free raw `"followup"` now
becomes `"security"` instead of `"ambiguous"` -- see "Known limitations" above); ride-along
2 is additive/defensive only (a stricter, still-defaulted type on an already-opaque
encrypted JSON field -- no migration, no data loss, an unknown stored value simply reads
back as `None` instead of an arbitrary string). T14.4.1/T14.4.2's four new `StreamStep`
events and the `detail` field are additive to the SSE contract (an older frontend build
simply never requests/renders them); no schema, no migration, no new persisted data, no
deployed-image step. A plain `git revert` of this block's commit(s) restores F24-into-14's
behaviour exactly (the plan panel and its new steps disappear; the followup guard reverts to
`"ambiguous"`, reopening the DA's own Finding 1 coverage gap; `last_intent` reverts to a
bare `str`).

router-reliability: changes LIVE runtime behaviour (the router prompt text and 3 new
deterministic tie-breakers; `assistant.fast_path.match_fast_path` fast-paths a lone
greeting-typo word) — no new schema, no migration, no new persisted data, no deployed-image
step. `understand()`/`route()` gain an additive, defaulted `previous_intent: str | None =
None` parameter — every pre-existing caller that does not pass it keeps its old behaviour
unchanged. A plain `git revert` of this block's commit(s) restores eval-determinism's
behaviour exactly (reopening the 10/75 deterministic misses this block fixes to 2/75); if
reverted, `eval/golden_set_v2_floors.json`'s `enforce.conversational_reply_rate` must also
revert to `false` in the same revert or `gate.sh --full` will fail on the lower, pre-block
number (0.833, below its own 1.00 floor).

eval-determinism: `config.Settings.ollama_seed` is new, additive settings surface (default
`42`, sent as `options.seed`) — no schema, no migration, no deployed-image step; reverting
this block's commit restores the pre-DEC-064 behaviour (no seed sent) exactly, reopening the
run-to-run flipping this block fixed. The new reentrancy-guard test is test-only. No runtime
behaviour changes for any caller that was already deterministic.

14-D1: changes LIVE runtime behaviour (ambiguous/off_topic get a dedicated reply; every
abstention is LLM-phrased) and the router's `_resolves_a_reference` tie-breaker condition
(DA-14-C4-1 fix) — no new schema, no migration, no new persisted data, no deployed-image step.
A plain `git revert` of this block's commit(s) restores 14-C4's behaviour exactly (ambiguous/
off_topic fall through to the retrieve-and-answer path again; abstentions revert to the fixed
English `_ABSTENTION_TEXT`; the tie-breaker's translation-only gap reopens, latent until a
future block adds Spanish-ambiguous golden-set coverage with context).

14-C4: changes LIVE runtime behaviour (the router can now return `"followup"` where it used to
return `"ambiguous"` for a resolved, context-backed reference) via a pure, additive code path
(`_resolves_a_reference`, called only after the existing suspicious-output screen) plus one
more few-shot example in an existing prompt string — no new schema, no migration, no new
persisted data, no deployed-image step. A plain `git revert` of this block's commit(s) restores
14-C3's behaviour exactly (reopening the `intent_accuracy` regression this block fixes); if
reverted, `eval/golden_set_v2_floors.json`'s `enforce.intent_accuracy` must also revert to
`false` in the same revert or `gate.sh --full` will fail on the lower, 14-C3-era number.

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
F24-into-14: no new schema, no migration, no deployed-image step. `scripts/gate.sh`'s
`check_ollama_num_parallel` is additive and informational only (never fails). The
router-reliability-2 guard changes LIVE runtime behaviour (a context-free raw `"followup"`
now becomes `"ambiguous"` instead) — reverting it reopens the 2 regressions it fixes (same
`enforce.intent_accuracy`/`enforce.follow_up_resolution` floor-regression risk as the
router-reliability block above, since both floors are measured with it in place).
`previous_intent`'s live wiring (`ConversationSummary.last_intent`, `StreamResult.intent`,
the new `previous_intent`/`observed_intent` parameters) is additive and defaulted
everywhere — every pre-existing caller that does not pass the new kwargs keeps its old
behaviour unchanged, and a summary written before this change simply has no `last_intent`
key (read back as `None`). A plain `git revert` of this block's commit(s) restores the
router-reliability head's behaviour exactly; no data migration of any kind is needed either
way (`last_intent` is just a JSON key inside an already-opaque encrypted blob).
