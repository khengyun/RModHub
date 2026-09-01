# RModHub — RNA modification site prediction server

Predicts RNA modification sites from a **nucleotide sequence** (12 types, MultiRM) or from
**nanopore direct-RNA signal** (6 types, DirectRM). No login, no cookies, no third-party assets.

## Run

```bash
docker compose up --build     # UI on :8080, API on :8000 — sequence branch only
```

Both branches (adds Postgres, Redis, DirectRM worker):

```bash
cp .env.example .env          # set POSTGRES_PASSWORD, RMODHUB_IP_HASH_SECRET, COMPOSE_PROFILES=phase2
make phase2-up
make phase2-smoke
```

Without Docker ([uv](https://docs.astral.sh/uv/) + Node >= 20):

```bash
make dev                      # API on :8000
make web-dev                  # UI on :5173
```

```bash
make up | down | logs | ps | smoke | prod-up
```

## Test

```bash
make test                     # API
cd worker && uv run pytest    # worker
cd frontend && npm test       # UI
```

## Docs

| | |
|---|---|
| <http://localhost:8000/docs> | API reference (Swagger) |
| [`docs/reference.md`](docs/reference.md) | everything else |
| [`docs/signal-branch.md`](docs/signal-branch.md) | signal-branch contract |
| [`.env.example`](.env.example) | all settings |

## License

MIT. Vendored models MultiRM (MIT) and DirectRM (MIT); **Remora is ONT Public License 1.0,
research use only** — the signal branch is non-commercial. Details in [`docs/reference.md`](docs/reference.md).

Cite [MultiRM](https://doi.org/10.1038/s41467-021-24313-3) (sequence) or
[DirectRM](https://doi.org/10.1038/s41467-025-64495-8) (signal).
