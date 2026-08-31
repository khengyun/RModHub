# RModHub developer shortcuts. Python deps are managed by uv; containers by Docker Compose.
#   make dev          - run the API locally with auto-reload (uses .venv via uv)
#   make test         - pytest
#   make lint         - ruff
#   make build        - build the api image via compose
#   make up           - start api + web (+ the signal branch when POSTGRES_PASSWORD is set), wait for health
#   make smoke        - hit /health and POST the sample sequence against the api port
#   make prod-up      - start api + web + Caddy (HTTPS) using docker-compose.prod.yml
#   make phase2-check - validate .env for the signal branch (password, HMAC key)
#   make phase2-up    - start api + web + postgres + redis + worker (signal branch), wait for health
#   make phase2-down  - stop the signal-branch stack (volumes kept)
#   make phase2-logs  - follow api + worker logs
#   make phase2-smoke - capabilities, run the synthetic sample job to completion, results, CSV
#   make worker-build - build the worker image (rmodhub-worker:local) via compose
#   make web-dev      - Vite dev server for the React UI (proxies API paths to :8000)
#   make web-check    - typecheck + build the UI + verify the bundle loads nothing external
#   make web-build    - build the web image (rmodhub-web:local) via compose
#   make web-smoke    - check the web container: /healthz, proxied /health, SPA fallback, CSP

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Values from .env (the file Compose interpolates as well); a variable already set in the
# environment or on the make command line wins. Surrounding quotes are dropped.
dotenv = $(strip $(shell sed -n -E 's/^$(1)=(.*)$$/\1/p' .env 2>/dev/null | head -1 | sed -E "s/^'(.*)'$$/\1/; s/^\"(.*)\"$$/\1/"))

# POSTGRES_PASSWORD switches the signal branch on: docker-compose.yml derives
# DATABASE_URL / CELERY_BROKER_URL from it, and postgres/redis/worker live in the "phase2"
# profile. The two are kept in step here so `make up` / `make prod-up` never start the api
# against a Postgres that is not there and `make down` does not leave worker/postgres/redis
# behind (COMPOSE_PROFILES=phase2 in .env does the same for plain `docker compose`).
# `$(value ...)` keeps a `$` inside an environment value literal; both are exported to
# the recipe shells (and read from there) so no secret ends up on an echoed command line.
export POSTGRES_PASSWORD_VALUE := $(or $(value POSTGRES_PASSWORD),$(call dotenv,POSTGRES_PASSWORD))
export IP_HASH_SECRET_VALUE    := $(or $(value RMODHUB_IP_HASH_SECRET),$(call dotenv,RMODHUB_IP_HASH_SECRET))
PROFILE                 := $(if $(POSTGRES_PASSWORD_VALUE),--profile phase2,)

COMPOSE      := docker compose $(PROFILE)
COMPOSE_P2   := docker compose --profile phase2
COMPOSE_PROD := docker compose $(PROFILE) -f docker-compose.yml -f docker-compose.prod.yml
# Host ports published by docker-compose.yml (RMODHUB_PORT / RMODHUB_WEB_PORT in .env).
ifeq ($(origin PORT),undefined)
PORT         := $(or $(call dotenv,RMODHUB_PORT),8000)
endif
ifeq ($(origin WEB_PORT),undefined)
WEB_PORT     := $(or $(call dotenv,RMODHUB_WEB_PORT),8080)
endif
BASE_URL     ?= http://127.0.0.1:$(PORT)
WEB_URL      ?= http://127.0.0.1:$(WEB_PORT)
# phase2-smoke: how many 5-second polls to wait for the sample job (default 10 min).
SMOKE_POLLS  ?= 120

