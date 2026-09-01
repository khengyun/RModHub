"""Names fixed by docs/signal-branch.md (shared with the worker and the frontend)."""

from __future__ import annotations

from typing import Literal

# Job states (`jobs.status`).
UPLOADING = "uploading"
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"
STATUSES: tuple[str, ...] = (UPLOADING, QUEUED, RUNNING, DONE, FAILED, CANCELLED, EXPIRED)
TERMINAL_STATUSES: frozenset[str] = frozenset({DONE, FAILED, CANCELLED, EXPIRED})
# Counted against `max_queued_per_ip`.
WAITING_STATUSES: tuple[str, ...] = (UPLOADING, QUEUED)

# Pipeline stages (`jobs.stage`).
STAGES: tuple[str, ...] = (
    "uploading",
    "preparing",
    "sampling",
    "features",
    "denovo",
    "inference",
    "aggregating",
)

Kit = Literal["RNA004", "RNA002"]
KITS: tuple[str, ...] = ("RNA004", "RNA002")
DEFAULT_KIT = "RNA004"

Slot = Literal["pod5", "bam", "reference", "regions"]
SLOTS: tuple[str, ...] = ("pod5", "bam", "reference", "regions")

# File names upstream DirectRM expects inside jobs/<job_id>/input/ (split name `input`).
INPUT_FILENAMES: dict[str, str] = {
    "pod5": "input.pod5",
    "bam": "input_sorted.bam",
    "bai": "input_sorted.bam.bai",
    "reference": "reference.fa",
    "regions": "regions.csv",
}

# Synthetic sample shipped in app/samples/signal/ (section 10 of the contract).
SAMPLE_FILENAMES: dict[str, str] = {
    "pod5": "sample.pod5",
    "bam": "sample_sorted.bam",
    "bai": "sample_sorted.bam.bai",
    "reference": "sample_reference.fa",
    "regions": "sample_regions.csv",
}

INPUT_KIND_UPLOAD = "upload"
INPUT_KIND_SAMPLE = "sample"

MODEL_NAME = "DirectRM"
MODEL_VERSION = "bc7a085"

# Celery task name and queue the API enqueues by name (section 7).
TASK_NAME = "rmodhub.signal.run_job"
QUEUE_NAME = "signal"

# Shared modification vocabulary of the signal branch (section 5).
SIGNAL_MOD_TYPES: tuple[str, ...] = ("ac4C", "m1A", "m5C", "m6A", "m7G", "Psi")
LOW_COVERAGE_THRESHOLD = 30

# Default DirectRM parameters recorded in `jobs.params`.
DEFAULT_PARAMS: dict[str, int] = {"model_id": 5, "min_coverage": 30, "max_coverage": 150}

# A worker whose heartbeat is older than this is considered dead (section 7).
DEAD_WORKER_AFTER_S = 600

TUS_VERSION = "1.0.0"
TUS_CONTENT_TYPE = "application/offset+octet-stream"

SIGNAL_DISABLED_DETAIL = "The nanopore signal branch is not enabled on this server."
