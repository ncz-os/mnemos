.PHONY: install dev test lint docker-build docker-up docker-down clean help

PYTHON := python3
VENV := venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

help:          ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:       ## Install production dependencies
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

dev:           ## Install development dependencies
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:          ## Run unit tests
	$(PYTEST) tests/ -v --tb=short --ignore=tests/test_live_e2e.py

test-archive:  ## Run archive salvage tests
	$(PYTEST) archive/tests/ -v --tb=short

test-all:      ## Run all tests including archive
	$(PYTEST) tests/ archive/tests/ -v --tb=short --ignore=tests/test_live_e2e.py

lint:          ## Run ruff linter
	$(RUFF) check . --exclude venv,archive

lint-fix:      ## Run ruff with auto-fix
	$(RUFF) check . --fix --exclude venv,archive

setup-db:      ## Run database migrations (requires PostgreSQL running)
	$(PYTHON) install.py --migrations-only

docker-build:  ## Build Docker image
	docker build -t mnemos:dev .

docker-up:     ## Start MNEMOS + PostgreSQL via docker-compose
	docker compose up -d

docker-down:   ## Stop docker-compose services
	docker compose down

docker-logs:   ## Follow MNEMOS container logs
	docker compose logs -f mnemos

clean:         ## Remove build artifacts and caches
	find . -type d -name __pycache__ -not -path "./venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./venv/*" -delete 2>/dev/null || true
	rm -rf .pytest_cache dist build *.egg-info
