# rmodhub-worker — the nanopore signal (DirectRM) worker

Runs the vendored, **unmodified** DirectRM pipeline (`directrm_vendor/`, upstream commit
`bc7a085`, MIT) for one RModHub job at a time and publishes `results.sqlite` for the API.
Everything it does is fixed by `docs/signal-branch.md`; this file explains how to run and
operate it. The worker never imports `app/`.

```
Celery (queue "signal") ──► rmodhub.signal.run_job(job_id)
                              │  reads jobs.kit/params, writes status/stage/progress/heartbeat (Postgres)
                              ▼
   preparing ─► sampling ─► features ─► denovo ─► inference ─► aggregating
   (worker)     sampling.py  feature_extraction.py  denovo_inference.py  inference.py  read2site.py + results.sqlite
                                        │
                                        └─ pod5 + BAM deleted here (inputs_deleted_at)
```

## Environment

An independent uv project (the repository root is Python 3.12-only):

| constraint | reason |
|---|---|
| Python **3.10** (`.python-version`, `requires-python = ">=3.10,<3.11"`) | the lowest interpreter with a `lib-pod5` >= 0.3.46 wheel, i.e. that reads POD5 v6 (see "Why Python 3.10" below) |
| `ont-remora==3.2.0` | sdist only: compiles three Cython extensions, needs a C compiler (`gcc` on the host, `build-essential` in the image builder stage); no Python pin of its own |
| `pod5==0.3.47`, `lib-pod5==0.3.47` | reads POD5 v5 **and v6** (files written by pod5 >= 0.3.46 = current MinKNOW / dorado / `pod5` CLI); pulls `pyarrow==22.0.0` (18.0.0 on 3.9) |
| `torch==2.8.0` (+cpu index) | CPU wheel pinned to `https://download.pytorch.org/whl/cpu` via `[tool.uv.sources]` |
| `polars==1.36.1`, `numpy==2.0.2`, `pandas==2.3.3`, `pysam==0.24.0`, `torcheval==0.0.7`, `tqdm==4.70.0` | the set first resolved and validated on 3.9; carried over unchanged |
| `celery[redis]==5.6.3`, `psycopg[binary]==3.2.13` | queue + Postgres |
| `[tool.uv].constraint-dependencies` | freezes every transitive package (scipy 1.13.1, scikit-learn 1.6.1, h5py 3.14.0, ...) at the 3.9 resolution, so the only differences between the two lockfiles are the interpreter, pod5/lib-pod5 and pyarrow |

```
cd worker
uv sync --all-groups          # downloads CPython 3.10 if needed, builds remora (~1-2 min)
uv run pytest -q              # 127 tests incl. the full sample pipeline three times (~40 s)
```

### Why Python 3.10

The worker started on Python 3.9 because that was where the first dependency resolution
(ont-remora 3.2.0 + torch 2.8.0+cpu) landed, but `lib-pod5` stopped shipping cp39 wheels at
0.3.35, and 0.3.35 cannot open POD5 **v6** files: pod5 0.3.46 changed the read table
(32-bit `channel` column) and older readers fail with `Schema field 'channel' is incorrect
type: 'uint32'`. Everything users upload today (MinKNOW, dorado, `pod5 subset` from 2026)
is v6, so a 3.9 worker would fail real jobs in `preparing`. Python 3.10 is the lowest
interpreter with a `lib-pod5` 0.3.47 wheel; it still uses the siphash24 string hash (3.11
moved to siphash13), so with `PYTHONHASHSEED=0` upstream's `set()` order — and therefore the
golden fixture produced under 3.9 — is reproduced byte for byte (`tests/test_golden_directrm.py`,
`tests/fixtures/golden_directrm_sample/README.md`). The committed synthetic sample stays a
v5 file so that older readers (e.g. the pod5 0.3.35 pinned in `tools/Dockerfile.subset`) can
open it too; the worker reads both.

## Running a job without the stack (`run_local`)

```
uv run python -m rmodhub_worker.run_local /tmp/job1 --kit RNA004 --no-db \
    --sample-dir ../app/samples/signal
```

