# ADR 0001 — Agentic router is not the default (kept behind a benchmark)

- **Status:** Accepted (2026-09-19)
- **Context:** Phase 5. We proposed an agentic router (CRAG-lite): detect "thin"
  retrieval (best cross-encoder score below a threshold), reformulate the query into
  precise security terminology, and retry before answering. The project rule is:
  *add agentic complexity only if a benchmark shows it beats the simple pipeline.*

## Decision

Keep the **simple pipeline** (hybrid retrieval → rerank → generate → groundedness →
abstain) as the default. The agentic router (`rag_app.agentic`) is implemented and
available, but **not** wired into the default answer path.

## Evidence

`python -m rag_app.eval.benchmark` over the 14-item golden set (qwen on RTX 4060):

| Pipeline | correctness | faithfulness | retrieval_recall | correct_abstention | mean latency | rewrites |
|----------|------------|--------------|------------------|--------------------|--------------|----------|
| simple   | 0.900 | 1.000 | 1.000 | 1.000 | 12.5 s | 0 |
| agentic  | 0.900 | 1.000 | 1.000 | 1.000 | 14.5 s | 5 |

The router reformulated 5 thin queries (mostly the out-of-corpus negatives) but changed
no outcome: identical quality, ~16% higher latency.

## Rationale

On this mini, well-matched corpus the reranker already surfaces the correct documents
(ceiling effect), so query reformulation has nothing to fix. The router only adds
latency and LLM calls (cost), with zero quality gain — so it does not earn its place
as the default.

## Consequences

- Default answer path stays simple and cheaper.
- The router code + benchmark are retained as tooling. Re-run the benchmark when the
  corpus grows or gets messier; adopt the router if it then improves correctness or
  abstention without unacceptable latency/cost.
- Metrics used for this decision are trustworthy: the correctness judge was validated
  (Cohen's kappa 0.875, see `rag_app.eval.validate_judge`).
