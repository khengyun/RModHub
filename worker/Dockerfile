# syntax=docker/dockerfile:1.7
# RModHub signal worker image — Python 3.10 + ont-remora 3.2.0 + PyTorch (CPU-only), multi-stage.
#
#   docker build -t rmodhub-worker:local worker/
#   docker run --rm rmodhub-worker:local python -c "import remora, pod5, pysam, torch; print(torch.__version__)"
#
# Stage 1 ("builder") resolves the locked dependency set with uv into /app/.venv. ont-remora is
# published as an sdist only and compiles three Cython extensions, hence build-essential in this
# stage only. Stage 2 ("runtime") copies the venv, the worker package and the vendored DirectRM
# tree (scripts byte-identical to upstream, weights re-serialised to CPU) onto a fresh
# python:3.10-slim base; no compiler, lockfile tooling or tests end up in the shipped image.
#
# Python 3.10 (see README "Environment"): the lowest interpreter with a lib-pod5 >= 0.3.46 wheel,
# i.e. the lowest that reads POD5 v6 files (pod5 >= 0.3.46, current MinKNOW / dorado / pod5 CLI);
# every other pin (torch 2.8.0+cpu, numpy 2.0.2, ...) is unchanged from the 3.9 resolution.

ARG PYTHON_VERSION=3.10

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

# Compiler for the ont-remora sdist (Cython extensions); builder stage only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer 1: third-party dependencies only. Rebuilt only when pyproject.toml / uv.lock change.
# `--frozen` refuses to touch the lockfile; `--no-dev` skips pytest. torch resolves from the
# CPU-only index pinned in [tool.uv.sources] (see pyproject.toml), so no nvidia_* / CUDA wheels
# are pulled in. The project itself is not a package (tool.uv.package = false), so there is no
# second sync: the code is plain files under /app.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --frozen --no-dev

# Layer 2: worker package + vendored DirectRM (scripts, level tables, weights, licence).
COPY pyproject.toml uv.lock README.md ./
COPY rmodhub_worker ./rmodhub_worker
COPY directrm_vendor ./directrm_vendor

# Trim inference-irrelevant parts of the torch wheel: the C++ test binaries and the headers
# used only to build custom C++/CUDA extensions at runtime.
RUN rm -rf /app/.venv/lib/python3.*/site-packages/torch/test \
           /app/.venv/lib/python3.*/site-packages/torch/include

# Pre-compile the worker package so the (non-root, read-only) runtime never has to write
# __pycache__ directories. The vendored scripts are run as `python scripts/x.py` (never
# byte-compiled by CPython for __main__) and their `utils` imports are tiny.
RUN /app/.venv/bin/python -m compileall -q /app/rmodhub_worker

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="RModHub signal worker" \
      org.opencontainers.image.description="Celery worker running the vendored DirectRM pipeline (Remora + torch CPU) for the RModHub nanopore signal branch" \
      org.opencontainers.image.source="https://github.com/rmodhub/RModHub" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="RModHub"

# Same uid/gid as the API image: both containers share the /data/uploads volume as `app`.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data

WORKDIR /app

# Code, venv and the vendored model tree are owned by root and therefore read-only for `app`.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/rmodhub_worker /app/rmodhub_worker
COPY --from=builder /app/directrm_vendor /app/directrm_vendor

# OMP_NUM_THREADS / MKL_NUM_THREADS = 1 is the container default (one job per worker; the
# thread count the golden fixture was made with). docker-compose.yml passes
# RMODHUB_WORKER_THREADS (4) instead and the DirectRM child processes run with that count;
# reruns are byte-identical only at a fixed count. PYTHONHASHSEED=0 fixes the set()
# iteration order upstream relies on (see README "Determinism").
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    RMODHUB_UPLOAD_DIR=/data/uploads \
    RMODHUB_DIRECTRM_ROOT=/app/directrm_vendor

USER app

# Exactly ONE task at a time (a job is a multi-GB, multi-hour pipeline); scale with replicas.
CMD ["celery", "-A", "rmodhub_worker.celery_app", "worker", "-Q", "signal", "--concurrency=1", "--prefetch-multiplier=1", "--loglevel=info"]
