"""End-to-end: the full pipeline on ``app/samples/signal`` (one shared session run)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rmodhub_worker.aggregate import MOD_TYPES
from rmodhub_worker.pipeline import STAGES

pytestmark = pytest.mark.slow

CONTRACT_META_KEYS = {
    "model_name",
    "model_version",
    "kit",
    "directrm_commit",
    "remora_version",
    "torch_version",
    "n_reads_sampled",
    "n_reads_features",
    "n_kmers",
    "denovo_frac_modified",
    "regions_total",
    "regions_skipped_low_coverage",
    "regions_subsampled",
    "min_coverage",
    "max_coverage",
    "stage_seconds",
}


@pytest.fixture(scope="module")
def results(sample_job):
    path = Path(sample_job["job_dir"]) / "results.sqlite"
    assert path.is_file(), sample_job["summary"]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def test_job_reached_done(sample_job):
    summary = sample_job["summary"]
    assert summary["status"] == "done", summary
    assert summary["kit"] == "RNA004"
    assert set(summary["stage_seconds"]) == set(STAGES)
    assert all(seconds >= 0 for seconds in summary["stage_seconds"].values())


def test_sampling_and_features_counts(sample_job):
    meta = sample_job["summary"]["meta"]
    assert meta["n_reads_pod5"] == 88
    assert meta["regions_total"] == 3
    assert meta["regions_skipped_low_coverage"] == 1  # tx_C: 12 reads <= 30
    assert meta["regions_subsampled"] == 0
    assert meta["n_reads_sampled"] == 76  # tx_A 40 + tx_B 36
    assert meta["n_reads_features"] == 76
    assert meta["n_reads_resquiggled"] == 76  # progress lines parsed from the features log
    assert meta["n_kmers"] > 0
    assert 0.0 <= meta["denovo_frac_modified"] <= 1.0


def test_inputs_deleted_after_features(sample_job):
    input_dir = Path(sample_job["job_dir"]) / "input"
    remaining = sorted(p.name for p in input_dir.iterdir())
    assert not any(name.endswith((".pod5", ".bam", ".bai")) for name in remaining), remaining
    assert "reference.fa" in remaining and "regions.csv" in remaining
    updates = sample_job["db"].updates
    deleted = [u for u in updates if u.get("inputs_deleted_at") is not None]
    assert len(deleted) == 1
    # The deletion is recorded before the de novo stage starts.
    idx_deleted = updates.index(deleted[0])
    idx_denovo = next(i for i, u in enumerate(updates) if u.get("stage") == "denovo")
    assert idx_deleted < idx_denovo


def test_stage_and_status_updates(sample_job):
    db = sample_job["db"]
    stages = [u["stage"] for u in db.updates if u.get("stage")]
    # Each stage announced once, in contract order (heartbeats never carry a stage).
    seen = []
    for stage in stages:
        if not seen or seen[-1] != stage:
            seen.append(stage)
    assert seen == list(STAGES)
    statuses = [u["status"] for u in db.updates if "status" in u]
    assert statuses == ["running", "done"]
    assert db.last["status"] == "done"
    assert db.last["n_sites"] == sample_job["summary"]["n_sites"]
    assert db.last["n_reads"] == 76
    assert db.last["n_transcripts"] == 3
    assert db.last["finished_at"] is not None and db.last["stage"] is None
    heartbeats = [u for u in db.updates if "heartbeat_at" in u and "stage" not in u]
    assert len(heartbeats) >= 3  # interval 0.5 s in the test settings; the run takes > 10 s
    assert any(0.0 < (u.get("progress") or 0.0) < 1.0 for u in heartbeats)


def test_results_schema(results):
    tables = {
        row[0] for row in results.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"meta", "transcripts", "sites", "reads"} <= tables
    indexes = {
        row[0] for row in results.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"sites_tx_pos", "sites_mod", "sites_cov", "reads_site"} <= indexes
    sites_cols = [row[1] for row in results.execute("PRAGMA table_info(sites)")]
    assert sites_cols == [
        "id",
        "transcript_id",
        "position",
        "strand",
        "mod_type",
        "rate",
        "ci_low",
        "ci_high",
        "coverage",
        "count",
        "max_prob",
        "noisyor_prob",
    ]
    reads_cols = [row[1] for row in results.execute("PRAGMA table_info(reads)")]
    assert reads_cols == [
        "id",
        "read_id",
        "transcript_id",
        "position",
        "strand",
        "mod_type",
        "probability",
    ]
    # No leftover staging tables / attached databases in the published file.
    assert "sites_stage" not in tables and "reads_stage" not in tables


def test_sites_values(results, sample_job):
    rows = results.execute(
        "SELECT transcript_id, position, strand, mod_type, rate, ci_low, ci_high, coverage, count, "
        "max_prob, noisyor_prob FROM sites ORDER BY id"
    ).fetchall()
    assert len(rows) == sample_job["summary"]["n_sites"] > 0
    assert {r[3] for r in rows} <= set(MOD_TYPES)
    for tx, pos, strand, mod, rate, low, high, coverage, count, max_prob, noisyor in rows:
        assert tx in ("tx_A", "tx_B")  # tx_C was skipped (12 reads)
        assert strand == "+"
        assert isinstance(pos, int) and pos >= 1
        assert coverage >= count >= 0 and coverage >= 1
        assert rate == pytest.approx(count / coverage)
        assert 0.0 <= low <= rate <= high <= 1.0
        assert 0.0 <= max_prob <= 1.0 and 0.0 <= noisyor <= 1.0
    keys = [(r[0], r[1], r[3]) for r in rows]
    assert keys == sorted(
        keys
    )  # ORDER BY id is the canonical (transcript, position, mod_type) order
    assert len(set(keys)) == len(keys)


def test_reads_values(results, sample_job):
    n_rows = results.execute("SELECT COUNT(*) FROM reads").fetchone()[0]
    assert n_rows == sample_job["summary"]["n_read_rows"] > 0
    assert {r[0] for r in results.execute("SELECT DISTINCT mod_type FROM reads")} <= set(MOD_TYPES)
    lo, hi = results.execute("SELECT MIN(probability), MAX(probability) FROM reads").fetchone()
    assert 0.0 < lo <= hi <= 1.0
    keys = results.execute(
        "SELECT transcript_id, position, mod_type FROM reads ORDER BY id"
    ).fetchall()
    assert keys == sorted(keys)
    # Every site's coverage equals the number of read-level rows at that site (read2site semantics).
    mismatch = results.execute(
        "SELECT COUNT(*) FROM sites s WHERE s.coverage != "
        "(SELECT COUNT(*) FROM reads r WHERE r.transcript_id = s.transcript_id AND r.position = s.position "
        "AND r.mod_type = s.mod_type)"
    ).fetchone()[0]
    assert mismatch == 0
    n_reads_distinct = results.execute("SELECT COUNT(DISTINCT read_id) FROM reads").fetchone()[0]
    assert n_reads_distinct <= 76


def test_transcripts_table(results):
    rows = results.execute(
        "SELECT transcript_id, length, n_reads, n_sites FROM transcripts ORDER BY transcript_id"
    ).fetchall()
    assert [r[:3] for r in rows] == [("tx_A", 560, 40), ("tx_B", 516, 36), ("tx_C", 579, 12)]
    assert rows[0][3] > 0 and rows[1][3] > 0 and rows[2][3] == 0
    total = results.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    assert sum(r[3] for r in rows) == total


def test_meta_table(results):
    meta = {k: json.loads(v) for k, v in results.execute("SELECT key, value FROM meta")}
    assert CONTRACT_META_KEYS <= set(meta)
    assert meta["model_name"] == "DirectRM"
    assert meta["model_version"] == "bc7a085"
    assert meta["directrm_commit"].startswith("bc7a085")
    assert meta["kit"] == "RNA004"
    assert meta["remora_version"] == "3.2.0"
    assert meta["torch_version"].startswith("2.8.0")
    assert meta["min_coverage"] == 30 and meta["max_coverage"] == 150
    assert set(meta["stage_seconds"]) == set(STAGES)
    assert meta["n_reads_sampled"] == 76 and meta["regions_skipped_low_coverage"] == 1
    # Per-region detail lives in the ``regions`` table, not in every results response.
    assert "region_read_counts" not in meta


def test_regions_table(results):
    rows = results.execute(
        "SELECT transcript_id, start, end, strand, n_reads FROM regions ORDER BY id"
    ).fetchall()
    assert rows == [
        ("tx_A", 60, 300, "+", 40),
        ("tx_B", 80, 320, "+", 36),
        ("tx_C", 50, 250, "+", 12),
    ]


def test_logs_written(sample_job):
    logs = Path(sample_job["job_dir"]) / "work" / "logs"
    names = sorted(p.name for p in logs.iterdir())
    assert names == [
        "aggregating.log",
        "denovo.log",
        "features.log",
        "inference.log",
        "sampling.log",
    ]
    features = (logs / "features.log").read_text()
    assert features.count("signal refinement by remora") == 76
    assert "0 failed" in features