`--sample-dir` copies the synthetic sample into `/tmp/job1/input/` with the upstream names;
otherwise `<job_dir>/input/` must already contain `input.pod5`, `input_sorted.bam`
(+ `.bai`, optional), `reference.fa` and `regions.csv`. Options: `--kit RNA004|RNA002`,
`--model-id N` (1..8, default 5), `--min-coverage`, `--max-coverage`, `--threads`,
`--keep-inputs` (skip the post-features deletion), `--json`, `--job-id`, `-v`. Without
`--no-db` the job row in `DATABASE_URL` is updated exactly as the Celery task does.
Exit status 0 means `done`. Output on the sample:

```
status   : done
  preparing         0.1 s
  sampling          1.6 s
  features          6.6 s
  denovo            0.9 s
  inference         1.0 s
  aggregating       0.8 s
  n_reads_sampled              76
  n_kmers                      3648
  n_sites                      725
  results  /tmp/job1/results.sqlite
```

## Celery worker

```
DATABASE_URL=postgresql://… CELERY_BROKER_URL=redis://redis:6379/0 \
uv run celery -A rmodhub_worker.celery_app worker -Q signal --concurrency=1 --prefetch-multiplier=1 --loglevel=info
```

Task `rmodhub.signal.run_job(job_id)`; `acks_late=False`, `max_retries=0`,
`soft_time_limit=RMODHUB_JOB_TIMEOUT_S`, `time_limit=soft+300`, no result backend. When a
delivery arrives the task first reads the `jobs` row (**start gate**): it runs only if
`status == 'queued'` and `cancel_requested_at` is null; a queued row with `cancel_requested_at`
set becomes `cancelled` (job dir removed); a terminal, `uploading` or `running` row (a second
delivery of the same job id) is skipped without touching it — so an API-side cancel is honoured
even if the Celery revoke broadcast was lost. It then reads `jobs.kit` and `jobs.params`
(`model_id` 1..8, `min_coverage` >= 0, `max_coverage` > `min_coverage` override the defaults;
anything else fails the job at once with "The job's parameters are invalid: …" instead of
leaving it `running`), **claims** the row with a conditional UPDATE (`… WHERE status = 'queued'
AND cancel_requested_at IS NULL`; a cancel that lands between the gate read and the claim wins
and the delivery is skipped) and writes only the columns the contract allows
(`status, stage, progress, eta_s, started_at, finished_at, inputs_deleted_at, error, n_sites,
n_reads, n_transcripts, worker_hostname, heartbeat_at`; enforced in `db.py`).

**Every write after the claim is conditional.** Stage, progress, `inputs_deleted_at`, the
heartbeat and the terminal `done` / `failed` / `cancelled` are all
`UPDATE jobs SET … WHERE id = %s AND status = 'running'` (the start gate's `cancelled` uses
`status = 'queued'`). The API owns every other status — `POST /cancel` writes `cancelled` at
once and only then revokes, the cleanup reaper writes `failed` — so a row the API has closed is
never overwritten, whatever the worker finds out later. An UPDATE that changes no row means
"the API changed the status": the worker logs
`job <id>: terminal write skipped, row no longer running (status changed by the API)` at warning
level (`heartbeat skipped, …` for the heartbeat), keeps the row exactly as the API wrote it, stops
the job if it is still running (child process group killed, unwound as a cancel) and removes the
job directory, which nobody will read any more. The API's cleanup backstop removes that directory
too; removing it twice is harmless. `run_local` with a database claims its row unconditionally
(the caller owns it) but its later writes carry the same guard.

Outcomes:

