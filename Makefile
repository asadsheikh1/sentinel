# SENTINEL developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE   := podman compose -f deploy/compose/compose.yaml
BACKEND   := cd backend &&
FRONTEND  := cd frontend &&
# Testcontainers talks to Podman through its Docker-compatible API socket.
PODMAN_SOCK = $(shell podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null)
INTEGRATION_ENV = DOCKER_HOST=unix://$(PODMAN_SOCK) TESTCONTAINERS_RYUK_DISABLED=true

.PHONY: help install dev dev-frontend test test-integration test-db test-all lint format build \
        up down logs ps clean migrate migration db-current db-docs db-shell db-reset

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

test: ## Unit tests (backend + frontend)
	$(BACKEND) uv run pytest --cov
	$(FRONTEND) npm test

test-integration: ## Tests that start real containers via Podman (Testcontainers)
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m integration

test-db: ## Database tests against a throwaway pgvector Postgres (Testcontainers)
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m db

test-all: ## Every backend test (unit + db + integration) with the coverage gate
	$(BACKEND) $(INTEGRATION_ENV) uv run pytest -m "" --cov

lint: ## Lint, format check, type check, architecture contracts
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
	$(FRONTEND) npm run lint && npm run format:check && npm run typecheck

format: ## Auto-format backend and frontend
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .
	$(FRONTEND) npm run format

build: ## Build the frontend bundle and the API image
	$(FRONTEND) npm run build
	podman build -t localhost/sentinel-api:dev -f backend/Containerfile backend

up: ## Start Postgres + API in Podman containers
	$(COMPOSE) up -d --build

down: ## Stop the containers (data volume is kept)
	$(COMPOSE) down

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

db-docs: ## Regenerate docs/database.md from the models
	$(BACKEND) uv run sentinel db docs

db-shell: ## Open psql inside the Postgres container (as superuser)
	podman exec -it sentinel_postgres_1 psql -U sentinel -d sentinel

db-reset: ## DELETES all local data: remove containers + volume, start fresh (roles + migrations)
	$(COMPOSE) down -v
	$(COMPOSE) up -d --build

clean: ## Remove local caches and build output (not containers or volumes)
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache backend/.coverage frontend/dist
