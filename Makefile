# CiteVyn — repo-root developer + operator entry points.
#
# Developer workflow:
#   make demo        — bring up the local stack (db + migrations + seed)
#   make lint        — ruff on backend/app + tests
#   make typecheck   — pyright on backend/app (strict)
#   make test        — pytest (excludes the ``postgres`` marker)
#   make golden      — run the 50-case golden evaluation suite
#   make eval        — run the RAG eval harness (retrieval hit-rate + LLM judge)
#   make smoke       — end-to-end curl against uvicorn on SQLite
#   make clean       — drop caches
#
# Production workflow (operator):
#   make deploy      — first-time cold start (see infra/docker/scripts/deploy.sh)
#   make refresh     — rebuild + re-deploy without losing data
#   make logs        — tail logs from api, worker, caddy
#   make backup      — pg_dump to ./backups/
#
# Variables you can override on the command line:
#   DB_URL     — SQLAlchemy URL for local alembic (default: docker-compose db)
#   VERSION    — image tag (default: dev); set from CI via git tag
#   PROFILE    — docker compose profile (default: prod)
#
# Heavy lifting lives in backend/, db/, infra/docker/, and scripts/.

SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker/docker-compose.yml
DB_URL ?= postgresql+psycopg://citevyn:citevyn@localhost:5432/citevyn
API_KEY ?= local-demo-key
VERSION ?= dev
PROFILE ?= prod
# NOTE: CITEVYN_DATABASE_URL is intentionally NOT exported globally
# because the ``test`` target runs against an in-memory SQLite
# (see ``backend/tests/conftest.py::_default_database_url``).
# Targets that need a real Postgres (``migrate``, ``seed``, ``db-up``,
# ``smoke``) set the variable on the command line they run.
export VERSION
export PROFILE

.PHONY: help env-bootstrap db-up db-verify ci-smoke db-down migrate seed demo demo-frontend stop smoke clean lint typecheck test ci \
        build push deploy refresh logs backup restore golden golden-smoke eval e2e install-hooks

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ─────────────────────────── Code quality ───────────────────────────
lint: ## Run ruff over backend/app + tests (check only — fixes go in a separate commit)
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .

typecheck: ## Run pyright strict on backend/app
	cd backend && uv run pyright

install-hooks: ## Install the git pre-commit hook (ruff format+check gate; see scripts/git-hooks/)
	@root="$$(git rev-parse --show-toplevel)"; \
	hooksdir="$$(git config core.hooksPath || git rev-parse --git-path hooks)"; \
	hook="$$hooksdir/pre-commit"; \
	if [ -e "$$hook" ] && ! grep -q "CiteVyn pre-commit hook" "$$hook" 2>/dev/null; then \
		echo "A different pre-commit hook already exists at $$hook — back it up or merge manually; not overwriting."; \
		exit 1; \
	fi; \
	mkdir -p "$$hooksdir"; \
	ln -sf "$$root/scripts/git-hooks/pre-commit" "$$hook" 2>/dev/null && [ -e "$$hook" ] || cp "$$root/scripts/git-hooks/pre-commit" "$$hook"; \
	chmod +x "$$hook"; \
	echo "Installed pre-commit hook -> $$hook (bypass a single commit with 'git commit --no-verify')."

test: ## Run the pytest suite (excludes the postgres marker; uses in-memory SQLite)
	cd backend && uv sync --group dev
	cd backend && env -u CITEVYN_DATABASE_URL uv run pytest -m "not postgres" -q

test-pg: ## Run the postgres-marked tests (requires CITEVYN_PG_TEST_URL)
	cd backend && uv run pytest -m postgres -q

golden: ## Run the golden-case test suite (see tests/golden/README.md)
	cd backend && uv sync --group dev
	cd backend && uv run python -m tests.golden.runner --report artifacts/golden_report.json

golden-smoke: ## Run 3 golden cases as a smoke test (answer, search, no_answer)
	cd backend && uv sync --group dev
	cd backend && uv run python -m tests.golden.runner --ids claude_api_001,claude_api_004,cross_005 --report artifacts/golden_report_smoke.json

eval: ## Run the RAG eval harness (retrieval hit-rate + LLM judge if a key is set; see tests/eval/README.md)
	cd backend && uv sync --group dev
	cd backend && env -u CITEVYN_DATABASE_URL uv run python -m tests.eval.runner --report artifacts/eval_report.json

# ─────────────────────────── Local development ───────────────────────────

