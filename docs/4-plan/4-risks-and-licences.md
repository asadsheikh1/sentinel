# 12 — Dependencies, Licences and Technical Risks

## 1. Licence review

Licences reflect the assistant's knowledge as of planning. **They are re-verified when each dependency is added** (Step reports note any change). Licence scanning (`pip-licenses`, `license-checker`) is added to CI in Step 32.

### ⚠️ Items with licence restrictions or commercial-use implications

| Software | Licence | Implication for SENTINEL | Decision |
|---|---|---|---|
| **Grafana Loki** | AGPL-3.0 | Free to use. If you **modify Loki and offer it over a network**, you must publish your modifications. Running it unmodified as a separate service does not affect SENTINEL's licence | Use unmodified (decision D5). Alternative: keep logs in Postgres (worse) or OpenSearch (Apache-2.0, much heavier) |
| **Grafana** (optional) | AGPL-3.0 | Same as Loki | Optional (Could) |
| **Redis ≥ 7.4** | RSALv2 / SSPLv1; Redis 8 adds an AGPL-3.0 option | Source-available, with restrictions on offering it as a service | **Use Valkey (BSD-3)**, the Linux Foundation fork, for the demo's Celery broker. SENTINEL itself needs no Redis |
| **Terraform ≥ 1.6** | Business Source Licence 1.1 | Restricts competitive commercial use. Fine for personal use but not open source | **Use OpenTofu (MPL-2.0)** |
| **podman-compose** | GPL-2.0 | A developer CLI tool only. Not linked into or shipped with SENTINEL, so no obligations for our code | Accept. Podman itself is Apache-2.0. **Docker Desktop is not used** (decision D7), which avoids its commercial subscription terms |
| **OrbStack** | Proprietary | Paid for commercial use | Not recommended as a requirement |
| **Llama 3.x models** | Llama Community Licence | Attribution, acceptable-use policy, a 700M MAU limit. Not OSI open source | Not a default. Usable for comparison only |
| **Gemma models** | Gemma Terms of Use | Use restrictions (prohibited-use policy) passed on to downstream users | Not a default |
| **Qwen2.5 3B / 72B** (and Qwen2.5-Coder 3B) | Qwen Licence / Qwen Research Licence | Some sizes are not Apache-2.0 | Avoid those sizes. **Qwen3 family is Apache-2.0** |
| **AWS Boto3 / moto** | Apache-2.0 | None | OK (AWS connector, tests) |
| **Podman, Buildah, podman-py** | Apache-2.0 | None | OK (container runtime, image builds, runner API client) |
| **Lima** | Apache-2.0 | None | OK (local test VMs) |
| **OpenTelemetry Collector (contrib) and OCB** | Apache-2.0 | None. Component stability levels vary (not a licence issue) | OK |
| **psycopg 3** (pulled in by `langgraph-checkpoint-postgres`) | LGPL-3.0 | Fine for use as an unmodified dynamically linked library | Accept |
| **CodeQL** | GitHub terms | Free for public repos. Private repos need GitHub Advanced Security | Use only if the repo is public (decision D8) |
| **Locust** | MIT | — | OK |

### Core dependencies (permissive licences)

| Area | Packages (licence) |
|---|---|
| Backend | Python (PSF), FastAPI (MIT), Starlette (BSD-3), Pydantic + pydantic-settings (MIT), SQLAlchemy (MIT), Alembic (MIT), asyncpg (Apache-2.0), httpx (BSD-3), structlog (MIT/Apache-2.0), argon2-cffi (MIT), cryptography (Apache-2.0/BSD), uvicorn (BSD-3), sse-starlette (BSD-3), podman-py (Apache-2.0, runner only), PyYAML (MIT) |
| AI | LangGraph + langgraph-checkpoint-postgres (MIT), Ollama (MIT), llama.cpp (MIT, via Ollama), Qwen3 models (Apache-2.0), nomic-embed-text v1.5 (Apache-2.0), bge-m3 (MIT, alternative) |
| Data | PostgreSQL (PostgreSQL licence), pgvector (PostgreSQL licence) |
| Observability | OpenTelemetry SDKs and Collector (Apache-2.0), Prometheus + Alertmanager + exporters (Apache-2.0), Jaeger (Apache-2.0), cAdvisor (Apache-2.0) |
| Demo app | Celery (BSD-3), Valkey (BSD-3), Locust (MIT) |
| Frontend | React (MIT), Vite (MIT), TypeScript (Apache-2.0), TanStack Query (MIT), React Router (MIT), Tailwind CSS (MIT), shadcn/ui + Radix (MIT), Recharts (MIT), react-diff-view (MIT), react-markdown (MIT), openapi-typescript (MIT) |
| Testing | pytest (MIT), pytest-asyncio (Apache-2.0), Hypothesis (MPL-2.0), testcontainers-python (Apache-2.0), respx (BSD-3), schemathesis (MIT), freezegun (Apache-2.0), Vitest (MIT), Testing Library (MIT), Playwright (Apache-2.0) |
| Tooling | uv (MIT/Apache-2.0), ruff (MIT), mypy (MIT), import-linter (BSD-2), pre-commit (MIT), gitleaks (MIT), pip-audit (Apache-2.0), Trivy (Apache-2.0), checkov (Apache-2.0), tflint (MPL-2.0), lychee (MIT/Apache-2.0), ripgrep (MIT/Unlicense) |
| Infrastructure | OpenTofu (MPL-2.0), Caddy (Apache-2.0), Podman (Apache-2.0), podman-compose (GPL-2.0, tool only), gVisor (Apache-2.0) |

## 2. External dependencies (services)

