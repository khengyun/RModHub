"""``results.sqlite`` writer: the ``regions`` table and the durability of the publish step."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from rmodhub_worker.aggregate import build_results

REGIONS = [("tx_A", 60, 300, "+", 40), ("tx_B", 80, 320, "+", 36), ("tx_C", 50, 250, "-", 12)]


def _build(tmp_path: Path, regions=()) -> tuple[Path, tuple[int, int, int]]:
    """Build a results file from empty upstream outputs (no sites, no read-level rows)."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    counts = build_results(
        job_dir,
        meta={"kit": "RNA004", "regions_total": len(regions)},
        transcripts=[("tx_A", 560, 40)],
        sites_dir=job_dir / "sites",
        inference_dir=job_dir / "inference",
        regions=regions,
    )
    return job_dir, counts


def test_regions_table_holds_one_row_per_region_in_csv_order(tmp_path: Path):
    job_dir, counts = _build(tmp_path, regions=REGIONS)
    assert counts == (0, 0, 1)
    conn = sqlite3.connect(f"file:{job_dir / 'results.sqlite'}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT transcript_id, start, end, strand, n_reads FROM regions ORDER BY id"
        ).fetchall()
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    assert rows == REGIONS
    assert "region_read_counts" not in meta and meta["regions_total"] == "3"
    assert not (job_dir / "results.sqlite.tmp").exists()
    assert not (job_dir / "results.staging.sqlite").exists()


def test_results_file_is_fsynced_before_and_the_directory_after_the_rename(
    tmp_path: Path, monkeypatch
):
    """``journal_mode = OFF`` / ``synchronous = OFF`` mean SQLite never fsyncs; the worker must
    (file, then rename, then directory) or a crash after ``status = done`` can leave a
    zero-length ``results.sqlite`` for the whole retention period."""
    events: list[tuple[str, ...]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        events.append(("fsync", Path(os.readlink(f"/proc/self/fd/{fd}")).name))
        real_fsync(fd)

    def spy_replace(src, dst) -> None:
        events.append(("replace", Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    job_dir, _ = _build(tmp_path)
    assert events == [
        ("fsync", "results.sqlite.tmp"),
        ("replace", "results.sqlite.tmp", "results.sqlite"),
        ("fsync", "job"),
    ]
    assert (job_dir / "results.sqlite").stat().st_size > 0
