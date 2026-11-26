# Technical Requirements Document (TRD) — SecRAG

This document records the technical decisions so the app can be built without guessing.

## 1. Architecture overview

```
Next.js (React/TS)  ──REST/JSON──▶  FastAPI backend
                                        │
   ┌────────────────────────────────────┼───────────────────────────────┐
   │ Auth & abuse            RAG pipeline                 Data/infra       │
   │  - email/password        router → retrieve → rerank   - PostgreSQL    │
   │  - argon2, JWT           → generate → groundedness     + pgvector     │
   │  - email verification     → cite / abstain            - Ollama (LLM)  │
   │  - rate limit (bucket)                                                │
   └──────────────────────────────────────────────────────────────────────┘
```

## 2. Stack

| Concern        | Choice | Rationale |
|----------------|--------|-----------|
| Frontend       | Next.js 15, React 19, TypeScript, Tailwind | Expected by employers; real SPA/SSR app |
| Backend        | FastAPI (Python 3.11+) | Async, typed, great for ML/IO backends |
| Config         | pydantic-settings | Typed config from env; no hardcoding |
| Database       | PostgreSQL 16 + pgvector | One store for relational data **and** vectors |
| Migrations     | Alembic | Versioned schema |
| LLM serving    | Ollama (dev, default) / Azure OpenAI (cloud, opt-in via `LLM_PROVIDER`) | Free & local for dev; hosted LLM for the cloud path (no GPU on Container Apps) |
| Auth hashing   | argon2 (argon2-cffi) | Modern password hashing |
| Ingestion      | PDF → Markdown (`pymupdf4llm` / Docling) | Layout-aware, preserves tables |
| Retrieval      | pgvector (ANN) + BM25 (hybrid) | Vectors miss exact terms/IDs |
| Reranking      | `bge-reranker` cross-encoder (FlagEmbedding) | Precision boost over pure ANN |
| Evaluation     | Ragas + pytest | Retrieval + generation metrics, gated in CI |
| Observability  | Structured tracing (Langfuse/self-hosted or JSON logs) | Find the failing/slow stage |
| Containers     | Docker + docker-compose | Reproducible dev + deploy |
| CI/CD          | GitHub Actions → GHCR → Azure Container Apps (free-tier alt: Railway/Render/Fly) | Test → gate → build → push → deploy |

## 3. Models (exactly three)

| Task | Model | Notes |
|------|-------|-------|
| Router, generation, groundedness check, LLM-judge | one **`qwen` Q4** (e.g. `qwen2.5:7b-instruct-q4_K_M`) | Shared across all LLM tasks; Q4 fits 8 GB VRAM |
| Embeddings (chunks + query) | **`bge-m3`** | Multilingual, strong retrieval; fixed early (changing it forces re-embedding) |
| Reranking | **`bge-reranker`** (`bge-reranker-base`, int8-quantized for CPU — T11.5.3) | Cross-encoder; CPU only (torch is a CPU-only build in this repo) |

Configured via env: `LLM_MODEL`, `EMBED_MODEL`, `RERANKER_MODEL` (see `.env.example`).

**Reranker operational parameters (T11.5.3, the adopted config; full detail/measurements in
[ADR 11](adr/adr_phase11_stability.md), decision 8):** `top_k=10`, `RERANKER_MAX_LENGTH=256`
(the tokenizer's joint query+chunk token budget), `RERANKER_QUANTIZE=true` (dynamic int8),
`RERANK_CONCURRENCY=2` (a process-wide semaphore around every `CrossEncoder.predict()` call —
extra concurrent callers queue instead of all contending for the same CPU at once; an
accepted gap until KEDA-based autoscaling, Phase 16.3, adds replicas under load instead).
**Known side-effect:** `max_length=256` truncates the *chunk* (not the query) when a
query+chunk pair does not fit, under HF's default pair-truncation strategy — a 1200-char
chunk (`chunk_size`) is routinely ~230–300 tokens before the query is even added, so this is
the common case, not an edge case, for this config. Only the reranker's *scoring* input is
truncated (the LLM still sees the full chunk once selected), but a chunk whose only relevant
sentence sits near the end can be scored on a prefix that omits it and never reach the LLM —
a silent recall loss the current 14-item golden set cannot detect. Golden-set items that
specifically exercise this ("late sentence in a long chunk") are **deferred to Phase 14.0**
(golden-set expansion), not added now (user decision, 2026-10-05). **14-A1 adds one probe item**
(`trunc-sqli-error-messages` in `eval/golden_set_v2.jsonl`, §6.1) targeting a detail that
typically sits late in a long cheat-sheet section; full confirmation (does it actually miss
retrieval under the real reranker) happens when 14-A2 runs the eval against it, not in this
schema-only block.

## 4. Ingestion pipeline

```
raw PDF → parse (layout-aware) → Markdown (inspectable, versioned)
        → chunk on semantic boundaries (headings/sections) with overlap
        → embed (bge-m3) → store in pgvector with metadata
```

Every chunk carries metadata: `source`, `version`, `effective_date`, `heading`. The Markdown output is
kept under `data/markdown/` for human inspection of chunk quality **before** it reaches the model.

## 5. Retrieval & generation

1. **Router** decides the route: no-retrieval / single-hop / multi-hop (decompose) / reformulate+retry /
   abstain.
2. **Hybrid retrieval**: vector (pgvector) + keyword (BM25), filtered by ACL and (where relevant)
   version/recency.
3. **Rerank** the candidates with the cross-encoder; keep top N (`RERANK_TOP_N`).
4. **Grade** chunks; if the result is thin, reformulate and retry (bounded) or abstain.
5. **Generate** the answer grounded in the selected chunks, with citations.
6. **Groundedness check**: verify the answer is supported by the context; if not, retry or abstain.

Agentic behavior (steps 1 and 4) is added **only if** evaluation shows it beats the simple pipeline on
correctness without unacceptable cost/latency (see §7 benchmark).

## 6. Evaluation (the deploy gate)

Retrieval and generation are measured **separately**, plus correctness against an **independent**
ground truth (the key defense against *faithful-but-stale* answers).

- **Retrieval** (deterministic, every push): context recall/precision, MRR, hit rate.
- **Generation** (LLM-judge, PR/nightly): faithfulness, answer relevance, **correctness vs truth**.
- **Negative set**: correct-abstention rate on out-of-corpus questions.
- **Judge validation**: the LLM-judge is validated against a human-labeled subset (agreement/kappa);
  `temperature=0`; re-validated whenever the judge model changes.
- **Regression gate**: results compared to `baseline_metrics.json`; a deploy is **blocked** if any
  metric falls below its threshold (PRD §4).

Rerun triggers: change to prompts, LLM model, embedding model, chunking, retrieval pipeline, or corpus.

### 6.1 Golden set v2 (dialogue schema, Phase 14.0)

`eval/golden_set.jsonl` (v1, 14 flat English security questions) is still read unchanged by the
pipeline through `rag_app.eval.dataset.GoldenItem`/`load_golden_set`. **Golden set v2**
(`eval/golden_set_v2.jsonl`, `rag_app.eval.dataset.Turn`/`Dialogue`/`load_dialogues`) replays
whole **dialogues** instead of single questions, so the conversational assistant's router,
rewriting and memory (Phase 14.2–14.3) can be measured alongside retrieval and generation:

- A dialogue (`{"id", "turns": [...]}`) is a list of **turns**, each with `message`,
  `expected_intent` (`security | chitchat | followup | ambiguous | off_topic`), `language`
  (`en | es`) and, for turns the pipeline is expected to retrieve/answer, `expected_doc` /
  `answerable` / `ground_truth`.
- **A single-turn item is just a one-turn dialogue.** `load_dialogues()` is a strict superset of
  the v1 loader: it also accepts a bare v1 record (no `"turns"` key) and wraps it into a one-turn
  `Dialogue` on the fly (`expected_intent="security"`, `language="en"`, matching what every v1
  item actually is) — v1 files load through either loader without being rewritten.
- The 48-item `eval/golden_set_v2.jsonl` fixture (38 single-turn + 10 dialogues) covers
  misspellings (`"hol"`/`"hell"` as a lone greeting vs. `"hell"` inside a real sentence,
  `"injecion"`, `"preparde statements"`), Spanish questions, chit-chat (en/es), follow-ups
  resolved 1 turn back and 3+ turns back (en/es), ambiguous turns, off-topic turns, abstention in
  both languages, and one DEBT-006 probe (a late-chunk-detail question).
- **Known limitation (self-reported):** the fixture is a first draft (Claude drafts, user
  reviews — P14-D4) over a narrow single-topic corpus (OWASP injection + broken-access-control
  cheat sheets and Top-10 pages only, §3) — it cannot exercise every retrieval edge the full
  corpus eventually will, and its Spanish items are hand-written, not sourced from the corpus
  (the corpus itself is English-only).
- **Not yet wired into the pipeline:** this block (14-A1) only adds the schema + fixture; the
  runner's migration to replay `Dialogue` objects, plus metrics (intent accuracy, noisy recall,
  follow-up resolution, clarification rate, conversational-reply check), is T14.0.4 (block 14-A2).