| Dependency | Needed for | Cost | Failure impact |
|---|---|---|---|
| GitHub (API, repos, Actions, GHCR) | Code investigation, issues and PRs, CI | Free for public repos (Actions minutes are free for public repos) | Code features degrade. Investigation continues without code evidence |
| Ollama model registry | One-time model download | Free | Pre-pulled models are cached on disk |
| Let's Encrypt | TLS | Free | Caddy retries. Certificates last 90 days |
| AWS | Hosting | Paid (see [AWS & costs](2-aws-and-costs.md)) | Demo offline |

## 3. Technical risks

| # | Risk | Likelihood | Impact | Mitigation | Early signal / step |
|---|---|---|---|---|---|
| R1 | **Local model RCA quality is too low** (wrong or vague hypotheses) | Medium | High | Strong deterministic evidence and scoring do much of the work. Concise digests. Prompt iteration on a tuning split. Model comparison. Code findings computed deterministically first | Step 18 baseline |
| R2 | **CPU inference on AWS is too slow** | High | Medium | Small model, asynchronous UX, fewer LLM calls, showcase option B, larger instance as fallback | Step 33 benchmark |
| R3 | **24 GB memory pressure** (14B model + Podman VM stack + IDE) | Medium | Medium | Native Ollama. `podman machine` memory capped at 8 GB. Container memory limits. 8B fallback, **now the default (D2): the Step 15 benchmark measured 14B swapping next to the stack**. `keep_alive` tuning | Steps 5, 15 |
| R4 | Structured output failures or thinking-mode quirks with local models | Medium | Medium | JSON-schema constrained decoding, validation retry, thinking toggle per task, schema-conformance metric | Step 15 |
| R5 | **Evaluation overfitting:** we author both the faults and the detector | High | High (credibility) | Held-out scenarios, a tuning/held-out split, N=5 repeats, publish failures, invite external scenario contributions | Steps 16, 35 |
| R6 | Prompt injection through logs or code | Medium | High | Capability containment, validation, human approval, S09 evaluation ([Security](../2-architecture/6-security.md)) | Steps 18, 32 |
| R7 | Sandbox escape or abuse | Low | Critical | Hardened container, runner isolation, gVisor on AWS, security tests | Step 29 |
| R8 | Container engine socket in the runner | Medium | High (reduced by rootless Podman) | **Rootless Podman** (socket ≠ host root), minimal API, allowlists, HMAC, no LLM dependency, internal network only | Step 28 |
| R9 | LangGraph API churn | Medium | Low | Use only the core StateGraph API. Pin versions. Thin wrapper | Step 18 |
| R10 | Observability stack config complexity (Collector, Loki versions) | Medium | Medium | Pin image digests. Integration test of the telemetry flow | Step 5 |
| R11 | Alert threshold noise (false positives) | Medium | Medium | Measured FP rate, soak test, baseline-relative rules | Steps 10, 16 |
| R12 | **Scope creep / time overrun** | High | High | Strict MoSCoW. Should steps are deferrable. Step-by-step approval gates | Every step |
| R13 | GitHub API rate limits or token scope mistakes | Low | Medium | Local mirrors (git operations don't use REST quota), backoff, minimal scopes, separate read and write tokens | Step 20 |
| R14 | AWS cost overrun (forgotten resources, egress) | Low | Medium | Budgets alarms, no NAT or ALB, IaC teardown (`tofu destroy`), scheduled stop | Step 33 |
| R15 | Disk growth (Loki, Jaeger, Prometheus) on EC2 | Medium | Medium | Retention limits, disk alarm, load generator throttling | Step 35 |
| R16 | Demo app too artificial, so results don't generalise | Medium | Medium | Realistic failure modes from common incident classes. Documented limitations. Generic OTLP onboarding (Could) | Steps 4, 35 |
| R18 | **Agent install friction** on diverse distros (paths, permissions, journald access) | Medium | Medium | Support two distro families in MVP. Container variant. Idempotent installer. Clear errors. VM-based CI-like tests | Step 8 |
| R19 | **OTel Collector component maturity** (journald, docker_stats against the Podman API, podman_stats alpha/beta) and config churn | Medium | Medium | Pin versions. Validate configs in CI. Re-check stability levels at implementation. Fall back to filelog where needed | Step 8 |
| R20 | **Telemetry volume and cardinality** from real hosts overwhelms one instance | Medium | High | Watched-process allowlist (no all-process scraping), per-integration caps, retention, measured capacity, ingestion load test | Steps 6, 33 |
| R21 | **SSRF via probes** | Medium | High | Isolated prober network, resolve-validate-pin, security test suite | Step 9 |
| R22 | Ingestion endpoint exposed publicly | Medium | Medium | Auth before Collector, rate limits, body limits, separate hostname, monitoring of 401/429 rates | Step 6 |
| R24 | **Podman compatibility gaps** (podman-compose vs Compose spec, Testcontainers' Ryuk, multi-arch builds, `docker_stats` against the Podman API) | Medium | Medium | Keep Compose files to the portable subset. `TESTCONTAINERS_RYUK_DISABLED=true` with `DOCKER_HOST` set to the Podman socket. `podman build --platform … --manifest` for multi-arch. Spike in Step 1. Quadlet (systemd) as the production fallback on EC2 | Step 1 |
| R23 | Breadth temptation (too many integrations, each shallow) | High | High | Strict MoSCoW I-section. One common pipeline. New integrations only through the connector or agent template patterns | Every step |
| R17 | Machine prerequisites missing (system Python 3.9, no Podman or Ollama yet) | Certain | Low | uv-managed Python 3.12. Install steps in Step 1 setup guide | Step 1 |
