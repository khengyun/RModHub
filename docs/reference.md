# RModHub — reference

Everything that is not needed to *run* the server. For installation and start-up see
[`README.md`](../README.md); the signal branch's design contract is
[`docs/signal-branch.md`](signal-branch.md).

## Web UI (`frontend/`)

React 19 + Vite 7 + TypeScript + Tailwind v4, served by nginx (`frontend/nginx.conf`) which also
proxies `/api`, `/health`, `/docs`, `/openapi.json` and `/static` to the API container.

Tabs: **Sequence** (`/`), **Nanopore signal** (`/signal`, present only when the branch is
enabled), **Help**, **API docs** (`/docs`, same origin). The signal tab has a kit selector
(RNA004 default, RNA002), four upload slots with per-file progress and **Retry**, a **Load sample
data** button (runs the built-in synthetic set), **Download sample files**, a collapsible helper
that prints the `subset_pod5` command with a size estimate, and the data-lifecycle statement
taken from `/api/capabilities`.

A submitted job navigates to **`/result/<job_id>`**: a public, bookmarkable page that polls
`GET /api/jobs/{id}` (2 s, backing off to 10 s) while the job is queued or running, shows
stage / progress / ETA / elapsed time, **Cancel** and **Copy link**, and on `done` renders the
same `ResultsTable` and SVG `TrackView` as the sequence branch (one transcript at a time, glyph
height = modification rate, 95 % CI and coverage in the tooltip), a coverage < 30 warning,
a **read-level drill-down** for any site, and CSV downloads (site level, read level). The job id
is the only key to a result: there are no accounts, no cookies and nothing is stored server-side
about who submitted a job. An interrupted upload can be resumed after a reload (best effort,
via a `localStorage` fingerprint); the resume prompt appears on the signal tab.

NAR Web Server Issue checklist, where it lives:

| requirement | where |
|---|---|
| Load sample data (+ download it) | both tabs |
| Filter (type, p-value / rate, position, text) and sort every column, coverage included; pagination | results table (the p-value column is hidden when every row has `p_value: null`, i.e. for signal results; the API additionally offers a server-side `min_coverage` filter on `GET /api/jobs/{job_id}/results`) |
| CSV download | `Download CSV` → backend (`?format=csv` / `download.csv`, all rows); `visible rows` → client-side |
| Visualisation | SVG track view: one lane per modification type, zoom/pan, nucleotide letters when zoomed in, MultiRM attention windows / DirectRM rate glyphs |
| Help that explains how to *read* results | `/help` (p-values, 25-nt flanks, the 12 types; `#nanopore-signal`: files, regions, coverage, rate + Wilson CI, jobs, data, sample, citation) |
| License on the landing page | footer + About strip on `/` and `/signal` |
| No third-party assets, no cookies, no login | system font stack, everything bundled; `npm run check:no-cdn` fails the Docker build on any external resource; the E2E suite records every network request and asserts none leaves the origin and that no cookie is set; nginx sends a `default-src 'self'` CSP |

Commands (from `frontend/`): `npm run dev`, `npm run build` (tsc + vite), `npm run test`
(vitest, jsdom, 187 tests), `npm run check:no-cdn`, `npm run e2e` (Playwright, 38 tests in
10 spec files, against `E2E_BASE_URL`, default the Docker stack on `:8080`; `E2E_START_VITE=1`
runs against a dev server + a local backend on `:8000` instead; `signal-flow.spec.ts` skips
itself unless `/api/capabilities` reports `signal: true`).

A raw `grep -rE "https?://" dist/` is *not* empty, and that is expected: it finds XML namespace
identifiers (`w3.org`), documentation links embedded in React / React Router *error message
strings*, and the credit hyperlinks (MultiRM and DirectRM repositories and DOIs, MIT license).
None of them is a resource the page loads — `check:no-cdn` classifies exactly these and fails on
anything else, and the `no-external-requests` E2E test verifies the runtime behaviour.

## API

Errors are always `{"detail": "<one plain sentence>"}`. Job and upload responses carry
`Cache-Control: no-store`.

### Sequence branch

#### `POST /api/predict/sequence`

```json
{ "sequence": "GGGGCCGUGG...", "alpha": 0.05 }
```

- `sequence`: 51–10,000 nt, characters `A C G U T` (case-insensitive, whitespace ignored, `U` is mapped to `T`).
  A single FASTA record (`>id description` on the first line) is accepted; `id` is returned as `transcript_id`.
- `alpha`: significance level in (0, 1]; a site is reported when its empirical p-value is `< alpha`.
  Use `alpha=1` to get the full 12 × (N−50) matrix in long format.
- `models`: which back-ends to run, by id (see `sequence_models` in `GET /api/capabilities`).
  A model whose window does not fit the input answers 422 naming its own minimum/maximum.
  Omit it for the server default. Naming two or more scores the same input with each and fills
  `comparison`; `results`/`meta` then repeat the first one, so existing clients are unaffected.
  An id the deployment did not load answers **422** naming what it does offer.
- `?format=csv` returns the same rows as a downloadable CSV. A comparison export keeps the seven
  shared columns and appends a `model` column, rows grouped per model in the requested order.

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

