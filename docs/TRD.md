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
| LLM serving    | Ollama | Free, local, simple HTTP API |
| Auth hashing   | argon2 (argon2-cffi) | Modern password hashing |
| Ingestion      | PDF → Markdown (`pymupdf4llm` / Docling) | Layout-aware, preserves tables |
| Retrieval      | pgvector (ANN) + BM25 (hybrid) | Vectors miss exact terms/IDs |
| Reranking      | `bge-reranker` cross-encoder (FlagEmbedding) | Precision boost over pure ANN |
| Evaluation     | Ragas + pytest | Retrieval + generation metrics, gated in CI |
| Observability  | Structured tracing (Langfuse/self-hosted or JSON logs) | Find the failing/slow stage |
| Containers     | Docker + docker-compose | Reproducible dev + deploy |
| CI/CD          | GitHub Actions → free tier (Railway/Render/Fly) | Test → gate → build → deploy |

## 3. Models (exactly three)

| Task | Model | Notes |
|------|-------|-------|
| Router, generation, groundedness check, LLM-judge | one **`qwen` Q4** (e.g. `qwen2.5:7b-instruct-q4_K_M`) | Shared across all LLM tasks; Q4 fits 8 GB VRAM |
| Embeddings (chunks + query) | **`bge-m3`** | Multilingual, strong retrieval; fixed early (changing it forces re-embedding) |
| Reranking | **`bge-reranker`** (`bge-reranker-v2-m3`) | Cross-encoder; can run on CPU |

Configured via env: `LLM_MODEL`, `EMBED_MODEL`, `RERANKER_MODEL` (see `.env.example`).

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

## 7. Cost & latency

- Q4 quantization; a single shared LLM; reranker on CPU.
- **Benchmark harness** sweeps configs (chunk size, k, rerank on/off, router on/off) and reports
  faithfulness / correctness / p95 latency / cost per query — so complexity is added by **evidence**.
- Semantic caching (invalidated on corpus change); parallelize independent tool calls.

## 8. Security & abuse

- **Auth**: email/password, argon2, email verification, JWT/session, login rate limiting.
- **Rate limiting**: token bucket per IP/user; **cost-aware** variant (a query consumes tokens ∝ its
  cost) to defend against **Denial of Wallet**.
- **Sybil/DoS defense**: risk scoring on signup (account age, IP velocity/reputation, disposable-email
  detection), CAPTCHA/proof-of-work, per-user quotas.
- **Access control**: ACL filter applied **in the vector query**, so unauthorized chunks are never
  retrieved.
- **Indirect prompt injection**: retrieved document text is treated as **untrusted data, never
  instructions**.
- **Agent limits**: bounded steps, retries, and tokens to prevent runaway loops.

## 9. Compliance (data erasure)

- **Crypto-shredding**: user data is encrypted with a per-user key; deleting the key renders the data
  unrecoverable.
- Erasure covers **all** copies: relational rows, **vectors**, **caches**, and **traces/logs** — not
  just the source documents. Per-user namespace in the vector store makes deletion clean.

## 10. Deployment (CD)

- Docker images for backend and frontend; `docker-compose` for local (db + ollama + apps).
- GitHub Actions: on merge to `main`, run tests + eval gate → build images → push to registry (GHCR) →
  deploy to the free tier. GPU inference runs where a GPU is available (self-hosted runner or local).
