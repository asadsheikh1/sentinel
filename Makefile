# SENTINEL developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE   := podman compose -f deploy/compose/compose.yaml
# Core + telemetry stores (Prometheus, Alertmanager, Loki, Jaeger).
COMPOSE_OBS := podman compose -f deploy/compose/compose.yaml -f deploy/compose/compose.observability.yaml
PROM_IMAGE := $(shell grep -o 'docker.io/prom/prometheus:[^ ]*' deploy/compose/compose.observability.yaml)
# Fails the target if the one-shot database migration did not succeed.
WAIT_MIGRATE = @test "$$(podman wait sentinel_migrate_1)" = "0" || { echo "Database migration failed:"; podman logs sentinel_migrate_1; exit 1; }
BACKEND   := cd backend &&
FRONTEND  := cd frontend &&
# Testcontainers talks to Podman through its Docker-compatible API socket.
PODMAN_SOCK = $(shell podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null)
INTEGRATION_ENV = DOCKER_HOST=unix://$(PODMAN_SOCK) TESTCONTAINERS_RYUK_DISABLED=true

.PHONY: help install dev dev-frontend test test-integration test-db test-all lint format build \
        up down logs ps clean migrate migration db-current db-docs db-shell db-reset \
        rbac-docs create-admin obs-up obs-down obs-ps obs-stats test-obs test-rules \
        stack-up stack-up-fast stack-down test-gateway test-agent

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install backend + frontend dependencies and git hooks
	$(BACKEND) uv sync --locked
	$(FRONTEND) npm ci
	uvx pre-commit install

dev: ## Run the API locally with auto-reload on http://localhost:8000
	$(BACKEND) uv run sentinel api --reload

dev-frontend: ## Run the web UI on http://localhost:5173 (proxies to the API)
	$(FRONTEND) npm run dev

test: ## Unit tests (backend + frontend). Coverage gate is enforced by test-all / CI
	$(BACKEND) uv run pytest --cov --cov-fail-under=0
	$(FRONTEND) npm test

test-integration: ## Tests that start real containers via Podman (Testcontainers)
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m integration

test-db: ## Database tests against a throwaway pgvector Postgres (Testcontainers)
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m db

test-all: ## Every backend test (unit + db + integration) with the coverage gate
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m "not observability and not gateway and not agent and not ollama" --cov

lint: ## Lint, format check, type check, architecture contracts
	$(BACKEND) uv run ruff check . ../scripts && uv run ruff format --check . ../scripts && uv run mypy && uv run mypy --strict ../scripts && uv run lint-imports
	$(FRONTEND) npm run lint && npm run format:check && npm run typecheck

format: ## Auto-format backend and frontend
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .
	$(FRONTEND) npm run format

build: ## Build the frontend bundle and the API image
	$(FRONTEND) npm run build
	podman build -t localhost/sentinel-api:dev -f backend/Containerfile .

up: ## Build, apply migrations and start Postgres + API in Podman (data volume is kept)
	$(COMPOSE) build
	@# podman-compose only recreates a container when its *config* changes, not its image,
	@# so force it: otherwise `migrate` and `api` keep running the previous build.
	$(COMPOSE) up -d --force-recreate
	$(WAIT_MIGRATE)

down: ## Stop the containers (data volume is kept)
	$(COMPOSE) down

obs-up: ## Start the telemetry stores (Prometheus :9090, Loki :3100, Jaeger :16686, Alertmanager :9093)
	$(COMPOSE_OBS) up -d --no-deps prometheus alertmanager loki jaeger

obs-down: ## Stop the telemetry stores (data kept)
	$(COMPOSE_OBS) stop prometheus alertmanager loki jaeger

obs-ps: ## Status of the telemetry stores
	$(COMPOSE_OBS) ps

obs-stats: ## Memory use of the telemetry stores (budget 2.5 GB)
	podman stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep -E 'prometheus|alertmanager|loki|jaeger'

stack-up: ## Start everything: Postgres, API, telemetry stores, gateway (ingest on :4319)
	$(COMPOSE_OBS) build
	$(COMPOSE_OBS) up -d --force-recreate
	$(WAIT_MIGRATE)

stack-up-fast: ## Same, with short health intervals (used by test-gateway)
	$(COMPOSE_OBS) build
	SENTINEL_INTEGRATION_FLUSH_INTERVAL_S=1 SENTINEL_INTEGRATION_STALE_AFTER_S=8 \
	SENTINEL_ENTITY_DISCOVERY_INTERVAL_S=3 SENTINEL_HEALTH_CHECK_MIN_INTERVAL_S=5 \
	SENTINEL_PROBE_STATE_INTERVAL_S=2 SENTINEL_PROBER_CONFIG_REFRESH_S=3 SENTINEL_PROBER_HEARTBEAT_S=3 SENTINEL_GAP_INTERVAL_S=2 \
	SENTINEL_ERROR_POLL_INTERVAL_S=3 SENTINEL_ERROR_POLL_LAG_S=5 SENTINEL_ERROR_GROUP_QUIET_S=60 \
	SENTINEL_CORRELATION_INTERVAL_S=2 SENTINEL_DEPENDENCY_DISCOVERY_INTERVAL_S=5 SENTINEL_MITIGATE_AFTER_S=20 \
	SENTINEL_ALERT_RECONCILE_INTERVAL_S=10 SENTINEL_LOGIN_RATE_LIMIT_PER_MINUTE=60 \
	$(COMPOSE_OBS) up -d --force-recreate
	$(WAIT_MIGRATE)

