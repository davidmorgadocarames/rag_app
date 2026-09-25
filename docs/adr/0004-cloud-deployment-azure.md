# ADR 0004 — Cloud deployment (Azure)

- **Status:** Accepted (2026-09-25)
- **Context:** Phase 10. The app is containerized and CD-published to GHCR
  ([ADR 0003](0003-deployment.md)), but "host deployment" was left as a separate,
  credentialed step with no named cloud provider — and, crucially, the model tier
  (**Ollama + `qwen`, needs a GPU**) can't run on the free tiers ADR 0003 pointed at.
  ADR 0003 already anticipated the exit: *"swapping the model tier for a hosted LLM later
  only changes `OLLAMA_HOST` / the LLM client — the rest is unaffected."* Phase 10 cashes
  that promise in.

## Decision

- **Web/data tier on Azure.** Deploy the two GHCR images to **Azure Container Apps**
  (backend, frontend) and store data in **Azure Database for PostgreSQL Flexible Server**
  with the `pgvector` extension enabled (`alembic upgrade head` runs against it). No
  migration to Azure Container Registry — Container Apps keep pulling from GHCR.
- **Model tier via a pluggable provider.** Introduce a `ChatClient` interface with two
  implementations — `OllamaChat` (local) and `AzureOpenAIChat` (hosted) — selected by a
  new `LLM_PROVIDER` setting (default `ollama`). The cloud deployment sets
  `LLM_PROVIDER=azure_openai`; **local development stays free and local by default.** This
  is the single seam ADR 0003 predicted: only the LLM client changes.
- **Azure OpenAI, not a GPU host.** The Container Apps consumption plan has no practical
  GPU, so `qwen`/Ollama is not deployed there; Azure OpenAI serves the cloud path instead.

## Secrets & credentials

- Runtime secrets (`DATABASE_URL`, `JWT_SECRET`, `DATA_MASTER_KEY`, `AZURE_OPENAI_*`) are
  supplied as **Container Apps built-in secrets** — escalate to Key Vault only if a real
  need shows up; a portfolio-scale deployment doesn't warrant it yet.
- CD authenticates to Azure with **OIDC federated credentials** (`azure/login`), so no
  long-lived Azure secret is stored in the repo. This extends norm #2 (no secrets
  committed); it does not add a new mechanism.
- The frontend bakes `NEXT_PUBLIC_API_URL` at **build time**, so the public backend URL is
  passed as the image build-arg in CD, not a container runtime variable.

## Consequences

- **Two LLM code paths to keep in sync.** Both implement the same `ChatClient` interface
  and are covered by the same unit tests, so drift is caught early. `OllamaChat` remains
  unchanged and the default.
- **Real per-token cost.** Azure OpenAI calls cost money per token, unlike local Ollama.
  The existing cost-aware rate limiting and per-user quotas bound request *volume*, which
  is the main abuse lever — but they don't cap absolute spend. For a portfolio-scale
  deployment we therefore **also set a hard monthly Azure budget alert** as a backstop;
  rate limiting alone is not treated as sufficient cost control.
- **The eval gate still guards the deploy.** The pre-push gate (`scripts/gate.sh`) runs
  the eval gate and blocks the very push that triggers CD, and the Azure deploy job is
  `needs: [images]`, so a bad build never reaches Azure — same guarantee as Phase 9.