With `"models": ["multirm", "other"]` the response gains `comparison`, one entry per requested
model in the requested order, each `{"model": "<id>", "results": [...], "meta": {...}}`. It is
absent (`null`) whenever a single model ran.

Models in this build:

| id | model | window | types | weights | notes |
|---|---|---|---|---|---|
| `multirm` | MultiRM (Song *et al.* 2021) | 51 nt | 12 | 8 MB | empirical p-value from `neg_prob.csv`; `alpha` filters |
| `transrnam` | TransRNAm (Zhang *et al.*) | 601 nt | 12 | 21 MB | transformer + CNN; **no p-value**, rows are those with probability >= 0.5, and the input is capped at 2,000 nt (~18 ms per site on four threads) |
| `stub` | development fake | 51 nt | 12 | - | torch-free, tests only |

Both real models share the same frozen Word2Vec 3-mer table (`embeddings_12RM.pkl`, byte-identical
in the two weight directories) and the same 12 modification types in `MOD_TYPES` order, so their
rows line up position for position. `GET /api/capabilities` reports each model's
`min_sequence_nt` / `max_sequence_nt` and the UI greys out a model the current input cannot feed.

TransRNAm's checkpoint is re-serialised from the upstream raw pickle with `weights_only=True`
(tensors only, never executable pickle) and the original sha256 is recorded in
`app/predictors/transrnam/weights/WEIGHTS_MANIFEST.json`, the same treatment the DirectRM weights get.

Which models a deployment loads is `RMODHUB_PREDICTOR` (the default model, always first) plus the
comma-separated extras in `RMODHUB_SEQUENCE_MODELS`. The extras never replace `RMODHUB_PREDICTOR`. Every entry costs its own weights in memory,
and each one is loaded once at startup like the default — no per-request loading.

Invalid input returns **422** with a plain-language `detail` (`"at least 51 nt"`, `"at most 10000 nt"`,
`"invalid character(s) in sequence: 'N'"`, `"alpha ..."`).

#### `GET /api/samples/sequence`, `GET /health`, `GET /api/capabilities`

The built-in 151-nt sample (from the MultiRM README); `200 {"status": "ok", "model_loaded": true,
"signal_enabled": false, ...}` once the model is in memory (`503` before); and what this
deployment offers:

```json
{"sequence": true, "signal": true,
 "sequence_models": [{"id": "multirm", "label": "MultiRM", "description": "...", "default": true,
                      "name": "MultiRM", "version": "trained_model_51seqs"}],
 "limits": {"max_pod5_gb": 5, "max_bam_gb": 5, "max_reference_mb": 500, "max_regions": 10000,
            "max_running_per_ip": 1, "max_queued_per_ip": 3, "job_timeout_h": 6, "tus_chunk_mb": 64,
            "upload_ttl_h": 48},
 "retention": {"inputs_deleted": "after feature extraction, at most 48 h", "results_days": 14}}
```

### Nanopore signal branch

| route | purpose |
|---|---|
| `POST /api/jobs/signal` | one-shot **multipart** submission (`pod5`, `bam`, `reference`, `regions` file parts + `kit` field, default `RNA004`) → `202` job status, `queued`. Files are streamed to disk, never buffered |
| `POST /api/jobs/signal/init` | declare a **resumable** job: `{"kit": "RNA004", "files": {"pod5": {"name", "size"}, "bam": {...}, "reference": {...}, "regions": {...}}}` → `201`, status `uploading`, `uploads: {slot: {url, length, offset, complete}}`. Caps and quotas are checked here, before any byte is sent |
| `HEAD` / `PATCH` / `DELETE /api/uploads/{upload_id}`, `OPTIONS /api/uploads` | tus 1.0.0 core + termination (below) |
| `POST /api/jobs/{job_id}/start` | queue the job once all four uploads are complete (`409` naming the incomplete slots otherwise) |
| `POST /api/jobs/signal/sample` | run the built-in synthetic sample → `202`, `queued` |
| `GET /api/jobs/{job_id}` | status; poll while `queued` / `running` |
| `GET /api/jobs/{job_id}/results?level=site\|read&offset=&limit=&transcript_id=&mod_type=&position=&strand=&min_coverage=&sort=position\|rate\|coverage\|mod_type&order=asc\|desc` | paged rows (`limit` <= 1000) + `meta` (`409` until `done`; `strand=+\|-` keeps one strand — send `+` as `%2B`) |
| `GET /api/jobs/{job_id}/download.csv?level=site\|read` | streamed CSV attachment `rmodhub_signal_<job_id>_<level>s.csv` |
| `POST /api/jobs/{job_id}/cancel` | `200` with status `cancelled` (`409` if already terminal) |
| `GET /api/samples/signal`, `GET /api/samples/signal/files/{filename}` | description and download of the sample files |

```bash
curl -F pod5=@small.pod5 -F bam=@small.bam -F reference=@transcripts.fa -F regions=@regions.csv \
     -F kit=RNA004 http://localhost:8080/api/jobs/signal          # -> {"job_id": "...", "status": "queued", ...}
curl http://localhost:8080/api/jobs/<job_id>                       # poll
curl -o sites.csv http://localhost:8080/api/jobs/<job_id>/download.csv
```

