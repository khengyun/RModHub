# Nanopore signal branch (DirectRM) — design contract

This document is the single source of truth shared by the API (`app/`), the worker
(`worker/`), the frontend (`frontend/`), the subset tool (`tools/`) and the tests. Every
name below (routes, JSON keys, table columns, file paths, env vars, task names) is a
contract: change it here first.

## 1. Overview

```
browser ──tus (PATCH chunks)──► api (FastAPI, py3.12) ──Celery (Redis)──► worker (py3.10 + Remora + torch CPU)
                                   │  Postgres: jobs / uploads metadata          │
                                   └──── shared volume /data/uploads ───────────┘
                                          jobs/<job_id>/{input,work,results.sqlite}
```

* The API never imports worker code; it enqueues **by task name**.
* The worker never imports `app/` (separate interpreter and lockfile; the API project is Python 3.12-only).
* Results live in one SQLite file per job on the shared volume; Postgres holds only job
  metadata (status, stage, progress, quotas, expiry).
* Upstream DirectRM scripts run **unmodified**, byte-identical, as subprocesses inside the
  worker container (`PYTHONPATH=<vendor root>`, `PYTHONHASHSEED=0`, `--device cpu`).

## 2. Model: DirectRM (vendored)

* Source: https://github.com/yuxinPenny/DirectRM @ `bc7a08573dfe7629e808256fa6ade6e4111ed1f9`, MIT
  (© 2025 Yuxin Zhang). Paper: Zhang et al., Nat Commun 16, 9450 (2025),
  https://doi.org/10.1038/s41467-025-64495-8
* Vendored at `worker/directrm_vendor/` : `scripts/`, `utils/`, `model/{RNA002,RNA004}/...`,
  `5mer_levels_v1.txt`, `9mer_levels_v1.txt`, `LICENSE`, `UPSTREAM.md`.
* Level tables are ONT `kmer_models` (MPL-2.0). Remora itself is under the Oxford Nanopore
  Technologies Public License 1.0 (research use only) — disclosed on the landing page.
* **Weights are re-serialised to CPU** at vendoring time (`torch.save(torch.load(p, map_location="cpu"))`)
  because upstream calls `torch.load` without `map_location` and the shipped files carry
  `cuda:0` storage tags. Values are identical (`torch.equal` for every tensor); the manifest
  `worker/directrm_vendor/WEIGHTS_MANIFEST.json` records the original sha256 of each file.
* Stage commands (split name is always `input`; `<V>` = vendor root, `<J>` = job dir):

| stage | command (cwd `<V>`, `PYTHONPATH=<V>`) |
|---|---|
| sampling | `python scripts/sampling.py --bam <J>/input --reg <J>/input/regions.csv -o <J>/work/reads.txt --splits input --min_coverage 30 --max_coverage 150` |
| features | `python scripts/feature_extraction.py --pod5_dir <J>/input --bam <J>/input --reg <J>/input/regions.csv --level <V>/{9mer,5mer}_levels_v1.txt -o <J>/work/features --splits input --read_ids <J>/work/reads.txt --kmer 9 --step 5` |
| denovo | `python scripts/denovo_inference.py --feature_dir <J>/work/features --outdir <J>/work/denovo --model_path <V>/model/<KIT>/id3_binary/model.pt --splits input --device cpu` |
| inference | `python scripts/inference.py --feature_dir <J>/work/features --outdir <J>/work/inference --device cpu --splits input --ml True --model_dir <V>/model/<KIT> --model_id 5` |
| aggregating | `python scripts/read2site.py --indir <J>/work/inference --outdir <J>/work/sites --delete False` then the worker builds `results.sqlite` |

  Input naming expected by upstream: `<J>/input/input.pod5`, `<J>/input/input_sorted.bam` (+ `.bai`),
  `<J>/input/reference.fa`, `<J>/input/regions.csv`. `KIT` ∈ {`RNA004` (9-mer table), `RNA002` (5-mer table)};
  `--kmer 9` for both kits (the models are 9-mer).
* Known upstream behaviour the worker must handle (do not "fix" upstream):
  * `_denovo.npy` is computed but not consumed by `inference.py` (filter lines are commented out).
    The worker stores the per-k-mer de novo probability summary in `meta` (`denovo_frac_modified`)
    and does **not** gate inference with it.
  * Empty feature file (0 k-mers) or std==0 in a dwell column crashes stages 3/4 → the worker
    checks `features/input.npz` after stage 2 and fails with a clear message.
  * `sampling.py` drops regions with ≤ 30 reads and randomly subsamples regions with ≥ 150 reads
    (unseeded). The worker reports both per region (`meta.regions_skipped_low_coverage`, `meta.regions_subsampled`).
  * Coordinates: `regions.csv` is 1-based inclusive (`seqnames,start,end,width,strand`); output
    `pos` is 1-based. Upstream never scores the first base of each region.
  * `read2site` `coverage` = number of reads with a non-zero score at that base for that type
    (not raw read depth); `count` = reads with score > 0.5.

## 3. Job directory layout (shared volume `RMODHUB_UPLOAD_DIR`, default `/data/uploads`)

```
/data/uploads/
  tus/<upload_id>            # bytes received so far (API writes)
  tus/<upload_id>.json       # {"job_id","slot","filename","length","offset","created_at"}
  jobs/<job_id>/
    input/                   # input.pod5, input_sorted.bam, input_sorted.bam.bai, reference.fa, regions.csv
    work/                    # reads.txt, features/, denovo/, inference/, sites/, logs/<stage>.log
    results.sqlite           # published atomically (write results.sqlite.tmp then os.replace)
```

`job_id` and `upload_id` are UUID4 strings (validated as `UUID` path params → no traversal).
Both containers run as uid/gid 1000 (`app`).

## 4. Postgres schema (owned by the API, `Base.metadata.create_all` in the lifespan; the
worker uses plain SQL with these exact column names)

