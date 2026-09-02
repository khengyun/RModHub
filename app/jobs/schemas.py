"""Pydantic models of the job API (docs/signal-branch.md section 6).

`SignalSite` extends the frozen cross-branch `ModSite` (app/schemas.py) without touching it:
every `SignalSite` validates as a plain `ModSite` and serialises with the shared seven
fields first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.constants import (
    DEFAULT_KIT,
    LOW_COVERAGE_THRESHOLD,
    MODEL_NAME,
    MODEL_VERSION,
    SIGNAL_MOD_TYPES,
    Kit,
)
from app.schemas import ModSite

JobState = Literal["uploading", "queued", "running", "done", "failed", "cancelled", "expired"]
Stage = Literal[
    "uploading", "preparing", "sampling", "features", "denovo", "inference", "aggregating"
]
ResultLevel = Literal["site", "read"]
SortKey = Literal["position", "rate", "coverage", "mod_type"]
SortOrder = Literal["asc", "desc"]


# ------------------------------------------------------------------------------ job status


class ModelInfo(BaseModel):
    name: str = MODEL_NAME
    version: str = MODEL_VERSION


class UploadInfo(BaseModel):
    """One tus upload of a job in `status == "uploading"`."""

    url: str = Field(description="Relative tus URL: PATCH bytes here, HEAD for the offset.")
    length: int = Field(ge=0, description="Declared size in bytes.")
    offset: int = Field(ge=0, description="Bytes received so far.")
    complete: bool


class JobStatus(BaseModel):
    job_id: str
    status: JobState
    stage: Stage | None = None
    progress: float | None = Field(default=None, description="0..1 within the current stage.")
    eta_s: float | None = None
    kit: Kit
    input_kind: Literal["upload", "sample"]
    input_bytes: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    inputs_deleted_at: datetime | None = None
    cancel_requested: bool = False
    error: str | None = Field(default=None, description="One user-safe sentence when failed.")
    n_sites: int | None = None
    n_reads: int | None = None
    n_transcripts: int | None = None
    model: ModelInfo = Field(default_factory=ModelInfo)
    uploads: dict[str, UploadInfo] | None = Field(
        default=None, description="Filled only while status == 'uploading'."
    )


# ---------------------------------------------------------------------------- job creation


class FileDecl(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Client-side file name.")
    size: int = Field(ge=0, description="Size in bytes.")


class JobInit(BaseModel):
    """Body of `POST /api/jobs/signal/init`: sizes are declared before any byte is sent."""

    kit: Kit = DEFAULT_KIT
    files: dict[str, FileDecl] = Field(
        description="Keys: pod5, bam, reference, regions (all four are required).",
        json_schema_extra={
            "example": {
                "pod5": {"name": "run.pod5", "size": 734003200},
                "bam": {"name": "run_sorted.bam", "size": 52428800},
                "reference": {"name": "transcripts.fa", "size": 1048576},
                "regions": {"name": "regions.csv", "size": 2048},
            }
        },
    )


# --------------------------------------------------------------------------------- results


class SignalSite(ModSite):
    """A site-level DirectRM call: the shared `ModSite` fields first, then the extras.

    `probability` is the modification rate (`count / coverage`); `p_value` is always null
    for the signal branch; `coverage` is the number of reads scored at the site.
    """

    model_config = ConfigDict(json_schema_extra={"title": "SignalSite"})

    source: Literal["signal"] = "signal"
    strand: str = Field(description="'+' or '-'.")
    count: int = Field(ge=0, description="Reads with a per-read probability > 0.5.")
    ci_low: float = Field(ge=0.0, le=1.0, description="Wilson 95 % interval, lower bound.")
    ci_high: float = Field(ge=0.0, le=1.0, description="Wilson 95 % interval, upper bound.")
    max_prob: float | None = Field(default=None, description="Highest per-read probability.")
    noisyor_prob: float | None = Field(
        default=None, description="Noisy-OR of the per-read probabilities."
    )


class SignalRead(BaseModel):
    """One per-read call (drill-down under a site)."""

    read_id: str
    transcript_id: str
    position: int = Field(ge=1)
    strand: str
    mod_type: str
    probability: float = Field(ge=0.0, le=1.0)
    source: Literal["signal"] = "signal"


class TranscriptInfo(BaseModel):
    transcript_id: str
    length: int | None = None
    n_reads: int | None = None
    n_sites: int | None = None


class SignalResultsMeta(BaseModel):
    source: Literal["signal"] = "signal"
    job_id: str
    model_name: str = MODEL_NAME
    model_version: str = MODEL_VERSION
    kit: Kit
    n_sites: int | None = None
    n_reads: int | None = None
    n_transcripts: int | None = None
    mod_types: list[str] = Field(default_factory=lambda: list(SIGNAL_MOD_TYPES))
    low_coverage_threshold: int = LOW_COVERAGE_THRESHOLD
    transcripts: list[TranscriptInfo] = Field(default_factory=list)
    extra: dict = Field(
        default_factory=dict, description="Every key of the job's results.sqlite `meta` table."
    )


class ResultsPage(BaseModel):
    results: list[SignalSite | SignalRead]
    meta: SignalResultsMeta
    total: int = Field(ge=0, description="Rows matching the filters (before paging).")
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


# ----------------------------------------------------------------------------- capabilities


class Limits(BaseModel):
    max_pod5_gb: float
    max_bam_gb: float
    max_reference_mb: float
    max_regions: int
    max_running_per_ip: int
    max_queued_per_ip: int
    job_timeout_h: float
    tus_chunk_mb: int
    upload_ttl_h: int = Field(
        description="Hours an unfinished upload (a job in state 'uploading') is kept "
        "(RMODHUB_UPLOAD_TTL_H)."
    )


class Retention(BaseModel):
    inputs_deleted: str = "after feature extraction, at most 48 h"
    results_days: int = 14


class SequenceModelInfo(BaseModel):
    """One sequence-branch back-end this deployment loaded."""

    id: str = Field(description="Value to send in `models` of POST /api/predict/sequence.")
    label: str = Field(description="Human-readable name for the picker.")
    description: str
    default: bool = Field(description="True for the model used when a request names none.")
    name: str = Field(description="Model name the back-end reports (meta.model_name).")
    version: str = Field(description="Model version the back-end reports (meta.model_version).")
    min_sequence_nt: int = Field(
        description="Shortest input this model can score (its window size)."
    )
    max_sequence_nt: int | None = Field(
        default=None,
        description=(
            "Longest input this model accepts, when it is stricter than the server's own "
            "RMODHUB_MAX_SEQUENCE_NT; null when only the server limit applies."
        ),
    )


class Capabilities(BaseModel):
    sequence: bool = True
    signal: bool
    limits: Limits
    retention: Retention
    sequence_models: list[SequenceModelInfo] = Field(
        default_factory=list,
        description="Sequence models this deployment loaded; the first one is the default.",
    )


# ----------------------------------------------------------------------------------- sample


class SampleFile(BaseModel):
    slot: str
    filename: str
    bytes: int
    url: str


class SampleRegion(BaseModel):
    seqnames: str
    start: int
    end: int
    width: int
    strand: str


class SignalSample(BaseModel):
    name: str
    description: str
    kit: Kit
    files: list[SampleFile]
    source: Literal["synthetic"] = "synthetic"
    regions: list[SampleRegion] = Field(default_factory=list)
