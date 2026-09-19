# Product Requirements Document (PRD) — SecRAG

## 1. Overview

**SecRAG** is a web application that answers questions about **application security** (OWASP Top 10
and Cheat Sheets) using **Retrieval-Augmented Generation** over a curated, **versioned** document
corpus. It runs entirely on **free, local models**.

The product exists to solve a problem that generic chatbots handle badly: security guidance is
**version-sensitive** (the OWASP Top 10 changed between 2021 and 2025) and **high-stakes** (a wrong or
outdated answer can cause a vulnerability). SecRAG is designed to answer **from cited, current sources**
and to **abstain** when it does not know, instead of hallucinating.

It is also a portfolio project: it demonstrates a full, production-shaped workflow — authentication,
abuse defense, evaluation gates, and CI/CD — not just a retrieval script.

## 2. Target users

- **Developers** looking up how to prevent a specific vulnerability (e.g. SQL injection).
- **Students / juniors** learning the OWASP Top 10.
- **Security reviewers** who need a quick, sourced reminder of a control.

## 3. Problem statement

1. General LLMs answer security questions confidently but may be **outdated** or **wrong**, with no
   sources.
2. OWASP guidance **changes across versions**; users need answers tied to a specific edition.
3. Security answers must be **auditable** (which document, which version) and **honest** (abstain when
   unsure).

## 4. Goals & success metrics

| Goal | Metric | Target (initial) |
|------|--------|------------------|
| Retrieve the right context | Context recall @5 | ≥ 0.80 |
| Answer from the context | Faithfulness | ≥ 0.90 |
| Answer the actual truth | Correctness vs ground truth | ≥ 0.85 |
| Don't hallucinate | Correct abstention rate (negative set) | ≥ 0.95 |
| Responsiveness | p95 latency per answer | ≤ 4 s |
| Abuse resistance | Blocked abusive signups / rate-limit hits | measured, no crash |

These thresholds are enforced by the **evaluation gate** before any deploy (see TRD).

## 5. Features

### 5.1 Core (MVP)

- **F1 — Ask a security question**: user submits a question; system returns an answer **with citations**
  (source document + version + effective date).
  - *Acceptance*: every non-abstained answer includes at least one citation resolvable to a corpus doc.
- **F2 — Version-aware answers**: when the answer depends on the OWASP edition, the system uses the
  **current/most authoritative** version and can distinguish 2021 vs 2025.
  - *Acceptance*: for a version-sensitive golden question, the answer matches the requested/current
    edition, not a superseded one.
- **F3 — Honest abstention**: if the corpus does not contain the answer, the system says so.
  - *Acceptance*: on the negative golden set, the system abstains (does not fabricate) ≥ 95% of the time.
- **F4 — Conversation history**: a signed-in user can see their past questions and answers.
- **F5 — Authentication**: email/password sign-up, email verification, login, logout.
- **F6 — Abuse defense**: rate limiting and per-user quotas; resistance to mass fake-account signups.
- **F7 — Data erasure**: a user can delete their account and all associated data (GDPR-style).

### 5.2 Later / optional

- Agentic router (query reformulation + retry) — only if evaluation shows it beats the simple pipeline.
- Figure/diagram captioning during ingestion (vision model).
- Semantic caching for cost/latency.

## 6. Non-goals

- Not a general-purpose chatbot; **out-of-domain questions are abstained**, not answered.
- Not a vulnerability scanner or exploit tool.
- No multi-tenant org management, billing, or admin console in the MVP.
- No mobile app; responsive web only.

## 7. Constraints & assumptions

- **Free / local only**: models run via Ollama on a single consumer GPU (8 GB VRAM). Exactly three
  models: one `qwen` (Q4) for all LLM tasks, `bge-m3` for embeddings, `bge-reranker` for reranking.
- **CI/CD-first**: no deploy without passing tests and the evaluation gate; no secrets committed.
- Corpus is **mini** (~10-20 documents) to keep cost and latency low and evaluation tractable.

## 8. Risks

| Risk | Mitigation |
|------|------------|
| Faithful-but-stale answers | Version/effective-date metadata + correctness-vs-truth eval |
| Hallucination on unknown topics | Groundedness check + abstention + negative eval set |
| Cost/abuse (Denial of Wallet) | Rate limiting, quotas, risk scoring on signup |
| Indirect prompt injection via docs | Treat retrieved text as data, never instructions |
| GPU memory limits | Q4 quantization; single shared LLM; reranker on CPU |
