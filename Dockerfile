# syntax=docker/dockerfile:1.7
# RModHub API image — FastAPI + PyTorch (CPU-only), multi-stage build.
#
#   docker build -t rmodhub-api:local .
#   docker run --rm -p 8000:8000 rmodhub-api:local
#
# Stage 1 ("builder") resolves the locked dependency set with uv into /app/.venv.
# Stage 2 ("runtime") copies only that venv and the `app/` package onto a fresh
# python:3.12-slim base, so no build tooling, lockfile tooling, tests, or git
# metadata end up in the shipped image.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: dependency resolution
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv binary only (no Python download: we use the interpreter of the base image).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Layer 1: third-party dependencies only. Rebuilt only when pyproject.toml / uv.lock
# change. `--frozen` refuses to touch the lockfile; `--no-dev` skips pytest/ruff/httpx.
# torch resolves from the CPU-only index pinned in [tool.uv.sources] (see pyproject.toml),
# so no nvidia_* / CUDA wheels are pulled in.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Layer 2: project source. README.md is referenced by `readme = "README.md"` in
# pyproject.toml, so it must be present for the second sync.
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Trim inference-irrelevant parts of the torch wheel (~145 MB): the C++ test binaries
# and the headers used only to build custom C++/CUDA extensions at runtime.
RUN rm -rf /app/.venv/lib/python3.12/site-packages/torch/test \
           /app/.venv/lib/python3.12/site-packages/torch/include

# Pre-compile the application package so the (non-root, read-only) runtime never has
# to write __pycache__ directories.
RUN /app/.venv/bin/python -m compileall -q /app/app

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="RModHub" \
      org.opencontainers.image.description="RNA modification site prediction web server (MultiRM sequence branch; DirectRM signal branch planned)" \
      org.opencontainers.image.source="https://github.com/rmodhub/RModHub" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="RModHub"

# Non-root runtime user. /data/uploads is reserved for the phase-2 nanopore branch
# (BAM + move-table uploads) and is the only writable location besides $HOME and /tmp.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data

WORKDIR /app

# Code and venv are owned by root and therefore read-only for the `app` user.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app

# OMP_NUM_THREADS / MKL_NUM_THREADS = 1 is the *container default* for torch intra-op
# parallelism: inference runs in FastAPI's threadpool, so one torch thread per request
# gives predictable throughput without oversubscribing a small CPU box. To trade
# throughput for single-request latency on a multi-core host set RMODHUB_TORCH_THREADS
# (read by app/config.py, applied via torch.set_num_threads), which overrides this.
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    RMODHUB_PREDICTOR=multirm

USER app

EXPOSE 8000

# /health returns 200 only once the model is loaded (503 before that). Model load is
# 2-4 s on a normal machine but can approach 30 s on a 1-core box, hence the generous
# start-period. python:slim ships no curl, so the probe is a stdlib one-liner.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

# Exactly ONE worker: every uvicorn worker would load its own copy of the model
# (~300-500 MB RSS each). Scale horizontally with container replicas instead.
# --proxy-headers/--forwarded-allow-ips trust X-Forwarded-* from the reverse proxy
# (Caddy in docker-compose.prod.yml) so generated URLs use https.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
