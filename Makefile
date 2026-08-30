# RModHub developer shortcuts. Python deps are managed by uv; containers by Docker Compose.
#   make dev     - run the API locally with auto-reload (uses .venv via uv)
#   make test    - pytest
#   make lint    - ruff
#   make build   - build the Docker image via compose
#   make up      - start phase-1 stack (api only) and wait for /health
#   make smoke   - hit /health and POST the sample sequence against localhost:8000
#   make prod-up - start api + Caddy (HTTPS) using docker-compose.prod.yml

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE      := docker compose
COMPOSE_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml
PORT         ?= 8000
BASE_URL     ?= http://127.0.0.1:$(PORT)

.PHONY: help dev test lint build up down logs ps smoke prod-up prod-down phase2-up image-check

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

dev: ## Run uvicorn locally with auto-reload on :$(PORT)
	uv run uvicorn app.main:app --reload --port $(PORT)

test: ## Run the test-suite
	uv run pytest

lint: ## Ruff lint
	uv run ruff check .

build: ## Build the api image (rmodhub-api:local)
	$(COMPOSE) build

up: ## Start phase-1 stack (api only) and wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop and remove containers (volumes are kept)
	$(COMPOSE) down

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

prod-up: ## Start api + Caddy (HTTPS on 443; needs RMODHUB_DOMAIN in .env)
	$(COMPOSE_PROD) up -d --build

prod-down: ## Stop the production stack
	$(COMPOSE_PROD) down

phase2-up: ## Start everything incl. postgres/redis/worker placeholders
	$(COMPOSE) --profile phase2 up -d --build

image-check: ## Verify the image ships CPU-only torch and no nvidia packages
	docker run --rm rmodhub-api:local python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
	@if docker run --rm rmodhub-api:local sh -c 'ls /app/.venv/lib/python3.12/site-packages | grep -i nvidia'; then \
	  echo "nvidia packages found in image"; exit 1; else echo "no nvidia packages: OK"; fi
	docker image ls rmodhub-api:local
