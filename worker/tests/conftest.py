"""Shared fixtures for the worker test suite.

``sample_job`` runs the full pipeline ONCE per session on the synthetic sample
(``app/samples/signal``) through ``execute_job`` with the in-memory ``NullJobDB``; the
end-to-end and golden tests read from that single run. Everything else is unit-level.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO / "app" / "samples" / "signal"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_directrm_sample"

SAMPLE_TO_INPUT = {
    "sample.pod5": "input.pod5",
    "sample_sorted.bam": "input_sorted.bam",
    "sample_sorted.bam.bai": "input_sorted.bam.bai",
    "sample_reference.fa": "reference.fa",
    "sample_regions.csv": "regions.csv",
}


def stage_sample_inputs(job_dir: Path) -> Path:
    """Copy the repository sample into ``<job_dir>/input`` with the upstream names."""
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for src, dst in SAMPLE_TO_INPUT.items():
        shutil.copyfile(SAMPLE_DIR / src, input_dir / dst)
    return job_dir


@pytest.fixture(scope="session")
def golden_meta() -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / "meta.json").read_text())


@pytest.fixture(scope="session")
def settings():
    from rmodhub_worker.config import Settings

    base = Settings.from_env()
    # One torch thread (matches the golden run) and a fast heartbeat so the tests can see it.
    return Settings(**{**base.__dict__, "worker_threads": 1, "heartbeat_interval_s": 0.5})


@pytest.fixture(scope="session")
def sample_job(tmp_path_factory, settings) -> dict[str, Any]:
    """Full pipeline run on the sample (about 15 s). Returns job dir, summary and the NullJobDB."""
    from rmodhub_worker.db import NullJobDB
    from rmodhub_worker.tasks import execute_job

    job_dir = stage_sample_inputs(tmp_path_factory.mktemp("sample_job"))
    db = NullJobDB()
    summary = execute_job(
        job_dir.name,
        settings=settings,
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    return {"job_dir": job_dir, "summary": summary, "db": db}