**14-A2 (T14.0.4/T14.0.5) adds the runner, the metrics and the "before" baseline.**
`rag_app.eval.runner.evaluate_dialogues` replays every dialogue turn through TODAY's
placeholder core — `classify_intent` (chit-chat canned reply) or retrieve + `answer_from_chunks`
— standalone per turn: **no memory is carried between turns yet** (the live stream handler has
the same limitation, F14.2), so a follow-up like *"What about OS commands?"* is retrieved with no
idea what "it" refers to. This is deliberate: it is the measurement the floors below are compared
against BEFORE the router/memory land (T14.2.1, block 14-B2), not a simulation of them.
`rag_app.eval.metrics.compute_dialogue_metrics` aggregates: `intent_accuracy`, `noisy_recall`
(turns from a `typo-*` dialogue id), `follow_up_resolution`, `clarification_rate` (a heuristic —
no clarification code path exists before 14.3, so this is 0 by construction, not hard-coded),
`conversational_reply_rate`, `abstention_no_claims_rate` (`rag_app.eval.judge.judge_no_claims`,
the validated-judge mechanism), `greeting_reply_p95_s` (measurable today) and `router_p95_s`
(always `None` — there is no router call to time yet).

**Floors (the Phase 14.0 plan).** §14.2's own Gate line reads "floors from 14.0", i.e.
enforcement starts once the router/memory/retrieval they measure exist. `eval
/golden_set_v2_floors.json` recorded every floor without enforcing any of them (14-A2/14-B2/
14-C1, `enforce_now: false`) while they were genuinely unmet; **block 14-C2 turns enforcement
on PER METRIC** (`enforce.<metric>`, §6.4) once a given floor is actually cleared by the
measured numbers — a floor this block's own numbers do not yet meet stays recorded, not
silently lowered. See the golden-set-v2 table in `docs/adr/adr_phase14_assistant.md` for the
measured numbers and which floors are enforced as of this block.

### 6.2 Understanding step: fast path + LLM router (T14.2.1–T14.2.3, block 14-B2)

The 14-A2 placeholder above (`classify_intent`, a binary chit-chat/security regex) is now
replaced on the LIVE pipeline (`generation.answer_question`/`answer_question_stream`) and the
eval runner alike by ONE shared understanding step, `rag_app.assistant.core.understand`:

1. **Regex fast path** (`rag_app.assistant.fast_path.match_fast_path`, T14.2.2): an EXACT
   greeting/thanks/farewell (en/es — e.g. `"hi"`, `"hola"`, `"thanks"`) skips the LLM router
   only; the reply is itself LLM-generated (`generate_chitchat_reply`, ≤ 80 tokens, P14-D1)
   with a bilingual template fallback on any failure (timeout, HTTP error, empty reply) —
   never a hard failure of the chat turn. A lone typo ("hol", "hell") is deliberately NOT an
   exact match, so it reaches the router instead (the plan's own phrasing for this rule).
2. **LLM router** (`rag_app.assistant.router.route`, T14.2.3): `chat_json` against
   `RouterOutput {intent: chitchat|security|followup|ambiguous|off_topic, language, standalone_query,
   clarification?}`. The user's message (and, from 14-C1, the conversation summary/last turn)
   is passed as DELIMITED (`<<<MESSAGE>>> ... <<<END MESSAGE>>>`) data in the USER turn, never
   concatenated into the system prompt — the same "untrusted data, never instructions"
   discipline `generation.py` already applies to retrieved corpus chunks. ANY failure — a
   timeout/network/HTTP error (not caught by `chat_json`'s own retry, which only covers
   invalid JSON/schema violations, T14.1.2), or a structurally VALID but suspicious reply
   (`standalone_query`/`clarification` echoing a `<<<`/`>>>` delimiter or a `system:`/
   `assistant:` role prefix — a sign the model partially obeyed injected instructions) —
   degrades to the SAME conservative fallback: intent `"security"` carrying the user's RAW,
   unmodified message. A misrouted or adversarial message can therefore never do worse than
   re-enter the existing, already-reviewed retrieve → generate → groundedness-check →
   cite-or-abstain path; it can never bypass citation/groundedness.
3. Non-chitchat intents (`followup`/`ambiguous`/`off_topic`, besides `security`) fall through
   to the SAME retrieve-and-answer path as `security` today — their own dedicated replies
   (a clarifying question, a polite redirect) are T14.3/14-D1, not built yet.

**Self-reported scope boundaries (not asked for beyond what is listed, DA-13-J2-1 discipline):**

- Memory (`summary`/`last_turn`) is accepted as an optional parameter everywhere in THIS
  block (14-B2) but is always `None` from every 14-B2 call site — the router sees only the
  current message, same F14.2 limitation as before, just with a real router call now. **Wired
  for real in block 14-C1 — see §6.3.**
- Retrieval used the ORIGINAL message, never `standalone_query`, until block 14-C2 wired it
  in — see §6.4.
- The retrieve/generate/groundedness-check code itself was NOT relocated into the `assistant`
  package: it stays in `generation.py`'s own functions (and the eval runner's own direct
  calls), because several existing tests monkeypatch module-qualified names
  (`generation.hybrid_search`/`generation.embed_query`) that only work when the calling code
  is textually inside `generation.py`. Collapsing stream/non-stream/eval into one literal
  generator end-to-end remains a larger refactor than T14.2.1-3's rows ask for.
- No new Prometheus step metric was added for the understanding step itself (it would need a
  `metrics.STEP_ALLOWLIST` entry, outside this block's named touches) — only the pre-existing
  coarse `secrag_chat_stage_seconds{stage="classify"|"responding"}` total grows to include it;
  per-step latency instrumentation is deferred to 14.5 (Latency, cost, dashboard).
- **(14-B2-fix)** The daily-answer-cap exemption (`api.deps.reserve_daily_answer`,
  `api.conversations._reserve_stream_answer`, both endpoints now) is keyed ONLY off
  `assistant.fast_path.match_fast_path` (exact, anchored) — NOT `classify_intent`, which had
  no remaining live call site after this fix. `classify_intent`'s unanchored `_META_RE`
  branch (no length cap) could be smuggled inside a long compound message to get a free
  answer from the full retrieve → generate → groundedness path (DA-14-B2 finding 2); the
  exact fast path cannot be smuggled the same way. Net effect: "chit-chat is (usually)
  cheap, and a typo'd/router-detected chit-chat turn correctly counts against the cap" —
  "chit-chat is free" stopped being literally true at T14.2.2 and this fix makes the
  accounting match that everywhere, not just on the stream path.
- **(14-B2-fix)** `assistant.fast_path.escape_delimiter_markers` neutralises a literal
  `<<<`/`>>>` marker run already present inside untrusted text (message/summary/last turn)
  before it is wrapped in a real delimited block (`_delimited_message`/
  `build_router_messages`) — otherwise a message carrying a fake `<<<END MESSAGE>>>` could
  forge a closing tag (DA-14-B2 finding 4). Input-side mitigation, layered onto the existing
  output-side screen (`_looks_suspicious`), not a replacement for it.

### 6.3 Conversation memory: read + write (T14.2.5/T14.2.6, block 14-C1)

**Scope (DEC-059, user 2026-11-25 = A).** T14.2.1 literally asks for one shared core that
also unifies retrieve/generate; 14-B2 shared only the UNDERSTAND step (§6.2's own
self-reported boundary) for concrete, documented reasons (monkeypatch targets). Rather than
block on that larger refactor, memory is wired directly into the TWO real call sites that
have a conversation at all:

1. **`/chat/stream`** (`generation.answer_question_stream`) — `api.conversations._load_memory`
   reads the conversation's own `Conversation.summary_encrypted` (decrypted with the user's
   per-user key — `rag_app.crypto`, the SAME Fernet scheme `title_encrypted` already uses,
   no new primitive) and its last clean user+assistant turn, **before** the current turn's
   own user message is persisted (`_start_turn` fetches `prior_messages` first). Both are
   forwarded, unchanged, into `understand()` → `router.build_router_messages`, which already
   wraps them as delimited, escaped untrusted data (§6.2, DA-14-B2 finding 4) exactly like the
   user's own message — no new escaping mechanism.
2. **The eval runner** (`eval.runner.evaluate_dialogues`) — the third entry point DEC-059
   names: `summary`/`last_turn` are carried **in memory** across one replayed dialogue's own
   turns (reset per dialogue; no DB, no encryption — a golden-set dialogue is not a real
   account).

`generation.answer_question` (`POST /chat`) stays memory-less by design — it genuinely has no
conversation (TF24) — nothing to read or write; this is a documented boundary, not a gap.