Job status:

```json
{"job_id": "…", "status": "running", "stage": "features", "progress": 0.42, "eta_s": 95.0,
 "kit": "RNA004", "input_kind": "upload", "input_bytes": {"pod5": …, "bam": …, "reference": …, "regions": …},
 "created_at": "…", "started_at": "…", "finished_at": null, "expires_at": null, "inputs_deleted_at": null,
 "cancel_requested": false, "error": null, "n_sites": null, "n_reads": null, "n_transcripts": null,
 "model": {"name": "DirectRM", "version": "bc7a085"}, "uploads": null}
```

- `status` ∈ `uploading`, `queued`, `running`, `done`, `failed`, `cancelled`, `expired`
  (an expired job still answers `GET /api/jobs/{id}` with `200` and `status: "expired"` for one
  more retention period — 14 days after its files went, 48 h for a job that never started — then
  its row is purged and the id answers `404`; `/results` and `/download.csv` answer `404` as soon
  as the files are gone). `error` is one user-safe sentence.
- `stage` ∈ `uploading`, `preparing`, `sampling`, `features`, `denovo`, `inference`, `aggregating`;
  `progress` is 0..1 within the stage (`features`: reads processed / reads sampled).
- **Quotas** per client address: 1 job `running`, 3 jobs `uploading` + `queued`; over quota →
  `429` with `Retry-After`. Addresses are never stored in clear (see [Data lifecycle](#data-lifecycle)).
- **Caps**: pod5 5 GB, BAM 5 GB, reference 500 MB, 10,000 regions, 6 h per job (Celery soft time
  limit; the job then fails with a clear message). Over a cap → `422`, e.g.
  `"The pod5 file is 6.2 GB; this server accepts at most 5 GB."`. All values are configurable
  and published by `GET /api/capabilities`.
- Other errors: `404` unknown job / upload, `409` wrong state, `503` branch disabled, and `503`
  with `Retry-After: 10` ("The job database is not reachable; please try again later.") while
  Postgres is down.

Result rows (`level=site`) start with the shared `ModSite` fields — `transcript_id, position,
mod_type, probability (= modification rate), p_value (null), coverage, source ("signal")` — followed
by `strand, count, ci_low, ci_high, max_prob, noisyor_prob`. `level=read` rows are
`{read_id, transcript_id, position, strand, mod_type, probability, source}`; `transcript_id` +
`position` (+ `mod_type`, `strand`) select one site for drill-down. `sort=position` (the default)
is the order of the CSV download — transcript, position, type, byte-wise text order — and
`order=desc` its exact reverse; read-level rows can be sorted by `rate` / `mod_type` only within
one site (`transcript_id` + `position`, otherwise `422`). `meta` carries `model_name`,
`model_version`, `kit`, `n_sites`, `n_reads`, `n_transcripts`, `mod_types`,
`low_coverage_threshold: 30`, the `transcripts` list and, under `extra`, everything the worker
recorded (`n_reads_sampled`, `n_kmers`, `regions_skipped_low_coverage`, `regions_subsampled`,
`stage_seconds`, versions, …).

In every CSV the server writes (both branches) a `transcript_id` / `read_id` that begins with
`=`, `+`, `-`, `@`, TAB or CR is prefixed with `'`, so a shared download cannot carry a
spreadsheet formula; the JSON responses are untouched.

### Resumable upload (tus)

Browsers (and scripts that want to survive a dropped connection) use `POST /api/jobs/signal/init`
followed by [tus 1.0.0](https://tus.io) core + termination on `/api/uploads/{upload_id}`:

- `HEAD` → `200` with `Upload-Offset`, `Upload-Length`, `Tus-Resumable: 1.0.0`;
- `PATCH` with `Content-Type: application/offset+octet-stream` and `Upload-Offset` → `204` + new
  `Upload-Offset`; `409` if the offset does not match (resume from the offset in the response),
  `413` if the chunk is larger than `tus_chunk_mb`;
- `DELETE` → `204`; because a job needs all four files, terminating one upload **cancels the job**;
- `OPTIONS /api/uploads` → `Tus-Version`, `Tus-Extension: termination`, `Tus-Max-Size`.

The web UI's hand-written client (`frontend/src/api/tus.ts`, XHR, no external library) sends
**16 MiB chunks**, two files at a time, and after any network error, `5xx` or `409` asks `HEAD`
for the offset and continues from there (retry delays 0 / 1 / 3 / 5 / 10 / 20 / 30 / 60 s — about
two minutes — pausing while the browser is offline; a PATCH that accepts no byte for 60 s is
aborted and resumed the same way). The API accepts PATCH bodies up to `RMODHUB_TUS_CHUNK_MB` (64),
and both proxies allow **64 MiB** on `/api/uploads/*` (nginx `client_max_body_size 64m`, Caddy
`max_size 64MiB`), so a chunk never has to be spooled by a proxy. Unfinished uploads expire after
48 h (`RMODHUB_UPLOAD_TTL_H`, published as `limits.upload_ttl_h`).

## Preparing your data

1. **Basecall with move tables and align to your transcript reference** (kit RNA004 or RNA002):

   ```bash
   dorado basecaller <model> pod5_dir/ --emit-moves --reference transcripts.fa > reads.bam
   samtools sort -o reads_sorted.bam reads.bam && samtools index reads_sorted.bam
   ```

   DirectRM needs the `mv`/`ts`/`ns` tags (`--emit-moves`) and an `MD` tag (dorado writes it when
   aligning; the worker adds it with `calmd` (pysam) if it is missing). The BAM must be aligned
   to the **same FASTA you upload**, and the reference should be a transcriptome so that region
   coordinates are transcript coordinates on the `+` strand. The worker sorts and indexes the BAM
   if needed, but a sorted BAM uploads and starts faster.
2. **Regions CSV** — `seqnames,start,end,width,strand`, **1-based inclusive**, `width = end − start + 1`
   (recomputed by the worker), strand `+` or `-`, at most 10,000 rows:

   ```
   seqnames,start,end,width,strand
   tx_A,60,300,241,+
   ```

   DirectRM skips regions with <= 30 reads, randomly subsamples regions with >= 150 reads, and
   never scores the first base of a region.
3. **One `.pod5` file** containing (at least) the reads that align to your regions. If your run
   is one large file or a directory, cut it down with the subset tool below rather than
   uploading everything.
4. Choose the **kit**: `RNA004` (default, 9-mer level table) or `RNA002` (5-mer table).

### Cutting a huge POD5 down to what DirectRM will use (`tools/subset_pod5.py`)

DirectRM only ever loads the reads that `sampling.py` picks from your regions, so a 50–500 GB
flowcell is mostly dead weight. `tools/subset_pod5.py` (single file, `pod5` + `pysam` only) writes
a POD5 with exactly those reads plus a 20-nt safety flank, and optionally the matching BAM:

```bash
# build once (from the RModHub checkout)
docker build -f tools/Dockerfile.subset -t rmodhub/subset:local tools

# run inside the directory that holds your files
docker run --rm -v "$PWD:/data" rmodhub/subset:local \
    -i /data/big.pod5 -b /data/in.bam -r /data/reg.csv \
    -o /data/small.pod5 --bam-out /data/small.bam
```

Add `--dry-run` first: it prints the reads per region and a size estimate before writing anything
(`-i` also accepts a directory, searched recursively; add `--user "$(id -u):$(id -g)"` if your uid
is not 1000). Size estimate = mean bytes per read of the input × selected reads; for instance a
480 GB run with 12 M reads is ~40 kB per read, so 2,000 reads overlapping your regions give a
**~80 MB** POD5 (plus a few MB of BAM).

The results are the same as with the full POD5: `sampling.py` sees the same alignments (the
subset BAM holds every record overlapping the flanked regions on the region strand) and writes
the same `reads.txt`; every feature is read-local (a read's own signal against its own
alignment), so the numbers per read are bit-identical; and DirectRM's k-mer batching order,
`list(set(pod5_read_ids) & set(reads.txt))`, depends only on the sampled ids and
`PYTHONHASHSEED` (pinned to 0 in the worker) as long as the POD5 is a superset of `reads.txt`,
so the batch-sensitive LSTMs see the same batches. Upload the subset BAM together with the subset
POD5. Details, options, the flank rationale, POD5 format versions and troubleshooting are in
[`tools/README.md`](../tools/README.md).

## Sample data (synthetic)

`app/samples/signal/` is **synthetic**: no RNA was sequenced. `scripts/make_signal_sample.py`
draws three random transcripts (`tx_A` 560 nt, `tx_B` 516 nt, `tx_C` 579 nt), 88 RNA004-like reads
(40 / 36 / 12) with a 9-mer level signal model, and writes a POD5, a dorado look-alike BAM (`mv`,
`ts`, `ns`, `MD`, header marked `SYNTHETIC`), the reference and a regions CSV, 1.36 MB in total,
from a fixed seed:

```bash
uv run --with "pod5==0.3.35" --with "lib-pod5==0.3.35" \
    python scripts/make_signal_sample.py --out app/samples/signal --seed 20250831 --layout rna_raw \
    --levels worker/directrm_vendor/9mer_levels_v1.txt
```

(The pin matters: pod5 >= 0.3.46 writes POD5 v6, which older readers reject; the generator
refuses newer versions unless `--allow-newer-pod5` is passed. `MANIFEST.json` records every
sha256 and a pod5 *content* digest, since the pod5 container embeds a random file id.)

Expected numbers for the sample job (also asserted by `worker/tests/test_golden_directrm.py`
against a by-hand run of the unmodified upstream scripts): **88 reads** in the POD5/BAM,
**76 sampled** (`tx_C` has 12 reads and is skipped by the 30-read threshold, so the report shows
the coverage filter at work), **3,648 k-mers**, **725 sites** (ac4C 123, m1A 120, m5C 123, m6A 115,
m7G 126, Psi 118) and 14,027 read-level rows; about 12 s of worker time on a laptop core. The
files are CC0; the UI and `GET /api/samples/signal` label them *synthetic*.

## Data lifecycle

- **pod5 + BAM are deleted by the worker as soon as feature extraction has finished**
  (`inputs_deleted_at` in the job status) — long before the job is done. A 48 h backstop
  (`RMODHUB_INPUTS_MAX_AGE_H`) covers the jobs the worker never got to: when a job is older than
  48 h and `inputs_deleted_at` is still empty (still `queued`, `running` on a worker that died,
  or `failed` before feature extraction), the cleanup loop removes its whole `input/` directory
  whatever the status. Raw signal files therefore never outlive 48 h after job creation. A job
  still `queued` at that point (no worker ever picked it up) is marked `failed` with a message
  saying so. The backstop does not revisit jobs the worker has already processed.
- **The reference FASTA and the regions CSV stay with the job.** The worker keeps them (they are
  small and the aggregation stage still needs the contig lengths); they are removed together with
  the job directory — at once on cancel, timeout or worker stop, otherwise when the job expires
  14 days after it finished or failed. An unpublished reference is on the server for exactly as
  long as the results are.
- Unfinished uploads and jobs stuck in `uploading` expire after 48 h (`RMODHUB_UPLOAD_TTL_H`).
- Results (`results.sqlite`) are kept **14 days** after completion (`RMODHUB_RESULTS_RETENTION_DAYS`),
  then the job directory is removed and the job is marked `expired`. The row is kept one more
  retention period (48 h for a job that never started) so `GET /api/jobs/{id}` can still say
  `expired`, then it is deleted and the id answers `404`.
- A cleanup pass runs inside the API every hour (`RMODHUB_CLEANUP_INTERVAL_S`; the first pass
  one interval after start-up) and can be run from cron: `python -m app.jobs.cleanup`
  (also `docker compose exec api python -m app.jobs.cleanup`). It also marks `running` jobs whose
  worker heartbeat is older than 10 min as `failed`, leaves a `cancelled` job alone while its
  worker is still heartbeating, removes orphaned directories, and logs the bytes freed.
- Cancelling a running job kills the DirectRM subprocess and removes the job directory.
- The **job id is the only key**: no accounts, no e-mail, no cookies; anyone with the link can see
  that job. Client addresses are stored only as `HMAC-SHA256(ip, RMODHUB_IP_HASH_SECRET)` for
  the fair-use quotas. Pasted sequences are processed in memory and never written to disk.

## How DirectRM is run

Upstream DirectRM is a set of CLI scripts written for a GPU conda environment. RModHub runs them
**unmodified** — byte-identical to commit `bc7a085` — as subprocesses inside a separate worker
container (`worker/`, its own uv project on **Python 3.10** because Python 3.9's newest lib-pod5
wheel, 0.3.35, cannot open POD5 v6 files written by pod5 >= 0.3.46; the worker pins lib-pod5
0.3.47 and reads both v5 and v6 files; the API stays on 3.12 and the two never import each other). The API enqueues `rmodhub.signal.run_job(job_id)` by name on the
`signal` queue; the worker runs one job at a time (`--concurrency=1 --prefetch-multiplier=1`),
writes status / stage / progress / heartbeat to Postgres, and publishes one `results.sqlite` per
job on the shared volume, which the API reads read-only.

| stage | what runs |
|---|---|
| `preparing` | worker code: validate `params`, validate and normalise `regions.csv` (contig names that pandas would read as a number, a boolean or NA — `1`, `1e5`, `True`, `NA`, … — are rejected with a rename hint), open the pod5, index the FASTA, sort/index the BAM if needed, require `mv` tags, add `MD` with `calmd` if missing, check contigs and read-id overlap, count the reads per region |
| `sampling` | `scripts/sampling.py --min_coverage 30 --max_coverage 150` → `reads.txt` |
| `features` | `scripts/feature_extraction.py --kmer 9 --step 5` (Remora re-squiggle); then **pod5 + BAM are deleted** |
| `denovo` | `scripts/denovo_inference.py` (binary "modified k-mer" model) |
| `inference` | `scripts/inference.py --ml True --model_id 5 --device cpu` (6-type model) |
| `aggregating` | `scripts/read2site.py`, then the worker builds `results.sqlite` (rate = count / coverage, 95 % Wilson interval, `mod_type` normalised to `ac4C m1A m5C m6A m7G Psi`; tables `sites`, `reads`, `transcripts`, `regions` — one row per regions-CSV line with its read count — and `meta`; fsynced, then published atomically) |

Details that make this work without touching upstream:

- **Weights re-serialised to CPU.** The shipped `model.pt` files carry `cuda:0` storage tags and the
  scripts call `torch.load` without `map_location`, which fails on a CPU box. Every one of the 106
  files was re-saved once with `torch.save(torch.load(p, map_location="cpu"))`; keys, order and
  values are verified identical (`torch.equal`) and `worker/directrm_vendor/WEIGHTS_MANIFEST.json`
  records the original and vendored sha256 of each file (`worker/scripts/vendor_directrm.py`).
- **`PYTHONHASHSEED=0` for every child process**: upstream orders reads through `set()` iteration
  and its LSTMs are built with `batch_first=False`, so predictions depend on the k-mer batching
  order. With the seed pinned, two runs on the same input *at the same thread count* are
  byte-identical. The child processes run with `RMODHUB_WORKER_THREADS` OMP/MKL threads: **1 in
  the image** (what the golden fixture and the worker tests use) and **4 under
  `docker-compose.yml`** for throughput. The thread count changes torch's floating-point summation
  order: on the sample, 4 threads vs 1 leaves `reads.txt`, the features and every `count`,
  `coverage` and `rate` identical and moves per-read probabilities by at most 6e-8 (the golden
  comparison tolerates 1e-6). Set `RMODHUB_WORKER_THREADS=1` to reproduce the fixture byte for byte.
- Each stage runs with `cwd` = `PYTHONPATH` = the vendor root, in its own process group
  (`PR_SET_PDEATHSIG`), with stdout/stderr in `work/logs/<stage>.log`; cancel and the 6 h soft time
  limit kill the whole group.

Upstream quirks you should know when reading results (kept as-is on purpose; full list in
`worker/directrm_vendor/UPSTREAM.md` and `worker/README.md`):

- the de novo stage is computed but **not consumed** by `inference.py` (its filter lines are
  commented out upstream); the worker reports `denovo_frac_modified` in `meta.extra` and does not
  gate on it;
- `coverage` is the number of reads with a **non-zero score** at that base for that type, not raw
  read depth; `count` is reads with score > 0.5; `probability` (= rate) is `count / coverage`;
- the **first base of every region is never scored** (and k-mers may extend a few bases past `end`);
- regions with <= 30 reads are skipped, regions with >= 150 reads are randomly subsampled
  (unseeded), and because of the batch-order sensitivity above **results depend on the sampled
  read set**: change the regions or the read set and the numbers change, as they would upstream.

## Result schema

One row per (position, modification type). **Shared by both branches — do not change.**

| field | type | branch A (sequence) | branch B (signal) |
|---|---|---|---|
| `transcript_id` | `str \| null` | `null`, or FASTA id | reference contig (transcript) name |
| `position` | `int` | 1-based position in the input sequence | 1-based position on the transcript |
| `mod_type` | `str` | one of the 12 `MOD_TYPES` | `ac4C`, `m1A`, `m5C`, `m6A`, `m7G`, `Psi` |
| `probability` | `float` | MultiRM sigmoid output | DirectRM modification rate (`count / coverage`) |
| `p_value` | `float \| null` | empirical, vs. 150 negative sequences (multiples of 1/150) | `null` |
| `coverage` | `int \| null` | always `null` | reads with a non-zero score at the base |
| `source` | `"sequence" \| "signal"` | `"sequence"` | `"signal"` |

Signal rows append `strand, count, ci_low, ci_high, max_prob, noisyor_prob` after these seven
(same order in JSON and CSV). Sequence rows are returned only when `p_value < alpha` and
`probability > 0`; positions 1–25 and N−24–N never appear because MultiRM scores the centre of a
51-nt sliding window.

## How MultiRM is served (load once, no subprocess)

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

Environment variables, read from `.env` by Docker Compose and by the API through pydantic-settings
(`app/config.py`); the worker reads the same names. `.env.example` walks through the ones an
operator usually changes; the table below is the complete list.

| variable | default | meaning |
|---|---|---|
| `RMODHUB_PREDICTOR` | `multirm` | `multirm` or `stub` (torch-free fake for UI work / CI) |
| `RMODHUB_MAX_SEQUENCE_NT` | `10000` | upper input limit of the sequence branch (DoS guard) |
| `RMODHUB_MIN_SEQUENCE_NT` | `51` | shortest accepted sequence; cannot go below the model's 51-nt window |
| `RMODHUB_DEFAULT_ALPHA` | `0.05` | p-value threshold applied when a request omits `alpha` |
| `RMODHUB_WARMUP` | `true` | run one dummy inference at startup |
| `RMODHUB_LOG_LEVEL` | `info` | uvicorn / application log level |
| `RMODHUB_TORCH_THREADS` | unset | torch intra-op threads. Unset → honour `OMP_NUM_THREADS` if present (the image sets 1), else `min(4, cpu_count)` |
| `RMODHUB_CORS_ORIGINS` | unset | JSON list or comma-separated; only needed if the UI is served from another origin |
| `RMODHUB_PORT` | `8000` | host port of the API container (compose, dev only) |
| `RMODHUB_WEB_PORT` | `8080` | host port of the web UI (compose, dev only) |
| `RMODHUB_DOMAIN` | `rmodhub.example.org` | public hostname for Caddy / Let's Encrypt (`localhost` = internal CA) |
| `POSTGRES_PASSWORD` | unset | **switches the signal branch on** under compose, together with `COMPOSE_PROFILES=phase2` and `RMODHUB_IP_HASH_SECRET`; compose derives the two URLs below from it. Letters and digits only (`openssl rand -hex 24`): `@ % $` and whitespace break the URL |
| `COMPOSE_PROFILES` | unset | Compose only: `phase2` includes `postgres` / `redis` / `worker` in every `docker compose` command (`make` derives the profile from the password itself) |
| `DATABASE_URL` (or `RMODHUB_DATABASE_URL`) | unset → branch disabled | SQLAlchemy URL, `postgresql+psycopg://…` (tests use `sqlite+pysqlite:///…`); redacted in the start-up log |
| `CELERY_BROKER_URL` (or `RMODHUB_CELERY_BROKER_URL`) | unset | Redis URL; no result backend (status lives in Postgres); redacted in the start-up log |
| `RMODHUB_IP_HASH_SECRET` | unset (`rmodhub-dev` development default, warned, outside the image) | HMAC key for the per-address quota key; **required for the signal branch** — the api container refuses to start it with a missing, empty or default key (`openssl rand -hex 32`) |
| `RMODHUB_ALLOW_DEV_SECRET` | unset | `1` lets the api container run the signal branch with the development key (local experiments only) |
| `RMODHUB_TRUSTED_PROXIES` | unset → the api container's own network(s) minus Docker's gateway | api container only: proxies whose `X-Forwarded-For` / `-Proto` uvicorn believes (comma-separated IPs / CIDRs → `FORWARDED_ALLOW_IPS`); set it to the gateway address (e.g. `172.20.0.1`) when a reverse proxy runs on the host; never `*` |
| `RMODHUB_MULTIPART_MAX_SIZE` | `11GiB` | Caddy only (`docker-compose.prod.yml`): outer body bound of `POST /api/jobs/signal`; keep it ≥ pod5 + BAM + reference caps + ~80 MiB |
| `RMODHUB_UPLOAD_DIR` | `/data/uploads` | shared volume: `tus/` partial uploads, `jobs/<job_id>/` |
| `RMODHUB_SAMPLE_DIR` | `app/samples/signal` (inside the package) | directory of the synthetic signal sample served by `GET /api/samples/signal` and copied by `POST /api/jobs/signal/sample`; when it is missing those routes answer 404 |
| `RMODHUB_MAX_POD5_GB` | `5` | pod5 cap (GiB) |
| `RMODHUB_MAX_BAM_GB` | `5` | BAM cap (GiB) |
| `RMODHUB_MAX_REFERENCE_MB` | `500` | reference FASTA cap (MiB) |
| `RMODHUB_MAX_REGIONS` | `10000` | most data rows in `regions.csv` |
| `RMODHUB_TUS_CHUNK_MB` | `64` | largest tus PATCH body; keep <= the nginx / Caddy 64 MiB limits (the browser sends 16 MiB) |
| `RMODHUB_MAX_RUNNING_PER_IP` | `1` | jobs in `running` per address |
| `RMODHUB_MAX_QUEUED_PER_IP` | `3` | jobs in `uploading` + `queued` per address |
| `RMODHUB_JOB_TIMEOUT_S` | `21600` | Celery soft time limit (6 h; hard limit +300 s) |
| `RMODHUB_RESULTS_RETENTION_DAYS` | `14` | days results are kept |
| `RMODHUB_INPUTS_MAX_AGE_H` | `48` | backstop: the `input/` of a job older than this whose worker never deleted its inputs (`inputs_deleted_at` empty) is removed; also the age limit for orphan job directories |
| `RMODHUB_UPLOAD_TTL_H` | `48` | unfinished uploads / `uploading` jobs expire |
| `RMODHUB_CLEANUP_INTERVAL_S` | `3600` | period of the in-process cleanup loop |
| `RMODHUB_WORKER_THREADS` | `4` in compose (image default 1) | OMP / MKL threads of the DirectRM subprocesses; reruns are byte-identical at a fixed count, and only 1 reproduces the golden fixture byte for byte (other counts differ at the 1e-8 level) |

An empty or whitespace-only value of any `RMODHUB_*` variable counts as unset.

Worker-only knobs (see `worker/README.md`): `RMODHUB_DIRECTRM_MODEL_ID` (`5`),
`RMODHUB_MIN_COVERAGE` / `RMODHUB_MAX_COVERAGE` (`30` / `150`, passed to `sampling.py`),
`RMODHUB_DIRECTRM_ROOT` (vendor root, `/app/directrm_vendor` in the image).

Run **one uvicorn worker per API container** (the model lives in process memory; the tus offset
locks assume one process) and **one job per worker container**; scale with container replicas
behind the reverse proxy instead.

## Production (HTTPS on 443)

```bash
cp .env.example .env            # set RMODHUB_DOMAIN (+ POSTGRES_PASSWORD, RMODHUB_IP_HASH_SECRET and COMPOSE_PROFILES=phase2 for the signal branch)
docker compose [--profile phase2] -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# or: make prod-up   (adds --profile phase2 and runs `make phase2-check` first whenever POSTGRES_PASSWORD is set)
```

`deploy/Caddyfile` terminates TLS with automatic Let's Encrypt certificates, adds security headers
and caps request bodies per path: **1 MiB** by default (a 10 kb sequence is a few kB of JSON),
**64 MiB** on `/api/uploads/*` (one tus chunk) and **`RMODHUB_MULTIPART_MAX_SIZE`, 11 GiB by
default,** on `/api/jobs/signal` (the one-shot multipart route: only an outer bound, which must stay
above the API's own total — pod5 + BAM + reference caps + ~80 MiB, ~10.6 GiB with the defaults —
because the API enforces the per-file caps while streaming; raise it together with the caps, or a
request the API would accept dies at Caddy with a bare 413). nginx inside the `web`
container applies the same 1 MiB and 64 MiB limits but leaves `/api/jobs/signal` **unbounded**
(`client_max_body_size 0`), so without Caddy — the dev compose stack — that route is limited only
by the API itself (a `Content-Length` above the sum of the per-file caps is rejected before any
byte is read, and each part is capped while it streams). Both proxies stream upload bodies
instead of spooling them. Neither `api` nor `web` is published on the host in production; only
Caddy is.
The server sets no cookies, requires no login and loads no third-party assets (landing page and
`/docs` are fully self-hosted). The per-address quota key is taken from `X-Forwarded-For` only
when the direct peer is a trusted proxy: the api image's entrypoint (`deploy/api-entrypoint.py`)
sets uvicorn's `FORWARDED_ALLOW_IPS` to the container's own compose network(s) minus Docker's
gateway address, so nginx and Caddy are believed while a spoofed header from the host, from an IPv6
client or from the internet is ignored (`--forwarded-allow-ips=*` is never used). A reverse proxy
on the host itself connects from the gateway address and must be listed in
`RMODHUB_TRUSTED_PROXIES` (`docker run --rm rmodhub-api:local --show` prints the detected list).
The same entrypoint refuses to start the signal branch with the development
`RMODHUB_IP_HASH_SECRET` (`RMODHUB_ALLOW_DEV_SECRET=1` to override).

CI (`.github/workflows/ci.yml`): `test` (ruff + pytest), `docker` (API image, boot, sample
prediction, branch-off 503 check, `X-Forwarded-For` trust matrix, dev-secret refusal),
`worker-image` (import + CPU-torch check + a 3 GB guard on the container's root filesystem),
`phase2-smoke` (`docker compose --profile phase2 up --wait`, sample job to completion, results
and CSV assertions), `frontend` (typecheck, vitest, build, no-CDN) and `e2e` (Playwright against
the compose stack).

## Repository layout

```
app/
  main.py               FastAPI app factory + lifespan (loads MultiRM once; DB, cleanup loop and queue when enabled)
  config.py             pydantic-settings (RMODHUB_*), signal_enabled = DATABASE_URL set
  schemas.py            ModSite (shared schema), request/response models
  db.py, csvio.py       SQLAlchemy engine/session helpers; the one CSV writer both branches use
  api/                  routers: predict, samples, health, capabilities, jobs, uploads_tus; normalize.py (input rules)
  jobs/                 signal-branch job layer: models (Postgres), schemas, service (state machine), storage (streaming
                        writes, tus files), quota (HMAC key), queue (Celery by task name), results (results.sqlite
                        read-only paging/CSV), cleanup (reaper + `python -m app.jobs.cleanup`)
  samples/signal/       synthetic sample: sample.pod5, sample_sorted.bam(.bai), sample_reference.fa(.fai), sample_regions.csv
  landing.html, static/ landing page, favicon, self-hosted Swagger UI (Apache-2.0)
  predictors/           SequencePredictor protocol, torch-free stub, vendored MultiRM (vendor/, weights/), predictor, adapter
worker/                 separate uv project (Python 3.10): rmodhub_worker/ (celery_app, tasks, pipeline, prepare, aggregate,
                        db, run_local CLI), directrm_vendor/ (upstream DirectRM bc7a085, byte-identical + CPU weights),
                        scripts/vendor_directrm.py, tests/ (golden fixture), Dockerfile, README.md
tools/                  subset_pod5.py + Dockerfile.subset + README.md (cut a flowcell POD5 down to the regions' reads)
docs/signal-branch.md   the contract shared by API, worker, frontend and tools (routes, JSON keys, tables, env names)
scripts/                bench_multirm.py, make_signal_sample.py (synthetic sample generator)
tests/                  golden regression, validation, equivalence (U/T), perf (load-once proof), job API, tus, schema,
                        subset tool, sample generator
frontend/
  src/api/              typed client, hand-written tus client, JSON fixtures captured from the real API
  src/pages/            SequencePage (tool + landing), SignalPage (uploads), ResultPage (/result/:jobId), HelpPage
  src/components/       form/, results/ (table, filters, CSV), track/ (SVG track view), signal/ (upload slots, job status,
                        read-level panel, subset helper), layout/ (tabs, capabilities, license notice)
  src/lib/              modTypes (12 + ac4C: colour + description), sequence normalisation, download
  e2e/                  Playwright: sample flow, filters/sort, CSV, validation, 10 kb, no-external-requests, result page,
                        upload resume, signal flow
  Dockerfile, nginx.conf (body limits per path, CSP), scripts/check-no-external-urls.mjs
Dockerfile, docker-compose.yml (api + web; postgres/redis/worker behind --profile phase2),
docker-compose.prod.yml + deploy/Caddyfile (HTTPS 443, per-path body limits), deploy/api-entrypoint.py
(api image entrypoint: trusted-proxy list, dev-secret guard), Makefile, .env.example
```