| event | what happens |
|---|---|
| all stages succeed | `done`, `n_sites`/`n_reads`/`n_transcripts`, `finished_at` |
| a stage fails | `failed` + one user-safe sentence in `error`; the worker log has the detail and the path of `work/logs/<stage>.log` |
| `POST /cancel` (API sets `cancelled` + `cancel_requested_at`, then `revoke(terminate=True, SIGTERM)`) | child process group killed, `cancelled` (the worker's own write changes no row when the API's already landed — expected, logged at warning level), job dir removed. Between stages the flag is also polled, so a cancel is honoured even without the signal |
| `POST /cancel` while the worker keeps working (revoke lost or late) | the next heartbeat / stage UPDATE (`WHERE status = 'running'`) changes no row → child process group killed, the run unwinds as a cancel within one heartbeat interval; a job that finished first sees its `done` UPDATE change no row either. Either way the row keeps the API's `cancelled`, no `done`/`failed` is ever written over it, and the job dir is removed (the API backstop removes it too; twice is harmless) |
| `inputs_deleted_at` | written right after the `features` stage, together with the deletion of pod5/BAM/BAI (before `denovo` starts) |
| SIGTERM without a cancel request (worker shutdown) | child killed, `failed` ("The worker was stopped while the job was running."), job dir removed |
| soft time limit | child killed, `failed` ("The job exceeded the 6 h limit and was stopped."), job dir removed |
| worker dies | children carry `PR_SET_PDEATHSIG=SIGKILL`; the API reaper marks the job failed via the stale `heartbeat_at` |
| Postgres unreachable at the end | the terminal write (`done`/`failed`/`cancelled`, `WHERE status = 'running'`) is retried 5 times (5/10/20/40 s back-off, identical UPDATE each time); if it still fails the error is logged and the row is left to the API reaper. A write that reaches Postgres but changes no row is *not* retried: the API closed the job (see above) |

`heartbeat_at` (plus `progress`/`eta_s`) is refreshed every 15 s by a thread, `WHERE status =
'running'`; a heartbeat that changes no row means the API closed the job and the thread stops it
(see the table) — the worker notices a lost revoke within one interval instead of at the next
stage boundary.

## Environment variables

| variable | default | meaning |
|---|---|---|
| `DATABASE_URL` (alias `RMODHUB_DATABASE_URL`) | unset | libpq or SQLAlchemy URL (`postgresql+psycopg://` is accepted) |
| `CELERY_BROKER_URL` (alias `RMODHUB_CELERY_BROKER_URL`) | unset | redis URL |
| `RMODHUB_UPLOAD_DIR` | `/data/uploads` | shared volume; jobs live in `jobs/<job_id>/` |
| `RMODHUB_JOB_TIMEOUT_S` | `21600` | soft time limit (hard = +300 s) |
| `RMODHUB_DIRECTRM_MODEL_ID` | `5` | integrated model `ml<N>` |
| `RMODHUB_MIN_COVERAGE` / `RMODHUB_MAX_COVERAGE` | `30` / `150` | passed to `sampling.py` |
| `RMODHUB_MAX_REGIONS` | `10000` | cap on `regions.csv` data rows |
| `RMODHUB_WORKER_THREADS` | `OMP_NUM_THREADS` or `1` (image); `docker-compose.yml` sets `4` | OMP/MKL threads of the child processes; reruns are byte-identical at a fixed count, and 1 is the count the golden fixture was made with (see *Determinism*) |
| `RMODHUB_DIRECTRM_ROOT` | `worker/directrm_vendor` | vendor root (`/app/directrm_vendor` in the image) |

## Stages, commands and files

Job directory (`<J>` = `RMODHUB_UPLOAD_DIR/jobs/<job_id>`):

```
<J>/input/   input.pod5  input_sorted.bam  input_sorted.bam.bai  reference.fa(.fai)  regions.csv
<J>/work/    reads.txt  features/{input.npz,input.csv}  denovo/input_denovo.npy
             inference/<type>/<seqname>.csv  sites/<type>.csv  logs/<stage>.log  meta not persisted
<J>/results.sqlite   tables meta, transcripts, sites, reads, regions (contract section 5 + item 38);
                     written as results.sqlite.tmp, fsynced, published with os.replace, directory fsynced
```

