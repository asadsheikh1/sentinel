# 13 — Recommended Technology Stack

Every major technology, why it is needed, and what was rejected. Items from the brief's suggested list that are **not** used (or are used only in a limited way) are explained in §3.

## 1. Selected stack

| Layer | Technology | Purpose in SENTINEL | Why this choice |
|---|---|---|---|
| Language (backend) | **Python 3.12** (managed by **uv**) | All backend services, agents, evaluation | Best ecosystem for LLM orchestration, OTel, data processing. uv gives fast, locked, reproducible environments (system Python is 3.9.6) |
| Web framework | **FastAPI** + Uvicorn | REST API, webhooks, SSE | Async, Pydantic-native validation, automatic OpenAPI (feeds the generated TS client and API docs) |
| Validation / settings | **Pydantic v2**, pydantic-settings | API schemas, **LLM output schemas**, config | One schema system for API and LLM structured output |
| ORM / migrations | **SQLAlchemy 2 (async) + Alembic**, asyncpg | Data access, schema evolution | Mature, typed, explicit SQL when needed (pgvector) |
| Database | **PostgreSQL 17 + pgvector** | App data, evidence, job queue, LangGraph checkpoints, embeddings | One datastore covers relational, vector, queue, and checkpoint needs. Fewer moving parts and lower cost |
| Job queue | **Postgres-backed queue** (in-house, ~200 LOC, `SKIP LOCKED` + `LISTEN/NOTIFY`) | Investigations, repairs, reports, embeddings, action execution | Transactional enqueue with domain writes. No Redis or Celery to operate. Volume is tiny ([ADR-0006](../adr/0006-postgres-job-queue.md)) |
| Agent orchestration | **LangGraph** (core `StateGraph` + Postgres checkpointer) | Investigation and repair workflows only | Explicit graphs, bounded loops, resumable checkpoints, streaming. Used narrowly ([Architecture §14](../2-architecture/1-system-architecture.md#14-where-langgraph-is-useful-and-where-plain-python-is-better)) |
| Local LLM runtime | **Ollama** (native on macOS for Metal, container or native on EC2) | Chat and embedding inference | Simple model management, JSON-schema structured outputs, OpenAI-compatible endpoint (swappable for llama.cpp server), MIT |
| Reasoning model | **Qwen3 14B** local / **Qwen3 4B–8B** AWS CPU (to be confirmed in Step 15) | RCA, code investigation, patching, reports | Apache-2.0, strong reasoning and coding for its size, thinking toggle, fits 24 GB |
| Embedding model | **nomic-embed-text v1.5** | Incident memory similarity | Apache-2.0, small (~137M params), 768-d, good retrieval quality, available in Ollama |
| Telemetry SDK | **OpenTelemetry** (Python SDK + auto-instrumentation) | Demo app and SENTINEL self-instrumentation | Vendor-neutral standard. One SDK for logs, metrics, and traces |
| Telemetry pipeline | **OTel Collector (contrib)** | Receive, process, sample, route telemetry | Decouples apps from backends. Tail sampling keeps error traces |
| Metrics | **Prometheus + Alertmanager** | Metric storage, PromQL, recording and alert rules, webhook to SENTINEL | Industry standard, tiny footprint, rule testing with `promtool` |
| Logs | **Grafana Loki** (AGPL, unmodified) | Log storage and LogQL queries | Lightweight label-indexed log store. Native OTLP ingestion. Much lighter than OpenSearch/Elasticsearch |
| Traces | **Jaeger v2** (Badger storage) | Trace storage, search, UI | Apache-2.0, OTel-native, single binary, query API |
| Monitoring agent | **OpenTelemetry Collector (contrib), pinned**, packaged with SENTINEL config templates, installer, and systemd unit. Later a slim **OCB** build | Host, process, journald, file-log, Podman/Docker container, and DB collection on monitored machines | Mature, signed, vendor-neutral, and already has every receiver needed. Writing our own agent would add months and risk ([ADR-0013](../adr/0013-otel-collector-as-agent.md)) |
| Ingestion gateway | **Caddy `forward_auth` + gateway OTel Collector** | Token auth, identity enrichment, normalisation (OTTL), redaction, routing | Auth stays in SENTINEL's own code. The Collector does the heavy telemetry processing ([ADR-0014](../adr/0014-otlp-first-ingestion-gateway.md)) |
| External probes | **In-house Python prober** (httpx + SSRF guard) | HTTP, TCP, and TLS checks | `blackbox_exporter` can't enforce resolve-validate-pin SSRF rules. The prober is small ([ADR-0015](../adr/0015-own-ssrf-safe-prober.md)) |
| Cloud connectors | **boto3** + STS AssumeRole (Should). **moto** for tests | AWS inventory, metrics, change events | Official SDK. No static keys |
| Local test VMs | **Lima** Ubuntu VMs (or a second `podman machine`) | Realistic agent install tests on the Mac | Free and open source. Matches the systemd target |
| Code search | **git CLI + ripgrep** on local bare mirrors | Commit windows, diffs, tracebacks → code, search | Fast, offline, no API quota, easy to confine |
| GitHub | **GitHub REST API** (httpx) | Repo metadata, issues, draft PRs, webhooks | Required integration. Fine-grained tokens (read and write split) |
| Frontend | **React 19 + Vite + TypeScript**, TanStack Query, React Router, Tailwind + shadcn/ui, Recharts, react-diff-view | Web UI | SPA is enough (no SSR or SEO need). Fast builds. Typed client generated from OpenAPI |
| Reverse proxy / TLS | **Caddy** | HTTPS (auto Let's Encrypt), static files, `/api` proxy, security headers | Zero-config TLS. Simpler than nginx + certbot |
| Containers | **Podman** (rootless) + **podman-compose**. `podman machine` on macOS | Dev and prod packaging, sandbox, CI image builds | Daemonless, rootless by default (a smaller blast radius for the runner and sandbox), no Docker Desktop. Same Compose files locally and on EC2. Docker-compatible API, so standard tools still work ([ADR-0018](../adr/0018-podman-runtime.md)) |
| Sandbox | Rootless Podman hardened containers via the **runner** broker. **gVisor** on AWS (Should) | Untrusted test execution | Strong isolation for the cost. No VM orchestration needed |
| Demo app (**AVIRO** shop) | FastAPI services, **Celery + Valkey**, Postgres, **Locust** | The monitored system and evaluation substrate | Realistic HTTP, DB, and background-job failure modes. Valkey avoids Redis licence issues |
| Testing | pytest, Hypothesis, Testcontainers, respx, schemathesis, Vitest, Playwright | See [Testing](../3-quality/1-testing-and-evaluation.md) | Standard, free, CI-friendly |
| Quality | ruff, mypy, import-linter, eslint, prettier, pre-commit | Lint, types, architectural contracts | import-linter enforces "LLM code cannot reach credentials or executor" |
| Security tooling | gitleaks, pip-audit, npm audit, Trivy, CodeQL (public repo), checkov | Supply-chain and config scanning | All free |
| CI/CD | **GitHub Actions** (+ OIDC to AWS) | Lint, test, build, scan, deploy | Native to GitHub. Free for public repos. No static AWS keys |
| IaC | **OpenTofu** | AWS provisioning | Open-source (MPL-2.0) Terraform fork. Avoids the BSL licence |
| Cloud | **AWS EC2 (Graviton), EBS, S3, SSM, IAM, Route 53, CloudWatch, Budgets** | Hosting | Cheapest credible single-node setup ([AWS & costs](2-aws-and-costs.md)) |

## 2. Python LLM-adjacent libraries deliberately *not* used
- **LangChain model wrappers / chains:** not needed. A thin httpx gateway gives full control over structured output, timeouts, and logging. LangGraph does not require LangChain models.
- **LangSmith:** a hosted, paid service. LLM telemetry goes to our own `llm_calls` table and OTel spans instead.
- **Langfuse / Arize Phoenix:** heavy to self-host (Langfuse needs ClickHouse and Redis). Phoenix uses the Elastic Licence 2.0. They can be revisited post-v1. The in-house evaluation harness covers our metrics.
- **Vector DBs (Qdrant, Chroma, Weaviate):** unnecessary at this scale. pgvector keeps vectors transactional with incidents.

## 3. Brief's suggested technologies — verdicts

| Suggested | Verdict | Reason |
|---|---|---|
| Python, FastAPI, PostgreSQL, pgvector, Ollama, Qwen, LangGraph, containers (Podman instead of Docker), OpenTelemetry, Prometheus, Jaeger, GitHub API, GitHub Actions, pytest, React, AWS EC2 | ✅ Used | See above |
| **MCP** | ⏸ Not in v1 (Could: read-only server later) | No internal benefit. See [ADR-0005](../adr/0005-no-mcp-in-v1.md) |
| **Redis** | ❌ Not for SENTINEL. **Valkey** (Redis-compatible) only in the demo app | SENTINEL's queue is in Postgres. Redis licence change |
| **Celery** | ⚠️ Demo app only | Celery is what the *monitored* app uses, so SENTINEL can monitor and act on background jobs. SENTINEL's own jobs use the Postgres queue |
| **Grafana** | ⏸ Optional (Could) | SENTINEL's UI is the product surface. Grafana is a nice operator extra |
| **Next.js** | ❌ | No SSR or SEO need. A Node server adds attack surface and memory. Vite SPA served by Caddy |
| **AWS ECS** | ❌ for MVP | Needs an ALB and possibly NAT (+$50/mo), and is awkward for a memory-heavy Ollama process. EC2 + Podman Compose is simpler and cheaper. Documented as a scaling path |
