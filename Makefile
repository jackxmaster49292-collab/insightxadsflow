.DEFAULT_GOAL := help
PY := backend/.venv/bin/python
PIP := backend/.venv/bin/pip

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv: ## Create the backend virtualenv and install dependencies
	python3.12 -m venv backend/.venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e "backend[dev]"

.PHONY: up
up: ## Start Postgres and Redis for local development
	docker compose up -d postgres redis

.PHONY: up-all
up-all: ## Start the whole stack in containers
	docker compose up -d --build

.PHONY: down
down: ## Stop the stack
	docker compose down

.PHONY: migrate
migrate: ## Apply database migrations
	cd backend && .venv/bin/alembic upgrade head

.PHONY: downgrade
downgrade: ## Roll back one migration (proves migrations are reversible)
	cd backend && .venv/bin/alembic downgrade -1

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="message"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: ## Create a demo user, mock connection, chats and rule (mock provider only)
	cd backend && .venv/bin/python -m app.seed

.PHONY: api
api: ## Run the API locally
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

.PHONY: worker
worker: ## Run a forwarding worker locally
	cd backend && .venv/bin/python -m app.worker

.PHONY: listener
listener: ## Run the intake listener locally
	cd backend && .venv/bin/python -m app.listener

.PHONY: adminbot
adminbot: ## Run the Telegram control-panel bot locally
	cd backend && .venv/bin/python -m app.adminbot.main

.PHONY: scheduler
scheduler: ## Run the scheduler locally
	cd backend && .venv/bin/python -m app.scheduler

.PHONY: test
test: ## Run the backend test suite (Telegram fully mocked)
	cd backend && .venv/bin/pytest -q

.PHONY: cov
cov: ## Run tests with coverage on the critical modules
	cd backend && .venv/bin/pytest -q \
		--cov=app --cov-report=term-missing:skip-covered

.PHONY: lint
lint: ## Lint and format-check
	cd backend && .venv/bin/ruff check app tests
	cd backend && .venv/bin/ruff format --check app tests

.PHONY: fmt
fmt: ## Auto-format
	cd backend && .venv/bin/ruff check --fix app tests
	cd backend && .venv/bin/ruff format app tests

.PHONY: typecheck
typecheck: ## Static type check
	cd backend && .venv/bin/mypy app

.PHONY: openapi
openapi: ## Regenerate openapi.yaml from the live app
	cd backend && .venv/bin/python -m app.export_openapi ../openapi.yaml

.PHONY: check
check: lint typecheck test ## Everything CI runs
