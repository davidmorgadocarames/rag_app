# Definition of Done — SecRAG phase gate

Every phase must satisfy **all** applicable norms below before it is considered done and
before its work is pushed. The mechanical norms are enforced by `scripts/gate.sh` (run it
before pushing; a Claude Code hook also runs it automatically on `git push`). The judgment
norms are a human/agent checklist.

> This exists because "it works on Windows for now" once slipped through: the gate makes the
> project's norms a barrier, not a good intention. The environment (WSL2) is only **one** norm.

## Universal norms (every phase)

| # | Norm | Enforced by |
|---|------|-------------|
| 1 | Developed and run in **WSL2 Ubuntu** (GPU used where relevant) | gate (`/proc/version`) |
| 2 | No secrets committed: `.env` git-ignored, gitleaks clean | gate |
| 3 | No hardcoding of config — everything via `pydantic-settings` | review + gate (grep smell) |
| 4 | Dependencies pinned | gate (checks `==` in requirements) |
| 5 | Lint + format clean (`ruff check`, `ruff format --check`) | gate |
| 6 | Types clean (`mypy` strict) | gate |
| 7 | Tests green (`pytest`); new logic has tests | gate |
| 8 | Frontend touched → `eslint` + `tsc --noEmit` + `next build` | gate (if `frontend/node_modules`) |
| 9 | **Verified end-to-end** — actually exercised, not only unit tests | judgment (checklist) |
| 10 | Docs updated (README roadmap + relevant docs) | judgment |
| 11 | CI green after push; conventional commit message | CI + review |

## Phase-specific norms

- **Phase 4+ (evaluation)** — the **eval gate**: retrieval metrics (context recall/precision),
  generation metrics (faithfulness, answer relevance), and **correctness vs ground truth** meet
  their thresholds; results do not regress below `baseline_metrics.json`; the LLM-judge is
  validated against human labels; correct-abstention rate on the negative set meets its threshold.
  Enforced by `scripts/gate.sh` once `rag_app.eval` exists.

## How to run

```bash
# inside WSL2, from the repo root
bash scripts/gate.sh          # runs all mechanical norms; non-zero exit = blocked
```

The `git push` hook runs this automatically and blocks the push if the gate fails.
Judgment norms (#9, #10) are confirmed by the author/agent before marking a phase done.