**Write** (`assistant.memory.summarize_turn`, T14.2.6): one bounded `chat_json` call against
`ConversationSummary {topics, entities, owasp_version, open_questions}` (≤
`MEMORY_SUMMARY_MAX_TOKENS` ≈ 300 tokens, `num_ctx=settings.num_ctx_answer` — same reused
pattern as the router/chit-chat calls, §6.2's own "no new num_ctx setting" decision), merging
the PREVIOUS summary + the last turn + the turn just answered. Runs **after** the final
answer is already persisted (`_persist_message`), **before** the stream's own `done`/`error`
event closes the turn (T14.2.6's own ordering requirement) — so a memory-write failure can
never turn an already-successful answer into a failed stream.

**Skipped on an exact fast-path turn (14-C1-fix, DA-14-C1 finding 1).** Both call sites
(`api.conversations._chat_events`/`eval.runner.evaluate_dialogues`) skip this call entirely
when the turn took the exact, anchored greeting/thanks/farewell fast path — the same
`match_fast_path` decision §6.2 already uses to exempt the turn from the daily answer cap.
A bare greeting has nothing worth merging into the running summary, so a cap-exempt turn
now costs exactly the one bounded LLM call its reply always did, never a second, uncounted
one. Every other answered turn, including one the LLM router itself classifies as
chit-chat, still writes memory.

**Fail-safe/drop discipline (mirrors the router exactly).** `summarize_turn` returns `None`
("dropped — leave the old summary exactly as it was") on EITHER of two failure classes, never
raises:
- a timeout/network/HTTP error reaching the LLM (not just an invalid reply) — caught the same
  way `router.route` catches a chat-client exception;
- `chat_json`'s own bounded retry (T14.1.2) exhausting itself on an invalid/schema-violating
  reply — detected by **identity**: `summarize_turn` passes its own fresh, empty
  `ConversationSummary()` as the `fallback`, and `_chat_json_with_retry` (`rag_app.llm`)
  returns that exact object, never a new one, on failure — so `result is fallback` reliably
  means "this was invalid", never confused with a genuinely empty but VALID reply (a
  different object, same content — `test_summarize_turn_accepts_a_genuinely_empty_but_valid_
  reply`). No prompt/response content is ever logged, only the call type and error class
  (same discipline as every other LLM call in this app).

**Isolation (authorization, T14.2.5).** `_load_memory` takes the already-ownership-checked
`Conversation`/its own `prior_messages` as plain arguments — there is no user-id lookup
inside it at all, so there is no code path by which it could read a different conversation's
rows; `test_load_memory_never_crosses_conversations` and the DB-backed
`test_stream_reads_this_conversations_own_memory_and_never_another_one`
(`tests/test_memory_stream.py`) exercise two conversations of the SAME user and confirm
neither's memory ever appears in the other's router call.

**Purge (migration 0009, X3).** `conversations.summary_encrypted` needs no new
`PURGE_STEPS` entry: it is plain content on the already-covered `conversations` table (no
`user_id`/`*_hmac`-shaped name, no FK to users/conversations/messages), and the existing
"conversations" purge step already `DELETE`s the whole row — the summary disappears with it,
same as `title_encrypted` already does. No new grant (table-level grants from 0005 already
cover the column). Downgrade refuses while any conversation holds a non-null summary (same
"never a rollback path once such data exists" rule as 0008).

### 6.4 Retrieval with the standalone query (T14.2.7, block 14-C2)

**What changed.** The three entry points that call `understand()` — `generation
.answer_question` (`POST /chat`), `generation.answer_question_stream` (`/chat/stream`) and
`eval.runner.evaluate_dialogues` — now search with `rag_app.assistant.core.retrieval_query
(decision, query)` instead of always the original message: the router's own English
`standalone_query` when it is a genuine, non-empty rewrite, else the original `query`
unchanged. This is the fix for the gap §6.2/§6.3 both self-reported ("retrieval still uses
the original message, never `standalone_query`") — a follow-up like "Where does it rank?"
means nothing to hybrid search on its own; the router already resolves it into a
self-contained English question (e.g. "How does SQL injection rank in the OWASP Top 10
2021?"), and now retrieval actually searches with that resolved query.

**Generation is unchanged — still the original message.** `answer_from_chunks`/
`_stream_answer_tokens` still receive the ORIGINAL `query`, never the rewrite: the reply must
address what the user actually typed, in the language they typed it in (D14.1), which the
English `standalone_query` would silently break if it reached generation too (an explicit
reply-language decision belongs to T14.3.1/14-D1, not this block). Reranking is retrieval,
not generation, so it also searches with the resolved query (`CrossEncoderReranker.rerank
(search_query, chunks, top_n)`) — the same single-query convention `reranking.retrieve()`
already used internally before this block existed.

**Why this is a no-op on the security-fallback path, by construction, not by a special
case.** `router._security_fallback` (any router failure: timeout, network error, invalid
reply after retry, suspicious output) already sets `standalone_query = query` verbatim — the
exact fast path does the same. `retrieval_query` therefore returns the untouched original
message on every path that is not a genuine router rewrite; there is no second, separate
"fallback" branch inside `retrieval_query` itself beyond a defensive guard against an empty
string (no live caller produces one today).

**Expected effect on the golden-set v2 floors.** `follow_up_resolution` (§6.1) is a pure
function of what `retrieve()` searched for — this is the ONE metric this block's own change
can move; `intent_accuracy`/`conversational_reply_rate` depend on the ROUTER's classification
and the CHIT-CHAT reply respectively, neither of which this block touches, so they are not
expected to move. See the ADR's "14-C2 decision" section for the measured before/after table
and which floors are enforced from this block onward.

### 6.4a Chunk dominance, named-edition filter, router few-shots (T14.2.9, block 14-C3)

**What changed.** Three retrieval-time fixes, layered under `retrieval_query` (§6.4) rather
than replacing it: (1) `retrieval._bm25_uids` now matches ANY of a query's terms (OR), not ALL
of them (the previous `plainto_tsquery` AND semantics silently emptied BM25 for longer
questions); (2) `retrieval.hybrid_search` fetches a wider raw candidate pool per list and caps
how many of the final fused candidates one document may occupy
(`diversify_candidates`) — stops a generic, multi-topic document (a slide-deck PDF, a full
PDF's own front matter) from crowding out the one relevant document's chunk; (3)
`reranking.CrossEncoderReranker.rerank` down-weights a chunk whose heading is a bare
reference/citation list or presentation/front-matter breadcrumb (`is_low_value_heading`) AFTER
the real cross-encoder score, so it stops out-ranking a lower-scored chunk that actually states
the answer. A fourth change, `assistant.core.named_owasp_version`, scopes retrieval to the
OWASP edition the RESOLVED query names (never the conversation summary directly, never when
nothing names one) — wired into the same three `retrieval_query` call sites as §6.4, an
explicit caller-supplied `version` always winning over the auto-detected one.

**Why layered, not merged into `retrieval_query`.** `retrieval_query` answers "what text does
retrieval search with"; `named_owasp_version` answers the orthogonal "which corpus version, if
any, should that search be scoped to" — kept as two separate, independently testable pure
functions rather than one that does both.

**Measured effect.** `follow_up_resolution` 0.500 → 0.900 (two real `--only eval` runs on the
final code, `n` raised from 6 to 10 follow-up turns) — now enforced. `intent_accuracy` stays
below its floor (a narrower router ambiguous-vs-followup boundary confusion on terse follow-ups
that does not affect `follow_up_resolution`). See the ADR's "14-C3 decision" section for the
full live-diagnosis evidence, the before/after ranking tables, and the "Known limitations"
section.

### 6.4b Ambiguous-vs-followup tie-breaker (T14.2.9-fix, block 14-C4, DEC-063)

**What changed.** §6.4a's own `intent_accuracy` gap is fixed in `assistant.router.route`, after
the existing suspicious-output screen: when the model returns `intent="ambiguous"` but (a)
conversation context (`summary`/`last_turn`) existed to consult AND (b) its own
`standalone_query` names a substantive word absent from the raw message (`router
._resolves_a_reference` — a deliberately crude, language-agnostic ≥3-letter-token diff, not
semantic verification), the verdict is corrected to `"followup"` (`clarification` reset to
`None`). This is a deterministic, code-level enforcement of the router's own stated rule ("if
you CAN write a resolved standalone_query … the intent is followup, never ambiguous") rather
than relying on prose alone — §6.4a's own two prompt-only attempts did not reliably close the
gap. Paired with one more contrasting few-shot (a genuinely-ambiguous, no-context example) in
`_ROUTER_SYSTEM`. Every golden-set `ambiguous` item is a first turn with no prior context, so
condition (a) alone already guards every known genuinely-ambiguous case from being overridden.

**Measured effect.** `intent_accuracy` 0.840 → 0.907 (two real `--only eval` runs, identical
both times) — now enforced; `follow_up_resolution` unchanged at 0.900 (this fix only touches
the intent LABEL, never `standalone_query`'s resolution quality, which `follow_up_resolution`
actually measures). See the ADR's "14-C4 decision" section for the live-diagnosis evidence,
the full measured-effect table and the "Known limitations" section.

### 6.4c Tie-breaker context-grounding fix (DA-14-C4-1, block 14-D1)

**What changed.** §6.4b's own `_resolves_a_reference` condition (b) — "`standalone_query`
names a substantive word absent from the raw `query`" — is corrected to ALSO require that
word to appear in the real `context` (`summary`/`last_turn`, concatenated), which the
function now takes as a third argument. §6.4b's ADR claimed a plain textual difference could
not be satisfied by translation alone ("a Spanish message translated to English always
'differs' ... translation alone never satisfies it on its own") — DA-14-C4.md's review proved
this wrong, empirically: `_resolves_a_reference("¿Cómo lo arreglo?", "How do I fix this?")`
returned `True` under the original (pre-14-D1) code, because a translation shares almost no
tokens with the original message in ANY language pair, so "absent from query" is satisfied by
translation alone every time, with zero real grounding in context.

**Why this matters.** Not a live bypass today (§6.4b's own `route()` call never reaches a
dedicated reply for `"ambiguous"` vs `"followup"` before this block — both fell through to the
same retrieve-and-answer path) but it would have become a real, systematic Spanish-language UX
regression the moment §6.5 below shipped a dedicated clarifying-question reply: a genuinely
ambiguous Spanish message with ANY prior context, however unrelated, would always have been
wrongly flipped to `"followup"` and silently answered instead of asked about.

**Why it still works for a genuine resolution.** The added word in a REAL resolution comes
FROM the context by construction (that is the whole point of pulling a topic out of the
conversation's own summary/last turn to resolve a terse reference) — requiring it to also
appear there costs nothing in the cases condition (b) was ever meant to catch, and closes
exactly the translation-only gap. Verified both ways, in Spanish, at the `route()` level:
`test_route_overrides_ambiguous_to_followup_for_a_spanish_followup_grounded_in_context` (a
real follow-up still flips) and
`test_route_leaves_a_genuinely_ambiguous_spanish_message_with_unrelated_context_alone` (a
translation-only "resolution" with unrelated context no longer does).

### 6.4d Router reliability: 5 deterministic fixes for the 10 misses (DEC-066, block
router-reliability), closing 14-D1 + eval-determinism

**What changed.** Block eval-determinism (ADR-only — fixed a non-determinism bug in the eval
harness itself, `config.Settings.ollama_seed`, no router/prompt change; see the ADR's own
"eval-determinism" section) proved the router's remaining 10/75 deterministic misses are
real, not noise. Five fixes close them:
1. `assistant.fast_path.match_fast_path` also fast-paths a LONE word within one edit of a
   greeting root ("hello"/"hola") — never inside a sentence. No router/model call at all for
   these messages, so this fix is 100% deterministic by construction.
2. `router._ROUTER_SYSTEM`'s `security` bullet now names security infrastructure (firewalls,
   proxies, TLS, WAF) as in-scope even when our corpus does not cover the specific vendor
   product (the ANSWER step's abstention handles that, not routing); `off_topic` is reserved
   for subjects not about security at all.
3. `router._names_a_security_term`: a deterministic tie-breaker overriding `ambiguous` to
   `security` when the message itself already names a security term (en/es), independent of
   context.
4. `router._previous_turn_asked_the_clarification` + a new `previous_intent` parameter on
   `understand()`/`route()`: the reply to a clarifying question is a NEW security question,
   never a followup to the old topic. Caller-tracked (never inferred from reply text — an
   `off_topic` redirect can also end in "?").
5. `router._is_bare_reference_query` + `_memory_subject`: a bare pronoun/reference question
   ("what rank does it have...") with a real subject in the conversation summary's own
   `topics`/`entities` is always a followup, resolved from memory directly rather than
   trusting the model's own (sometimes unresolved) rewrite.

**Order preserved.** All three new tie-breakers (3/4/5) run strictly after the existing
delimited-untrusted-data → schema validation → security fallback → suspicious-output-screen
sequence (§6.2/§6.3) — none adds a new attacker-reachable path; each only relabels an output
the already-screened router produced. None can ever produce `chitchat` (the only
cap-exempt/fast-path intent) — only `security`/`followup`, both still running the full
retrieve → generate → groundedness-check → cite-or-abstain path.

**Measured effect.** 10/75 → 2/75 deterministic misses, on 2 byte-identical `--only eval` runs
(`intent_accuracy` 0.867 → 0.973, `follow_up_resolution` 0.700 → 1.0, `conversational_reply_rate`
0.833 → 1.0, now also enforced). See the ADR's "router-reliability" decision section for the
full table, the 2 new misses change 2's prompt wording introduced (a self-reported, accepted
trade-off — fixing them cleanly re-opened a different deterministic miss instead, not a net
improvement), and the greeting-typo fast path's own known false-positive risk.

### 6.5 Conversational answers (T14.3.1–T14.3.5, block 14-D1)

**What changed.** The THREE entry points that already call `understand()`/dispatch on
`decision.intent` — `generation.answer_question`, `generation.answer_question_stream`,
`eval.runner.evaluate_dialogues` — gain two more dedicated branches (`chitchat` already had
one since 14-B2), and every abstention changes:

- **`ambiguous`** (T14.3.3) now replies with the router's OWN clarifying question
  (`assistant.replies.clarifying_reply`) instead of falling through to retrieval — asks,
  rather than guessing what the user meant. No extra LLM call: the router
  already writes a short clarifying question, in the user's own language, whenever it decides
  `"ambiguous"` (`router._ROUTER_SYSTEM`'s rule, §6.2). A bilingual fallback question
  (`CLARIFYING_FALLBACK_EN`/`_ES`) covers the one case the router can still contradict its own
  rule on (an `"ambiguous"` verdict with `clarification=None` — §6.4b/§6.4c narrowed but did
  not eliminate this).
- **`off_topic`** (T14.3.3) now gets a polite, LLM-phrased redirect
  (`assistant.replies.generate_offtopic_reply`) — never an uncited answer. Same
  delimited-untrusted-data/bounded-tokens/template-fallback discipline as
  `fast_path.generate_chitchat_reply` (§6.2): the message is wrapped in `<<<MESSAGE>>>...`
  markers in the user turn, never the system prompt; ANY call failure degrades to a bilingual
  template.
- **Every abstention is now the conversational abstention** (T14.3.4,
  `assistant.replies.generate_abstention_reply`), replacing the literal, fixed English
  `generation._ABSTENTION_TEXT` as the PRIMARY path — at all three abstention points
  (`answer_from_chunks`'s "no chunks" and "ungrounded" cases, and the equivalent inline
  "no chunks" case in `answer_question_stream`). LLM-phrased, in the user's detected
  language; may rephrase or apologise, and may point at `related_topics` — the HEADINGS of
  chunks retrieval actually found but did not ground an answer in (`chunks[:3]`), passed
  through, never invented by the reply generator itself; the system prompt explicitly
  forbids stating any fact, number or recommendation that would answer the original
  question. `answer_from_chunks` gains an optional `language: str | None = None` parameter
  (default preserves the OLD, fixed English text for every caller that never detected a
  language — the CLI's `main()`, `eval.benchmark`'s v1 golden-set run, `agentic.py` — out of
  this block's scope, must not regress); the three conversational entry points now pass
  `language=decision.language`.
- **Reply language (T14.3.1, D14.1).** Every function above replies in
  `Understanding.language` (the router's own detected language, or the fast path's exact
  match) — never a fixed English default — closing the gap self-reported since 14-B2/14-C2
  ("an explicit reply-language decision belongs to T14.3.1/14-D1", §6.4).

**Security/groundedness unchanged.** `security`/`followup` (and, as a conservative fallback,
a router error carrying the raw query as `"security"`) are the ONLY intents that still
retrieve, generate and run the groundedness check — citations and the groundedness check
themselves are byte-identical to before this block; `ambiguous`/`off_topic` never retrieve at
all, so they can never produce an uncited claim.

**Judge "no claims" on the conversational abstention (T14.3.5).** `eval.judge.judge_no_claims`
(validated since 14-A2) is unchanged; it is applied to whatever text ends up in
`DialogueTurnResult.reply_text` when `answer.abstained`, so it now judges the LLM-phrased
conversational abstention instead of the old fixed sentence — a stronger, more realistic test
of "no claims" than judging a hand-written string that can never accidentally drift into
making one.

**Expected effect on the golden-set v2 floors.** `conversational_reply_rate` (chit-chat
classified as chit-chat) is unaffected in definition — this block does not touch `chitchat`'s
own branch or the metric's own computation; see the ADR's "14-D1 decision" section for
whether it is enforced from this block based on the measured runs. `clarification_rate`
(ambiguous turns whose reply looks like a clarifying question,
`metrics.looks_like_clarifying_question`) is expected to move toward 1.0 for the first time
— no code path producing a real clarifying reply existed before this block.

**`noisy_recall` side effect, found and fixed in the resume (§6.2's `_ROUTER_SYSTEM`).**
Making `ambiguous` never retrieve has a real cost: any noisy (misspelled) query the router
mislabels `"ambiguous"` now scores a genuine `noisy_recall` miss, where before this block the
retrieve-anyway `else` branch happened to still hit the expected doc despite the wrong label.
Golden-set item `typo-injecion-what` ("What is injecion in security?", no prior context) is
exactly this case — confirmed PRE-EXISTING at the `bf548f0` baseline too (same misclassification,
replayed in a throwaway worktree), not something this block's dispatch code introduced; the
dispatch change only made it visible in `noisy_recall` (1.0 → 0.75, deterministic across 2
live eval runs). Fixed with one contrastive few-shot added to `router._ROUTER_SYSTEM`: a
misspelling alone is not an unresolved reference. A code-level tie-breaker keyed on
`clarification is None` (the same self-contradiction §6.4b/§6.4c already narrowed for the
followup case) was considered and rejected: live-verified, 2 of the 5 genuine golden-set
`ambiguous` items ("Can you help me?", "¿Cómo lo arreglo?") ALSO produce `clarification=None`
— that signal does not distinguish a genuine ambiguity from this one. **Verified solid across
7 `--only eval`/`--full` runs: `noisy_recall` measured 1.000 every single time** — see the
ADR's "Eval table" (7 runs) for the full picture, including 2 reworded-few-shot attempts that
were tried to ALSO fix an unrelated, pre-existing greeting-typo noise item and were reverted
after they caused new regressions on other items (a literal-word-naming few-shot is not safe
to generalize without re-testing the whole golden set). `intent_accuracy`/
`follow_up_resolution` turned out noisier at the margin than first assumed from 2 runs alone —
documented as pre-existing, not caused by this fix, in the ADR's "Known limitations".

## 7. Cost & latency

- Q4 quantization; a single shared LLM; reranker on CPU.
- **Benchmark harness** sweeps configs (chunk size, k, rerank on/off, router on/off) and reports
  faithfulness / correctness / p95 latency / cost per query — so complexity is added by **evidence**.
- Semantic caching (invalidated on corpus change); parallelize independent tool calls.
- **`num_ctx`**: `NUM_CTX_ANSWER`/`NUM_CTX_GROUNDEDNESS` (default 8192) bound the Ollama context
  window per call — large enough for the retrieved chunks + prompt, small enough to bound
  per-call latency/memory; changing either is a latency-relevant config change (re-run the
  `latency` gate step, T11.6b.1).
- **Baked model images** (T11.4.2 backend reranker, T11.4.3 Ollama `bge-m3`): the reranker/
  embedding model weights are downloaded at **image build time**, not on first request/cold
  start, removing the old "wait for a multi-GB download" cold-start risk entirely (measured
  cold-start numbers: ADR 11 decision 8).
- **Cold start, block H (T11.6b.6-6b, 11b):** the model being baked in does not remove the
  cold-start **image pull** itself — a fresh Container Apps replica still has to pull the
  (large) Ollama image from GHCR before it can serve. The 2026-10-06 smoke measured this at
  130s for the pre-block-H fat image (6.3 GB on GHCR) vs. a hard-coded 120s embed-wait
  timeout in the backend, so the backend answered HTTP 500 before Ollama had even finished
  waking up. Two independent fixes: (1) `EMBED_TIMEOUT_SECONDS` (default **170s**, was a
  module constant) is the SAME setting for `/chat` and `/chat/stream` — backend cold ~60s +
  embed wait ≤170s + answer ~5s ≈ 235s, under Azure ingress's ~240s cutoff (waiting longer
  is useless: the ingress cuts the request first) — `validate_api_settings` fail-closed
  rejects any `EMBED_TIMEOUT_SECONDS` above 230s for the same reason (DA-11bH-4: past that
  bound the ingress always wins the race, turning a clear timeout error into a confusing
  generic ingress one); (2) the Ollama image's **final stage is
  now slim** — `ubuntu:24.04` pinned by digest (the exact base `ollama/ollama` itself ships,
  so no ABI shim) with only the CPU `libggml-cpu-*.so` backends + the `bge-m3` model store
  copied in, dropping the GPU-only backends (`cuda_v12`, `cuda_v13`, `mlx_cuda_v13`,
  `vulkan`) this CPU-only Azure instance never loads — measured locally at 1.12 GB vs. the
  fat image's 4.83 GB (`docker image inspect .Size`), so the GHCR pull itself is much
  faster. Embeddings are bit-identical (cosine 1.0 on fixed texts) and the baked `bge-m3`
  digest is unchanged (`7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`).
  Demo mode (README "Demo mode") sidesteps the cold start entirely for a bounded window.
  Full numbers: ADR 11 decision 9.
- **Serial cold-start warm-up (T13.13.1–T13.13.3, DEBT-061, 13-J4; narrowed by DEC-049/-050,
  T13.J4-fix):** even with the fixes above, the real path stayed serial — backend container
  start (torch import + reranker load/warm-up) → classify → retrieving → only THEN
  `retrieval.embed_query()` calls Ollama (a SEPARATE Container App, also scaled to zero),
  stacking two cold starts one after another instead of overlapping them. Three changes
  (`rag_app.warmup`):
  1. **Lifespan warm-up**: the API lifespan fires a background, non-blocking warm-up embed
     to Ollama right after the (synchronous) reranker warm-up — it never delays
     readiness/`/health`, and a failure is only logged (`secrag_ollama_warmup_total{result=
     "failed"}`), never raised. A module-wide single-flight guard (`threading.Lock` +
     `_in_progress`) means at most one warm-up embed call is ever in flight — a second
     trigger while one is running is skipped (`result="skipped"`), not queued.
  2. **The warm-up itself, server-side only (DEC-050):** a SUCCESSFUL `POST /auth/login` and
     an authenticated `GET /conversations` (the call `/chat` already makes on every page
     load with a valid session) both re-trigger the SAME single-flight guard. `GET /wake`
     USED TO be a third trigger (T13.13.2) — `/login` and `/chat` each sent one light,
     unauthenticated ping (through the BFF's own `/api/wake`) that re-ran the warm-up on
     every call; re-running it against the generic shared rate limiter (60 capacity/1 req/s
     refill) let a single actor sustain ~1 req/s and keep Ollama awake indefinitely, at a
     real Azure cost, defeating scale-to-zero. `/wake` still exists (same two pages still
     ping it on page load) and still answers a plain 204 either way (no leak), but now only
     to nudge the BACKEND container awake — never Ollama — through its OWN much tighter
     bucket (`rag_app.api.deps.rate_limit_wake_narrow`, default 1 request/60s per key, a
     separate `RateLimiter` instance from the generic one).
  3. **`embed_keep_alive`** (DEC-049: default `"5m"` — Ollama's own server default —
     everywhere including the dev PC, sent on EVERY `/api/embed` call via `OllamaEmbedder`,
     not only the warm-up one): the ORIGINAL design (default `"3h"`, always, everywhere)
     pinned `bge-m3` resident on the user's own dev PC for up to 3h after any local
     dev/test run that touched the embedder, which could tie up VRAM/RAM before a gaming
     session. `demo-mode.sh on` now sets `EMBED_KEEP_ALIVE=3h` as a Container App env var on
     the BACKEND only, in the SAME `az containerapp update` call it already makes to scale
     it up (no extra revision); `off` removes it, restoring the default; a rolled-back `on`
     removes it again too. Outside a demo the Container App itself scales back to 0 on its
     own idle cooldown well before 3h elapses either way, so this remains cost-neutral there
     (Azure bills replica-seconds, not how long Ollama keeps one small model resident in an
     already-running container). `demo-mode.sh on` additionally fires the wake ping once,
     right after its existing `/api/health` check, best-effort (never fails or rolls back
     `on`). Local before/after first-token measurement + method: ADR 13-J4/13-J4-fix.
- **The `latency` gate step** (T11.6b.1) is a real pass/fail check: a routine run's p50/p95
  per stage is compared against a committed, 2-thread-pinned *adopted* baseline
  (`eval/latency_adopted_baseline.json`, distinct from the frozen "before" baseline) with a
  documented tolerance, plus a hard absolute `rerank_inference` floor (≤ 1.5 s) that is never
  loosened by the baseline. Full rule and rationale: ADR 11 decision 8, "Landed in 11.6b".

## 8. Security & abuse

- **Auth**: email/password, argon2, email verification, JWT/session, login rate limiting.
- **Rate limiting**: token bucket per IP/user; **cost-aware** variant (a query consumes tokens ∝ its
  cost) to defend against **Denial of Wallet**.
- **Global daily answer cap (R6-1)**: `DAILY_ANSWER_CAP` answers per UTC day for all users
  together (default 150 = the Azure value; `0` = off, allowed only with `ENV=dev`; `ENV=prod` refuses to start
  without a positive cap). The counter is `usage_daily` (`day`, `answers`, `tokens` — no
  personal data). `/chat` and `/chat/stream` reserve the answer at request start, before any
  LLM call, with one `INSERT … ON CONFLICT (day) DO UPDATE … WHERE answers < cap RETURNING`
  (race-safe: concurrent requests at `cap − 1` get exactly one answer). Failed or
  interrupted turns stay counted, but a turn that never reaches the pipeline does not (a data
  key that cannot be unwrapped is refused before the reservation; a turn whose message cannot
  be stored gives it back); tokens are added when the pipeline reports them. At the
  cap: `/chat` → `429` with `Retry-After` (seconds to UTC midnight); `/chat/stream` → the
  `conversation` event and an `error` event (`daily_cap_reached`) with the id, nothing
  stored. The chit-chat canned reply (no LLM call) is not counted. Each refusal logs
  `daily answer cap reached: day=… cap=…` (no personal data) for the Phase 13 banner.
  Every LLM call is bounded (`max_tokens`: generation 1024, groundedness check 16), so one
  answer costs at most ~$0.0076 (ADR 11 "Costs").
- **Sybil/DoS defense**: risk scoring on signup (account age, IP velocity/reputation, disposable-email
  detection), CAPTCHA/proof-of-work, per-user quotas.
- **Access control**: ACL filter applied **in the vector query**, so unauthorized chunks are never
  retrieved.
- **Indirect prompt injection**: retrieved document text is treated as **untrusted data, never
  instructions**.
- **Agent limits**: bounded steps, retries, and tokens to prevent runaway loops.

### 8.1 Session, BFF and CSRF (Phase 12a)

See [`docs/security/THREAT_MODEL.md`](security/THREAT_MODEL.md) for the full STRIDE/
catalogue analysis; this is the implementation summary.

- **Server-side sessions** (migration 0006, `rag_app.sessions`): a 256-bit random session
  ID, only its SHA-256 hash stored. Idle (30 min) and absolute (12 h; admin 15 min / 8 h,
  P12-D3) expiry; `last_seen_at` is throttled (≥ 60 s between writes). The cookie
  (`__Host-secrag_session`; `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`) carries **no
  `Max-Age`/`Expires`** — it is a browser-session cookie by design (DEBT-032): the server's
  own absolute/idle expiry is the real limit either way, and this avoids a client-controlled
  lifetime claim that could outlive it. Logout deletes the row; account erasure revokes every
  session for the user (crypto-shred's short transaction).
- **BFF** (`frontend/app/api/[...path]/route.ts` + `lib/bffProxy.ts`/`bffRoutes.ts`/
  `bffConfig.ts`, P12-D1): an explicit allow-list of `(method, pattern)` pairs — anything not
  listed is a 404 by construction, never `/metrics`/`/docs`/`/redoc`/`/openapi.json`. Cookies
  forwarded both directions; the upstream fetch's own timeout is ≥ 240 s (`MIN_TIMEOUT_MS`,
  DEC-006) so the Azure ingress's ~240 s cutoff stays the only real limit on a slow/cold
  answer; once headers arrive the response body streams through unbuffered (SSE survives the
  hop token-by-token). `access_token` stripping from `/auth/login`/`/auth/register`
  responses is defense in depth only since 12b (T12.5.3): the backend itself stopped
  issuing one at all (F1 closes on this path; the legacy Bearer rollback, TF10, was
  removed entirely the same block — see the 12b bullet below).
- **Wake-up retry policy** (`frontend/lib/wakeUp.ts`, T13.9.1/DEBT-056/DEC-048): the one
  place that classifies a failed BFF call so a scale-to-zero cold start never looks like a
  dead page. A connect-level failure (the BFF's own 502 `"upstream unreachable"`, thrown
  before `proxyToBackend`'s `fetch` ever got a response — i.e. no replica is listening yet)
  is classified **"safe"**: provably never reached the backend, so it is retried
  automatically with bounded exponential backoff (base 1 s, factor 1.8, capped at 8 s/step,
  total budget 60 s) regardless of HTTP method. A BFF timeout (502 `"upstream timed out"`,
  the `MIN_TIMEOUT_MS` abort above) or any other pass-through 502/503/504 is classified
  **"unsafe"**: the request may already have reached the backend, so it is only
  auto-retried for a call the caller declares `idempotent` (every GET, by the HTTP method
  alone, plus `login` explicitly — repeating it has no harmful side effect); every other
  "unsafe" failure (a chat question, register, an admin mutation) is surfaced once as a
  `WakeUpError`, with **no automatic resend** — the UI shows a Retry button instead, so a
  duplicate answer/charge requires one real, user-initiated click, never a silent retry. A
  real 4xx is classified `"none"` and never enters this path at all. `lib/api.ts`'s
  `request()`/`streamChat()` are the only two call sites; every page wires the same
  `onWaking`/`onRecovered`/`onWakeFailed` hooks to the shared `<WakeBanner>`
  (`frontend/components/WakeBanner.tsx`, `aria-live="polite"`).
- **Pending-visual timer for a HELD cold start** (`frontend/lib/pendingWake.ts`,
  T13.11.5/DEBT-060, 13-J2): the retry policy above never fires at all when the Container
  Apps activator simply HOLDS a request (no error, no response) during a scale-from-zero --
  `createPendingWakeTimer` (injectable `setTimeout`/`clearTimeout`, `node --test`-able) arms
  a one-shot ~3 s (`PENDING_WAKE_DELAY_MS`) timer per logical call; `lib/useWakeState.ts`'s
  `startPending()`/`clearPending()` bracket it around login, the chat stream's wait for its
  first byte/token, and the admin dashboard's initial load. `combinePendingWakeState()`
  merges this purely-visual flag with the retry state above into the one state
  `<WakeBanner>` renders, with the retry state always winning -- a real 502-shaped retry
  already "waking"/"failed" is never masked or duplicated by the timer. No retry, no resend:
  this module never touches the network.
- **Real pipeline sub-steps inside the chat wake banner** (T13.12.5–T13.12.7, DEBT-062): a
  THIRD `/chat/stream` SSE event type, `{"type":"step","stage","step","phase":"start"|"end"}`
  (`generation.StreamStep`, additive to the existing `conversation`/`stage`/`token`/`done`/
  `error` events — an unrecognised `type` was already a no-op in `lib/chatStream.ts`'s
  `dispatch`), brackets `retrieval.embed_query` (pulled out of `hybrid_search` so the
  streaming generator's own `yield` can suspend around it — the dominant cold-start cost,
  Ollama waking `bge-m3`). Chat only: `lib/wakeSteps.ts` is a pure reducer
  (`events -> step states`) rendering six rows under the existing banner text once it is
  visible — Backend starting / Waking the embeddings model / Retrieving / Reranking /
  Generating / Checking — done ✓, current (with an `aria-hidden` elapsed-seconds tick),
  pending muted. `lib/pendingWake.ts`'s clear point moves, for chat specifically, from the
  first SSE event to the first streamed token (or `done`/`error` for the no-token
  abstention/failure paths) — the banner now stays up for the whole slow pipeline instead of
  disappearing just as the real wait begins. Login/the admin dashboard are untouched (the
  plain banner, no steps).
- **Custom 404 page** (`frontend/app/not-found.tsx`, T13.10.1/T13.10.2/DEBT-057): Next's App
  Router renders it automatically, with a real HTTP 404 status, for any request matching no
  route — never a soft-404 200; `/api/*`'s own catch-all route (`app/api/[...path]/route.ts`)
  always answers in JSON and is never reached here, and `/admin` for a non-admin stays a real
  route (its own SSR role check, §12 below). The door-slab/wheel formula is shared, not
  duplicated, with the closed-door hero on `/` (`frontend/lib/vaultGeometry.ts`,
  `sampleDoorBody`/`createVaultSampler` — the same math `components/VaultDoor.tsx` uses);
  the ajar scene's own frame/interior/footprints/stickman composition is
  `frontend/lib/vaultAjarScene.ts` (pure, `node --test`-able, no DOM). The page's copy model
  (`frontend/lib/notFoundContent.ts`) exposes exactly one destination (`/login`) and no field
  for the request path at all — the component never reads the pathname
  (no `usePathname()`/`useSearchParams()`/`headers()`), so there is nothing it could echo
  back even by accident (user: an error page's extra links/path echo are attack surface).
  `prefers-reduced-motion`: drawn once, no interval; the art is `aria-hidden`, the heading is
  a real `<h1>`, and it holds at a ~390px phone width with no horizontal scroll. Served
  through the same `middleware.ts` as every other route, so it gets the identical per-request
  CSP nonce and security headers (proven by `e2e/security-headers.spec.ts`'s walk, which now
  also visits an unknown path).
- **CSRF** (`rag_app.csrf`, P12-D2): double-submit, `csrf_token = HMAC-SHA256(CSRF_SECRET,
  raw_session_id)`, issued in a **readable** (non-`HttpOnly`) `__Host-secrag_csrf` cookie so
  page script can echo it back as `X-CSRF-Token`. `require_csrf` is attached to every
  cookie-authenticated state-changing route; a request with **no session cookie at all**
  (e.g. an already-logged-out `logout`) is exempt — nothing to protect. `require_origin`
  (lighter: a present-but-wrong
  `Origin` is rejected, a missing one allowed) covers register/login, where no session exists
  yet to bind a token to — DA-12aC's accepted trade-off (a curl/legacy client, or a browser
  that omits `Origin` on some same-origin requests, still gets through; a cross-site forger
  still cannot set a matching `Origin`).
- **Signed client IP** (`rag_app.client_ip` / `frontend/lib/clientIp.ts`, T5/V7): once
  `CLIENT_IP_HMAC_KEYS` is configured, the BFF derives the real client address from
  `X-Forwarded-For` (after `CLIENT_IP_TRUSTED_HOPS`), signs
  `client_ip|method|path|timestamp` (HMAC-SHA256, 300 s window — widened from 30 s in
  T12.3.0a/DEBT-038, a cold start could otherwise outlast the window) and sends it as
  `X-SecRAG-Client-IP`; the backend verifies the signature and keys rate limits by
  account/user **plus** this real IP instead of the shared Azure connection address. A
  missing/forged/stale signature falls back to the connection address and emits a
  `result`-labelled security event (`client_ip_signature_total{result="verified|fallback|
  stale|forged"}`, no IP label — `rag_app.metrics.CLIENT_IP_SIGNATURE_TOTAL`).
  **Caveat (DA-12aC-2, DEBT-034, accepted until 12b):** this only protects callers that
  actually go through the BFF — a request reaching the still-public backend directly in 12a
  falls back to the shared address, so `login:<email>:<ip>` collapses to `login:<email>`
  for that path; not worse than the old pure-IP keying, closes fully once the backend's
  ingress goes internal (12b).
- **Constant-time login, lockout, password policy, register/verify contract** (12b-B,
  T12.4.1-T12.4.5, F3/F5/F10/F11, OWASP-A07/API-2/API-6): `login` runs `verify_password`
  against a REAL argon2 hash whether the account exists or not (`rag_app.security.
  dummy_password_hash`), so argon2's own cost dominates either path (timing-proven,
  `backend/tests/test_security.py::test_login_timing_does_not_distinguish_a_real_account_
  from_a_missing_one`). `rag_app.lockout` keys a per-account exponential-back-off counter by
  `HMAC(LOCKOUT_HMAC_SECRET, email)` (never `user_id` — an attempt against an unregistered
  address is throttled the same as a real one), checked-and-incremented in one atomic
  `INSERT … ON CONFLICT DO UPDATE` (X10, REC-05); deleted in the erasure request path while
  the email is still known, plus a 30-day age-based retention sweep in the purger.
  `rag_app.password_policy` enforces >= 12 chars, the NCSC/SecLists breach list and "not the
  email's own local part" (licence note in the module). `register` returns a generic 202
  whatever happened server-side (new account, already-registered, or a race between two
  registrations of the same address) — never a 409 — and opens **no session** any more
  (an unverified account has nothing useful to do yet); `resend-verification` moved from an
  authenticated call to an email-addressed one for the same reason. `REQUIRE_EMAIL_
  VERIFICATION` now defaults to `true` with `ENV=prod` unless set explicitly. `/auth/verify`
  moved from a mutating GET (query-string token) to a POST (body token, T12.4.5) — the
  frontend's `/verify`/`/reset` pages (`PUBLIC_APP_URL`) are what a GET actually renders; a
  mail scanner pre-fetching the emailed link can no longer consume anything. DEBT-034's
  design note (does not close it — closes at T12.6b.5) and REC-05 are in
  `docs/security/THREAT_MODEL.md`.
- **Password reset + email abuse limits + `EmailSender`** (12b-C, T12.4.6-T12.4.8, X10,
  REC-06): `password_reset_tokens` (0007 "part 3") follows `email_verification_tokens`'
  storage convention exactly (only the SHA-256 hash is stored) and the SAME atomic-consume
  shape (`UPDATE ... WHERE used_at IS NULL AND expires_at > now()`, never SELECT-then-
  UPDATE — `rag_app.api.auth._consume_password_reset_token`/`_consume_email_verification_
  token`); `POST /auth/reset` success revokes every session of the account
  (`rag_app.sessions.revoke_all`) and clears any login lockout. `POST /auth/forgot-password`
  is enumeration-safe the same way as register (generic response; a real reset email to the
  real owner, a harmless "no account" notice to anyone else, so the response/timing never
  discloses which happened) — the register-style "unconditional DB write" trick does not
  apply (a reset token needs a real `user_id` for its FK), so cost parity instead comes from
  always sending exactly one email via the SAME `_send` call either way.
  `rag_app.email_abuse.EmailAbuseLimiter` adds four independent in-memory token buckets
  (per real IP, per `(recipient, real IP)`, per recipient as a many-IP-flood backstop, and
  global) for register/resend-verification/forgot-password, each an atomic
  `RateLimiter.allow` call (X10) — code constants (B6), no new secret. The `(recipient, IP)`
  bucket (DA-12bC-1/DEC-026 = A fix) stops a single trickling attacker IP from starving one
  victim's address forever; the recipient identity is HMACed with a per-process salt before
  it is ever used as an in-memory key (see `docs/security/THREAT_MODEL.md` REC-07).
  `rag_app.emailer` (one SMTP implementation, T12.4.7) counts every send attempt by `kind`
  (`secrag_email_sent_total`/`secrag_email_failed_total`, never the recipient). DA re-check:
  `register`'s `except IntegrityError` now treats ONLY the `users.email` unique-constraint
  violation (SQLSTATE 23505, constraint `users_email_key`) as "already registered" — any
  other integrity error (e.g. a NOT NULL/FK problem on a different table) re-raises as a 500
  instead of silently returning the generic 202.
- **Roles + permission map + admin MFA** (`rag_app.authz`, T12.3.2/T12.3.2b/T12.3.3/T12.3.4/
  T12.3.5): `users.role` stays `user`/`admin` (CHECK constraint, migration 0006); every
  protected route declares a **permission** (`require_permission("admin:read")`), never a
  role string — `ROLE_PERMISSIONS` maps role -> the permissions it holds, and
  `require_permission` fails closed (403, every role including admin) when a route names a
  permission not yet in that map, so a future department role (Phase 21) is one map entry,
  never a route-level code change. `require_role(role)` is a thin alias
  (`require_permission(f"{role}'s base permission")`). `role` is never accepted from a
  client request (every request body is a `StrictModel`, `extra="forbid"` — an unexpected
  `role`/`is_admin`/`user_id` field is rejected, not silently dropped) and the only writer
  of `role = 'admin'` anywhere in the codebase is the interactive bootstrap CLI
  (`python -m rag_app.admin create`, password via `getpass`, never an argument or logged).
  Admin-grade routes additionally require `require_permission_and_mfa`: a fresh TOTP
  step-up (`pyotp`, migration 0007 `user_mfa`/`mfa_recovery_codes`, single-use recovery
  codes hashed like every other token) that **rotates the session id** and sets
  `sessions.mfa_verified_at` — the legacy Bearer path can never satisfy this (no session row
  to step up). The recovery-code claim itself follows the X10 no-TOCTOU rule
  (`docs/security/THREAT_MODEL.md` X10/REC-04): a single atomic `UPDATE … WHERE used_at IS
  NULL` (`rag_app.api.admin.claim_recovery_code`), proved single-use under genuine 8-thread
  concurrency by `backend/tests/security/test_mfa.py::
  test_concurrent_claims_of_the_same_recovery_code_let_exactly_one_through` — the template
  12b-B's lockout counters and 12b-C's reset/verify token consume follow. `PROTECTED_ROUTES`
  (populated at route-definition time, never guessed
  afterwards) drives a role x route client-tampering matrix (T12.3.6, OWASP A01/API5):
  mass assignment, query/header tricks (`X-Role`, `X-Admin`, forged `X-SecRAG-Client-IP`),
  cookie tampering (the cookie is always opaque — the role is re-read from the database on
  every request, never decoded from the cookie), direct backend calls bypassing the
  frontend, and a missing MFA step-up all deny — proven in
  `backend/tests/security/test_client_tampering.py`. **13-F (DEBT-051/DEC-041):**
  `POST /account/mfa/enroll` on an already-ENABLED record additionally requires a valid
  current TOTP code or an unused recovery code (403, generic message, otherwise) — a
  session cookie + CSRF token alone can no longer replace an admin's second factor; proven
  by `backend/tests/security/test_mfa.py::
  test_reenrolling_an_enabled_account_without_proof_is_rejected_and_nothing_changes`
  (THREAT_MODEL REC-13). First-time enrolment is unchanged.
- **Security headers + CSP** (T12.2.1/T12.2.2/T12.2.3, `frontend/middleware.ts` +
  `lib/csp.ts`/`lib/securityHeaders.mjs`; backend `rag_app.api.app._add_security_headers`):
  HSTS (no `preload` — shared `*.azurecontainerapps.io` domain, E7), `X-Content-Type-Options:
  nosniff`, `Referrer-Policy`, `Permissions-Policy`, `poweredByHeader: false` on every
  frontend response; a per-request nonce CSP (`script-src 'self' 'nonce-…'
  'strict-dynamic'`, `style-src 'self' 'unsafe-inline'`, `frame-ancestors 'none'`) on every
  HTML route, reported to `/api/csp-report` (`rag_app.csp_report`, E5: unauthenticated,
  rate-limited by real IP, body-size capped, logs only the violated directive and the
  blocked origin — never a path or query string, CSRF-exempt by design) via both
  `report-uri` (Firefox/Safari) and the Reporting API (`Reporting-Endpoints` header,
  Chromium). `CSP_MODE` (`lib/csp.ts`) is a single switch: `"report-only"` until the
  Playwright page walk proves zero `script-src` violations against the real built app in a
  real browser, then `"enforce"` (T12.2.3's literal Done-when) — enforcing since 12a-E
  Phase 2 (2026-10-17). The backend narrowed CORS to the old (pre-BFF) frontend's exact
  needs in 12a (single origin, `GET`/`POST`/`PATCH`/`DELETE`, `Authorization`/
  `Content-Type`, no credentials), then **removed it entirely in 12b** (T12.5.3 — the
  BFF-based frontend never triggers a browser CORS preflight at all); `/docs`/`/redoc`/
  `/openapi.json` stay gated behind `ENV=dev`.
- **Legacy Bearer path, CORS and route walker removed/added** (12b-D, T12.5.2/T12.5.3):
  `rag_app.api.auth.get_current_user` is cookie-only now — `HTTPBearer`, the `_bearer_user`
  fallback and `/auth/login`'s `TokenResponse(access_token=...)` are all gone
  (`create_token`/`decode_token` stay in `rag_app.security` as plain, independently-tested
  utility functions until T15.2.5's full cleanup). `CORSMiddleware` is removed from
  `create_app()` entirely. Every route now declares, directly or transitively, an auth
  dependency, a **permission** (`rag_app.authz.DECLARED_PERMISSION_ROUTES`, separate from
  the narrower `PROTECTED_ROUTES` the T12.3.6 tampering matrix uses, see that module's
  docstring) and a rate limit (`rag_app.api.deps.RATE_LIMIT_DEPENDENCIES`, including the new
  `rate_limit_account(action)` factory for routes that had none before) — or is on
  `backend/tests/security/test_route_walker.py`'s closed public allow-list; that same test
  walks the real app and proves the rule has teeth against two toy routes.
- **Health checks via the frontend's BFF** (DEC-005/T12.5.6) **and an exposed-files check**
  (T12.5.7, both 12b-D, user idea 2026-11-09, DEC-023): `cd.yml`'s post-deploy check and
  `scripts/azure/demo-mode.sh on` now poll the frontend's own `/api/health` route (built in
  12a-C) instead of a backend-specific FQDN — the backend will have no public URL to poll
  directly at all once ingress goes internal (T12.6b.5). `scripts/azure/
  exposed_files_check.sh` probes a frontend URL for `/.git/HEAD`, `/.git/config`, `/.env`,
  `/.env.local`, a `.next/` server file, a `*.map` source map, `/backend/` and
  `/docker-compose.yml` (a 200 is the only failure); the backend's own lack of static file
  serving is checked unconditionally (`backend/tests/security/test_exposed_files.py`).

## 9. Compliance (data erasure)

- **Crypto-shredding**: user data is encrypted with a per-user key; deleting the key renders the data
  unrecoverable.
- Erasure covers **all** copies: relational rows, **vectors**, **caches**, and **traces/logs** — not
  just the source documents. Per-user namespace in the vector store makes deletion clean.

## 10. Deployment (CD)

- Docker images for backend and frontend; `docker-compose` for local (db + ollama + apps).
- GitHub Actions: on merge to `main`, run tests + eval gate → build images → push to registry (GHCR) →
  deploy to the free tier. GPU inference runs where a GPU is available (self-hosted runner or local).

## 11. Cloud deployment (Azure)

- **Web/data tier**: two **Azure Container Apps** (backend, frontend) pull the existing GHCR images;
  **Azure Database for PostgreSQL Flexible Server** with the `pgvector` extension enabled holds the
  data (migrated by the migration Job since Phase 11a).
- **Model tier**: the Container Apps consumption plan has no practical GPU, so `qwen`/Ollama is **not**
  deployed there — **Azure OpenAI** replaces it for the cloud path only, selected via `LLM_PROVIDER`
  (Ollama stays the local default). See [ADR phase 10](adr/adr_phase10_azure.md).
- **Secrets**: supplied as Container Apps built-in secrets (`DATABASE_URL`, `JWT_SECRET`,
  `DATA_MASTER_KEY`, `AZURE_OPENAI_*`); escalate to Key Vault only if a real need shows up.
- **CD**: after the GHCR push, `cd.yml` authenticates to Azure with **OIDC federated credentials**
  (no stored secrets) and rolls both apps to the new image tag; the pre-push eval gate still guards
  the deploy. *From Phase 11a:* CD runs on a successful CI run on `main`, refuses a commit without
  the `secrag/gate-full` status, deploys images **by digest**, starts the **migration Job** (slim
  `jobs` image) before the apps and updates the **purge** (hourly) and **backup** (daily, encrypted
  to Blob Storage) Jobs to the same digest; it never rolls back
  ([ADR phase 11](adr/adr_phase11_stability.md)). *From Phase 13 (block 13-0):* `cd.yml` builds and
  deploys **per image** — the files changed since the last deployed SHA decide which of the 4
  images (backend, frontend, jobs, ollama) actually need a new build; an app/Job whose image did
  not change keeps running its currently deployed digest (read from Azure, fail-closed) instead of
  being redeployed, so an unrelated frontend-only change no longer forces an Ollama cold start
  ([ADR phase 13](adr/adr_phase13_dashboard.md)).

## 12. Admin dashboard (Phase 13)

- **Feature registry** (`rag_app.features.REGISTRY`, P13-D3): one typed, importable list —
  every feature names its phase, status (`enabled` / `disabled` / `retained-not-adopted` /
  `expected-fail`), an optional health probe (≤ 2 s, degrades gracefully with no live
  stack), the Prometheus metrics it claims, defence-catalogue ids
  ([`THREAT_MODEL.md`](security/THREAT_MODEL.md)) and quality-probe ids. The `registry`
  gate step (`rag_app.devtools.registry_check`) cross-checks every claim against the real
  app/Prometheus registry and runs every quality probe with a real DB session once one is
  reachable (`--full`). **DoD rule (T13.5.1):** a feature added from 13-E onward needs a
  registry entry, a probe and ≥ 1 metric (structurally enforced,
  `rag_app.features.check_dod_compliance`; entries predating the rule are an explicit,
  reviewed exception) and a defence-catalogue probe if actually catalogued (review). Every
  metric label name is checked against a reviewed allow-list
  (`rag_app.metrics.check_no_pii_labels`) so a new PII-shaped label fails automatically.
- **Admin API** (`/admin/*`, `rag_app.api.admin`): every route resolves through
  `require_permission_and_mfa` (permission map + MFA set on the session, T12.3.2/T12.3.5)
  and a per-account rate limit. `GET /admin/features` (registry + live health + metrics
  summary), `GET /admin/metrics` (every declared metric's current value through
  `MetricsBackend`), `GET /admin/latency` (per-stage p50/p95 from the chat-pipeline
  histogram, `stages`; plus `pipeline`, T13.8.2: the SAME computation over the atomic-step
  histogram below, grouped by UI pipeline stage in `STEP_ALLOWLIST` order — the Pipeline
  waterfall's expandable rows, aggregate/this-replica-only, never per-request), `GET
  /admin/eval` (last eval run vs baseline/floors, aggregate metrics only —
  never a per-question golden-set text, T13.1.7), `GET /admin/erasure` (open erasure
  requests, rows remaining, last purger run, `completed_at` once `done` — T13.8.3) + `POST
  .../purge-now` / `.../retry`, `POST
  /admin/users/erase` (DEBT-002, last-admin guard, DEC-033/DEC-035), and `GET
  /admin/alerts` (T13.5.2, below; `feature_ids` per alert — T13.8.3). Every mutating call is audited in `admin_actions`
  (migration 0008, [BACKEND_SCHEMA §5](BACKEND_SCHEMA.md#5-as-implemented-migrations-0001-0009)).
  `FeatureHealthOut.docs` (T13.8.6, 13-G2) passes the registry's own `docs` reference straight
  through, so the dashboard's feature drawer shows a docs/ADR pointer without a second,
  possibly-drifting lookup table.
- **Atomic-step metrics** (T13.8.1): `secrag_chat_step_seconds{stage,step}`, a SECOND
  histogram alongside `secrag_chat_stage_seconds` (unchanged), labelled by the UI-facing
  pipeline stage (`generation.StreamStage`'s own vocabulary: classifying/retrieving/
  reranking/generating/checking/responding — a different, higher-level grouping than the
  stage histogram's own `stage` label) and a FIXED, reviewed allow-list of steps within it
  (`rag_app.metrics.STEP_ALLOWLIST`); `rag_app.metrics.observe_step` rejects (raises) any
  pair outside it. `rag_app.timing.step()`/`record_step()` wire `retrieval.py`,
  `reranking.py` and `generation.py` (both answer paths) into it; the non-streaming path has
  no incremental token stream, so `llm_first_token` alone covers its one blocking LLM call
  and `token_stream` is simply never emitted there. On a mid-stream client disconnect
  (`GeneratorExit` inside `_stream_answer_tokens`), `token_stream` records whatever PARTIAL
  duration had elapsed so far (a `finally`) instead of being silently dropped — a third,
  now-documented case alongside the non-streaming one above (DA-13-G1-3). Each stage's own
  step-sum is the single source of truth for that stage's time in the `pipeline` field
  (T13.8.2) — never reconciled against `stages`' value for the same stage: `retrieving`'s
  `load_chunks` step also covers the ranked-list build, and `reranking`'s
  `order_by_scores`/`generating`'s `parse_answer` steps now run INSIDE their own stage's
  `with timing.stage(...)` span (DA-13-G1-1 fix block 13-G1-fix) so each stage's step-sum
  lands within a small tolerance of that stage's own `secrag_chat_stage_seconds` total; this
  slightly grew the `rerank_inference`/`generate` stage totals themselves (they now also
  cover `order_by_scores`/`parse_answer`'s own, small, cost) — intended, not a regression.
- **Live updates** (T13.4.1/T13.4.2) and **metrics backends** (T13.4.3, R6-4: why no
  managed Prometheus on Azure) are a dedicated ADR decision — see [ADR phase 13, "Live
  updates + metrics backends"](adr/adr_phase13_dashboard.md#live-updates--metrics-backends-13-c-t1341t1343-r6-4).
- **Banner alerts** (T13.5.2, R6-1, P13-D4): `rag_app.api.admin._banner_alerts` is the ONE
  implementation behind `GET /admin/features`' `alerts` field (what the dashboard polls on
  Azure — one probe sweep per tick, DA-13-E-2), the stand-alone `GET /admin/alerts` and the SSE snapshot/
  heartbeat's `alerts` field (local) — an open erasure request past 24 h (amber past 20 h),
  a lockout-hit count past a fixed floor, any probe reporting `down` whose feature is
  `expected_here` (T13.8.9, DEBT-053 — a `dev_only`/`azure_only` feature switched OFF on
  this stack never alerts, but one switched ON here that is still `down` now does), any
  email send failure, and the daily answer cap (R6-1) reached (read
  from `usage_daily` directly, never a Prometheus metric). The frontend
  (`components/admin/AdminDashboard.tsx`) sorts red before amber and renders nothing when
  the list is empty.
- **Defence lab** (13-D, local only — DEC-028, `rag_app.defence_lab`): re-runs every
  THREAT_MODEL-catalogue probe's own Test ID as a real CLI/admin-button action (`POST
  /admin/lab/run`), persisting history in `defence_runs` (migration 0008); `LAB_ENABLED`
  genuinely removes the router with it off (a 404, not a 403) and `ENV=prod` refuses to
  serve it regardless of the flag. See [ADR phase 13, "Defence
  lab"](adr/adr_phase13_dashboard.md#defence-lab-13-d-t1331t1336-local-only--dec-028) for
  the probe-framework/erasure-at-scale/negative-control detail.
