"""Input deletion and job-directory removal (contract section 8, "Lifecycle rules")."""

from __future__ import annotations

import shutil
from pathlib import Path

#: Raw inputs deleted right after feature extraction. The reference (small) and regions are
#: kept because the aggregation stage still needs contig lengths, and they are useful context.
DELETABLE_SUFFIXES = (".pod5", ".bam", ".bai")


def delete_inputs(job_dir: Path) -> list[str]:
    """Delete the pod5/BAM(.bai) files under ``<job>/input``; return the names removed."""
    input_dir = Path(job_dir) / "input"
    removed: list[str] = []
    if not input_dir.is_dir():
        return removed
    for path in sorted(input_dir.iterdir()):
        if path.is_file() and path.suffix in DELETABLE_SUFFIXES:
            path.unlink()
            removed.append(path.name)
    return removed


def remove_job_dir(job_dir: Path, uploads_root: Path | None = None) -> bool:
    """``rm -rf`` the job directory.

    When ``uploads_root`` is given the directory must live under ``<uploads_root>/jobs`` (a
    guard against a misconfigured path wiping something else). Returns True if removed.
    """
    job_dir = Path(job_dir).resolve()
    if uploads_root is not None:
        jobs_root = (Path(uploads_root) / "jobs").resolve()
        if jobs_root not in job_dir.parents:
            raise ValueError(f"refusing to remove {job_dir}: not under {jobs_root}")
    if not job_dir.exists():
        return False
    shutil.rmtree(job_dir, ignore_errors=True)
    return not job_dir.exists()


def dir_bytes(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
