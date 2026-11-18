# SENTINEL developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE   := podman compose -f deploy/compose/compose.yaml
# Core + telemetry stores (Prometheus, Alertmanager, Loki, Jaeger).
COMPOSE_OBS := podman compose -f deploy/compose/compose.yaml -f deploy/compose/compose.observability.yaml
PROM_IMAGE := $(shell grep -o 'docker.io/prom/prometheus:[^ ]*' deploy/compose/compose.observability.yaml)
BACKEND   := cd backend &&
FRONTEND  := cd frontend &&
# Testcontainers talks to Podman through its Docker-compatible API socket.
PODMAN_SOCK = $(shell podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null)
INTEGRATION_ENV = DOCKER_HOST=unix://$(PODMAN_SOCK) TESTCONTAINERS_RYUK_DISABLED=true

.PHONY: help install dev dev-frontend test test-integration test-db test-all lint format build \
        up down logs ps clean migrate migration db-current db-docs db-shell db-reset \
        rbac-docs create-admin obs-up obs-down obs-ps obs-stats test-obs test-rules \
        stack-up stack-up-fast stack-down test-gateway

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
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m "not observability and not gateway" --cov

lint: ## Lint, format check, type check, architecture contracts
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
	$(FRONTEND) npm run lint && npm run format:check && npm run typecheck

format: ## Auto-format backend and frontend
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .
	$(FRONTEND) npm run format

build: ## Build the frontend bundle and the API image
	$(FRONTEND) npm run build
	podman build -t localhost/sentinel-api:dev -f backend/Containerfile backend

up: ## Build, apply migrations and start Postgres + API in Podman (data volume is kept)
	$(COMPOSE) build
	@# podman-compose only recreates a container when its *config* changes, not its image,
	@# so force it: otherwise `migrate` and `api` keep running the previous build.
	$(COMPOSE) up -d --force-recreate

down: ## Stop the containers (data volume is kept)
	$(COMPOSE) down

obs-up: ## Start the telemetry stores (Prometheus :9090, Loki :3100, Jaeger :16686, Alertmanager :9093)
	$(COMPOSE_OBS) up -d prometheus alertmanager loki jaeger

obs-down: ## Stop the telemetry stores (data kept)
	$(COMPOSE_OBS) stop prometheus alertmanager loki jaeger

obs-ps: ## Status of the telemetry stores
	$(COMPOSE_OBS) ps

obs-stats: ## Memory use of the telemetry stores (budget 2.5 GB)
	podman stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep -E 'prometheus|alertmanager|loki|jaeger'

stack-up: ## Start everything: Postgres, API, telemetry stores, gateway (ingest on :4319)
	$(COMPOSE_OBS) build
	$(COMPOSE_OBS) up -d --force-recreate

stack-up-fast: ## Same, with short health intervals (used by test-gateway)
	$(COMPOSE_OBS) build
	SENTINEL_INTEGRATION_FLUSH_INTERVAL_S=1 SENTINEL_INTEGRATION_STALE_AFTER_S=8 \
	SENTINEL_ENTITY_DISCOVERY_INTERVAL_S=3 $(COMPOSE_OBS) up -d --force-recreate

stack-down: ## Stop everything (data kept)
	$(COMPOSE_OBS) down

test-gateway: ## End-to-end ingestion tests (needs stack-up-fast)
	$(BACKEND) uv run pytest -m gateway -v

test-obs: ## Push synthetic OTLP into each store and query it back (needs obs-up)
	$(BACKEND) uv run pytest -m observability -v

test-rules: ## Unit-test the Prometheus recording rules with promtool
	podman run --rm --entrypoint promtool -v ./deploy/prometheus:/p:ro,Z $(PROM_IMAGE) test rules /p/tests/red_test.yml
	podman run --rm --entrypoint promtool -v ./deploy/prometheus:/p:ro,Z $(PROM_IMAGE) check rules /p/rules/red.yml

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

db-docs: ## Regenerate docs/database.md from the models
	$(BACKEND) uv run sentinel db docs

db-shell: ## Open psql inside the Postgres container (as superuser)
	podman exec -it sentinel_postgres_1 psql -U sentinel -d sentinel

db-reset: ## DELETES all local data: remove containers + volume, start fresh (roles + migrations)
	$(COMPOSE) down -v
	$(MAKE) up

clean: ## Remove local caches and build output (not containers or volumes)
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache backend/.coverage frontend/dist
