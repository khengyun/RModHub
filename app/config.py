"""Runtime configuration. Every field can be set as an environment variable with the
`RMODHUB_` prefix (e.g. `RMODHUB_PREDICTOR=stub`), or in a `.env` file in the working directory.

The nanopore signal branch (job API, tus uploads, Celery producer) is enabled by setting a
database URL (`DATABASE_URL` or `RMODHUB_DATABASE_URL`). Without it the server runs the
sequence branch only and every `/api/jobs` and `/api/uploads` route answers 503.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app import __version__
from app.predictors.base import MIN_SEQUENCE_NT

LogLevel = Literal["critical", "error", "warning", "info", "debug"]

DEFAULT_IP_HASH_SECRET = "rmodhub-dev"
DEFAULT_SAMPLE_DIR = Path(__file__).parent / "samples" / "signal"

GiB = 1024**3
MiB = 1024**2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RMODHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Fields with an explicit `validation_alias` (the un-prefixed DATABASE_URL & co.)
        # must still be settable by name: tests build `Settings(database_url=...)`.
        validate_by_name=True,
        validate_by_alias=True,
    )

    app_name: str = "RModHub API"
    version: str = __version__

    # ------------------------------------------------------------------ sequence branch
    predictor: Literal["multirm", "stub"] = Field(
        default="multirm",
        description="'multirm' loads the real model; 'stub' is a torch-free fake for development.",
    )
    min_sequence_nt: int = Field(
        default=MIN_SEQUENCE_NT,
        ge=MIN_SEQUENCE_NT,
        description="Shortest accepted input. Cannot go below the model's 51-nt window.",
    )
    max_sequence_nt: int = Field(
        default=10_000,
        ge=MIN_SEQUENCE_NT,
        description="Longest accepted input; bounds per-request CPU time and memory.",
    )
    default_alpha: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        description="p-value threshold used when the request omits alpha.",
    )
    warmup: bool = Field(default=True, description="Run one dummy inference at startup.")
    torch_threads: int | None = Field(
        default=None,
        ge=1,
        description=(
            "torch intra-op threads for inference. Unset: honour OMP_NUM_THREADS if present, "
            "otherwise min(4, cpu_count) — torch's own default (all cores) oversubscribes a "
            "shared box and makes single requests slower, not faster."
        ),
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Allowed CORS origins. JSON list or comma-separated. Empty = CORS middleware disabled.",
    )
    log_level: LogLevel = "info"

    # -------------------------------------------------------------------- signal branch
    # Names, aliases and defaults follow docs/signal-branch.md section 8. The worker reads
    # the same variables.
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RMODHUB_DATABASE_URL", "DATABASE_URL"),
        description=(
            "SQLAlchemy URL of the job metadata store (postgresql+psycopg://...; tests use "
            "sqlite+pysqlite:///...). Unset = signal branch disabled."
        ),
    )
    celery_broker_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RMODHUB_CELERY_BROKER_URL", "CELERY_BROKER_URL"),
        description=(
            "Redis URL of the Celery broker (may carry a password, hence a secret). "
            "Unset = jobs are recorded but never sent."
        ),
    )
    upload_dir: Path = Field(
        default=Path("/data/uploads"),
        description="Shared volume holding tus/ partial uploads and jobs/<job_id>/.",
    )
    sample_dir: Path = Field(
        default=DEFAULT_SAMPLE_DIR,
        description="Directory with the synthetic signal sample (sample.pod5, sample_sorted.bam, ...).",
    )
    max_pod5_gb: float = Field(
        default=5,
        gt=0,
        validation_alias=AliasChoices("RMODHUB_MAX_POD5_GB", "MAX_POD5_GB"),
        description="Largest accepted pod5 file (GiB).",
    )
    max_bam_gb: float = Field(default=5, gt=0, description="Largest accepted BAM file (GiB).")
    max_reference_mb: float = Field(
        default=500, gt=0, description="Largest accepted reference FASTA (MiB)."
    )
    max_regions: int = Field(default=10_000, ge=1, description="Most data rows in regions.csv.")
    max_running_per_ip: int = Field(default=1, ge=1, description="Jobs in `running` per address.")
    max_queued_per_ip: int = Field(
        default=3, ge=1, description="Jobs in `uploading` + `queued` per address."
    )
    job_timeout_s: int = Field(default=21_600, ge=60, description="Celery soft time limit.")
    results_retention_days: int = Field(default=14, ge=1, description="Days results are kept.")
    inputs_max_age_h: int = Field(
        default=48, ge=1, description="Backstop: input/ dirs older than this are deleted."
    )
    upload_ttl_h: int = Field(
        default=48, ge=1, description="Unfinished uploads / `uploading` jobs expire after this."
    )
    tus_chunk_mb: int = Field(default=64, ge=1, description="Largest accepted tus PATCH body (MiB).")
    cleanup_interval_s: int = Field(
        default=3600, ge=1, description="Period of the in-process cleanup loop."
    )
    ip_hash_secret: SecretStr = Field(
        default=SecretStr(DEFAULT_IP_HASH_SECRET),
        description="HMAC key for the per-address quota key; raw IPs are never stored.",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_log_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _empty_means_unset(cls, data: object) -> object:
        # `DATABASE_URL=` / `RMODHUB_UPLOAD_DIR=` in a .env or compose file means "not
        # configured", the same as compose's own `${VAR:+...}` semantics: drop the key so
        # the field default applies (an empty upload_dir would otherwise become the cwd, an
        # empty ip_hash_secret an empty HMAC key without the development-default warning,
        # an empty numeric cap a validation error at start-up).
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    def effective_torch_threads(self) -> int | None:
        """Thread count to pass to the model loader; None means "leave torch alone"."""
        if self.torch_threads is not None:
            return self.torch_threads
        if os.environ.get("OMP_NUM_THREADS"):
            return None  # torch already reads it; e.g. the Dockerfile sets 1
        return min(4, os.cpu_count() or 1)

    @model_validator(mode="after")
    def _check_length_bounds(self) -> Settings:
        if self.max_sequence_nt < self.min_sequence_nt:
            raise ValueError(
                f"max_sequence_nt ({self.max_sequence_nt}) must be >= "
                f"min_sequence_nt ({self.min_sequence_nt})"
            )
        return self

    # ------------------------------------------------------------------- derived values
    @property
    def signal_enabled(self) -> bool:
        """The signal branch is on exactly when a metadata database is configured."""
        return self.database_url is not None

    @property
    def max_pod5_bytes(self) -> int:
        return int(self.max_pod5_gb * GiB)

    @property
    def max_bam_bytes(self) -> int:
        return int(self.max_bam_gb * GiB)

    @property
    def max_reference_bytes(self) -> int:
        return int(self.max_reference_mb * MiB)

    @property
    def tus_chunk_bytes(self) -> int:
        return self.tus_chunk_mb * MiB

    @property
    def ip_hash_secret_is_default(self) -> bool:
        return self.ip_hash_secret.get_secret_value() == DEFAULT_IP_HASH_SECRET

    def for_log(self) -> dict:
        """Settings as a plain dict for the startup log line, secrets redacted."""
        out: dict = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr):
                out[name] = "***" if value.get_secret_value() else None
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        out["signal_enabled"] = self.signal_enabled
        return out


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()