stack-down: ## Stop everything (data kept)
	$(COMPOSE_OBS) --profile web down

ui: ## Build the web UI and serve it with the API on http://127.0.0.1:3000 (needs stack-up)
	$(FRONTEND) npm run build
	$(COMPOSE_OBS) --profile web up -d --no-deps --force-recreate web

test-e2e: ## Browser end-to-end tests (needs stack-up-fast and `make ui`)
	$(FRONTEND) npx playwright test

test-gateway: ## End-to-end ingestion tests (needs stack-up-fast)
	$(BACKEND) uv run pytest -m gateway -v

test-agent: ## Install the SENTINEL Agent on a disposable Ubuntu 24.04 systemd machine (needs stack-up-fast)
	podman build -q -t localhost/sentinel-test-ubuntu:24.04 -f backend/tests/agent/Containerfile.systemd backend/tests/agent
	$(BACKEND) uv run pytest -m agent -v

test-ollama: ## The LLM gateway against the local Ollama (needs `ollama serve` and qwen3:4b, nomic-embed-text:v1.5)
	$(BACKEND) uv run pytest -m ollama -v

bench-models: ## Benchmark the candidate models on this machine (needs `ollama serve` and the models pulled)
	$(BACKEND) caffeinate -dims uv run python ../scripts/bench_models.py $(ARGS)

test-obs: ## Push synthetic OTLP into each store and query it back (needs obs-up)
	$(BACKEND) uv run pytest -m observability -v

RULE_PACK_TESTS := host_test.yml container_test.yml service_test.yml database_test.yml endpoint_test.yml integration_test.yml

test-rules: ## Unit-test the Prometheus recording and alert rules (rule packs) with promtool
	podman run --rm --entrypoint promtool -v ./deploy/prometheus:/p:ro,Z $(PROM_IMAGE) test rules /p/tests/red_test.yml
	podman run --rm --entrypoint promtool -v ./deploy/prometheus:/p:ro,Z -w /p/tests/packs $(PROM_IMAGE) test rules $(RULE_PACK_TESTS)
	podman run --rm --entrypoint promtool -v ./deploy/prometheus:/p:ro,Z $(PROM_IMAGE) check rules /p/rules/red.yml /p/rules/packs/host.yml /p/rules/packs/container.yml /p/rules/packs/service.yml /p/rules/packs/database.yml /p/rules/packs/endpoint.yml /p/rules/packs/integration.yml

logs: ## Follow container logs
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

migrate: ## Apply database migrations (needs `make up`; connects as sentinel_migrator)
	$(BACKEND) uv run sentinel db upgrade

migration: ## Autogenerate a migration from model changes: make migration m="add foo"
	@test -n "$(m)" || (echo 'usage: make migration m="describe the change"'; exit 1)
	$(BACKEND) uv run sentinel db revision -m "$(m)"

db-current: ## Show the applied migration revision
	$(BACKEND) uv run sentinel db current

rbac-docs: ## Regenerate docs/rbac.md (roles, scopes, every route's access rule)
	$(BACKEND) uv run sentinel rbac-docs

create-admin: ## Create the first admin: make create-admin email=you@example.com name="Your Name"
	@test -n "$(email)" -a -n "$(name)" || (echo 'usage: make create-admin email=... name="..."'; exit 1)
	$(BACKEND) uv run sentinel create-admin --email "$(email)" --name "$(name)"

# openapi-typescript needs TypeScript 5's compiler API (the project is on TypeScript 7),
# so it runs as a pinned tool rather than a project dependency.
OPENAPI_TS = npx --yes -p openapi-typescript@7.13.0 -p typescript@5.9.3 openapi-typescript
api-client: ## Regenerate the frontend's typed API client from the backend's OpenAPI schema
	$(BACKEND) uv run sentinel openapi
	$(FRONTEND) $(OPENAPI_TS) src/api/openapi.json -o src/api/schema.d.ts

db-docs: ## Regenerate docs/database.md from the models
	$(BACKEND) uv run sentinel db docs

db-shell: ## Open psql inside the Postgres container (as superuser)
	podman exec -it sentinel_postgres_1 psql -U sentinel -d sentinel

db-reset: ## DELETES all local data: remove containers + volume, start fresh (roles + migrations)
	$(COMPOSE) down -v
	$(MAKE) up

clean: ## Remove local caches and build output (not containers or volumes)
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache backend/.coverage frontend/dist