# ─────────────────────────── Local development ───────────────────────────
env-bootstrap: ## Create infra/docker/.env from prod.env.example if absent (DEV-ONLY stub secrets)
	@# Compose env_file: refs on every service require the file to
	# exist on disk, even for services behind other profiles — so ANY
	# ``docker compose`` invocation (including ``down -v``) fails if
	# .env is missing. This target is a prerequisite of db-up and is
	# also run first by the CI smoke so the pre-boot ``down -v`` has a
	# .env to parse. On a fresh clone we bootstrap from prod.env.example.
	# POSTGRES_PASSWORD is set to the repo-wide local dev credential
	# ``citevyn`` so the bootstrapped db matches DB_URL / smoke.sh /
	# config.py / CI and ``make migrate`` connects without an auth
	# mismatch. ADMIN_API_KEY and ACME_EMAIL stay at the
	# ``dev-only-change-me`` stub: the shared guard in _env_guard.sh is
	# an OR over all three fields, so those two still make it refuse
	# every prod entry point (deploy/refresh/backup/restore). The guard
	# also rejects POSTGRES_PASSWORD=citevyn, so a prod deploy that
	# reuses this dev db password is caught too.
	@if [[ ! -f infra/docker/.env ]]; then \
	  echo "infra/docker/.env missing; bootstrapping from prod.env.example (DEV ONLY)"; \
	  sed -E 's|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=citevyn|; s|^CITEVYN_ADMIN_API_KEY=.*|CITEVYN_ADMIN_API_KEY=dev-only-change-me|; s|^CITEVYN_ACME_EMAIL=.*|CITEVYN_ACME_EMAIL=dev-only-change-me|' \
	    infra/docker/prod.env.example > infra/docker/.env; \
	  chmod 600 infra/docker/.env; \
	  echo ""; \
	  echo "  ⚠  infra/docker/.env contains DEV-ONLY stub secrets."; \
	  echo "     Running \`docker compose --profile prod up -d\` directly"; \
	  echo "     will start the prod stack with these stubs. The shared"; \
	  echo "     guard in infra/docker/scripts/_env_guard.sh refuses to"; \
	  echo "     run deploy/refresh/backup/restore against this file,"; \
	  echo "     but it cannot intercept a raw docker compose invocation."; \
	  echo "     Remove infra/docker/.env and copy prod.env.example"; \
	  echo "     with real secrets before going to prod."; \
	  echo ""; \
	fi

db-up: env-bootstrap ## Start Postgres + Redis via docker compose (no app containers)
	$(COMPOSE) up -d db redis
	@echo "Waiting for Postgres to accept connections…"
	@for i in $$(seq 1 60); do \
	  if docker exec citevyn-db pg_isready -U citevyn -d citevyn >/dev/null 2>&1; then \
	    echo "Postgres ready"; exit 0; \
	  fi; \
	  sleep 1; \
	done; \
	echo "Postgres did not become ready in 60s" >&2; exit 1

db-verify: ## Assert the composed db truly SERVES (SELECT 1 + pgvector) — a container that boots but can't serve is a FAIL
	@# ``docker compose config`` only proves the YAML parses; ``db-up``
	# only waits for pg_isready. This target proves the pg18 cluster
	# actually accepts a live query AND that the pgvector extension the
	# app depends on can be created — so a half-broken boot (wrong
	# PGDATA layout, missing pgvector image) surfaces as a hard failure
	# rather than a false green. ``-v ON_ERROR_STOP=1`` makes psql exit
	# non-zero on any SQL error so ``make`` propagates it.
	@echo "Verifying the composed db serves a live query…"
	docker exec citevyn-db psql -U citevyn -d citevyn -v ON_ERROR_STOP=1 -c "SELECT 1;"
	@echo "Verifying the pgvector extension can be created…"
	docker exec citevyn-db psql -U citevyn -d citevyn -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
	@echo "db-verify: OK — the composed pg18 db is serving."

ci-smoke: db-up db-verify migrate ## Fresh-volume DB-stack boot smoke used by .github/workflows/ci.yml (Closes #52)
	@# Single source of truth for "does the real compose DB boot on a
	# greenfield volume?". db-up boots db+redis (bootstrapping .env from
	# prod.env.example) and waits for pg_isready; db-verify proves it
	# serves + pgvector works; migrate applies every Alembic migration
	# against the composed pg18 DB, which also proves the bootstrap
	# POSTGRES_PASSWORD matches DB_URL (an auth mismatch fails here).
	# The CALLER owns volume lifecycle: run ``$(COMPOSE) down -v`` before
	# (fresh) and after (teardown). We deliberately do NOT down -v here
	# so ``make ci-smoke`` never nukes a developer's local data volume.
	@echo "ci-smoke: composed pg18 DB booted on a fresh volume, serves, and migrates cleanly."

