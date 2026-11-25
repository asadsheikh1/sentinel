# SENTINEL — Autonomous AI Site Reliability Engineer

**SENTINEL is an AI SRE in active development. The goal: watch your systems, detect incidents, explain the likely root cause with evidence you can click, and propose fixes (rollback, GitHub issue, tested patch) that never run without human approval.**

- **Local, open-weight models (planned).** Investigation will run on models served by [Ollama](https://ollama.com) (Qwen3 planned as default). No paid AI APIs.
- **Evidence or it didn't happen (planned).** Every root-cause hypothesis will have to cite telemetry, deploy or code evidence IDs; uncited hypotheses will be dropped.
- **Safe by design (planned).** Untrusted code will run only in network-isolated rootless [Podman](https://podman.io) sandboxes. Nothing is merged or deployed automatically.
- **Measured, not claimed (planned).** AI quality will be scored against fault-injection scenarios on the [AVIRO demo shop](https://github.com/asadsheikh1/aviro-shop).

> **Status: in active development (Step 14 of 35 in review, 25 Nov 2026).** Built: the telemetry pipeline (agent, prober, OTLP gateway, Prometheus/Loki/Jaeger, query adapters with redaction), auth (Argon2id, RBAC, API keys, audit log) and the first detection layer (alert rule packs, signals, deployment and GitHub webhook ingestion, log error grouping by fingerprint, and entity-aware correlation into incidents with severity, lifecycle and an append-only timeline), and the web UI: sign-in, incidents and their timeline with role-aware controls, and onboarding (an integrations catalogue, setup wizards with a live verification checklist, and the inventory). Planned: LangGraph root-cause agents, pgvector incident memory, the repair sandbox, the prompt-injection test suite and published precision/recall. See the [roadmap](docs/4-plan/1-roadmap.md) and [step reports](docs/progress/).

## What's built vs planned

| Area | Status | Notes |
|---|---|---|
| Repo, CI, secret scanning | ✅ Built | ruff, mypy (strict), import-linter contracts, oxlint, tsc, gitleaks; CI for backend, frontend, gateway, agent, observability |
| Database | ✅ Built | Async SQLAlchemy 2, Alembic migrations, Testcontainers DB tests |
| Auth & access control | ✅ Built | Argon2id passwords, RBAC, scoped API keys, audit log, rate limiting |
| AVIRO demo shop | ✅ Built | FastAPI microservices, PostgreSQL, Celery + Valkey, Locust load, fault injection ([repo](https://github.com/asadsheikh1/aviro-shop)) |
| Telemetry stores | ✅ Built | Prometheus (OTLP receiver), Alertmanager, Loki, Jaeger v2 |
| OTLP ingestion gateway | ✅ Built | Per-integration ingest tokens, authentication, normalisation |
| Telemetry query adapters | ✅ Built | Typed, size-capped Prometheus/Loki/Jaeger queries with secret redaction |
| SENTINEL Agent | ✅ Built | OpenTelemetry Collector (pinned) for Linux hosts and Podman/Docker containers; data-only, never runs commands |
| External health checks | ✅ Built | Isolated prober container with an SSRF guard (private, loopback, link-local, metadata, CGNAT ranges; DNS-rebinding and redirect re-validation) |
| Detection: rule packs & signals | ✅ Built | 21 generic Prometheus alerts + 9 recording rules (promtool-tested), Alertmanager → signals, telemetry-gap classification, deployment ingestion, GitHub webhooks (HMAC-verified) |
| Correlation & incidents | 🔜 Planned (M3) | Log error fingerprinting, entity-aware correlation, incidents UI |
| AI investigation | 🔜 Planned (M4) | Local LLM gateway + benchmark, evaluation harness, LangGraph RCA graph with citation validation |
| Code & memory | 🔜 Planned (M5) | Culprit-commit search, pgvector incident memory, reports |
| Response & repair | 🔜 Planned (M6) | Human approval workflow, GitHub issues, sandboxed patch tests, draft PRs |
| Production & published eval | 🔜 Planned (M7) | Prompt-injection suite, AWS via OpenTofu, published precision/recall and RCA accuracy |

## Architecture

```
 monitored systems                         SENTINEL
 ┌──────────────────┐  OTLP + token   ┌──────────────────┐     ┌────────────┐
 │ hosts/containers │ ──────────────▶ │ ingestion gateway│ ──▶ │ Prometheus │
 │ (OTel Collector) │                 │ auth · normalise │ ──▶ │ Loki       │
 │ instrumented apps│                 └──────────────────┘ ──▶ │ Jaeger     │
 └──────────────────┘                                         └─────┬──────┘
 ┌──────────────────┐   probe results (OTLP)                        │ query adapters
 │ prober (isolated)│ ─────────────────────────────────────────▶    ▼ (redacted, capped)
 └──────────────────┘                              ┌───────────────────────────────┐
                                                   │ FastAPI API · Postgres · RBAC │
   planned ▶ detection → evidence → RCA graph      │ audit log · API keys          │
             (local LLM, cited) → human approval   └───────────────────────────────┘
```

Design decisions are recorded as ADRs in [`docs/adr/`](docs/adr/) — e.g. modular monolith, local inference via Ollama, Postgres + pgvector, evidence-cited hypotheses, OTel Collector as the agent, own SSRF-safe prober, Podman runtime.

## AI design (planned, M4–M6)

Only three components are LLM-driven; everything that must be predictable stays deterministic.

| Component | Role | Guardrails |
|---|---|---|
| **RCA Analyst** | Ranks ≤5 root-cause hypotheses from an evidence digest | Structured output schema; may only *request* extra evidence through validated requests; citations validated, uncited hypotheses dropped |
| **Code Investigator** | Finds the likely culprit commit and code location | Read-only repo mirror; bounded search rounds; capped excerpts |
| **Patch Engineer** *(optional)* | Proposes a fix | Tested only in an isolated sandbox; PR is a draft; human approval required |

Monitoring, triage, test execution and approval are deliberately **not** agents.

## Evaluation plan

Scenario suite on AVIRO with ground truth (S01–S11+): bad deploys (`KeyError`, N+1 query), config pool exhaustion, Celery schema break, dependency timeout, memory leak, broker outage, a **negative** traffic-spike case, and a **prompt-injection** case. Metrics: detection precision/recall, RCA top-1/top-3 accuracy, MRR, citation validity, calibration, latency and tokens. Results will be committed with provenance. Results will be published here once the evaluation steps land.

## Quickstart (macOS, Apple Silicon)

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 24+, Podman with `podman machine` running, podman-compose. See [docs/guides/setup-local.md](docs/guides/setup-local.md).

```bash
make install          # backend + frontend deps, git hooks
make dev              # API on http://localhost:8000
curl localhost:8000/healthz
make dev-frontend     # web UI on http://localhost:5173
make test             # unit tests
make test-integration # tests that start real containers via Podman
make lint
make up               # Postgres + migrations + API in Podman
make down
```

Run `make help` for every command.

## Repository layout

| Path | What |
|---|---|
| `backend/` | Python 3.12 API and services (FastAPI, uv) |
| `frontend/` | Web UI (React 19, Vite, TypeScript) |
| `deploy/` | Podman Compose files, Prometheus rule packs |
| `docs/` | Plan, architecture, ADRs, step reports. Start at [docs/README.md](docs/README.md) |
| `.github/workflows/` | CI: backend, frontend, gateway, agent, observability, secret scanning |

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL 17 · OpenTelemetry · Prometheus · Alertmanager · Loki · Jaeger · Caddy · React 19 · Vite · TypeScript · Podman · GitHub Actions · *planned:* LangGraph · Ollama (Qwen3) · pgvector · OpenTofu · AWS

## Licence

[Apache-2.0](LICENSE) · Built by [Asad Sheikh](https://github.com/asadsheikh1) · [LinkedIn](https://www.linkedin.com/in/asadsheikh1/)