# Sanity checks for the signal branch. The password is spliced raw into DATABASE_URL by
# docker-compose.yml, so `@` (ends the credentials), `%` (percent-decoded by SQLAlchemy)
# and `$` (interpolated by Compose) would silently change it; the HMAC key must be a real
# secret (the api container refuses the development default too, deploy/api-entrypoint.py).
define check-phase2-env
pw="$$POSTGRES_PASSWORD_VALUE"; secret="$$IP_HASH_SECRET_VALUE"; \
if [ -z "$$pw" ]; then echo "POSTGRES_PASSWORD is not set: put it in .env (it switches the signal branch on)."; exit 1; fi; \
if [ "$$pw" = "change-me" ]; then echo "WARNING: POSTGRES_PASSWORD is the placeholder 'change-me'; set a real one in .env before exposing this server."; fi; \
case "$$pw" in *[@%$$]*|*[[:space:]]*) echo "POSTGRES_PASSWORD must not contain @ % \$$ or whitespace (docker-compose.yml splices it into DATABASE_URL); use e.g. openssl rand -hex 24"; exit 1;; esac; \
if [ -z "$$secret" ] || [ "$$secret" = "rmodhub-dev" ]; then echo "RMODHUB_IP_HASH_SECRET is not set (or is the development default): put a random value in .env, e.g. RMODHUB_IP_HASH_SECRET=\$$(openssl rand -hex 32)."; exit 1; fi; \
echo "signal branch configuration: OK"
endef

.PHONY: help dev test lint build up down logs ps smoke prod-up prod-down image-check \
        phase2-check phase2-up phase2-down phase2-logs phase2-smoke worker-build \
        web-dev web-check web-test web-build web-smoke

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

dev: ## Run uvicorn locally with auto-reload on :$(PORT)
	uv run uvicorn app.main:app --reload --port $(PORT)

test: ## Run the test-suite
	uv run pytest

lint: ## Ruff lint
	uv run ruff check .

build: ## Build the api image (rmodhub-api:local)
	$(COMPOSE) build

up: $(if $(PROFILE),phase2-check) ## Start api + web (+ postgres/redis/worker when POSTGRES_PASSWORD is set), wait until healthy
	$(COMPOSE) up -d --build --wait $(if $(PROFILE),--wait-timeout 600)
	@echo "UI:  $(WEB_URL)   API: $(BASE_URL)   API docs: $(WEB_URL)/docs$(if $(PROFILE),   nanopore tab: $(WEB_URL)/signal)"

down: ## Stop and remove containers, phase2 ones included (volumes are kept)
	$(COMPOSE_P2) down

logs: ## Follow api logs
	$(COMPOSE) logs -f api

ps: ## Show container status / health
	$(COMPOSE) ps

smoke: ## Health check + predict on the sample sequence against $(BASE_URL)
	@echo "GET $(BASE_URL)/health"
	@curl -fsS $(BASE_URL)/health; echo
	@echo "POST $(BASE_URL)/api/predict/sequence (sample from /api/samples/sequence)"
	@curl -fsS $(BASE_URL)/api/samples/sequence \
	  | python3 -c 'import sys,json; print(json.dumps({"sequence": json.load(sys.stdin)["sequence"]}))' \
	  | curl -fsS -X POST $(BASE_URL)/api/predict/sequence -H 'content-type: application/json' -d @- \
	  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("n_sites =", d.get("meta", {}).get("n_sites", len(d.get("results", []))))'
	@echo "UI: $(WEB_URL)  (make web-smoke checks the nginx side)"

prod-up: $(if $(PROFILE),phase2-check) ## Start api + web + Caddy (HTTPS on 443; needs RMODHUB_DOMAIN in .env; + signal branch when POSTGRES_PASSWORD is set)
	$(COMPOSE_PROD) up -d --build

prod-down: ## Stop the production stack, phase2 services included
	docker compose --profile phase2 -f docker-compose.yml -f docker-compose.prod.yml down

# --- signal branch (profile phase2) ------------------------------------------------------
phase2-check: ## Validate the signal-branch settings in .env (POSTGRES_PASSWORD, RMODHUB_IP_HASH_SECRET)
	@$(check-phase2-env)

phase2-up: phase2-check ## Start api + web + postgres + redis + worker and wait until healthy
	$(COMPOSE_P2) up -d --build --wait --wait-timeout 600
	@echo "UI:  $(WEB_URL)   API: $(BASE_URL)   nanopore tab: $(WEB_URL)/signal"

phase2-down: ## Stop the signal-branch stack (volumes are kept)
	$(COMPOSE_P2) down

phase2-logs: ## Follow api + worker logs
	$(COMPOSE_P2) logs -f api worker

worker-build: ## Build the worker image (rmodhub-worker:local)
	$(COMPOSE_P2) build worker

