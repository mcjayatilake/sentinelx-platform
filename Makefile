.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up down restart build logs ps \
        backend-shell frontend-shell db-shell \
        migrate migrate-generate \
        test test-backend test-frontend \
        lint lint-backend lint-frontend \
        fmt fmt-backend fmt-frontend \
        typecheck clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

## --- Environment -----------------------------------------------------------

up: ## Start all services in the background
	$(COMPOSE) up -d

down: ## Stop and remove all services
	$(COMPOSE) down

restart: down up ## Restart all services

build: ## Build (or rebuild) all service images
	$(COMPOSE) build

logs: ## Tail logs for all services
	$(COMPOSE) logs -f

ps: ## List running services
	$(COMPOSE) ps

clean: ## Remove containers, networks, and volumes
	$(COMPOSE) down -v --remove-orphans

## --- Shells ------------------------------------------------------------------

backend-shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend bash

frontend-shell: ## Open a shell in the frontend container
	$(COMPOSE) exec frontend sh

db-shell: ## Open a psql shell against the postgres container
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-sentinelx} -d $${POSTGRES_DB:-sentinelx}

## --- Database ------------------------------------------------------------

migrate: ## Apply database migrations
	$(COMPOSE) exec backend alembic upgrade head

migrate-generate: ## Generate a new migration (usage: make migrate-generate name="add users table")
	$(COMPOSE) exec backend alembic revision --autogenerate -m "$(name)"

## --- Testing ---------------------------------------------------------------

test: test-backend test-frontend ## Run all test suites

test-backend: ## Run backend test suite
	$(COMPOSE) exec backend pytest

test-frontend: ## Run frontend test suite
	$(COMPOSE) exec frontend npm test

## --- Linting & formatting --------------------------------------------------

lint: lint-backend lint-frontend ## Run all linters

lint-backend: ## Lint backend code with ruff
	$(COMPOSE) exec backend ruff check .

lint-frontend: ## Lint frontend code with eslint
	$(COMPOSE) exec frontend npm run lint

fmt: fmt-backend fmt-frontend ## Format all code

fmt-backend: ## Format backend code with ruff + black
	$(COMPOSE) exec backend ruff format .

fmt-frontend: ## Format frontend code with prettier
	$(COMPOSE) exec frontend npm run format

typecheck: ## Run static type checkers for backend and frontend
	$(COMPOSE) exec backend mypy app
	$(COMPOSE) exec frontend npm run typecheck
