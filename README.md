# SENTINEL — Autonomous AI Site Reliability Engineer

SENTINEL connects to your systems, detects incidents, and explains the likely root cause **with evidence you can click**. It suggests fixes (rollback, GitHub issue, tested code patch) and **never acts without human approval**.

- **No paid AI APIs.** It uses open-weight models running locally through [Ollama](https://ollama.com).
- **Safe by design.** Code runs only in isolated rootless [Podman](https://podman.io) sandboxes. Nothing is merged or deployed automatically.
- **Measured, not claimed.** AI quality is evaluated against fault-injection scenarios on the [AVIRO demo shop](https://github.com/asadsheikh1/aviro-shop).

> **Status:** early development. Step 7 of 35 (safe telemetry query adapters and secret redaction). See the [roadmap](docs/4-plan/1-roadmap.md).

## Quickstart (macOS, Apple Silicon)

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 24+, Podman with `podman machine` running, and podman-compose. See [docs/guides/setup-local.md](docs/guides/setup-local.md).

```bash
make install          # backend + frontend deps, git hooks
make dev              # API on http://localhost:8000 (auto-reload)
curl localhost:8000/healthz
make dev-frontend     # web UI on http://localhost:5173 (second terminal)

make test             # unit tests
make test-integration # tests that start real containers via Podman
make lint             # ruff, mypy, import-linter, oxlint, prettier, tsc
make up               # Postgres + migrations + API in Podman containers
make test-db          # database tests (Testcontainers)
make migrate          # apply migrations (after make up)
make down
```

Run `make help` for every command.

## Repository layout

| Path | What |
|---|---|
| `backend/` | Python 3.12 API and services (FastAPI, uv) |
| `frontend/` | Web UI (React 19, Vite, TypeScript) |
| `deploy/compose/` | Podman Compose files |
| `docs/` | Plan, architecture, ADRs, step reports. **Start at [docs/README.md](docs/README.md)** |
| `.github/workflows/` | CI: backend, frontend, secret scanning |

## Licence

[Apache-2.0](LICENSE)
