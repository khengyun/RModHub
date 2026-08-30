# syntax=docker/dockerfile:1.7
# RModHub web image: the built React app served by nginx, which also reverse-proxies the
# API paths (/api, /health, /docs, /openapi.json, /static) to the `api` container.
#
#   docker compose build web              (from the repository root)
#   docker build -t rmodhub-web:local .   (from frontend/)
#
# Stage 1 ("build") compiles the SPA (tsc + vite) and then FAILS the build if the bundle
# references any external resource (scripts/check-no-external-urls.mjs) - NAR Web Server
# Issue rule: no third-party assets. Stage 2 ("runtime") is the official nginx image with
# our vhost config and the static files; no node runtime, sources or tests ship.

ARG NODE_VERSION=24
ARG NGINX_VERSION=1.27

# ---------------------------------------------------------------------------
# Stage 1: build the static bundle
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS build

WORKDIR /app
ENV CI=true

# Dependencies first so this layer is cached until the lockfile changes. Dev
# dependencies are required here (tsc, vite, tailwind, and the vitest/jest-dom types
# referenced by tsconfig.json), so NODE_ENV must not be "production" at this point.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Sources (frontend/.dockerignore keeps node_modules, dist, e2e and tests out).
COPY . .

# `npm run build` = tsc --noEmit + vite build. The check script scans dist/ for any
# http(s) URL that is not an allow-listed plain hyperlink and exits non-zero.
RUN npm run build \
 && node scripts/check-no-external-urls.mjs dist

# ---------------------------------------------------------------------------
# Stage 2: runtime (nginx)
# ---------------------------------------------------------------------------
# Official image: the master process runs as root (binds :80, owns the cache dirs) and
# drops to the unprivileged `nginx` user for the worker processes that handle requests.
# docker-compose.yml adds `no-new-privileges`. (nginxinc/nginx-unprivileged would run
# fully rootless on :8080; switch the EXPOSE/healthcheck/compose port if you prefer it.)
FROM nginx:${NGINX_VERSION}-alpine AS runtime

LABEL org.opencontainers.image.title="RModHub web" \
      org.opencontainers.image.description="RModHub web UI (React) and reverse proxy to the RModHub API" \
      org.opencontainers.image.source="https://github.com/rmodhub/RModHub" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="RModHub"

# Replace the image's default vhost with ours (SPA + API proxy). The image's own
# /etc/nginx/nginx.conf (worker settings, mime types, log format) is kept.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# /healthz is answered by nginx itself (no dependency on the api container).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:80/healthz >/dev/null || exit 1