`jobs`
| column | type | notes |
|---|---|---|
| id | varchar(36) PK | uuid4, also the Celery task id |
| status | varchar(16) | `uploading` `queued` `running` `done` `failed` `cancelled` `expired` |
| stage | varchar(16) null | `uploading` `preparing` `sampling` `features` `denovo` `inference` `aggregating` |
| progress | float null | 0..1 within the current stage (features: reads processed / reads sampled) |
| eta_s | float null | seconds, best effort |
| kit | varchar(8) | `RNA004` / `RNA002` |
| input_kind | varchar(8) | `upload` / `sample` |
| input_bytes | json | `{"pod5":..,"bam":..,"reference":..,"regions":..}` |
| params | json | free-form (model_id, min/max coverage); validated by the worker before the claim (section 11 item 39) |
| client_key | varchar(64) idx | HMAC-SHA256(client IP, `RMODHUB_IP_HASH_SECRET`) — raw IPs are never stored |
| created_at, started_at, finished_at, expires_at, inputs_deleted_at, results_deleted_at, cancel_requested_at, heartbeat_at | timestamptz null | |
| error | text null | one user-safe sentence |
| n_sites, n_reads, n_transcripts | int null | filled at `done` |
| model_name, model_version | varchar | `DirectRM`, `bc7a085` |
| worker_hostname | varchar null | |

`uploads`
| column | type |
|---|---|
| id varchar(36) PK, job_id FK→jobs.id (cascade), slot varchar(16) (`pod5` `bam` `reference` `regions`), filename varchar(255), length bigint, offset bigint default 0, complete bool default false, created_at, expires_at |

Worker → Postgres writes (only these): `status, stage, progress, eta_s, started_at, finished_at,
inputs_deleted_at, error, n_sites, n_reads, n_transcripts, worker_hostname, heartbeat_at`
(heartbeat at least every 30 s while running). Worker reads `cancel_requested_at` between stages.

## 5. `results.sqlite` (worker writes with stdlib `sqlite3`; API opens read-only)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);          -- json-encoded values
CREATE TABLE transcripts (transcript_id TEXT PRIMARY KEY, length INTEGER, n_reads INTEGER, n_sites INTEGER);
CREATE TABLE sites (
  id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL, position INTEGER NOT NULL, strand TEXT NOT NULL,
  mod_type TEXT NOT NULL, rate REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
  coverage INTEGER NOT NULL, count INTEGER NOT NULL, max_prob REAL, noisyor_prob REAL);
CREATE INDEX sites_tx_pos ON sites (transcript_id, position);
CREATE INDEX sites_mod ON sites (mod_type);
CREATE INDEX sites_cov ON sites (coverage);
CREATE TABLE reads (
  id INTEGER PRIMARY KEY, read_id TEXT NOT NULL, transcript_id TEXT NOT NULL, position INTEGER NOT NULL,
  strand TEXT NOT NULL, mod_type TEXT NOT NULL, probability REAL NOT NULL);
CREATE INDEX reads_site ON reads (transcript_id, position, mod_type);
CREATE TABLE regions (                                          -- one row per regions.csv data row, file order (section 11 item 38)
  id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
  strand TEXT NOT NULL, n_reads INTEGER NOT NULL);