| stage | what runs (cwd = vendor root, `PYTHONPATH` = vendor root, `PYTHONHASHSEED=0`) |
|---|---|
| `preparing` | Python (`prepare.py`): regions validated (contig names that pandas would read as a number, a boolean or NA — `1`, `1e5`, `True`, `NA`, `nan`, `null`, … — are rejected with a rename hint, because upstream passes the parsed value to `pysam.fetch` and builds file names from it) and rewritten as `seqnames,start,end,width,strand`; pod5 opened and read ids collected; `pysam.faidx`; BAM sorted if `SO` != coordinate, indexed if no fresh `.bai`; first 500 mapped primary records must carry `mv` (else the dorado `--emit-moves` error), `MD` added with `pysam.calmd` if missing; region contigs must exist in BAM header and FASTA with equal lengths; reads per region counted with Remora `fetch` semantics (skip / subsample bookkeeping) and their names must intersect the pod5 read ids |
| `sampling` | `python scripts/sampling.py --bam <J>/input --reg <J>/input/regions.csv -o <J>/work/reads.txt --splits input --min_coverage 30 --max_coverage 150` |
| `features` | `python scripts/feature_extraction.py --pod5_dir <J>/input --bam <J>/input --reg <J>/input/regions.csv --level <V>/{9mer,5mer}_levels_v1.txt -o <J>/work/features --splits input --read_ids <J>/work/reads.txt --kmer 9 --step 5`; progress = "signal refinement by remora" lines / reads sampled; then `input.npz` is checked (0 k-mers or a zero-variance dwell column fail with "No usable k-mers were extracted from the sampled reads (...)"); pod5 + BAM deleted, `inputs_deleted_at` set |
| `denovo` | `python scripts/denovo_inference.py --feature_dir <J>/work/features --outdir <J>/work/denovo --model_path <V>/model/<KIT>/id3_binary/model.pt --splits input --device cpu`; `denovo_frac_modified` = fraction of k-mers with p >= 0.5 (informational; upstream does not gate on it) |
| `inference` | `python scripts/inference.py --feature_dir <J>/work/features --outdir <J>/work/inference --device cpu --splits input --ml True --model_dir <V>/model/<KIT> --model_id 5` |
| `aggregating` | `python scripts/read2site.py --indir <J>/work/inference --outdir <J>/work/sites --delete False`, then `aggregate.py` builds `results.sqlite` (schema of contract section 5; `rate = count/coverage`, 95 % Wilson interval, `mod_type` normalised `ac4c→ac4C m1a→m1A m5c→m5C m6a→m6A m7g→m7G psi→Psi`, rows sorted by `(transcript_id, position, mod_type)`; `transcripts` = every contig named in `regions.csv` with FASTA length, mapped reads from the BAM index and site count; `regions` = one row per `regions.csv` line with the read count measured in `preparing` (kept out of `meta`, which the API inlines into every results page); `meta` = the contract keys plus versions and the preparing-stage aggregates) |

Logs: `<J>/work/logs/<stage>.log` holds the command line and the merged stdout/stderr of each
upstream script (`feature_extraction.py` prints one line per read; tqdm bars are disabled via
`TQDM_DISABLE=1`). The worker's own log (Celery `--loglevel=info`) records stage timings,
the user-facing error and its detail.

## Determinism and the golden fixture

Upstream orders reads through `set()` iteration and its LSTMs are built with
`batch_first=False`, so predictions depend on k-mer order and batch composition. The worker
therefore pins `PYTHONHASHSEED=0` for every child process (the image sets it globally too) and
passes `OMP_NUM_THREADS` / `MKL_NUM_THREADS` = `RMODHUB_WORKER_THREADS`; with that, two runs on
the same inputs *at the same thread count* are byte-identical (`reads.txt`, read-level CSVs,
`input_denovo.npy`, site tables — checked with two 1-thread runs and with two 4-thread runs).
The thread count is part of the numeric environment: torch sums in a different order with a
different number of threads, and on the sample 4 threads vs 1 leaves `reads.txt`, the features
and every `count` / `coverage` / `rate` identical but moves per-read probabilities by up to 6e-8.
The golden fixture and this test suite use **1 thread** (the image default). `docker-compose.yml`
sets `RMODHUB_WORKER_THREADS=4` for throughput: that stays within the golden test's 1e-6
tolerance but is not byte-identical to the fixture. `tests/fixtures/golden_directrm_sample/`
holds the site tables of a by-hand run of the five upstream scripts on the sample
(725 sites, 3648 k-mers, 76 reads); `tests/test_golden_directrm.py` checks the worker reproduces
them exactly. The fixture was produced under CPython 3.9 and is reproduced byte for byte under
3.10 (same siphash24 hash); re-verify or regenerate it if the interpreter minor version changes
again (3.11+ hashes differently; commands in the fixture README). `tests/test_subset_equivalence.py`
additionally runs `tools/subset_pod5.py` on the sample and checks that the pipeline gives identical
`sites`/`reads` tables, `n_kmers` and read2site CSVs on the subset and on the full files.