phase2-smoke: ## Capabilities, run the synthetic sample job to completion, results, CSV (against $(WEB_URL))
	@echo "GET $(WEB_URL)/api/capabilities"
	@curl -fsS $(WEB_URL)/api/capabilities \
	  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d)); assert d["signal"] is True, "the signal branch is not enabled on this server"'
	@echo "POST $(WEB_URL)/api/jobs/signal/sample"
	@job=$$(curl -fsS -X POST $(WEB_URL)/api/jobs/signal/sample \
	  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["status"]=="queued", d; print(d["job_id"])'); \
	echo "job_id = $$job"; \
	status=""; \
	for i in $$(seq 1 $(SMOKE_POLLS)); do \
	  st=$$(curl -fsS $(WEB_URL)/api/jobs/$$job); \
	  status=$$(echo "$$st" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"])'); \
	  echo "  [$$i] $$(echo "$$st" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], d.get("stage") or "-", "" if d.get("progress") is None else "{:.0%}".format(d["progress"]), d.get("error") or "")')"; \
	  case "$$status" in done) break;; failed|cancelled|expired) exit 1;; esac; \
	  sleep 5; \
	done; \
	test "$$status" = done || { echo "job did not finish within $$(( $(SMOKE_POLLS) * 5 )) s"; exit 1; }; \
	echo "GET $(WEB_URL)/api/jobs/$$job/results"; \
	curl -fsS "$(WEB_URL)/api/jobs/$$job/results?limit=5" \
	  | python3 -c 'import sys,json; d=json.load(sys.stdin); m=d["meta"]; print("n_sites =", d["total"], "n_reads =", m["n_reads"], "transcripts =", [t["transcript_id"] for t in m["transcripts"]]); assert d["total"] > 0, "no sites"; assert d["results"][0]["source"] == "signal"'; \
	echo "GET $(WEB_URL)/api/jobs/$$job/download.csv (first lines)"; \
	curl -fsS "$(WEB_URL)/api/jobs/$$job/download.csv" | awk 'NR<=3'; \
	curl -fsS "$(WEB_URL)/api/jobs/$$job/download.csv?level=read" | awk 'NR<=2'

# --- web UI --------------------------------------------------------------------------------
web-dev: ## Vite dev server on :5173 for the React UI (API paths proxied to $(BASE_URL))
	cd frontend && VITE_API_TARGET=$(BASE_URL) npm run dev

web-check: ## Typecheck + production build + verify dist/ references no external resource
	cd frontend && npm run typecheck && npm run build && npm run check:no-cdn

web-test: ## Frontend unit tests (vitest)
	cd frontend && npm run test

web-build: ## Build the web image (rmodhub-web:local); the build fails on any external asset
	$(COMPOSE) build web

web-smoke: ## Check the web container at $(WEB_URL): nginx liveness, proxied API, SPA fallback, CSP
	@echo "GET $(WEB_URL)/healthz"; curl -fsS $(WEB_URL)/healthz
	@echo "GET $(WEB_URL)/health (proxied to api)"; curl -fsS $(WEB_URL)/health; echo
	@curl -fsS $(WEB_URL)/help | grep -q '<div id="root">' && echo "SPA fallback (/help -> index.html): OK"
	@curl -fsSI $(WEB_URL)/ | grep -qi '^content-security-policy:' && echo "Content-Security-Policy header: OK"
	@curl -fsS $(WEB_URL)/docs | grep -q 'static/swagger' && echo "/docs (self-hosted Swagger UI): OK"
	@echo "POST $(WEB_URL)/api/predict/sequence (sample)"
	@curl -fsS $(WEB_URL)/api/samples/sequence \
	  | python3 -c 'import sys,json; print(json.dumps({"sequence": json.load(sys.stdin)["sequence"]}))' \
	  | curl -fsS -X POST $(WEB_URL)/api/predict/sequence -H 'content-type: application/json' -d @- \
	  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("n_sites =", d.get("meta", {}).get("n_sites", len(d.get("results", []))))'

image-check: ## Verify the api image ships CPU-only torch and no nvidia packages
	docker run --rm rmodhub-api:local python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
	@if docker run --rm rmodhub-api:local sh -c 'ls /app/.venv/lib/python3.12/site-packages | grep -i nvidia'; then \
	  echo "nvidia packages found in image"; exit 1; else echo "no nvidia packages: OK"; fi
	docker image ls rmodhub-api:local