db-down: ## Stop the docker-compose db stack (keeps volumes)
	$(COMPOSE) down

db-reset: ## Destroy and recreate the database volume (DESTRUCTIVE; requires CONFIRM=yes)
	@if [[ "${CONFIRM:-}" != "yes" ]]; then \
	  echo "error: this drops the database volume (all data lost)" >&2; \
	  echo "       re-run with: make db-reset CONFIRM=yes" >&2; \
	  exit 2; \
	fi
	$(COMPOSE) down -v

migrate: ## Apply Alembic migrations to head against DB_URL
	CITEVYN_DATABASE_URL=$(DB_URL) uv run --project backend alembic -c db/alembic.ini upgrade head

seed: ## Seed demo users + catalog (idempotent)
	CITEVYN_DATABASE_URL=$(DB_URL) uv run --project backend python -m db.seed.seed_users
	CITEVYN_DATABASE_URL=$(DB_URL) uv run --project backend python -m db.seed.seed_catalog

demo: db-up migrate seed ## Bring up db, migrate, seed (one-shot)
	@echo "Demo stack is up. Run 'make stop' to tear down."

demo-frontend: ## Build the optional React/Vite frontend into frontend/dist
	cd frontend && npm ci && npm run build
	@echo "Frontend bundle written to frontend/dist. Serve it behind the API, or open frontend/dist/index.html directly."

stop: db-down ## Tear the demo stack down

smoke: ## End-to-end smoke (db-up + migrate + seed + uvicorn + curl + stop)
	bash scripts/smoke.sh

e2e: ## End-to-end test (chat UI happy-path: render + ask + citation)
	@echo "e2e: running the chat-UI smoke (curl-based) since the Playwright harness"
	@echo "e2e: lands in Slice 11. The smoke here is: API healthy + 1 grounded ask."
	@echo "e2e: To upgrade to Playwright, see docs/adr/0004-frontend-ci.md."
	bash scripts/smoke.sh

clean: ## Remove __pycache__ + .pytest_cache + .ruff_cache + smoke artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .smoke-uvicorn.log .smoke-uvicorn.pid .smoke-last-response.json

# ─────────────────────────── Production build ───────────────────────────
build: ## Build the api + worker images (VERSION=tag to label)
	$(COMPOSE) --profile $(PROFILE) build --pull

push: ## Push the api + worker images to the configured registry
	$(COMPOSE) --profile $(PROFILE) push

# ─────────────────────────── Production deploy ───────────────────────────
deploy: ## First-time / cold-start deploy (run from infra/docker/.env host)
	./infra/docker/scripts/deploy.sh

refresh: ## Rebuild + re-deploy in place (no data loss)
	./infra/docker/scripts/refresh.sh

logs: ## Tail logs from api, worker, caddy
	$(COMPOSE) --profile $(PROFILE) logs -f --tail=100 api worker caddy

ps: ## Show running containers (prod profile)
	$(COMPOSE) --profile $(PROFILE) ps

backup: ## Dump the live database to ./backups/
	./infra/docker/scripts/backup.sh

restore: ## Restore a pg_dump file (usage: make restore FILE=path)
	@if [[ -z "$(FILE)" ]]; then echo "usage: make restore FILE=path/to/citevyn-*.dump" >&2; exit 2; fi
	@if [[ ! -f "$(FILE)" ]]; then echo "error: $(FILE) not found" >&2; exit 1; fi
	# Source the env file so docker compose + the backup container
	# can read POSTGRES_PASSWORD (the ``backup`` service has it
	# in env_file, which docker compose requires to be present
	# at run-time). The shared guard refuses to run if the .env
	# is still the dev-only stub that ``make demo`` writes.
	@if [[ ! -f infra/docker/.env ]]; then echo "error: infra/docker/.env not found; copy prod.env.example first" >&2; exit 1; fi
	@( source infra/docker/scripts/_env_guard.sh infra/docker ) || exit 1
	@set -a; . infra/docker/.env; set +a; \
	docker compose --profile backup run --rm \
		backup sh -c "pg_restore --clean --if-exists --no-owner --no-privileges \
			-h db -U citevyn -d citevyn < /dev/stdin" < $(FILE)

# ─────────────────────────── Convenience composites ───────────────────────────
# ``make ci`` is the deterministic gate the pr-quality workflow uses
# (lint + typecheck + test, hermetic SQLite). ``make verify`` is the
# developer-side equivalent with the same dependencies.
ci: lint typecheck test ## Deterministic CI gate used by .github/workflows/pr-quality.yml
	@echo "make ci: all checks passed"

verify: lint typecheck test ## Run the full pre-merge gate locally
