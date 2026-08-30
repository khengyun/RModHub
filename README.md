# RModHub — RNA modification site prediction server

RModHub predicts RNA modification sites (12 types: Am, Cm, Gm, Um, m1A, m5C, m5U, m6A, m6Am, m7G, Psi, A-to-I)
from a nucleotide sequence, as a web service. It is built for wet-lab users who do not code and
targets the *Nucleic Acids Research* Web Server Issue.

| | Branch A — sequence (**phase 1, this repo**) | Branch B — nanopore signal (phase 2) |
|---|---|---|
| Input | pasted ACGU sequence (51–10,000 nt) | BAM + move table |
| Model | [MultiRM](https://github.com/Tsedao/MultiRM) (Song *et al.*, Nat Commun 2021, MIT) | DirectRM |
| Execution | synchronous, sub-second | asynchronous job queue |

Both branches emit the **same row schema** (see [Result schema](#result-schema)), so one results
table / CSV export serves both.

## Quick start

```bash
docker compose up --build          # web UI on http://localhost:8080, API on http://localhost:8000
# ports busy? RMODHUB_WEB_PORT=18080 RMODHUB_PORT=18000 docker compose up --build
```

Open <http://localhost:8080>, press **Load sample data**, then **Predict modification sites**:
the 151-nt sample yields 22 sites at alpha = 0.05, shown in a track view and a filterable table
with CSV download. **Help** explains how to read the results.

Without Docker (needs [uv](https://docs.astral.sh/uv/) and Node ≥ 20):

```bash
uv sync                                    # installs CPU-only torch from download.pytorch.org/whl/cpu
uv run uvicorn app.main:app --port 8000    # backend            (or: make dev)
cd frontend && npm ci && npm run dev       # web UI on :5173, proxies /api to :8000 (or: make web-dev)
uv run pytest                              # backend tests      (or: make test)
```

Interactive API docs: <http://localhost:8000/docs> (Swagger UI is self-hosted from `app/static/swagger/`,
no CDN). The API also serves a minimal landing page with the license notice at <http://localhost:8000/>.

## API

### `POST /api/predict/sequence`

```json
{ "sequence": "GGGGCCGUGG...", "alpha": 0.05 }
```

- `sequence`: 51–10,000 nt, characters `A C G U T` (case-insensitive, whitespace ignored, `U` is mapped to `T`).
  A single FASTA record (`>id description` on the first line) is accepted; `id` is returned as `transcript_id`.
- `alpha`: significance level in (0, 1]; a site is reported when its empirical p-value is `< alpha`.
  Use `alpha=1` to get the full 12 × (N−50) matrix in long format.
- `?format=csv` returns the same rows as a downloadable CSV.

Response:

```json
{
  "results": [
    {"transcript_id": null, "position": 52, "mod_type": "Gm", "probability": 0.31,
     "p_value": 0.0267, "coverage": null, "source": "sequence"},
    ...
  ],
  "meta": {"sequence_length": 151, "predicted_start": 26, "predicted_end": 126, "alpha": 0.05,
           "n_sites": 22, "model_name": "MultiRM", "model_version": "trained_model_51seqs",
           "inference_ms": 180.4, "source": "sequence", "transcript_id": null, "mod_types": [...],
           "note": "MultiRM does not predict the first and last 25 nt of the input.", "extra": {...}}
}
```

Invalid input returns **422** with a plain-language `detail` (`"at least 51 nt"`, `"at most 10000 nt"`,
`"invalid character(s) in sequence: 'N'"`, `"alpha ..."`).

### `GET /api/samples/sequence`

Returns the built-in sample (151 nt from the MultiRM README) for a "Load sample" button.

### `GET /health`

`200 {"status": "ok", "model_loaded": true, ...}` once the model is in memory, `503` before.

## Web UI (`frontend/`)

React 19 + Vite 7 + TypeScript + Tailwind v4, served by nginx (`frontend/nginx.conf`) which also
proxies `/api`, `/health`, `/docs` to the API container. Built for wet-lab users and the NAR Web
Server Issue checklist:

| requirement | where |
|---|---|
| Load sample data (+ download it as FASTA) | form buttons |
| Filter (type, p-value, probability, position, text) and sort every column; pagination for poly-U inputs | results table |
| CSV download | `Download CSV` → backend `?format=csv` (all rows); `visible rows` → client-side |
| Visualisation | SVG track view: one lane per modification type, zoom/pan, nucleotide letters when zoomed in, **attention windows** highlighted for the selected site |
| Help that explains how to *read* results | `/help` (p-values, 25-nt flanks, the 12 types, several types at one position) |
| License on the landing page | footer + About strip on `/` |
| No third-party assets, no cookies, no login | system font stack, everything bundled; `npm run check:no-cdn` fails the Docker build on any external resource; the E2E suite records every network request and asserts none leaves the origin and that no cookie is set; nginx sends a `default-src 'self'` CSP |
| Sync / async ready | `/signal` tab and `/result/{job_id}` route reserved for the nanopore branch |

Commands (from `frontend/`): `npm run dev`, `npm run build` (tsc + vite), `npm run test`
(vitest, jsdom), `npm run check:no-cdn`, `npm run e2e` (Playwright, 31 tests, against
`E2E_BASE_URL`, default the Docker stack on `:8080`; `E2E_START_VITE=1` runs against a dev
server + a local backend on `:8000` instead).

A raw `grep -rE "https?://" dist/` is *not* empty, and that is expected: it finds XML namespace
identifiers (`w3.org`), documentation links embedded in React / React Router *error message
strings*, and the three credit hyperlinks (MultiRM repository, paper DOI, MIT license). None of
them is a resource the page loads — `check:no-cdn` classifies exactly these and fails on anything
else, and the `no-external-requests` E2E test verifies the runtime behaviour.

## Result schema

One row per (position, modification type). **Shared by both branches — do not change.**

| field | type | branch A (sequence) | branch B (signal) |
|---|---|---|---|
| `transcript_id` | `str \| null` | `null`, or FASTA id | transcript / read reference |
| `position` | `int` | 1-based position in the input sequence | 1-based position on the transcript |
| `mod_type` | `str` | one of the 12 `MOD_TYPES` | idem |
| `probability` | `float` | MultiRM sigmoid output | DirectRM score |
| `p_value` | `float \| null` | empirical, vs. 150 negative sequences (multiples of 1/150) | may be `null` |
| `coverage` | `int \| null` | always `null` | read depth |
| `source` | `"sequence" \| "signal"` | `"sequence"` | `"signal"` |

Only rows with `p_value < alpha` and `probability > 0` are returned. Positions 1–25 and N−24–N never
appear: MultiRM scores the centre of a 51-nt sliding window.

## How the model is served (no subprocess)

Upstream MultiRM is a CLI script (`main.py`) that hard-codes CUDA and reloads weights on every run.
Here it is vendored into `app/predictors/multirm/` and refactored into `MultiRMPredictor`:

- weights, k-mer embeddings and the negative background are loaded **once** in the FastAPI lifespan
  and kept in RAM (`app.state.predictor`);
- all 51-nt windows of a sequence are scored in batched forward passes under `torch.inference_mode()`
  (chunked to bound memory for 10 kb inputs);
- the `h_n.view(batch, 512)` bug in upstream `model_v3` (only correct for batch = 1) is replaced by a
  proper permute/reshape so batched results equal upstream single-window results;
- outputs are reshaped wide → long by `app/predictors/multirm/adapter.py`.

Every modification vs. upstream is listed in `app/predictors/multirm/vendor/UPSTREAM.md`.
`tests/fixtures/golden_multirm_151nt/` holds the matrices produced by the *unmodified* upstream code;
`tests/test_golden.py` requires the served model to reproduce them (probabilities to 1e-5, p-values exactly).

### Measured (2026-08-31, torch 2.13+cpu, 16-core dev box shared with other work)

| | 4 torch threads (default on multi-core) | 1 torch thread (1-core box / container default) |
|---|---|---|
| import torch + app | 0.16 s | ~1.5 s |
| `MultiRMPredictor.load()` (weights + embeddings + background) | 0.02 s, +17 MB RSS | 0.02 s |
| cold process → app ready | 0.80 s | — |
| 1st request, 151 nt (no warmup) | 60 ms | 114 ms |
| 2nd request, 151 nt | 32 ms | 104–112 ms |
| 10,000 nt (9,950 windows) | 3.7 s, +114 MB RSS | 12.7 s, peak RSS 395 MB |

For comparison, the upstream CLI costs ~3 s **per sequence** because it re-imports torch and
re-loads the weights every time; here that cost is paid once at startup.
`tests/test_perf.py` asserts the load-once behaviour (cold process: second request < first,
and far below startup + first) and `uv run python scripts/bench_multirm.py [--threads N]`
reproduces the table.

## Configuration

Environment variables (prefix `RMODHUB_`, see `.env.example`):

| variable | default | meaning |
|---|---|---|
| `RMODHUB_PREDICTOR` | `multirm` | `multirm` or `stub` (torch-free fake for UI work / CI) |
| `RMODHUB_MAX_SEQUENCE_NT` | `10000` | upper input limit (DoS guard) |
| `RMODHUB_WARMUP` | `true` | run one dummy inference at startup |
| `RMODHUB_TORCH_THREADS` | unset | torch intra-op threads. Unset → honour `OMP_NUM_THREADS` if present (the image sets 1), else `min(4, cpu_count)`. Torch's own default (all cores) is *slower* on a shared box |
| `RMODHUB_CORS_ORIGINS` | `[]` | JSON list; only needed if the frontend is served from another origin |

Run **one uvicorn worker per container** (the model lives in process memory); scale with container
replicas behind the reverse proxy instead.

## Production (HTTPS on 443)

```bash
cp .env.example .env            # set RMODHUB_DOMAIN
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`deploy/Caddyfile` terminates TLS with automatic Let's Encrypt certificates, adds security headers
and caps request bodies at 1 MB. The server sets no cookies, requires no login and loads no
third-party assets (landing page and `/docs` are fully self-hosted).

CI (`.github/workflows/ci.yml`) runs ruff + pytest and builds the image, boots it and posts the
sample sequence.

## Repository layout

```
app/
  main.py               FastAPI app factory + lifespan (loads the model once)
  config.py             pydantic-settings (RMODHUB_*)
  schemas.py            ModSite (shared schema), request/response models
  api/                  HTTP routers: predict, samples, health; normalize.py (input rules)
  landing.html, static/ landing page, favicon, self-hosted Swagger UI (Apache-2.0)
  predictors/
    base.py             SequencePredictor protocol + SequencePrediction
    stub.py             torch-free fake predictor
    multirm/            vendored MultiRM (vendor/, weights/), predictor.py, adapter.py
scripts/bench_multirm.py
tests/                  golden regression, validation, equivalence (U/T), perf (load-once proof)
frontend/
  src/api/              typed client + JSON fixtures captured from the real API
  src/pages/            SequencePage (tool + landing), HelpPage, SignalPage (phase-2 placeholder)
  src/components/       form/, results/ (table, filters, CSV), track/ (SVG track view), layout/
  src/lib/              modTypes (12 types: colour + description), sequence normalisation, download
  e2e/                  Playwright: sample flow (22 sites), filters/sort, CSV, validation, 10 kb, no-external-requests
  Dockerfile, nginx.conf, scripts/check-no-external-urls.mjs
Dockerfile, docker-compose.yml (api + web; postgres/redis/worker behind --profile phase2),
docker-compose.prod.yml + deploy/Caddyfile (HTTPS 443)
```

## Plugging in branch B (nanopore / DirectRM) later

Nothing in phase 1 needs to change; add alongside (backend first, then the UI):

1. **Predictor** — `app/predictors/directrm/` implementing a `SignalPredictor` protocol next to
   `SequencePredictor` in `app/predictors/base.py` (`predict(bam_path, move_table_path, ...) -> list[ModSite]`
   with `source="signal"`, `coverage` filled). Reuse the wide → long adapter pattern.
2. **Async execution** — `docker compose --profile phase2 up` already starts Postgres, Redis and a
   `worker` container (placeholder command). Replace the placeholder with `celery -A app.worker worker`,
   put job metadata in Postgres and results as rows of the same `ModSite` schema (or a CSV under the
   `uploads` volume).
3. **Endpoints** — `POST /api/predict/signal` (multipart upload → `202 {job_id}`),
   `GET /api/jobs/{job_id}` (status), `GET /api/jobs/{job_id}/results` (same `{results, meta}` body
   as the sequence endpoint, so the frontend table/CSV code is shared). Raise the Caddy
   `request_body max_size` for BAM uploads.
4. **Sample** — `GET /api/samples/signal` serving a small BAM + move table.
5. **UI** — fill in `frontend/src/pages/SignalPage.tsx` (upload form → job id), add
   `/result/:jobId` (poll, then render the same `ResultsTable` + `TrackView` from the shared
   `ModSite` rows; the table already shows `transcript_id` / `coverage` columns when present).

Out of scope for both phases so far: calibration of probabilities between the two branches.

## License

Server code: MIT (see `LICENSE`). Bundled model MultiRM: MIT, © 2021 Zitao Song
(`app/predictors/multirm/vendor/LICENSE`). Bundled Swagger UI: Apache-2.0
(`app/static/swagger/LICENSE`).
