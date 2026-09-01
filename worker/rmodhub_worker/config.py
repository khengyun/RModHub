"""Environment configuration (names from ``docs/signal-branch.md`` section 8).

The worker reads the same ``RMODHUB_*`` names as the API. Everything is read once via
:meth:`Settings.from_env`; nothing here imports torch or the model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: ``worker/directrm_vendor`` (works both from the source tree and inside the image).
DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parent.parent / "directrm_vendor"

KITS = ("RNA004", "RNA002")
#: k-mer level table per kit (``--kmer 9`` for both: the models are 9-mer, see contract §2).
LEVEL_TABLES = {"RNA004": "9mer_levels_v1.txt", "RNA002": "5mer_levels_v1.txt"}


def _env(*names: str, default: str | None = None) -> str | None:
    """First non-empty value among ``names`` in the environment."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{names[0]} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Worker settings; see the README for the meaning of every field."""

    database_url: str | None
    celery_broker_url: str | None
    upload_dir: Path
    job_timeout_s: int
    directrm_model_id: int
    min_coverage: int
    max_coverage: int
    max_regions: int
    worker_threads: int
    vendor_root: Path
    heartbeat_interval_s: float = 15.0
    child_grace_s: float = 5.0

    @classmethod
    def from_env(cls) -> Settings:
        threads = _env_int("RMODHUB_WORKER_THREADS", default=0)
        if threads <= 0:
            # Container default is OMP_NUM_THREADS=1 (deterministic, one job per worker).
            threads = _env_int("OMP_NUM_THREADS", default=1)
        return cls(
            database_url=_env("DATABASE_URL", "RMODHUB_DATABASE_URL"),
            celery_broker_url=_env("CELERY_BROKER_URL", "RMODHUB_CELERY_BROKER_URL"),
            upload_dir=Path(_env("RMODHUB_UPLOAD_DIR", default="/data/uploads")),
            job_timeout_s=_env_int("RMODHUB_JOB_TIMEOUT_S", default=21600),
            directrm_model_id=_env_int("RMODHUB_DIRECTRM_MODEL_ID", default=5),
            min_coverage=_env_int("RMODHUB_MIN_COVERAGE", default=30),
            max_coverage=_env_int("RMODHUB_MAX_COVERAGE", default=150),
            max_regions=_env_int("RMODHUB_MAX_REGIONS", default=10000),
            worker_threads=max(1, threads),
            vendor_root=Path(_env("RMODHUB_DIRECTRM_ROOT", default=str(DEFAULT_VENDOR_ROOT))),
        )

    def level_table(self, kit: str) -> Path:
        return self.vendor_root / LEVEL_TABLES[kit]

    def model_dir(self, kit: str) -> Path:
        return self.vendor_root / "model" / kit

    def denovo_model(self, kit: str) -> Path:
        return self.model_dir(kit) / "id3_binary" / "model.pt"

    def job_dir(self, job_id: str) -> Path:
        return self.upload_dir / "jobs" / job_id


def validate_kit(kit: str) -> str:
    kit = (kit or "").strip().upper()
    if kit not in KITS:
        raise ValueError(f"kit must be one of {KITS}, got {kit!r}")
    return kit
