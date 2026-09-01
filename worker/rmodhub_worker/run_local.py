"""Run the full pipeline on a job directory without Celery (and, with ``--no-db``, without Postgres).

    python -m rmodhub_worker.run_local <job_dir> --kit RNA004 --no-db
    python -m rmodhub_worker.run_local <job_dir> --kit RNA004 --no-db --sample-dir ../app/samples/signal

``<job_dir>/input`` must contain the upstream input names (``input.pod5``,
``input_sorted.bam`` [+ ``.bai``], ``reference.fa``, ``regions.csv``); ``--sample-dir`` copies
the repository sample into that layout first. Without ``--no-db`` the job row (``--job-id``,
default: the directory name) is updated in ``DATABASE_URL`` exactly as the Celery task does.
Exit status is 0 when the job reaches ``done``, 1 otherwise; ``--json`` prints the summary as JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .config import Settings, validate_kit
from .db import NullJobDB, PostgresJobDB
from .prepare import INPUT_BAI, INPUT_BAM, INPUT_POD5, INPUT_REFERENCE, INPUT_REGIONS
from .tasks import execute_job

#: ``app/samples/signal`` file -> upstream input name.
SAMPLE_FILES: dict[str, str] = {
    "sample.pod5": INPUT_POD5,
    "sample_sorted.bam": INPUT_BAM,
    "sample_sorted.bam.bai": INPUT_BAI,
    "sample_reference.fa": INPUT_REFERENCE,
    "sample_regions.csv": INPUT_REGIONS,
}


def stage_sample(sample_dir: Path, job_dir: Path) -> None:
    """Copy the repository sample into ``<job_dir>/input`` with the upstream names."""
    input_dir = Path(job_dir) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in SAMPLE_FILES.items():
        src = Path(sample_dir) / src_name
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copyfile(src, input_dir / dst_name)


def _print_summary(summary: dict) -> None:
    print(f"job      : {summary.get('job_id')}  ({summary.get('kit')})")
    print(f"status   : {summary.get('status')}")
    if summary.get("error"):
        print(f"error    : {summary['error']}")
        if summary.get("detail"):
            print(f"detail   : {summary['detail']}")
    for stage, seconds in (summary.get("stage_seconds") or {}).items():
        print(f"  {stage:<12} {seconds:8.1f} s")
    meta = summary.get("meta") or {}
    for key in (
        "n_reads_pod5",
        "regions_total",
        "regions_skipped_low_coverage",
        "regions_subsampled",
        "n_reads_sampled",
        "n_reads_features",
        "n_kmers",
        "denovo_frac_modified",
    ):
        if key in meta:
            print(f"  {key:<28} {meta[key]}")
    for key in ("n_sites", "n_reads", "n_transcripts", "n_read_rows", "results"):
        if key in summary:
            print(f"  {key:<28} {summary[key]}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rmodhub_worker.run_local",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--kit", default="RNA004", help="RNA004 (default) or RNA002")
    parser.add_argument("--no-db", action="store_true", help="do not touch Postgres")
    parser.add_argument("--job-id", default=None, help="jobs.id (default: job_dir name)")
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=None,
        help="copy app/samples/signal into job_dir/input first",
    )
    parser.add_argument(
        "--model-id",
        type=int,
        default=None,
        help="DirectRM integrated model id (default RMODHUB_DIRECTRM_MODEL_ID=5)",
    )
    parser.add_argument("--min-coverage", type=int, default=None)
    parser.add_argument("--max-coverage", type=int, default=None)
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help=(
            "OMP threads for the child processes (default RMODHUB_WORKER_THREADS, else "
            "OMP_NUM_THREADS, else 1)"
        ),
    )
    parser.add_argument(
        "--keep-inputs", action="store_true", help="do not delete pod5/BAM after feature extraction"
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = Settings.from_env()
    if args.threads is not None:
        settings = Settings(**{**settings.__dict__, "worker_threads": max(1, args.threads)})
    job_dir = args.job_dir.resolve()
    job_id = args.job_id or job_dir.name
    if args.sample_dir is not None:
        stage_sample(args.sample_dir, job_dir)

    if args.no_db:
        db = NullJobDB()
    else:
        if not settings.database_url:
            parser.error("DATABASE_URL is not set (use --no-db to run without Postgres)")
        db = PostgresJobDB(settings.database_url)

    params = {}
    if args.model_id is not None:
        params["model_id"] = args.model_id
    if args.min_coverage is not None:
        params["min_coverage"] = args.min_coverage
    if args.max_coverage is not None:
        params["max_coverage"] = args.max_coverage

    summary = execute_job(
        job_id,
        settings=settings,
        db=db,
        job_dir=job_dir,
        kit=validate_kit(args.kit),
        params=params,
        remove_dir_on_abort=False,
        delete_inputs=not args.keep_inputs,
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_summary(summary)
    return 0 if summary.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
