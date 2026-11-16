.PHONY: help config config-check doctor desktop-dist desktop-status desktop-install up down clean dev dev-web web install test test-sandbox e2e-live e2e-ui e2b-templates daytona-snapshots test-web test-all lint setup-db migrate

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

config: ## Interactive setup wizard (LLM, sandbox, web search/fetch, storage, infra)
	@bash scripts/configure.sh

config-check: ## Validate .env/config.yaml/agent_config.yaml/model manifests
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/validate_config.py; else python3 scripts/validate_config.py; fi

doctor: ## Pre-flight check: Docker, DB, Redis, ports, backend, MCP, providers (ARGS="--json")
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/doctor.py $(ARGS); else python3 scripts/doctor.py $(ARGS); fi

# ---------------------------------------------------------------------------
# Desktop release (one version, one installed Conductor)
# ---------------------------------------------------------------------------
desktop-dist: ## Build the desktop DMG/ZIP for desktop/package.json's version
	cd desktop && pnpm dist

desktop-status: ## Compare repo/build/installed desktop versions and find duplicate apps
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/desktop_release.py status $(ARGS); else python3 scripts/desktop_release.py status $(ARGS); fi

desktop-install: ## Install the built app into ~/Applications and remove duplicate build apps
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/desktop_release.py install $(ARGS); else python3 scripts/desktop_release.py install $(ARGS); fi
# ---------------------------------------------------------------------------
# Docker Compose (full-stack)
# ---------------------------------------------------------------------------
up: _sandbox-prepare ## Start full stack (PROVIDER=docker|daytona|e2b)
	SANDBOX_PROVIDER=$(PROVIDER) docker compose up --build

down: ## Stop all Docker Compose services
	docker compose down

clean: down ## Stop everything and remove stale sandbox containers
	@echo "Removing stale sandbox containers..."
	@docker rm -f $$(docker ps -aq --filter "name=conductor-sandbox") 2>/dev/null || true
	@echo "Clean."

# Build sandbox image only when provider needs it (docker)
_sandbox-prepare:
ifeq ($(PROVIDER),docker)
	@echo "Building sandbox image for docker provider..."
	docker build -f Dockerfile.sandbox -t conductor-sandbox:latest .
endif

# ---------------------------------------------------------------------------
# Manual development (without Docker for backend/frontend)
# ---------------------------------------------------------------------------
install: ## Install all dependencies (backend + CLI + frontend; observability extra if .env enables OTel)
	@if [ -f .env ] && grep -qE "^OTEL_EXPORTER_OTLP_ENDPOINT=." .env; then \
		echo "→ OTEL_EXPORTER_OTLP_ENDPOINT set in .env — installing observability extra"; \
		uv sync --group dev --extra test --extra cli --extra observability; \
	else \
		echo "→ OTel endpoint unset — skipping observability extra (set OTEL_EXPORTER_OTLP_ENDPOINT in .env to enable)"; \
		uv sync --group dev --extra test --extra cli; \
	fi
	cd web && pnpm install

# Loopback by default. Override explicitly, e.g. `make dev DEV_HOST=0.0.0.0`.
DEV_HOST ?= 127.0.0.1

dev: ## Start backend with hot-reload on DEV_HOST (default 127.0.0.1; requires DB + Redis)
	uv run python server.py --host $(DEV_HOST) --reload

dev-web: ## Start frontend dev server
	cd web && pnpm dev

web: ## Build web/dist (pnpm build in web/), which `cond up` serves in static mode
	cd web && pnpm build

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test: ## Run backend unit tests
	uv run pytest tests/unit/ -v --tb=short

test-sandbox: _sandbox-prepare ## Run sandbox integration tests (PROVIDER=memory|docker|daytona|e2b)
	SANDBOX_TEST_PROVIDER=$(PROVIDER) uv run pytest tests/integration/sandbox/ -v --tb=short

e2b-templates: ## Build the E2B sandbox templates for every resource tier
	uv run python scripts/e2b_templates.py

daytona-snapshots: ## Build the Daytona sandbox snapshots for every resource tier
	uv run python scripts/daytona_snapshots.py

test-web: ## Run frontend unit tests
	cd web && pnpm vitest run

e2e-ui: ## Real UI task walkthrough: Research + Command Palette -> Markets/Settings
	cd web && node scripts/live-ui-walkthrough.mjs

e2e-live: ## Real-stack E2E: APIs + native browser turns + key pages (ARGS="--quick")
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/live_e2e.py $(ARGS); else python3 scripts/live_e2e.py $(ARGS); fi

test-desktop: ## Run desktop shell tests
	cd desktop && pnpm test


refactor-diff: ## Compare refactor behavior fixtures (OLD=<baseline.json> NEW=<candidate.json>)
	@test -n "$(OLD)" -a -n "$(NEW)" || { echo "usage: make refactor-diff OLD=<baseline.json> NEW=<candidate.json>"; exit 2; }
	uv run python -m scripts.refactor_diff.cli compare --old "$(OLD)" --new "$(NEW)"

refactor-diff-validate: ## Schema + redaction check for every refactor fixture
	uv run python -m scripts.refactor_diff.cli validate $$(find scripts/refactor_diff/fixtures -name '*.json' | sort)

refactor-event-coverage: ## A-D7-1 read-only turn/compaction coverage audit (strict fails on gaps)
	uv run python -m scripts.refactor_event_coverage

memory-eval: ## Run the memory-context selection eval (exit code gates thresholds)
	uv run python -m scripts.memory_eval.runner

refactor-status: ## Refactor flags, schema revision and projection row counts (canary evidence)
	uv run python -m scripts.refactor_status --database


test-all: test test-web test-desktop ## Run all tests (backend + frontend + desktop)
	$(MAKE) test-sandbox PROVIDER=memory

# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------
lint: ## Run all linters (Ruff + ESLint)
	uv run ruff check src/
	cd web && pnpm lint

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
setup-db: ## Start PostgreSQL + Redis in Docker and initialize tables
	./scripts/start_db.sh

migrate: ## Run database migrations
	uv run alembic upgrade head
