"""Golden test: the worker's ``sites`` table must reproduce the by-hand upstream run exactly.

The fixture in ``fixtures/golden_directrm_sample`` was produced ONCE by running the five
unmodified DirectRM scripts by hand (see its README). ``count``/``coverage`` must match
exactly, ``max_prob``/``noisyor_prob`` within 1e-6; ``rate`` and the Wilson interval are
derived here from the golden counts.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest
from conftest import GOLDEN_DIR

from rmodhub_worker.aggregate import MOD_TYPE_MAP, UPSTREAM_TYPES
from rmodhub_worker.wilson import wilson_interval

pytestmark = pytest.mark.slow

TOL = 1e-6
Key = tuple[str, int, str, str]


def load_golden_sites() -> dict[Key, dict]:
    expected: dict[Key, dict] = {}
    for upstream_type in UPSTREAM_TYPES:
        with (GOLDEN_DIR / f"{upstream_type}.csv").open(newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row["seqnames"], int(row["pos"]), row["strand"], MOD_TYPE_MAP[upstream_type])
                assert key not in expected
                expected[key] = {
                    "max_prob": float(row["max_prob"]),
                    "noisyor_prob": float(row["noisyor_prob"]),
                    "count": int(float(row["count"])),
                    "coverage": int(row["coverage"]),
                }
    return expected


@pytest.fixture(scope="module")
def golden_sites() -> dict[Key, dict]:
    return load_golden_sites()


@pytest.fixture(scope="module")
def worker_sites(sample_job) -> dict[Key, dict]:
    path = Path(sample_job["job_dir"]) / "results.sqlite"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT transcript_id, position, strand, mod_type, rate, ci_low, ci_high, coverage, "
            "count, max_prob, noisyor_prob FROM sites"
        ).fetchall()
    finally:
        conn.close()
    got: dict[Key, dict] = {}
    for tx, pos, strand, mod, rate, low, high, coverage, count, max_prob, noisyor in rows:
        got[(tx, pos, strand, mod)] = {
            "rate": rate,
            "ci_low": low,
            "ci_high": high,
            "coverage": coverage,
            "count": count,
            "max_prob": max_prob,
            "noisyor_prob": noisyor,
        }
    return got


def test_fixture_integrity(golden_meta):
    for upstream_type, info in golden_meta["sites_per_type"].items():
        path = GOLDEN_DIR / f"{upstream_type}.csv"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]
    assert (
        sum(v["n_sites"] for v in golden_meta["sites_per_type"].values())
        == golden_meta["n_sites"]
        == 725
    )
    assert (
        hashlib.sha256((GOLDEN_DIR / "reads.txt").read_bytes()).hexdigest()
        == golden_meta["reads_txt_sha256"]
    )


def test_same_site_set(golden_sites, worker_sites):
    assert len(golden_sites) == 725
    missing = set(golden_sites) - set(worker_sites)
    extra = set(worker_sites) - set(golden_sites)
    assert not missing, f"{len(missing)} golden sites missing, e.g. {sorted(missing)[:5]}"
    assert not extra, f"{len(extra)} unexpected sites, e.g. {sorted(extra)[:5]}"


def test_counts_and_probabilities_match(golden_sites, worker_sites):
    for key, exp in golden_sites.items():
        got = worker_sites[key]
        assert got["count"] == exp["count"], key
        assert got["coverage"] == exp["coverage"], key
        assert got["max_prob"] == pytest.approx(exp["max_prob"], abs=TOL), key
        assert got["noisyor_prob"] == pytest.approx(exp["noisyor_prob"], abs=TOL), key


def test_rate_and_wilson_derived_from_golden_counts(golden_sites, worker_sites):
    for key, exp in golden_sites.items():
        got = worker_sites[key]
        rate = exp["count"] / exp["coverage"]
        low, high = wilson_interval(exp["count"], exp["coverage"])
        assert got["rate"] == pytest.approx(rate, abs=1e-12), key
        assert got["ci_low"] == pytest.approx(low, abs=1e-12), key
        assert got["ci_high"] == pytest.approx(high, abs=1e-12), key


def test_upstream_site_tables_byte_identical(sample_job, golden_meta):
    """Stronger than the tolerance check: the read2site CSVs the worker produced are identical."""
    sites_dir = Path(sample_job["job_dir"]) / "work" / "sites"
    for upstream_type, info in golden_meta["sites_per_type"].items():
        produced = sites_dir / f"{upstream_type}.csv"
        assert produced.is_file()
        assert hashlib.sha256(produced.read_bytes()).hexdigest() == info["sha256"], upstream_type


def test_sampling_order_reproduced(sample_job, golden_meta):
    """PYTHONHASHSEED=0 in the child processes reproduces upstream's set() order exactly."""
    reads_txt = Path(sample_job["job_dir"]) / "work" / "reads.txt"
    assert hashlib.sha256(reads_txt.read_bytes()).hexdigest() == golden_meta["reads_txt_sha256"]


def test_meta_matches_golden(sample_job, golden_meta):
    meta = sample_job["summary"]["meta"]
    assert meta["n_reads_sampled"] == golden_meta["n_reads_sampled"] == 76
    assert meta["n_reads_features"] == golden_meta["n_reads_features"] == 76
    assert meta["n_kmers"] == golden_meta["n_kmers"] == 3648
    assert meta["denovo_frac_modified"] == pytest.approx(
        golden_meta["denovo_frac_modified"], abs=1e-9
    )
    assert meta["regions_skipped_low_coverage"] == golden_meta["regions_skipped_low_coverage"]
    assert sample_job["summary"]["n_sites"] == golden_meta["n_sites"]
    assert sample_job["summary"]["n_read_rows"] == golden_meta["n_read_rows"]