## Docker

```
docker build -t rmodhub-worker:local worker/
docker run --rm rmodhub-worker:local python -c "import remora, pod5, pysam, torch; print(torch.__version__, torch.cuda.is_available())"
# full pipeline on the sample inside the container
docker run --rm -v /tmp/job:/data/uploads/jobs/job -v $PWD/app/samples/signal:/sample:ro rmodhub-worker:local \
    python -m rmodhub_worker.run_local /data/uploads/jobs/job --kit RNA004 --no-db --sample-dir /sample
```

Two stages (`python:3.10-slim` + uv 0.11; `build-essential` only in the builder), same
`app` uid/gid 1000 and `/data/uploads` as the API image, root-owned venv/code, torch
`test/`+`include/` trimmed. Size: the container's root filesystem is ~1.8 GB (`du -sxb /`
inside it — the figure CI guards at 3 GB, independent of the image store), 0.57 GB compressed
(`docker save`; also what `docker image inspect .Size` reports on a containerd image store,
where `docker image ls` shows 2.48 GB = compressed + unpacked); the venv layer is 1.72 GB
(torch 0.69 GB, polars 0.14 GB, pyarrow 0.13 GB, pysam/scipy/parasail ~0.08 GB each). Default
command: `celery -A rmodhub_worker.celery_app worker -Q signal --concurrency=1
--prefetch-multiplier=1 --loglevel=info`.

## Upstream quirks to keep in mind

See `directrm_vendor/UPSTREAM.md` for the full list. In short: the first base of every region
is never scored and k-mers may run up to 8 bases past `end`; `coverage` is "reads with a
non-zero score at this base", not depth; `count` is reads with score > 0.5; regions with
<= 30 reads are skipped and regions with >= 150 reads are randomly subsampled (unseeded);
the de novo probability is not used for gating; `feature_extraction.py` does not pass
`reverse_signal=True`, so per-base signal on real dorado RNA reads is mirrored within a read
relative to Remora's convention (reproduced faithfully).

## Limitations

* **POD5 format**: lib-pod5 0.3.47 reads every POD5 released so far (v5 and the v6 written by
  pod5 >= 0.3.46). A file from a future pod5 that changes the read-table schema again fails in
  `preparing` with "The pod5 file uses a newer POD5 format than this server can read (its reader
  is lib-pod5 0.3.47)…"; bump `pod5`/`lib-pod5` in `pyproject.toml` when that happens.
* Upstream loads all features of a split into memory (`inference.py`), so very large jobs are
  bounded by the worker's RAM, not by the worker code.
* Upstream builds paths with `os.system('mkdir -p ' + path)`: job directories must not contain
  spaces or shell metacharacters (UUID job ids never do; mind this with `run_local`).
* Contig names may not contain `/` (upstream writes `<type>/<seqname>.csv`).

## Re-vendoring DirectRM

```
git clone https://github.com/yuxinPenny/DirectRM && git -C DirectRM checkout bc7a08573dfe7629e808256fa6ade6e4111ed1f9
uv run --project worker python worker/scripts/vendor_directrm.py DirectRM
```

Copies scripts/utils/level tables/LICENSE byte-for-byte, re-serialises the 106 weight files to
CPU (values verified with `torch.equal`) and rewrites `directrm_vendor/WEIGHTS_MANIFEST.json`.
