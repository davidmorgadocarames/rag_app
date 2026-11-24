# ADR phase 14 — Conversational assistant

- **Status:** **In progress.** This ADR is created at block **14-A1** (golden set v2 schema +
  items) and documents only that part so far; the router/rewriting/memory, conversational-answer,
  visible-plan, latency/cost/dashboard and Azure-migration decisions (blocks 14-B1/B2, 14-C1/C2,
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

## Known limitations (self-reported, not just design)

- **Narrow corpus.** The fixture's `expected_doc`s are drawn from a single-topic corpus (OWASP
  injection + broken-access-control cheat sheets and Top-10 pages, `docs/TRD.md` §3) — it cannot
  exercise retrieval across a broad corpus, and every Spanish item is hand-written (the corpus
  itself is English-only), not a translated real document.
- **Not yet consumed by the pipeline.** This block adds the schema and the fixture only; nothing
  in `rag_app.eval.runner`, `generation.py` or the API reads `Dialogue` objects yet. The floors
  set for this sub-phase are not measured until 14-A2 (runner + metrics) runs against this
  fixture — a `Dialogue`/`Turn` that type-checks and passes its own schema tests is not proof the
  *questions* are well-chosen until a real eval run scores them.
- **DEBT-006 probe is unverified.** `trunc-sqli-error-messages` targets a detail expected to sit
  late in a long cheat-sheet section (reranker `max_length=256` truncates the chunk's tail,
  `docs/TRD.md` §3), but this block has no DB/LLM access to actually run retrieval against it —
  whether it reproduces the truncation-recall loss is confirmed (or not) only when 14-A2 runs the
  real eval.
- **Item count is a first pass, not a ceiling.** The Phase 14.0 plan calls for roughly 60
  single-turn items + 10 dialogues for the whole sub-phase; this block ships 38 + 10 = 48. The gap
  is intentional headroom for the user's review pass (P14-D4), not a scope cut.

## Rollback

No runtime behaviour changes in this block: `eval/golden_set_v2.jsonl` and the new
`Turn`/`Dialogue`/`load_dialogues` code are inert until 14-A2 wires a runner to them. Reverting
this block's commit(s) is a plain revert with no migration, no deployed-image, and no API impact.
