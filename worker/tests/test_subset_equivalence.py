"""Acceptance criterion 3: a subset made by ``tools/subset_pod5.py`` gives the same results as the full files.

The subset tool (repository root, needs pod5 + pysam only, run here with the worker interpreter)
is applied to the synthetic sample with a regions file that lists ``tx_A`` and ``tx_B`` only,
writing ``subset.pod5`` + ``subset.bam``. The worker pipeline is then run twice with that same
regions file -- once on the full sample, once on the subset -- and the two ``results.sqlite``
files must agree: ``sites`` and ``reads`` tables column by column (``ORDER BY id``, the canonical
order), ``transcripts``, ``meta.n_kmers``, plus the upstream ``reads.txt`` and read2site CSVs byte
for byte. Because ``tx_C`` is below the coverage threshold anyway, both runs must also reproduce
the golden fixture (three-region run) exactly.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, SAMPLE_DIR

pytestmark = pytest.mark.slow

SUBSET_TOOL = REPO / "tools" / "subset_pod5.py"
REGIONS_AB = "seqnames,start,end,width,strand\ntx_A,60,300,241,+\ntx_B,80,320,241,+\n"
N_READS_AB = 76  # tx_A 40 + tx_B 36


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage(job_dir: Path, pod5: Path, bam: Path, bai: Path) -> Path:
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    shutil.copyfile(pod5, input_dir / "input.pod5")
    shutil.copyfile(bam, input_dir / "input_sorted.bam")
    shutil.copyfile(bai, input_dir / "input_sorted.bam.bai")
    shutil.copyfile(SAMPLE_DIR / "sample_reference.fa", input_dir / "reference.fa")
    (input_dir / "regions.csv").write_text(REGIONS_AB)
    return job_dir


def _run(job_dir: Path, settings) -> dict[str, Any]:
    from rmodhub_worker.db import NullJobDB
    from rmodhub_worker.tasks import execute_job

    summary = execute_job(
        job_dir.name,
        settings=settings,
        db=NullJobDB(),
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "done", summary
    return summary


@pytest.fixture(scope="module")
def runs(tmp_path_factory, settings) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("subset_equivalence")
    regions = root / "regions_AB.csv"
    regions.write_text(REGIONS_AB)
    subset_pod5 = root / "subset.pod5"
    subset_bam = root / "subset.bam"
    proc = subprocess.run(
        [
            sys.executable,
            str(SUBSET_TOOL),
            "-i",
            str(SAMPLE_DIR / "sample.pod5"),
            "-b",
            str(SAMPLE_DIR / "sample_sorted.bam"),
            "-r",
            str(regions),
            "-o",
            str(subset_pod5),
            "--bam-out",
            str(subset_bam),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert subset_pod5.is_file() and subset_bam.is_file() and (root / "subset.bam.bai").is_file()

    full_dir = _stage(
        root / "full",
        SAMPLE_DIR / "sample.pod5",
        SAMPLE_DIR / "sample_sorted.bam",
        SAMPLE_DIR / "sample_sorted.bam.bai",
    )
    subset_dir = _stage(root / "subset", subset_pod5, subset_bam, root / "subset.bam.bai")
    return {
        "tool_stdout": proc.stdout,
        "subset_pod5": subset_pod5,
        "full": {"job_dir": full_dir, "summary": _run(full_dir, settings)},
        "subset": {"job_dir": subset_dir, "summary": _run(subset_dir, settings)},
    }


def _table(job_dir: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{job_dir / 'results.sqlite'}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _meta(job_dir: Path) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in _table(job_dir, "SELECT key, value FROM meta")}


def test_subset_tool_selected_the_two_regions(runs):
    out = runs["tool_stdout"]
    assert f"selected   : {N_READS_AB} unique read ids" in out
    assert f"found      : {N_READS_AB} / {N_READS_AB} read ids in pod5" in out
    import pod5

    with pod5.Reader(runs["subset_pod5"]) as reader:
        assert reader.num_reads == N_READS_AB
    assert runs["subset"]["summary"]["meta"]["n_reads_pod5"] == N_READS_AB
    assert runs["full"]["summary"]["meta"]["n_reads_pod5"] == 88


def test_sites_tables_identical(runs):
    sql = "SELECT * FROM sites ORDER BY id"
    full = _table(runs["full"]["job_dir"], sql)
    subset = _table(runs["subset"]["job_dir"], sql)
    assert len(full) == len(subset) > 0
    assert full == subset
    columns = [r[1] for r in _table(runs["subset"]["job_dir"], "PRAGMA table_info(sites)")]
    assert len(full[0]) == len(columns) == 12  # every column compared, id included


def test_reads_tables_identical(runs):
    sql = "SELECT * FROM reads ORDER BY id"
    full = _table(runs["full"]["job_dir"], sql)
    subset = _table(runs["subset"]["job_dir"], sql)
    assert len(full) == len(subset) > 0
    assert full == subset


def test_transcripts_and_counts_identical(runs):
    sql = "SELECT * FROM transcripts ORDER BY transcript_id"
    assert _table(runs["full"]["job_dir"], sql) == _table(runs["subset"]["job_dir"], sql)
    full, subset = runs["full"]["summary"], runs["subset"]["summary"]
    for key in ("n_sites", "n_reads", "n_transcripts", "n_read_rows"):
        assert full[key] == subset[key], key


def test_meta_n_kmers_equal(runs):
    full, subset = _meta(runs["full"]["job_dir"]), _meta(runs["subset"]["job_dir"])
    assert full["n_kmers"] == subset["n_kmers"] > 0
    assert (
        runs["full"]["summary"]["meta"]["n_kmers"] == runs["subset"]["summary"]["meta"]["n_kmers"]
    )
    for key in (
        "n_reads_sampled",
        "n_reads_features",
        "denovo_frac_modified",
        "regions_total",
        "regions_skipped_low_coverage",
        "regions_subsampled",
    ):
        assert full[key] == subset[key], key


def test_upstream_files_byte_identical(runs):
    """``reads.txt`` (sampling order) and the six read2site CSVs are identical between the runs."""
    full, subset = runs["full"]["job_dir"] / "work", runs["subset"]["job_dir"] / "work"
    assert _sha256(full / "reads.txt") == _sha256(subset / "reads.txt")
    names = sorted(p.name for p in (full / "sites").iterdir())
    assert names == sorted(p.name for p in (subset / "sites").iterdir()) and names
    for name in names:
        assert _sha256(full / "sites" / name) == _sha256(subset / "sites" / name), name


def test_both_runs_reproduce_the_golden_fixture(runs, golden_meta):
    """tx_C is dropped by sampling anyway, so the two-region runs equal the three-region golden."""
    for label in ("full", "subset"):
        sites_dir = runs[label]["job_dir"] / "work" / "sites"
        for upstream_type, info in golden_meta["sites_per_type"].items():
            assert _sha256(sites_dir / f"{upstream_type}.csv") == info["sha256"], (
                label,
                upstream_type,
            )
        assert runs[label]["summary"]["n_sites"] == golden_meta["n_sites"] == 725
        assert runs[label]["summary"]["meta"]["n_kmers"] == golden_meta["n_kmers"]
