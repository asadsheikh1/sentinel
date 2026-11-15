# 10 — Repository Structure, Documentation and CI

## 1. Repositories

| Repo | Contents | Why separate |
|---|---|---|
| `sentinel` (this repo) | SENTINEL platform, infrastructure, docs, evaluations | — |
| `aviro-shop` (**recommended separate repo**; decision D1) | The monitored demo application, its tests, fault scenarios as **real commits and tags** (`scenario/S01-bad`, `scenario/S01-fix`) | Code investigation needs a genuine GitHub repo with real commit history, deployments, and PRs. Bundling it in the SENTINEL repo would mix histories and make "suspicious commit" analysis artificial |

## 2. `sentinel` monorepo layout

```
sentinel/
├── README.md                     # pitch, demo GIF, architecture image, quickstart, eval results table
├── LICENSE                       # Apache-2.0 (recommended; decision D8)
├── SECURITY.md                   # vuln reporting, threat model summary
├── CONTRIBUTING.md
├── Makefile                      # dev, test, lint, eval, db-docs, up, down
├── .env.example
├── .pre-commit-config.yaml       # ruff, mypy (fast), gitleaks, prettier, eslint
├── .github/
│   ├── workflows/                # see §4
│   ├── dependabot.yml
│   └── pull_request_template.md
├── backend/
│   ├── pyproject.toml            # uv-managed; ruff, mypy, pytest config
│   ├── uv.lock
│   ├── Containerfile
│   ├── alembic.ini
│   ├── src/sentinel/             # packages per docs/03 §3
│   │   └── db/migrations/
│   └── tests/
│       ├── unit/  db/  api/  integration/  workflows/  security/  recovery/
│       └── fixtures/             # alertmanager payloads, github responses, telemetry
├── agent/                        # SENTINEL Agent packaging (OTel Collector): templates/, install.sh, uninstall.sh, Containerfile, ocb/ (later)
├── runner/                       # privileged broker (separate minimal package + Containerfile)
│   ├── src/sentinel_runner/
│   ├── allowlist.example.yaml
│   └── tests/
├── sandbox-images/
│   └── python312/Containerfile      # pinned base for test sandboxes
├── frontend/
│   ├── package.json, package-lock.json, vite.config.ts, tsconfig.json
│   ├── src/ (api/ generated client, pages/, components/, hooks/, lib/)
│   └── tests/ (vitest) , e2e/ (playwright)
├── evals/
│   ├── README.md                 # methodology, target revisions log
│   ├── scenarios/S01…/scenario.yaml
│   ├── fixtures/                 # recorded telemetry per scenario run
│   ├── harness/                  # (inside backend package: sentinel.evals) runner + metrics
│   └── results/<date>_<sha>_<model>/
├── deploy/
│   ├── compose/
│   │   ├── compose.yaml          # sentinel core
│   │   ├── compose.observability.yaml
│   │   ├── compose.demo.yaml     # pulls AVIRO shop images
│   │   └── compose.prod.yaml     # overrides for EC2
│   ├── otel/gateway.yaml         # ingestion gateway collector
│   ├── prometheus/{prometheus.yml,rules/*.yml}
│   ├── alertmanager/alertmanager.yml
│   ├── loki/loki.yaml
│   ├── caddy/Caddyfile
│   ├── grafana/ (optional, Could)
│   ├── infra/                    # OpenTofu: modules/, envs/prod/
│   └── scripts/ (deploy.sh, backup.sh, restore.sh, bootstrap-ec2.sh)
├── scripts/                      # dev helpers: seed, load-gen wrappers, model pull
└── docs/                         # see §3
```

## 3. Documentation plan

| Required document | Location | Created in step |
|---|---|---|
| README.md | `/README.md` | 1 (skeleton), updated each release |
| Architecture documentation | `docs/2-architecture/1-system-architecture.md` (+ `docs/architecture/` diagrams as they evolve) | Planning; updated per step |
| API documentation | `docs/2-architecture/5-api.md` + generated `docs/api/openapi.json` + `/api/v1/docs` (dev) | 2+, generated in CI |
| Database documentation | `docs/2-architecture/4-database.md` + generated `docs/database.md` | 2 |
| AI agent documentation | `docs/2-architecture/3-ai-agents.md` + `docs/prompts.md` (prompt versions) | 15, 18 |
| Setup instructions | `docs/guides/setup-local.md` | 1, 5, 8, 15 |
| Environment variable examples | `.env.example` + `docs/guides/configuration.md` | 1+ |
| Container (Podman) configuration | `deploy/compose/*` + `docs/guides/podman.md` | 1, 5 |
| Test instructions | `docs/guides/testing.md` | 1+ |
| Deployment instructions | `docs/guides/deploy-aws.md` | 33–34 |
| Security documentation | `docs/2-architecture/6-security.md` + `SECURITY.md` | Planning, 32 |
| Evaluation methodology | `docs/3-quality/1-testing-and-evaluation.md` + `evals/README.md` | Planning, 16 |
| Roadmap | `docs/4-plan/1-roadmap.md` | Planning |
| Architecture decision records | `docs/adr/NNNN-title.md` (MADR-lite format) | Planning, as needed |
| Step reports | `docs/progress/step-NN.md` (what changed, commands, test results, limitations) | Every step |

## 4. GitHub Actions workflows

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci-backend.yml` | PR, push to main (paths: backend/**) | `uv sync --locked` → ruff check + ruff format --check → mypy → pytest (unit, db, api, integration, workflows, security) with a pgvector service container → coverage report and gate → import-linter contracts |
| `ci-agent.yml` | PR (paths: agent/**) | Render every agent config template → `otelcol validate`. Installer shellcheck. Install test in a systemd container. Socket-proxy denial test |
| `ci-runner.yml` | PR (paths: runner/**, sandbox-images/**) | lint, type check, unit tests, **sandbox security tests** (real rootless Podman on the ubuntu runner, which has Podman preinstalled) |
| `ci-frontend.yml` | PR (paths: frontend/**) | `npm ci` → eslint → `tsc --noEmit` → vitest → `vite build` → check that the OpenAPI-generated client is up to date |
| `build-images.yml` | PR (build only), main (build + push) | `podman build --platform linux/amd64,linux/arm64 --manifest` (qemu) for api/worker, runner, frontend. Trivy scan (fail on critical) |
| `security.yml` | PR + weekly | gitleaks, pip-audit, npm audit (high+), CodeQL (free for public repos), OpenTofu `validate` + `tflint` + `checkov` (or trivy config) |
| `e2e.yml` | nightly, main, manual | `podman compose up` (FakeLLM mode, fixture telemetry) → Playwright → failure and recovery tests → upload traces and screenshots |
| `docs.yml` | PR (docs/**) | markdownlint, link check (lychee), Mermaid render check |
| `deploy.yml` | manual `workflow_dispatch`, `production` environment with required reviewer | OIDC → build/push arm64 → SSM deploy → smoke test → report |

Branch protection on `main`: required checks, linear history, no direct pushes. Conventional Commits. Dependabot for pip (uv), npm, container base images, and Actions.

Note: GitHub-hosted runners have no GPU and limited RAM, so **no real LLM runs in CI**. AI evaluation runs locally (see [Testing §6](../3-quality/1-testing-and-evaluation.md#6-ci-policy)).