```

* `rate = count / coverage`; `ci_low/ci_high` = 95 % **Wilson score interval** (z = 1.959964).
* `mod_type` values are normalised to the shared vocabulary: `ac4c→ac4C`, `m1a→m1A`, `m5c→m5C`,
  `m6a→m6A`, `m7g→m7G`, `psi→Psi` (same id as branch A).
* `meta` keys: `model_name`, `model_version`, `kit`, `directrm_commit`, `remora_version`, `torch_version`,
  `n_reads_sampled`, `n_reads_features`, `n_kmers`, `denovo_frac_modified`, `regions_total`,
  `regions_skipped_low_coverage`, `regions_subsampled`, `min_coverage`, `max_coverage`, `stage_seconds` (json).
* Rows are sorted by (transcript_id, position, mod_type) on insert so `ORDER BY id` is the canonical order.
* `regions`: `start` / `end` as in the normalised `regions.csv` (1-based inclusive), `n_reads` = alignment
  records overlapping the region, counted in `preparing` (the per-region list is not in `meta`; item 38).

## 6. HTTP API (all under the same origin; no cookies, no login)

Common: errors are `{"detail": "<one plain sentence>"}`; 404 unknown/expired job; 409 wrong
state; 422 invalid input or over a cap; 429 quota; 503 signal branch not enabled; 503 with
`Retry-After: 10` when the job database is unreachable (section 11 item 34).
All job/upload responses send `Cache-Control: no-store`.

`GET /api/capabilities` → `{"sequence": true, "signal": bool, "limits": {"max_pod5_gb", "max_bam_gb", "max_reference_mb",
"max_regions", "max_running_per_ip", "max_queued_per_ip", "job_timeout_h", "tus_chunk_mb", "upload_ttl_h"},
"retention": {"inputs_deleted": "after feature extraction, at most 48 h", "results_days": 14}}`
(`max_bam_gb` = `RMODHUB_MAX_BAM_GB`, `upload_ttl_h` = `RMODHUB_UPLOAD_TTL_H`; section 11 items 4 and 45)

`GET /api/samples/signal` → `{"name","description","kit","files":[{"slot","filename","bytes","url"}],"source":"synthetic","regions":[...]}`;
`GET /api/samples/signal/files/{filename}` serves the sample files (attachment).

`POST /api/jobs/signal` (multipart/form-data: `pod5`, `bam`, `reference`, `regions` files; `kit` field, default `RNA004`)
→ `202 JobStatus` (status `queued`). Files are streamed to `jobs/<id>/input/` (no full buffering).

`POST /api/jobs/signal/init` (JSON `{"kit":"RNA004","files":{"pod5":{"name","size"},"bam":{...},"reference":{...},"regions":{...}}}`)
→ `201 JobStatus` with `status:"uploading"` and `uploads: {slot: {"url": "/api/uploads/<upload_id>", "length", "offset", "complete"}}`.
Caps and quotas are enforced here (sizes are declared up front).

tus 1.0.0 core + termination on `/api/uploads/{upload_id}`:
* `HEAD` → 200, `Upload-Offset`, `Upload-Length`, `Tus-Resumable: 1.0.0`, `Cache-Control: no-store`
* `PATCH` (`Content-Type: application/offset+octet-stream`, `Upload-Offset`) → 204 + `Upload-Offset`;
  409 if the offset does not match; 413 if a chunk exceeds `tus_chunk_mb`; body is streamed to disk.
* `DELETE` → 204. `OPTIONS /api/uploads` → `Tus-Version`, `Tus-Extension: termination`, `Tus-Max-Size`.

`POST /api/jobs/{job_id}/start` → 202 `JobStatus` (`queued`) once all four uploads are complete
(else 409 listing the incomplete slots). Files are moved (same filesystem rename) into `input/`.

`POST /api/jobs/signal/sample` → 202 `JobStatus` (server copies `app/samples/signal/*` into a new job; quotas apply).

`GET /api/jobs/{job_id}` → `JobStatus`:
```json
{"job_id":"…","status":"running","stage":"features","progress":0.42,"eta_s":95.0,
 "kit":"RNA004","input_kind":"upload","input_bytes":{"pod5":…,"bam":…,"reference":…,"regions":…},
 "created_at":"…","started_at":"…","finished_at":null,"expires_at":null,"inputs_deleted_at":null,
 "cancel_requested":false,"error":null,"n_sites":null,"n_reads":null,"n_transcripts":null,
 "model":{"name":"DirectRM","version":"bc7a085"},"uploads":null}
```
(`uploads` is filled only while `status == "uploading"`.)

`GET /api/jobs/{job_id}/results?level=site|read&offset=0&limit=100&transcript_id=&mod_type=&position=&strand=&min_coverage=&sort=position|rate|coverage|mod_type&order=asc|desc`
→ `{"results":[…],"meta":{…},"total":N,"offset":0,"limit":100}` (limit ≤ 1000; 409 unless `done`;
`sort=position` is the CSV order and `order=desc` its exact reverse, read-level `rate` / `mod_type`
sorts need `transcript_id` + `position`, read-level `coverage` is 422 — section 11 item 31).
* `level=site` rows are `SignalSite` = the shared `ModSite` fields **first**
  (`transcript_id, position, mod_type, probability (= rate), p_value: null, coverage, source: "signal"`)
  plus `strand, count, ci_low, ci_high, max_prob, noisyor_prob`.
* `level=read` rows: `{"read_id","transcript_id","position","strand","mod_type","probability","source":"signal"}`;
  `transcript_id` + `position` (+ optional `mod_type`, `strand=+|-`) filter a site for drill-down
  (`strand` matters on a contig with regions on both strands; it applies at either level; send `+` as
  `%2B` — a bare `+` decodes to a space and answers 422; section 11 item 47).
* `meta`: `{"source":"signal","job_id","model_name","model_version","kit","n_sites","n_reads","n_transcripts",
  "mod_types":["ac4C","m1A","m5C","m6A","m7G","Psi"],"low_coverage_threshold":30,
  "transcripts":[{"transcript_id","length","n_reads","n_sites"}], "extra": {…all results.sqlite meta…}}`

`GET /api/jobs/{job_id}/download.csv?level=site|read` → streaming `text/csv`, attachment
`rmodhub_signal_<job_id>_<level>s.csv`. Site CSV header: the shared 7 columns first
(`transcript_id,position,mod_type,probability,p_value,coverage,source`) then `strand,count,ci_low,ci_high,max_prob,noisyor_prob`.
Read CSV header: `read_id,transcript_id,position,strand,mod_type,probability,source`. Identifier cells
(`transcript_id`, `read_id`) are formula-neutralised (section 11 item 35).

`POST /api/jobs/{job_id}/cancel` → 200 `JobStatus` (`cancelled`); 409 if already terminal.

## 7. Celery

* Broker `CELERY_BROKER_URL` (redis db 0), result backend disabled (status lives in Postgres).
* Task `rmodhub.signal.run_job(job_id: str)`, queue `signal`, `task_id = job_id`,
  `acks_late=False`, `max_retries=0`, `soft_time_limit = RMODHUB_JOB_TIMEOUT_S` (default 21600),
  `time_limit = soft + 300`. Worker: `--concurrency=1 --prefetch-multiplier=1 -Q signal`.
* Cancel: API `revoke(job_id, terminate=True, signal="SIGTERM")` for running jobs; the worker's
  SIGTERM/soft-timeout handler kills the child process group, marks `cancelled`/`failed` and removes the job dir.
* Dead worker: the API reaper marks `running` jobs whose `heartbeat_at` is older than 10 min as
  `failed` ("The worker stopped responding").

## 8. Limits, quotas, lifecycle (env → `app/config.py`, prefix `RMODHUB_`; worker reads the same names;
the rows marked *deployment only* are read by compose / the api entrypoint / Caddy, not by `app/config.py`)

| env | default | meaning |
|---|---|---|
| `DATABASE_URL` / `RMODHUB_DATABASE_URL` | unset → signal branch disabled | SQLAlchemy URL (`postgresql+psycopg://…`, tests use `sqlite+pysqlite:///…`) |
| `CELERY_BROKER_URL` / `RMODHUB_CELERY_BROKER_URL` | unset | redis URL; tests use the null queue |
| `RMODHUB_UPLOAD_DIR` | `/data/uploads` | shared volume |
| `RMODHUB_MAX_POD5_GB` (alias `MAX_POD5_GB`) | 5 | pod5 cap |
| `RMODHUB_MAX_BAM_GB` | 5 | BAM cap |
| `RMODHUB_MAX_REFERENCE_MB` | 500 | reference.fa cap |
| `RMODHUB_MAX_REGIONS` | 10000 | regions.csv data rows |
| `RMODHUB_MAX_RUNNING_PER_IP` | 1 | jobs in `running` |
| `RMODHUB_MAX_QUEUED_PER_IP` | 3 | jobs in `uploading`+`queued` |
| `RMODHUB_JOB_TIMEOUT_S` | 21600 | hard cap 6 h |
| `RMODHUB_RESULTS_RETENTION_DAYS` | 14 | results kept |
| `RMODHUB_INPUTS_MAX_AGE_H` | 48 | backstop: `input/` of a job older than this whose worker never deleted its inputs (`inputs_deleted_at` null) is removed whatever the status (section 11 item 23) |
| `RMODHUB_SAMPLE_DIR` | `app/samples/signal` (in the package) | directory of the synthetic sample served by `GET /api/samples/signal` and copied by `POST /api/jobs/signal/sample`; missing → those routes answer 404 (section 11 item 26) |
| `RMODHUB_WORKER_THREADS` | 1 (image); 4 under `docker-compose.yml` | worker only: OMP/MKL threads of the DirectRM child processes (section 11 item 24) |
| `RMODHUB_UPLOAD_TTL_H` | 48 | unfinished uploads / `uploading` jobs expire |
| `RMODHUB_TUS_CHUNK_MB` | 64 | max PATCH body (nginx/Caddy allow 64 MiB on upload paths; client sends 16 MiB) |
| `RMODHUB_CLEANUP_INTERVAL_S` | 3600 | in-process cleanup period (also `python -m app.jobs.cleanup` for cron) |
| `RMODHUB_IP_HASH_SECRET` | unset; `rmodhub-dev` is the development default (warned) outside the image, but the api **container** refuses to start the signal branch with it (section 11 item 27) | HMAC key for `client_key`; generate with `openssl rand -hex 32` |
| `RMODHUB_ALLOW_DEV_SECRET` | unset | *deployment only*: `1` lets the api container run the signal branch with the development key (item 27) |
| `RMODHUB_TRUSTED_PROXIES` | unset → the api container's own network(s) minus Docker's gateway | *deployment only*, api container: proxies whose `X-Forwarded-For` / `-Proto` uvicorn believes (comma-separated IPs / CIDRs → `FORWARDED_ALLOW_IPS`; items 29–30) |
| `RMODHUB_MULTIPART_MAX_SIZE` | `11GiB` | *deployment only*, Caddy (`docker-compose.prod.yml`): outer body bound of `POST /api/jobs/signal`, keep ≥ pod5 + BAM + reference caps + ~80 MiB (item 29) |
| `COMPOSE_PROFILES` | unset | *deployment only*, compose: `phase2` includes `postgres` / `redis` / `worker` in every command (item 28) |

Lifecycle rules: pod5 + BAM are deleted by the worker immediately after feature extraction
(`inputs_deleted_at`); the cleanup loop (API lifespan, hourly, plus CLI) deletes the `input/`
of any job older than 48 h whose inputs the worker has not deleted (section 11 item 23),
deletes job dirs past `expires_at` (= `finished_at` + 14 d) and marks them `expired`, removes
stale tus uploads, and logs the number of bytes freed per run. The reference FASTA and
`regions.csv` are not deleted by the worker; they leave with the job directory.

## 9. Frontend

* Tabs: **Sequence** (`/`), **Nanopore signal** (`/signal`, shown only when `GET /api/capabilities`
  reports `signal: true`), **Help**, **API docs**. No phase labels, no health indicator.
* `/result/:jobId`: public, bookmarkable; polls `GET /api/jobs/{id}` (2 s → 10 s backoff) while
  `queued`/`running`; shows stage, progress, ETA, cancel; on `done` renders site-level table +
  track view per transcript, coverage < 30 warning, drill-down to read-level, CSV download.
* Resumable upload: hand-written tus client (XHR, 16 MiB chunks, retry with HEAD-offset resume,
  best-effort resume across reloads via localStorage fingerprint). No external assets.

## 10. Sample data

`app/samples/signal/` (generated by `scripts/make_signal_sample.py`, seed fixed): synthetic
RNA004-like reads, 3 transcripts (`tx_A` 40 reads, `tx_B` 36 reads, `tx_C` 12 reads → below the
30-read threshold, demonstrates the coverage filter), ~1 MB total, dorado move-table convention
(`rna_raw`). Clearly labelled *synthetic* in the UI and README.

## 11. Implementation notes and deviations (wave 1)

Wave 1 implemented sections 1–10 in `app/`, `worker/`, `frontend/` and `tools/`. The
points below are where the code deliberately differs from, or fills a gap in, the text
above. They are **accepted** and supersede the earlier wording; everything not listed here
is implemented as written.

### API (`app/`)

1. **`GET /api/jobs/{job_id}` on an expired job → `200` with `status: "expired"`** (so the UI
   can say so), not `404`. Only `GET …/results` and `GET …/download.csv` answer `404`
   ("The results of this job have expired and were deleted.") once the results are gone.
   Section 6's "404 unknown/expired job" applies to those two routes and to unknown ids.
2. Non-UUID job / upload ids answer `404` with the same sentence as unknown ids (a FastAPI
   `UUID` path param would give `422`); the UUID check still happens before any path join.
3. **`DELETE /api/uploads/{upload_id}` cancels the whole job** (a job needs all four files):
   every received file is removed and the job becomes `cancelled`; `409` if the job has
   already left `uploading`.
4. `GET /api/capabilities` → `limits` carries one **additional key `max_bam_gb`**
   (`RMODHUB_MAX_BAM_GB`, default 5) next to the seven keys section 6 originally listed
   (section 6 now names it, and `upload_ttl_h` of item 45). The UI uses it for the BAM size
   check and falls back to `max_pod5_gb` when it is absent.
5. Cancel of a `running` job: the API sets `status = cancelled`, `finished_at`,
   `cancel_requested_at` and `expires_at = now + 1 h` (cleanup backstop) **immediately**, then
   `revoke(terminate=True, SIGTERM)`; the worker's SIGTERM handler removes the job directory.
   `uploading` / `queued` cancels remove the files at once and revoke without terminate.
   The worker also re-checks `jobs.status` before starting and between stages, so a cancel is
   honoured even if the revoke broadcast is lost.
6. Input sanity checks beyond the caps (all `422` with one sentence): POD5 file signature,
   BGZF magic for the BAM, FASTA must start with `>`, `regions.csv` header must contain
   `seqnames,start,end,width,strand` with integer 1-based intervals and `+`/`-` strands;
   the multipart route rejects early when `Content-Length` exceeds the sum of the caps
   (+ 64 MiB regions + 16 MiB framing).
7. tus details: `Tus-Resumable` is optional on requests (`412` only when present with another
   version); missing/invalid `Upload-Offset` → `422`; a `Content-Type` other than
   `application/offset+octet-stream` → `415`. Per-upload locks are in-process (one uvicorn
   worker per container, as the Dockerfile enforces).
8. The in-process cleanup loop runs its first pass one `RMODHUB_CLEANUP_INTERVAL_S` after
   start-up (never immediately after a restart); `python -m app.jobs.cleanup` runs one pass now.
9. `docker-compose.yml` derives **`DATABASE_URL` and `CELERY_BROKER_URL` from
   `POSTGRES_PASSWORD`** (`${VAR:+…}`), so a plain `docker compose up` starts with the branch
   disabled and `POSTGRES_PASSWORD` + `--profile phase2` is the single switch. There is no
   `CELERY_RESULT_BACKEND` (result backend disabled, as in section 7). Outside compose set the
   two URLs directly. Superseded in part by item 28: the switch is now the password **plus**
   `COMPOSE_PROFILES=phase2` **plus** `RMODHUB_IP_HASH_SECRET`.

### Worker (`worker/`)

10. **Python 3.10, not 3.9.** Section 1's diagram says `py3.9`; the worker project targets
    Python 3.10 because the newest lib-pod5 wheel for 3.9 (0.3.35) cannot open **POD5 v6**
    files (32-bit `channel` column, written by pod5 / MinKNOW / dorado >= 0.3.46 — i.e. by
    current instruments). The worker pins `pod5`/`lib-pod5` 0.3.47 and reads both POD5 v5
    and v6; it still never imports `app/` (the API is 3.12). The
    committed sample POD5 is written with pod5 0.3.35 (format v5) so it opens under every
    reader; `scripts/make_signal_sample.py` refuses newer pod5 versions unless
    `--allow-newer-pod5` is given, and `tools/Dockerfile.subset` pins pod5 0.3.35 as well.
11. **`jobs.n_reads` = number of distinct reads that produced features**
    (`meta.n_reads_features`, 76 on the sample), not the `reads`-table row count (14,027) nor
    the number of reads in the pod5 (`meta.n_reads_pod5`, 88). Section 4 left it undefined.
12. **`transcripts` table scoped to the contigs named in `regions.csv`** (not every contig of
    the reference), so a whole-transcriptome FASTA does not put 10^5 rows into every
    `results.meta`. Per contig: `length` from the FASTA index, `n_reads` = mapped alignment
    records from the BAM index (`get_index_statistics`, computed in `preparing` before the BAM
    is deleted), `n_sites` from the sites table. `n_transcripts` counts these rows.
13. **SIGTERM semantics.** SIGTERM with `jobs.cancel_requested_at` set (or the database
    unreachable) → `cancelled`; SIGTERM without a cancel request (worker cold shutdown,
    `docker stop`) → `failed`, "The worker was stopped while the job was running."; soft time
    limit → `failed`, "The job exceeded the 6 h limit and was stopped." In all three cases the
    child process group is killed and the job directory removed; the child then re-raises
    SIGTERM to itself so the prefork pool observes the terminate it requested. Jobs do not
    survive a worker restart (they must be resubmitted).
14. **Heartbeat every 15 s** (`heartbeat_at`, `progress`, `eta_s`), stricter than the "at least
    every 30 s" of section 4; the API reaper threshold stays 10 min.
15. `regions.csv` is always rewritten normalised (`seqnames,start,end,width,strand`, whitespace
    stripped, `width` recomputed) before upstream reads it. The pod5 ∩ BAM read-id check is
    evaluated on the alignment records overlapping the requested regions (up to 50,000
    names), not on the first 500 records of the file; the `mv` / `MD` inspection uses the
    first 500 mapped primary records and any of them lacking `mv` fails the job.
16. `meta` in `results.sqlite` has extra keys beyond the section 5 list (`n_reads_pod5`,
    `n_reads_resquiggled`, `bam_sorted_by_worker`, `bam_indexed_by_worker`,
    `md_added_by_worker`, `directrm_model_id`, `numpy_version`, `pysam_version`,
    `python_version`, `worker_version`, `threads`, …); the API exposes all of them under
    `meta.extra`. The per-region read counts that used to be `meta.region_read_counts` now
    live in the `regions` table (item 38). Child processes additionally get `TQDM_DISABLE=1` and
    `PYTHONDONTWRITEBYTECODE=1`.
17. `jobs.params` (`model_id`, `min_coverage`, `max_coverage`) is honoured by the worker when
    present; otherwise `RMODHUB_DIRECTRM_MODEL_ID` / `RMODHUB_MIN_COVERAGE` /
    `RMODHUB_MAX_COVERAGE` apply (defaults 5 / 30 / 150). The API records the defaults.
    Validation of the three values: item 39.

### Frontend (`frontend/`)

18. **The p-value column is hidden for signal rows** (whenever every row of a result set has
    `p_value: null`); sequence results are unchanged. Signal rows add Strand, 95 % CI
    (`[low, high]`, sortable by `ci_low`), Coverage and Modified reads columns, and the
    client-side "visible rows" CSV appends `strand,count,ci_low,ci_high,max_prob,noisyor_prob`
    after the shared seven columns (same order as the server CSV).
19. The **Nanopore signal** tab is hidden while `/api/capabilities` is still loading (not
    shown-then-hidden); `/signal` shows a one-line status until the answer arrives and the
    server's 503 sentence when the branch is disabled. A malformed `/result/:jobId` (non-UUID)
    renders "job not found" without calling the API.
20. Site rows are fetched in pages of 1,000 up to 20,000 rows (a notice is shown beyond that);
    read-level rows are paged server-side.

### Tools and sample (`tools/`, `scripts/`, `app/samples/signal/`)

21. `tools/subset_pod5.py` adds a `--force` flag (refuse to overwrite outputs otherwise) and
    prints a `WARNING` (does not refuse) when it runs under pod5 > 0.3.45; the Docker image
    and the documented `uv run --with "pod5==0.3.35"` command pin the writer version.
22. The synthetic sample is 1.36 MB (section 10 says "~1 MB"): 88 reads at ~30 samples per
    base over 500–600-nt transcripts. Expected numbers: 88 reads, 76 sampled (`tx_C` skipped),
    3,648 k-mers, 725 sites, 14,027 read-level rows, 3 transcripts.

### Lifecycle, deployment and configuration (review follow-up)

Recorded after the wave-1 review; the section 8 rows they refer to carry a pointer here.

23. **Input backstop scope.** `_delete_old_inputs` (`app/jobs/cleanup.py`) removes the
    `input/` of jobs with `created_at` older than `RMODHUB_INPUTS_MAX_AGE_H` **and**
    `inputs_deleted_at IS NULL` (status other than `uploading`; `uploading` jobs are handled by
    the `RMODHUB_UPLOAD_TTL_H` rule, which removes the whole directory). The worker deletes only
    `*.pod5`, `*.bam`, `*.bai` after `features` (`worker/rmodhub_worker/lifecycle.py`) and then
    stamps `inputs_deleted_at`, so for every job the worker processed `reference.fa` and
    `regions.csv` stay in `input/` until the job directory goes: at once on cancel / timeout /
    worker stop, otherwise at `expires_at` (`finished_at` + 14 d, also for `failed` jobs, whose
    directory is kept for diagnosis). Section 8's original wording ("inputs older than this are
    deleted whatever the state") holds for pod5 + BAM (they never outlive 48 h after job
    creation) but not for the reference and regions; the README, landing page and Help page
    describe this rule.
24. **Worker threads and determinism.** The child processes get `OMP_NUM_THREADS` /
    `MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` = `RMODHUB_WORKER_THREADS` (falls back to the
    container's `OMP_NUM_THREADS`, else 1). The image sets 1; `docker-compose.yml` sets
    `OMP_NUM_THREADS`/`MKL_NUM_THREADS` to `${RMODHUB_WORKER_THREADS:-4}` on the worker, so a
    compose deployment runs DirectRM with 4 threads. Section 1's `PYTHONHASHSEED=0` fixes the
    k-mer order; with it, reruns at a fixed thread count are byte-identical (verified with two
    1-thread and two 4-thread runs on the sample). The thread count changes torch's summation
    order: 4 threads vs 1 leaves `reads.txt`, the features and every `count` / `coverage` /
    `rate` identical on the sample and moves per-read probabilities by at most 6e-8, inside
    the golden test's 1e-6 tolerance but not byte-identical to the fixture, which was made
    with 1 thread (`worker/tests/conftest.py` pins `worker_threads=1`).
25. **nginx sets no body limit on `POST /api/jobs/signal`.** `frontend/nginx.conf` uses
    `client_max_body_size 0` (unlimited, unbuffered) on that route; the outer bound exists
    only in `deploy/Caddyfile` (production; `RMODHUB_MULTIPART_MAX_SIZE`, default 11 GiB —
    above the API's own ~10.6 GiB total with the default caps, item 29). Without Caddy the route is bounded by the
    API alone: item 6's `Content-Length` pre-check and the per-file caps applied while
    streaming. The 1 MiB default and the 64 MiB tus-chunk limit are the same in both proxies.
26. **`RMODHUB_SAMPLE_DIR`** (`app/config.py::Settings.sample_dir`, default
    `app/samples/signal` inside the package) is the directory section 6 refers to as
    `app/samples/signal/*`; `GET /api/samples/signal`, its file route and
    `POST /api/jobs/signal/sample` read from it and answer 404 ("The sample data set is not
    installed on this server.") when it is missing. Listed in the README configuration table
    and in `.env.example` (compose does not forward it; the image uses the packaged default).

### Deployment (review follow-up)

Recorded after the deploy review of the wave-1 code; `deploy/api-entrypoint.py` is the
`ENTRYPOINT` of the api image.

27. **The api container refuses to start the signal branch with the development HMAC key.**
    `deploy/api-entrypoint.py` exits with status 3 and a message (pointing at
    `openssl rand -hex 32`) when `DATABASE_URL` / `RMODHUB_DATABASE_URL` is set and
    `RMODHUB_IP_HASH_SECRET` is unset, empty or `rmodhub-dev`; `RMODHUB_ALLOW_DEV_SECRET=1`
    opts out for local experiments. `docker-compose.yml` no longer bakes `rmodhub-dev` in (the
    variable is forwarded only when set). Outside the image (`uv run uvicorn`) the application
    still only warns, which is what section 8 used to say for every deployment.
28. **The signal-branch switch is `POSTGRES_PASSWORD` + `COMPOSE_PROFILES=phase2` +
    `RMODHUB_IP_HASH_SECRET`** (item 9 said password + `--profile phase2`). Compose reads
    `COMPOSE_PROFILES` from `.env` for every command, `down` and the prod override included,
    so the three profile services are neither forgotten on `up` nor orphaned on `down`;
    `make` derives `--profile phase2` from the password (`.env` or the shell) for `up` /
    `prod-up` / `logs` / `ps` and always passes it to `down` / `prod-down`. `make phase2-check`
    (a prerequisite of `phase2-up`, and of `up` / `prod-up` whenever a password is set)
    rejects a missing password, `@ % $` or whitespace in it (`docker-compose.yml` splices it
    raw into `DATABASE_URL`; `: / ? #` are fine; `openssl rand -hex 24`) and a missing or
    development HMAC key; secrets are exported to the recipe shell, never echoed.
29. **Three deployment-only variables** (not read by `app/config.py`; section 8 lists them):
    `RMODHUB_TRUSTED_PROXIES` (api container only; comma-separated IPs / CIDRs mapped to
    uvicorn's `FORWARDED_ALLOW_IPS`; default = the container's own attached network(s), read
    from `/proc/net/route` and `/proc/net/ipv6_route` at start-up, **minus the default
    gateway**, which is where Docker's userland proxy, the host itself and IPv6 clients arrive
    from; `python /app/entrypoint.py --show` prints the list), `RMODHUB_MULTIPART_MAX_SIZE`
    (Caddy only, `docker-compose.prod.yml` → `deploy/Caddyfile`; default `11GiB`, above the
    API's ~10.6 GiB total with the default caps, raise it with them) and
    `RMODHUB_ALLOW_DEV_SECRET` (item 27). The api `CMD` no longer carries
    `--forwarded-allow-ips=*`.
30. **Quota-key trust model.** `client_key` is derived from `X-Forwarded-For` only when the
    direct peer is a trusted proxy (item 29); uvicorn walks the header from the right and
    stops at the first untrusted address, so a client cannot choose its own quota key by
    sending the header (with `*` uvicorn would take the *first*, client-written, entry).
    nginx (`web`) keeps `$proxy_add_x_forwarded_for` (append), the correct form behind Caddy.
    A reverse proxy running on the host reaches the published port from the gateway address
    and must be named in `RMODHUB_TRUSTED_PROXIES`; a port published directly to the internet
    keys on the real peer address whatever the header says. CI asserts the trusted list, the
    two-hop header from a network peer and the spoof from the runner host.

### API (review follow-up)

31. **Sort semantics.** `sort=position` (the default) is the canonical order of the CSV
    download — `(transcript_id, position, mod_type, id)`, rows of one transcript contiguous,
    text columns in byte-wise (SQLite `BINARY`) order, so `Psi` sorts before `ac4C` … `m7G`
    — and `order=desc` is its exact reverse; the other keys tie-break on the same tuple.
    Read-level rows are sortable by `rate` / `mod_type` only within one site
    (`transcript_id` + `position` given; otherwise `422`), and `sort=coverage` at read level
    answers `422` (no such column); both walk the `reads_site` index instead of sorting
    10^7–10^8 rows per page.
32. **tus / start reconciliation.** `PATCH` never NUL-pads: when the file on disk is shorter
    than the row's `offset` (a lost write) the row and sidecar are resynced to the on-disk
    size and the request answers `409` with that `Upload-Offset`; truncation only ever
    shrinks. `POST …/start` answers `409` "Uploads incomplete for: …" and **reopens**
    (offset = bytes on disk) uploads whose file is missing or short, reopens at offset 0 a
    slot whose file fails validation (`422`), and accepts files an earlier `start` already
    moved into `input/`. Per-upload locks are reference-counted, and the quota
    check-then-insert of `init` / multipart / sample is serialised per client key
    (in-process striped lock plus `pg_advisory_xact_lock` on Postgres).
33. **Lifecycle additions** (`app/jobs/cleanup.py`): a `queued` job older than
    `RMODHUB_INPUTS_MAX_AGE_H` becomes `failed` ("The job waited longer than 48 h for a
    worker and its input files were removed; please resubmit.") when its `input/` goes; the
    1 h backstop of item 5 does not reap a `cancelled` job whose `heartbeat_at` is younger
    than 10 min (the worker is still tearing down; reported as deferred) — the worker's
    terminal writes are conditional too (item 40); and **terminal rows are purged**
    `RMODHUB_RESULTS_RETENTION_DAYS` after `results_deleted_at` (`RMODHUB_UPLOAD_TTL_H` after
    it for jobs that never started), after which `GET /api/jobs/{id}` answers `404` — this
    bounds item 1's `expired` visibility to one further retention period. The cleanup
    summary line carries the new counters (deferred, timed-out queued, purged rows).
34. **Database outage → `503`** `{"detail": "The job database is not reachable; please try
    again later."}` with `Retry-After: 10` on every job / upload route (SQLAlchemy
    `OperationalError` / `InterfaceError` handler) instead of a 500. `GET /api/capabilities`
    keeps reporting `signal` from the configuration flag.
35. **CSV formula neutralisation.** In `download.csv` (both levels) and in the sequence
    branch's `?format=csv`, a `transcript_id` / `read_id` that starts with `=`, `+`, `-`,
    `@`, TAB or CR is prefixed with `'` (`app/csvio.py::text_cell`); JSON responses are
    unchanged, `mod_type` / `strand` are a fixed vocabulary and left verbatim. The
    client-side "visible rows" CSV of item 18 is not covered.
36. **`CELERY_BROKER_URL` is a secret** (`SecretStr`, redacted as `***` in the start-up log
    like `DATABASE_URL`), and any **empty or whitespace-only** `RMODHUB_*` / alias environment
    value means "unset" (model-level before-validator), so `RMODHUB_IP_HASH_SECRET=` in a
    `.env` does not become an empty HMAC key.
37. **Landing page and OpenAPI description are rendered from settings**: `app/landing.html`
    and `app/main.py::DESCRIPTION` carry `{{inputs_max_age_h}}`, `{{results_retention_days}}`
    and `{{upload_ttl_h}}` placeholders filled at start-up, so a deployment with other
    retention values does not advertise "48 h" / "14 days".

### Worker (review follow-up)

38. **`results.sqlite` has a fifth table `regions`** (`id, transcript_id, start, end, strand,
    n_reads`; section 5 shows it), one row per `regions.csv` data row in file order with the
    read count measured in `preparing`. `meta.region_read_counts` (item 16) **no longer
    exists**: the per-region list was ~1 MB at 10,000 regions and the API inlines `meta` into
    every paginated `/results` response; `meta` keeps the aggregates `regions_total`,
    `regions_skipped_low_coverage`, `regions_subsampled`.
39. **`jobs.params` is validated before the claim** (item 17): `model_id` an integer 1..8,
    `min_coverage` an integer ≥ 0, `max_coverage` an integer ≥ 1 and > `min_coverage`
    (numeric strings accepted; bool / float rejected; `null` = default). Anything else fails
    the job at once with "The job's parameters are invalid: <problem>." — it is never marked
    `running`. A failure while constructing the pipeline after the claim also ends in
    `failed` rather than a job stuck in `running`.
40. **Atomic claim.** After the start gate (section 7, item 5) the worker claims the row with
    `UPDATE … WHERE id = %s AND status = 'queued' AND cancel_requested_at IS NULL`
    (`JobDB.claim_job`); a cancel landing between the gate read and the claim wins and the
    delivery is skipped without touching the row. `run_local` / explicit-kit callers keep the
    unconditional update.
41. **Terminal status writes are retried**: `done` / `failed` / `cancelled` (including the
    early failures and the gate's `cancelled`) are attempted up to 5 times with
    5 / 10 / 20 / 40 s back-off, `finished_at` fixed before the first attempt so every
    attempt is the same UPDATE; if all fail the error is logged, the run summary carries
    `db_write_failed=true` and the row is left to the API reaper (section 7).
42. **`results.sqlite` is published durably**: `results.sqlite.tmp` is `fsync`ed before the
    `os.replace` of section 3 and the job directory is `fsync`ed (best effort) after it.
43. **Contig names that pandas would not read back as a string are rejected in
    `preparing`**: names `pandas.read_csv` turns into a number, a boolean or NaN (`1`, `-3`,
    `1e5`, `.5`, `inf`, `True`, `NA`, `nan`, `null`, `None`, `<NA>`, `#NA`, …) fail the job
    with "Region N: the contig name 'X' is read as a number / a boolean / a missing value
    rather than a name by DirectRM's CSV reader; rename the contig … (for example by
    prefixing it with 'chr')." Upstream passes the parsed value to `pysam.fetch` and builds
    file names from it, so such a job would otherwise die later with a misleading "No region
    has more than 30 reads". The rule is per name (any subset of the regions can end up alone
    in a later per-k-mer CSV, so a mixed `1` + `X` file is rejected too) and is evaluated
    with the worker's own pandas. The API's upload-time `regions.csv` check (item 6) does not
    mirror it yet.
44. **Interrupts are never swallowed** (internal): `JobCancelled` derives from
    `BaseException` (like `KeyboardInterrupt`) and `errors.INTERRUPTS = (JobCancelled,
    SoftTimeLimitExceeded)` is re-raised before every broad `except Exception` on the main
    thread, so a SIGTERM / soft time limit that lands inside a Postgres round trip or a
    pysam / pod5 C call ends the job as `cancelled` / `failed` per item 13 instead of being
    relabelled as a stage error.

### Frontend and API surface (review follow-up)

45. **`limits.upload_ttl_h`** in `GET /api/capabilities` (`RMODHUB_UPLOAD_TTL_H`; section 6
    lists it) is what the Help page, the data-lifecycle notice, the resume prompt and the
    `localStorage` resume-record pruning quote; the UI falls back to 48 h when the key is
    absent (older API).
46. **Site identity in the UI includes the strand**: signal rows are keyed
    `position:mod_type:strand` (React keys, `data-key` attributes, selection, track glyphs,
    the read-level panel's reset key; e2e `keyOf`), so both strands of one position / type
    on a contig with regions on both strands stay distinct.
47. **Read-level `strand` filter.** `GET …/results?level=read&strand=+|-` (section 6)
    restricts the drill-down to one strand; without it the read-level panel of such a site
    lists the reads of both strands (the UI does not filter client-side, which would break
    the server-side paging totals). The parameter is accepted at either level; only `+` / `-` /
    empty are valid (`422` otherwise), and `+` must be URL-encoded as `%2B` because a bare
    `?strand=+` arrives as a space.
48. **Sample description strings.** `app/api/samples.py::SIGNAL_SAMPLE_DESCRIPTION` (shown
    verbatim in the sample dropdown) says "about 1 MB in total" for the 1.36 MB set of
    item 22, while the Help page says "about 1.4 MB" and "well under a minute of worker time
    (plus any wait in the queue)". Accepted as rounding; the API string is the one place to
    change if the sample is regenerated.
49. **Data router.** The SPA uses `createBrowserRouter` / `RouterProvider` (routes exported
    as `appRoutes`) so `useBlocker` can guard navigation while an upload is in flight (inline
    "Stay / Leave and pause" dialog plus `beforeunload`); tests use `createMemoryRouter`, and
    `src/test-setup.ts` shims Node's `Request` to accept jsdom `AbortSignal`s. The other UI
    behaviour from the same review — a 503 or `signal: false` renders the disabled notice and
    stops polling, a failed capabilities fetch retries with back-off, the tus client aborts a
    PATCH that accepts no byte for 60 s, retries at 0/1/3/5/10/20/30/60 s and waits while the
    browser is offline, abandoning a job sends one tus `DELETE` (item 3) — stays within
    section 9.

50. **Guarded worker writes.** Every worker write after the claim (`stage`, `progress`, `eta_s`,
    `inputs_deleted_at`, heartbeat, and the terminal `done`/`failed`/`cancelled`) is
    `UPDATE ... WHERE id = %s AND status IN ('running')` (the start-gate `cancelled` and
    pre-claim failures use `'queued'`). A rowcount of 0 means the API changed the row
    (cancel, reaper): the terminal write is logged as skipped and never retried, the job
    directory is removed, and — when the heartbeat or a progress write detects it — the
    worker stops the running child within one heartbeat interval (15 s) instead of only
    at the next stage boundary. `run_local` (no claim) keeps unconditional writes.
