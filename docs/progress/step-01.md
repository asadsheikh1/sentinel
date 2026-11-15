# Step 1 report — Repository scaffold, tooling, CI skeleton

**Status:** ✅ done. All acceptance criteria met locally and in CI.
**Branch / PR:** `step-01-scaffold` → [PR #1](https://github.com/asadsheikh1/sentinel/pull/1) (not merged; merging is your decision)
**Date:** 2026-11-15

## What was built

| Area | Files |
|---|---|
| Backend | `backend/pyproject.toml`, `uv.lock`, `src/sentinel/{__init__,config,log,cli}.py`, `src/sentinel/api/app.py` |
| Backend tests | `tests/unit/test_{health,config,cli}.py`, `tests/integration/test_podman_spike.py` |
| Frontend | `frontend/` (Vite + React 19 + TS): `src/App.tsx`, `src/api/health.ts`, `src/App.test.tsx` |
| Containers | `backend/Containerfile`, `backend/.containerignore`, `deploy/compose/compose.yaml` |
| Tooling | `Makefile`, `.pre-commit-config.yaml`, `.gitignore`, `.env.example` |
| CI | `.github/workflows/{ci-backend,ci-frontend,security}.yml` |
| Docs | `README.md`, `docs/guides/setup-local.md`, this report |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| `make dev` starts the API, `curl localhost:8000/healthz` → 200 | ✅ `{"status":"ok","version":"0.1.0","env":"dev"}`, HTTP 200 |
| `make test` passes | ✅ backend 11 passed (coverage 90%, gate 85%), frontend 2 passed |
| `make lint` passes | ✅ ruff, ruff format, mypy --strict, import-linter (1 contract kept), oxlint, prettier, tsc |
| Frontend builds | ✅ `vite build`, 69 kB gzipped JS |
| CI green on GitHub | ✅ 5/5 checks pass on PR #1: backend lint/types/unit (22 s), integration on rootless Podman (29 s), multi-arch build + smoke test (2 min 13 s), frontend (15 s), gitleaks (6 s) |

## Podman compatibility spike (risk R24)

| Check | Result on M4 (Podman 6.2, libkrun) |
|---|---|
| Testcontainers → pgvector Postgres 17 via Podman socket | ✅ passed (`make test-integration`). Needs `DOCKER_HOST` = machine socket and `TESTCONTAINERS_RYUK_DISABLED=true` |
| `podman compose` (podman-compose 1.6): build, `depends_on: service_healthy`, healthchecks, `read_only`, `cap_drop`, localhost-only ports | ✅ both services reached `healthy`. API runs as uid 10001 |
| Multi-arch manifest `linux/amd64,linux/arm64` | ✅ manifest has both. The amd64 image runs under emulation (`uname -m` = x86_64). The amd64 half took about 7 minutes on the M4 |

**Conclusion:** no blocker found. The Podman decision (ADR-0018) stands.

## Deviations from the plan (small; no architecture change)

1. **oxlint instead of ESLint.** The current Vite template ships oxlint (MIT, much faster). Same role. Runs with `--deny-warnings`.
2. **`httpx2` instead of `httpx` for tests.** Current Starlette deprecates `httpx` in its TestClient.
3. **`testcontainers.community.postgres`.** The old import path is deprecated.
4. **Node.** CI uses Node 24 LTS as planned. Your Mac has Node 26, which also works (`engines: >=24`).
5. **`sentinel-ai-sre.md`** (your original brief) is kept locally and excluded from git through `.git/info/exclude`, so it isn't published. Tell me if you want it committed.

## Problems found

- **CI first run failed** because `astral-sh/setup-uv@v10` doesn't exist (that action publishes only exact tags). Pinned to `v10.3.3`, and CI is green.

- **Podman VM clock drift.** The VM clock was 37 minutes behind the Mac, most likely after the Mac slept. Fixed with `podman machine stop && start`. Documented in the setup guide.
- **One unexplained hang** of `vitest run` during the first `make lint && make test`. I couldn't reproduce it in 3 later runs (each under 1 s). Possibly linked to the Mac sleeping. I'll watch for it.
- **`make up` timing.** One run took 17 minutes of wall-clock time. Other commands in the same window were also stretched, so the Mac was most likely sleeping. The functional result was correct (both containers healthy, HTTP 200). The numbers aren't a valid performance measurement.

## How you verify

```bash
cd "SENTINEL Autonomous AI Site Reliability Engineer"   # the repo root
make install
make lint && make test && make test-integration
make dev                 # terminal 1
curl localhost:8000/healthz
make dev-frontend        # terminal 2 → open http://localhost:5173 ("Healthy — version 0.1.0")
make up && make ps && make down
```

## Not in this step (by design)

Database models and migrations (Step 2), auth (Step 3), Dependabot, pip-audit, npm audit, CodeQL (later security work), the OpenAPI-generated TS client (Step 13).
