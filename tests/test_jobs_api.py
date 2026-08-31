"""HTTP layer of the nanopore signal branch (`/api/jobs`, `/api/uploads`, capabilities, sample).

Every test builds its own app on the torch-free stub predictor with a SQLite metadata store
(`sqlite+pysqlite:///<tmp>/jobs.db`), a temporary upload directory, a temporary sample
directory with tiny fake files, and no broker (the `NullQueue` records what would have been
sent to Celery). Nothing here needs Postgres, Redis, the worker or the real model.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.csvio import SIGNAL_READ_COLUMNS, SIGNAL_SITE_COLUMNS
from app.jobs.constants import INPUT_FILENAMES, QUEUE_NAME, SAMPLE_FILENAMES, TASK_NAME
from app.schemas import ModSite

CSV_HEADER = "transcript_id,position,mod_type,probability,p_value,coverage,source"
SIGNAL_CSV_HEADER = CSV_HEADER + ",strand,count,ci_low,ci_high,max_prob,noisyor_prob"
READ_CSV_HEADER = "read_id,transcript_id,position,strand,mod_type,probability,source"
DISABLED_DETAIL = "The nanopore signal branch is not enabled on this server."
BAM_DETAIL = (
    "A BAM file basecalled with dorado --emit-moves is required; a pod5 alone is not "
    "enough - see Help."
)
TUS = {"Content-Type": "application/offset+octet-stream", "Tus-Resumable": "1.0.0"}

# Tiny stand-ins with the right magic bytes (the API only sniffs headers; the worker parses).
POD5 = b"\x8bPOD\r\n\x1a\n" + bytes(range(256)) * 8  # 2056 bytes
BAM = b"\x1f\x8b\x08\x04" + bytes(range(256)) * 2  # 516 bytes
BAI = b"BAI\x01" + b"\x00" * 28
REF = b">tx_A synthetic\nACGUACGUACGU\n>tx_B\nGGCCAAUU\n>tx_C\nAAAACCCC\n"
REGIONS = (
    b"seqnames,start,end,width,strand\ntx_A,60,300,241,+\ntx_B,80,320,241,+\ntx_C,50,200,151,+\n"
)
FILES = {"pod5": POD5, "bam": BAM, "reference": REF, "regions": REGIONS}
SAMPLE_BYTES = {"pod5": POD5, "bam": BAM, "bai": BAI, "reference": REF, "regions": REGIONS}

RESULTS_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE transcripts (transcript_id TEXT PRIMARY KEY, length INTEGER, n_reads INTEGER, n_sites INTEGER);
CREATE TABLE sites (
  id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL, position INTEGER NOT NULL, strand TEXT NOT NULL,
  mod_type TEXT NOT NULL, rate REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
  coverage INTEGER NOT NULL, count INTEGER NOT NULL, max_prob REAL, noisyor_prob REAL);
CREATE INDEX sites_tx_pos ON sites (transcript_id, position);
CREATE INDEX sites_mod ON sites (mod_type);
CREATE INDEX sites_cov ON sites (coverage);
CREATE TABLE reads (
  id INTEGER PRIMARY KEY, read_id TEXT NOT NULL, transcript_id TEXT NOT NULL, position INTEGER NOT NULL,
  strand TEXT NOT NULL, mod_type TEXT NOT NULL, probability REAL NOT NULL);
CREATE INDEX reads_site ON reads (transcript_id, position, mod_type);
"""

# (transcript_id, position, strand, mod_type, rate, ci_low, ci_high, coverage, count, max_prob, noisyor)
# Sorted by (transcript_id, position, mod_type) as the worker inserts them (SQLite BINARY
# collation, so "Psi" < "m6A"); tx_C is below the 30-read coverage threshold; all six
# modification types occur. The regions file lists tx_B on both strands, so position 81
# carries one m5C call per strand (same base, different reads) and 95 a minus-strand Psi.
SITES = [
    ("tx_A", 61, "+", "Psi", 0.05, 0.01, 0.17, 40, 2, 0.60, 0.70),
    ("tx_A", 61, "+", "m6A", 0.50, 0.35, 0.65, 40, 20, 0.97, 0.999),
    ("tx_A", 70, "+", "m1A", 0.25, 0.14, 0.41, 40, 10, 0.80, 0.95),
    ("tx_A", 88, "+", "ac4C", 0.10, 0.03, 0.23, 39, 4, 0.55, None),
    ("tx_B", 81, "+", "m5C", 0.75, 0.59, 0.86, 36, 27, 0.99, 1.0),
    ("tx_B", 81, "-", "m5C", 0.20, 0.105, 0.348, 40, 8, 0.75, 0.85),
    ("tx_B", 95, "-", "Psi", 0.30, 0.181, 0.454, 40, 12, 0.88, 0.97),
    ("tx_B", 95, "+", "m6A", 0.11, 0.04, 0.26, 35, 4, 0.70, 0.80),
    ("tx_B", 95, "+", "m7G", 0.33, 0.20, 0.50, 36, 12, 0.90, 0.98),
    ("tx_C", 55, "+", "m6A", 0.40, 0.15, 0.72, 10, 4, 0.85, 0.90),
    ("tx_C", 60, "+", "Psi", 0.20, 0.05, 0.53, 10, 2, 0.65, 0.70),
]
MINUS_SITES = [s for s in SITES if s[2] == "-"]
READS = [  # (transcript_id, position, mod_type, read_id) order, as the worker inserts them
    ("r1", "tx_A", 61, "+", "Psi", 0.60),
    ("r1", "tx_A", 61, "+", "m6A", 0.95),
    ("r2", "tx_A", 61, "+", "m6A", 0.10),
    ("r3", "tx_A", 61, "+", "m6A", 0.80),
    ("r4", "tx_B", 81, "+", "m5C", 0.99),
    ("r5", "tx_B", 81, "-", "m5C", 0.30),
    ("r6", "tx_B", 81, "-", "m5C", 0.85),
]
META = {
    "model_name": "DirectRM",
    "model_version": "bc7a085",
    "kit": "RNA004",
    "directrm_commit": "bc7a08573dfe7629e808256fa6ade6e4111ed1f9",
    "n_reads_features": 76,
    "regions_total": 4,
    "regions_skipped_low_coverage": ["tx_C"],
    "stage_seconds": {"sampling": 1.2, "features": 8.5},
}


# --------------------------------------------------------------------------------- fixtures


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sample"
    d.mkdir()
    for slot, name in SAMPLE_FILENAMES.items():
        (d / name).write_bytes(SAMPLE_BYTES[slot])
    return d


@pytest.fixture
def make_client(tmp_path: Path, sample_dir: Path):
    """Factory for signal-enabled apps (stub predictor, sqlite, tmp upload dir, null queue)."""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    clients: list[TestClient] = []

    def factory(**overrides):
        kwargs: dict = {
            "predictor": "stub",
            "warmup": False,
            "database_url": f"sqlite+pysqlite:///{tmp_path}/jobs.db",
            "upload_dir": tmp_path / "uploads",
            "sample_dir": sample_dir,
            "max_queued_per_ip": 10,
        }
        kwargs.update(overrides)
        client = TestClient(create_app(Settings(**kwargs)))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def _ctx(client):
    return client.app.state.signal


def _multipart_files(**overrides):
    data = {**FILES, **overrides}
    return {
        slot: (f"upload.{slot}", data[slot], "application/octet-stream")
        for slot in data
        if data[slot] is not None
    }


def _post_multipart(client, files=None, data=None):
    return client.post("/api/jobs/signal", files=files or _multipart_files(), data=data)


def _init(client, sizes: dict | None = None, kit: str = "RNA004", files: dict | None = None):
    if files is None:
        sizes = {**{slot: len(FILES[slot]) for slot in FILES}, **(sizes or {})}
        files = {slot: {"name": f"my.{slot}", "size": size} for slot, size in sizes.items()}
    return client.post("/api/jobs/signal/init", json={"kit": kit, "files": files})


def _patch(client, url: str, body: bytes, offset: int, **headers):
    return client.patch(url, content=body, headers={**TUS, "Upload-Offset": str(offset), **headers})


def _upload_all(client, status: dict, chunks: int = 2) -> None:
    for slot, up in status["uploads"].items():
        body = FILES[slot]
        step = max(1, len(body) // chunks)
        offset = 0
        while offset < len(body):
            r = _patch(client, up["url"], body[offset : offset + step], offset)
            assert r.status_code == 204, r.text
            offset = int(r.headers["Upload-Offset"])
        assert offset == len(body)


def _write_results_sqlite(path: Path, sites=SITES, reads=READS, meta=META) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(RESULTS_SCHEMA)
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()]
    )
    conn.executemany(
        "INSERT INTO transcripts VALUES (?, ?, ?, ?)",
        [("tx_A", 400, 40, 4), ("tx_B", 400, 36, 5), ("tx_C", 400, 10, 2)],
    )
    conn.executemany(
        "INSERT INTO sites (transcript_id, position, strand, mod_type, rate, ci_low, ci_high, "
        "coverage, count, max_prob, noisyor_prob) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        sites,
    )
    conn.executemany(
        "INSERT INTO reads (read_id, transcript_id, position, strand, mod_type, probability) "
        "VALUES (?,?,?,?,?,?)",
        reads,
    )
    conn.commit()
    conn.close()


def _set_job(client, job_id: str, **columns) -> None:
    from app.jobs.models import Job

    with _ctx(client).sessions() as session:
        job = session.get(Job, job_id)
        for key, value in columns.items():
            setattr(job, key, value)
        session.commit()


def _done_job(client) -> str:
    """A queued job promoted to `done` with a hand-built results.sqlite."""
    r = _post_multipart(client)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    now = datetime.now(UTC)
    _set_job(
        client,
        job_id,
        status="done",
        stage=None,
        started_at=now,
        finished_at=now,
        expires_at=now + timedelta(days=14),
        n_sites=len(SITES),
        n_reads=76,
        n_transcripts=3,
    )
    _write_results_sqlite(_ctx(client).storage.results_path(job_id))
    return job_id


# ------------------------------------------------------------------- capabilities / disabled


def test_capabilities_when_enabled(client):
    r = client.get("/api/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sequence"] is True and body["signal"] is True
    limits = body["limits"]
    assert {
        "max_pod5_gb",
        "max_reference_mb",
        "max_regions",
        "max_running_per_ip",
        "max_queued_per_ip",
        "job_timeout_h",
        "tus_chunk_mb",
        # section 11 additions: the BAM cap and the unfinished-upload TTL the UI displays
        "max_bam_gb",
        "upload_ttl_h",
    } <= set(limits)
    assert limits["max_pod5_gb"] == 5 and limits["max_regions"] == 10000
    assert limits["job_timeout_h"] == 6 and limits["tus_chunk_mb"] == 64
    assert limits["max_bam_gb"] == 5 and limits["upload_ttl_h"] == 48
    assert body["retention"] == {
        "inputs_deleted": "after feature extraction, at most 48 h",
        "results_days": 14,
    }
    assert client.get("/health").json()["signal_enabled"] is True


def test_capabilities_report_the_configured_limits(make_client):
    client = make_client(upload_ttl_h=12, max_bam_gb=2.5)
    limits = client.get("/api/capabilities").json()["limits"]
    assert limits["upload_ttl_h"] == 12 and limits["max_bam_gb"] == 2.5


def test_signal_routes_return_503_when_disabled(stub_client):
    """The session-wide stub app has no DATABASE_URL: the branch is off but documented."""
    assert stub_client.get("/api/capabilities").json()["signal"] is False
    assert stub_client.get("/health").json()["signal_enabled"] is False
    some_id = str(uuid.uuid4())
    for method, url in [
        ("GET", f"/api/jobs/{some_id}"),
        ("POST", "/api/jobs/signal/init"),
        ("POST", "/api/jobs/signal/sample"),
        ("POST", f"/api/jobs/{some_id}/start"),
        ("POST", f"/api/jobs/{some_id}/cancel"),
        ("GET", f"/api/jobs/{some_id}/results"),
        ("GET", f"/api/jobs/{some_id}/download.csv"),
        ("OPTIONS", "/api/uploads"),
        ("HEAD", f"/api/uploads/{some_id}"),
        ("PATCH", f"/api/uploads/{some_id}"),
        ("DELETE", f"/api/uploads/{some_id}"),
    ]:
        r = stub_client.request(method, url)
        assert r.status_code == 503, (method, url, r.status_code, r.text)
        if method != "HEAD":
            assert r.json() == {"detail": DISABLED_DETAIL}, (method, url)
    r = stub_client.post("/api/jobs/signal", files=_multipart_files())
    assert r.status_code == 503 and r.json()["detail"] == DISABLED_DETAIL
    paths = stub_client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/jobs/signal",
        "/api/jobs/signal/init",
        "/api/jobs/signal/sample",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/results",
        "/api/jobs/{job_id}/download.csv",
        "/api/jobs/{job_id}/cancel",
        "/api/uploads/{upload_id}",
        "/api/capabilities",
        "/api/samples/signal",
    ):
        assert path in paths, path
    assert {"head", "patch", "delete"} <= set(paths["/api/uploads/{upload_id}"])


# ------------------------------------------------------------------------ multipart create


def test_multipart_create_streams_files_and_enqueues(client):
    r = _post_multipart(client, data={"kit": "RNA002"})
    assert r.status_code == 202, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    job_id = body["job_id"]
    assert uuid.UUID(job_id).version == 4
    assert body["status"] == "queued" and body["stage"] is None
    assert body["kit"] == "RNA002" and body["input_kind"] == "upload"
    assert body["input_bytes"] == {slot: len(data) for slot, data in FILES.items()}
    assert body["uploads"] is None and body["cancel_requested"] is False
    assert body["model"] == {"name": "DirectRM", "version": "bc7a085"}

    input_dir = _ctx(client).storage.input_dir(job_id)
    for slot, data in FILES.items():
        assert (input_dir / INPUT_FILENAMES[slot]).read_bytes() == data
    assert not list(input_dir.glob("*.part"))

    queue = _ctx(client).queue
    assert queue.sent == [
        {"task": TASK_NAME, "kwargs": {"job_id": job_id}, "task_id": job_id, "queue": QUEUE_NAME}
    ]
    assert TASK_NAME == "rmodhub.signal.run_job" and QUEUE_NAME == "signal"

    status = client.get(f"/api/jobs/{job_id}")
    assert status.status_code == 200 and status.json() == body
    assert status.headers["cache-control"] == "no-store"


def test_multipart_defaults_to_rna004(client):
    r = _post_multipart(client)
    assert r.status_code == 202, r.text
    assert r.json()["kit"] == "RNA004"


def test_multipart_missing_bam(client):
    r = _post_multipart(client, files=_multipart_files(bam=None))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == BAM_DETAIL
    # nothing is left behind
    assert _ctx(client).queue.sent == []
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"pod5": b"not a pod5 at all" * 10}, "does not look like a POD5"),
        ({"bam": b"@HD\tVN:1.6\n" * 10}, "does not look like a BAM"),
        ({"reference": b"ACGT\nACGT\n"}, "does not look like FASTA"),
        ({"regions": b"chrom,from,to\ntx_A,1,2\n"}, "seqnames,start,end,width,strand"),
        ({"regions": b"seqnames,start,end,width,strand\ntx_A,x,2,2,+\n"}, "non-integer"),
        ({"regions": b"seqnames,start,end,width,strand\ntx_A,5,2,2,+\n"}, "invalid interval"),
        ({"regions": b"seqnames,start,end,width,strand\n"}, "no data rows"),
    ],
)
def test_multipart_rejects_malformed_inputs(client, overrides, fragment):
    r = _post_multipart(client, files=_multipart_files(**overrides))
    assert r.status_code == 422, r.text
    assert fragment in r.json()["detail"]
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


def test_multipart_bad_kit(client):
    r = _post_multipart(client, data={"kit": "RNA999"})
    assert r.status_code == 422
    assert "kit must be one of RNA004, RNA002" in r.json()["detail"]


def test_multipart_not_multipart(client):
    r = client.post("/api/jobs/signal", json={"kit": "RNA004"})
    assert r.status_code == 422
    assert "multipart/form-data" in r.json()["detail"]


def test_multipart_pod5_over_cap_is_rejected_mid_stream(make_client):
    # 1e-6 GiB = 1073 bytes: the 2056-byte pod5 trips the cap while streaming.
    client = make_client(max_pod5_gb=1e-6)
    r = _post_multipart(client)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "The pod5 file exceeds 1 KB; this server accepts at most 1 KB."
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


def test_multipart_declared_length_over_total_cap(client):
    huge = str(20 * 1024**3)
    r = client.post(
        "/api/jobs/signal",
        content=b"",
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": huge},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("The request body is 20 GB; this server accepts at most")
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


# Every spelling pandas' default `read_csv` turns into a number, a boolean or NaN (the
# worker's `find_unsafe_contig_names` verdict, mirrored here without pandas).
UNSAFE_CONTIG_NAMES = [
    ("1", "a number"),
    ("-3", "a number"),
    ("+7", "a number"),
    ("007", "a number"),
    ("1e5", "a number"),
    ("1E-3", "a number"),
    (".5", "a number"),
    ("5.", "a number"),
    ("-.5e2", "a number"),
    ("inf", "a number"),
    ("-inf", "a number"),
    ("Infinity", "a number"),
    ("True", "a boolean"),
    ("true", "a boolean"),
    ("FALSE", "a boolean"),
    ("NA", "a missing value"),
    ("nan", "a missing value"),
    ("NaN", "a missing value"),
    ("-NaN", "a missing value"),
    ("null", "a missing value"),
    ("NULL", "a missing value"),
    ("None", "a missing value"),
    ("<NA>", "a missing value"),
    ("#NA", "a missing value"),
    ("#N/A", "a missing value"),
    ("N/A", "a missing value"),
    ("n/a", "a missing value"),
    ("#N/A N/A", "a missing value"),
    ("1.#IND", "a missing value"),
    ("-1.#QNAN", "a missing value"),
]
SAFE_CONTIG_NAMES = [
    "chr1", "tx_A", "ENST0001", "NA12878", "chrNA", "chrM", "MT-CO1", "1abc", "nan_gene",
    "0x10", "1_000", "e5", "TrueNorth", "Infinite", "1.2.3", "None1", "NC_000001.11",
]


@pytest.mark.parametrize(("name", "kind"), UNSAFE_CONTIG_NAMES)
def test_regions_contig_names_pandas_would_misread_are_rejected(tmp_path, name, kind):
    from app.jobs.service import validate_regions_file

    path = tmp_path / "regions.csv"
    path.write_text(f"seqnames,start,end,width,strand\ntx_A,1,10,10,+\n{name},1,10,10,+\n")
    with pytest.raises(HTTPException) as exc:
        validate_regions_file(path, max_rows=10000)
    assert exc.value.status_code == 422
    if "/" in name:  # the worker's per-row '/' check comes first; same precedence here
        assert exc.value.detail == f"Region 2: the contig name '{name}' may not contain '/'."
        return
    assert exc.value.detail == (
        f"Region 2: the contig name '{name}' is read as {kind} rather than a name by "
        "DirectRM's CSV reader; rename the contig in the reference FASTA, the BAM and the "
        "regions file (for example by prefixing it with 'chr')."
    )


def test_regions_contig_names_pandas_keeps_as_strings_are_accepted(tmp_path):
    from app.jobs.service import validate_regions_file

    path = tmp_path / "regions.csv"
    rows = "".join(f"{name},1,10,10,+\n" for name in SAFE_CONTIG_NAMES)
    path.write_text("seqnames,start,end,width,strand\n" + rows)
    assert validate_regions_file(path, max_rows=10000) == len(SAFE_CONTIG_NAMES)


def test_multipart_rejects_unsafe_empty_and_slashed_contig_names(client):
    header = b"seqnames,start,end,width,strand\n"
    # surrounding whitespace is stripped first, as the worker does before upstream reads it
    r = _post_multipart(client, files=_multipart_files(regions=header + b"tx_A,60,300,241,+\n 1 ,1,10,10,+\n"))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (
        "Region 2: the contig name '1' is read as a number rather than a name by DirectRM's "
        "CSV reader; rename the contig in the reference FASTA, the BAM and the regions file "
        "(for example by prefixing it with 'chr')."
    )
    r = _post_multipart(client, files=_multipart_files(regions=header + b",1,10,10,+\n"))
    assert r.status_code == 422 and r.json()["detail"] == "Region 1 has an empty seqnames value."
    r = _post_multipart(client, files=_multipart_files(regions=header + b"a/b,1,10,10,+\n"))
    assert r.status_code == 422
    assert r.json()["detail"] == "Region 1: the contig name 'a/b' may not contain '/'."
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


def test_regions_over_max_rows(make_client):
    client = make_client(max_regions=2)
    r = _post_multipart(client)  # REGIONS has 3 data rows
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "The regions file has 3 data rows; this server accepts at most 2."


# ----------------------------------------------------------------------------------- quotas


def test_quota_max_queued_per_ip(make_client):
    client = make_client(max_queued_per_ip=1)
    first = _init(client)
    assert first.status_code == 201, first.text
    second = _init(client)
    assert second.status_code == 429, second.text
    assert second.headers["Retry-After"].isdigit()
    assert second.json()["detail"] == (
        "You already have 1 job(s) uploading or queued; this server allows at most 1 waiting "
        "jobs per address. Cancel one or wait for it to start."
    )
    assert _post_multipart(client).status_code == 429
    assert client.post("/api/jobs/signal/sample").status_code == 429
    # cancelling frees the slot
    assert client.post(f"/api/jobs/{first.json()['job_id']}/cancel").status_code == 200
    assert _init(client).status_code == 201


def test_quota_max_running_per_ip(client):
    r = _post_multipart(client)
    assert r.status_code == 202
    _set_job(client, r.json()["job_id"], status="running", started_at=datetime.now(UTC))
    r = _init(client)
    assert r.status_code == 429, r.text
    assert r.json()["detail"].startswith(
        "You already have 1 job running; this server allows at most 1"
    )
    assert r.headers["Retry-After"] == "300"


# ------------------------------------------------------------------------ init + tus + start


def test_init_declared_pod5_over_cap(client):
    r = _init(client, sizes={"pod5": int(6.2 * 1024**3)})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "The pod5 file is 6.2 GB; this server accepts at most 5 GB."
    r = _init(client, sizes={"reference": 600 * 1024**2})
    assert (
        r.json()["detail"] == "The reference FASTA is 600 MB; this server accepts at most 500 MB."
    )
    r = _init(client, sizes={"bam": 0})
    assert r.status_code == 422 and r.json()["detail"] == "The BAM file is empty."


def test_init_missing_bam(client):
    files = {slot: {"name": "f", "size": 10} for slot in ("pod5", "reference", "regions")}
    r = _init(client, files=files)
    assert r.status_code == 422 and r.json()["detail"] == BAM_DETAIL


def test_init_tus_start_flow(client):
    r = _init(client)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    status = r.json()
    job_id = status["job_id"]
    assert status["status"] == "uploading" and status["stage"] == "uploading"
    assert set(status["uploads"]) == {"pod5", "bam", "reference", "regions"}
    for slot, up in status["uploads"].items():
        assert up["url"].startswith("/api/uploads/")
        assert uuid.UUID(up["url"].rsplit("/", 1)[1]).version == 4
        assert up == {"url": up["url"], "length": len(FILES[slot]), "offset": 0, "complete": False}

    # start before any byte: 409 listing every slot
    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 409
    assert r.json()["detail"] == "Uploads incomplete for: pod5, bam, reference, regions."

    url = status["uploads"]["pod5"]["url"]
    head = client.head(url)
    assert head.status_code == 200
    assert head.headers["Upload-Offset"] == "0"
    assert head.headers["Upload-Length"] == str(len(POD5))
    assert head.headers["Tus-Resumable"] == "1.0.0"
    assert head.headers["Cache-Control"] == "no-store"

    half = len(POD5) // 2
    r = _patch(client, url, POD5[:half], 0)
    assert r.status_code == 204 and r.headers["Upload-Offset"] == str(half)
    assert r.headers["Tus-Resumable"] == "1.0.0"

    # wrong offset -> 409 and the server offset in the response
    r = _patch(client, url, POD5[half:], 0)
    assert r.status_code == 409, r.text
    assert r.headers["Upload-Offset"] == str(half)
    assert "offset mismatch" in r.json()["detail"]
    assert client.head(url).headers["Upload-Offset"] == str(half)

    # second half completes the upload
    r = _patch(client, url, POD5[half:], half)
    assert r.status_code == 204 and r.headers["Upload-Offset"] == str(len(POD5))
    assert client.head(url).headers["Upload-Offset"] == str(len(POD5))
    st = client.get(f"/api/jobs/{job_id}").json()
    assert st["uploads"]["pod5"]["complete"] is True and st["uploads"]["bam"]["complete"] is False

    # partial start -> the remaining slots are listed
    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 409
    assert r.json()["detail"] == "Uploads incomplete for: bam, reference, regions."

    for slot in ("bam", "reference", "regions"):
        _upload_all(client, {"uploads": {slot: status["uploads"][slot]}})

    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["uploads"] is None
    assert body["input_bytes"] == {slot: len(data) for slot, data in FILES.items()}
    storage = _ctx(client).storage
    for slot, data in FILES.items():
        assert storage.input_path(job_id, slot).read_bytes() == data
    assert list(storage.tus_dir.iterdir()) == []  # moved, not copied
    assert _ctx(client).queue.sent[-1]["task_id"] == job_id
    # a second start is a conflict
    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 409 and r.json()["detail"] == "The job is already queued."
    # the upload still answers HEAD with the final offset, PATCH is refused
    assert client.head(url).headers["Upload-Offset"] == str(len(POD5))
    assert _patch(client, url, b"x", len(POD5)).status_code == 409


def test_tus_chunk_too_big(make_client):
    client = make_client(tus_chunk_mb=1)
    r = _init(client, sizes={"pod5": 3 * 1024**2})
    assert r.status_code == 201, r.text
    url = r.json()["uploads"]["pod5"]["url"]
    r = _patch(client, url, b"\x8bPOD\r\n\x1a\n" + b"x" * (1024**2), 0)
    assert r.status_code == 413, r.text
    assert "at most 1 MB" in r.json()["detail"]
    assert client.head(url).headers["Upload-Offset"] == "0"
    # exactly the cap is fine
    r = _patch(client, url, b"y" * 1024**2, 0)
    assert r.status_code == 204 and r.headers["Upload-Offset"] == str(1024**2)


def test_tus_patch_beyond_declared_length_and_bad_headers(client):
    r = _init(client)
    url = r.json()["uploads"]["regions"]["url"]
    r = _patch(client, url, REGIONS + b"extra", 0)
    assert r.status_code == 413 and "declared upload length" in r.json()["detail"]
    assert client.head(url).headers["Upload-Offset"] == "0"
    r = client.patch(
        url, content=b"x", headers={"Content-Type": "text/plain", "Upload-Offset": "0"}
    )
    assert r.status_code == 415
    r = client.patch(url, content=b"x", headers={"Content-Type": TUS["Content-Type"]})
    assert r.status_code == 422 and "Upload-Offset" in r.json()["detail"]
    r = client.head(url, headers={"Tus-Resumable": "0.2.2"})
    assert r.status_code == 412


def test_tus_options_and_unknown_upload(client):
    r = client.options("/api/uploads")
    assert r.status_code == 204
    assert r.headers["Tus-Version"] == "1.0.0"
    assert r.headers["Tus-Extension"] == "termination"
    assert r.headers["Tus-Max-Size"] == str(5 * 1024**3)
    assert client.head(f"/api/uploads/{uuid.uuid4()}").status_code == 404
    assert client.head("/api/uploads/not-a-uuid").status_code == 404
    assert _patch(client, f"/api/uploads/{uuid.uuid4()}", b"x", 0).status_code == 404


def test_tus_delete_terminates_upload_and_cancels_job(client):
    status = _init(client).json()
    job_id = status["job_id"]
    url = status["uploads"]["pod5"]["url"]
    assert _patch(client, url, POD5[:100], 0).status_code == 204
    r = client.delete(url)
    assert r.status_code == 204
    assert client.head(url).status_code == 404
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
    storage = _ctx(client).storage
    assert list(storage.tus_dir.iterdir()) == []
    assert not storage.job_dir(job_id).exists()


# ------------------------------------------------------------------------------- status


def test_status_unknown_and_invalid_ids(client):
    r = client.get(f"/api/jobs/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "No job with this id exists on this server."
    for bad in (
        "nope",
        "12345678-1234-1234-1234-1234567890123",
        "1234567812341234123412345678901z",
    ):
        r = client.get(f"/api/jobs/{bad}")
        assert r.status_code == 404, bad
        assert r.json()["detail"] == "No job with this id exists on this server."
    assert client.get("/api/jobs/../../etc/passwd").status_code == 404
    assert client.post(f"/api/jobs/{uuid.uuid4()}/start").status_code == 404
    assert client.post(f"/api/jobs/{uuid.uuid4()}/cancel").status_code == 404
    assert client.get(f"/api/jobs/{uuid.uuid4()}/results").status_code == 404
    assert client.get(f"/api/jobs/{uuid.uuid4()}/download.csv").status_code == 404


def test_status_reflects_worker_columns(client):
    job_id = _post_multipart(client).json()["job_id"]
    now = datetime.now(UTC)
    _set_job(
        client,
        job_id,
        status="running",
        stage="features",
        progress=0.42,
        eta_s=95.0,
        started_at=now,
        heartbeat_at=now,
        worker_hostname="worker-1",
    )
    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "running" and body["stage"] == "features"
    assert body["progress"] == 0.42 and body["eta_s"] == 95.0
    assert body["started_at"] is not None and body["finished_at"] is None


# ------------------------------------------------------------------------------ results


def test_results_409_before_done(client):
    job_id = _post_multipart(client).json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}/results")
    assert r.status_code == 409
    assert r.json()["detail"] == "The job is queued; results are available once it is done."
    assert client.get(f"/api/jobs/{job_id}/download.csv").status_code == 409
    _set_job(client, job_id, status="failed", error="boom")
    assert client.get(f"/api/jobs/{job_id}/results").status_code == 409


def test_results_site_level_default_page(client):
    job_id = _done_job(client)
    r = client.get(f"/api/jobs/{job_id}/results")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert (body["total"], body["offset"], body["limit"]) == (len(SITES), 0, 100)
    rows = body["results"]
    assert len(rows) == len(SITES)
    # canonical order: one transcript at a time, then position, then mod_type (= id order,
    # the order of the CSV download); never interleaved by absolute coordinate
    assert [(x["transcript_id"], x["position"], x["mod_type"]) for x in rows] == [
        (s[0], s[1], s[3]) for s in SITES
    ]
    first = rows[1]
    assert list(first)[:7] == CSV_HEADER.split(",")
    assert first == {
        "transcript_id": "tx_A",
        "position": 61,
        "mod_type": "m6A",
        "probability": 0.5,
        "p_value": None,
        "coverage": 40,
        "source": "signal",
        "strand": "+",
        "count": 20,
        "ci_low": 0.35,
        "ci_high": 0.65,
        "max_prob": 0.97,
        "noisyor_prob": 0.999,
    }
    for row in rows:
        site = ModSite.model_validate(row)
        assert site.source == "signal" and site.p_value is None and site.coverage is not None
    meta = body["meta"]
    assert meta["source"] == "signal" and meta["job_id"] == job_id
    assert meta["model_name"] == "DirectRM" and meta["model_version"] == "bc7a085"
    assert meta["kit"] == "RNA004"
    assert meta["n_sites"] == len(SITES) and meta["n_reads"] == 76 and meta["n_transcripts"] == 3
    assert meta["mod_types"] == ["ac4C", "m1A", "m5C", "m6A", "m7G", "Psi"]
    assert meta["low_coverage_threshold"] == 30
    assert meta["transcripts"] == [
        {"transcript_id": "tx_A", "length": 400, "n_reads": 40, "n_sites": 4},
        {"transcript_id": "tx_B", "length": 400, "n_reads": 36, "n_sites": 5},
        {"transcript_id": "tx_C", "length": 400, "n_reads": 10, "n_sites": 2},
    ]
    assert meta["extra"] == META  # every results.sqlite meta key, JSON-decoded


def test_results_paging_filters_and_sorting(client):
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"

    page = client.get(base, params={"offset": 2, "limit": 3}).json()
    assert (page["total"], page["offset"], page["limit"]) == (len(SITES), 2, 3)
    assert len(page["results"]) == 3

    tx = client.get(base, params={"transcript_id": "tx_B"}).json()
    assert tx["total"] == 5 and {x["transcript_id"] for x in tx["results"]} == {"tx_B"}

    mod = client.get(base, params={"mod_type": "m6A"}).json()
    assert mod["total"] == 3 and {x["mod_type"] for x in mod["results"]} == {"m6A"}

    pos = client.get(base, params={"transcript_id": "tx_A", "position": 61}).json()
    assert [x["mod_type"] for x in pos["results"]] == ["Psi", "m6A"]

    rev = client.get(base, params={"order": "desc"}).json()  # exact reverse of the default
    assert [x["position"] for x in rev["results"]] == [s[1] for s in reversed(SITES)]

    cov = client.get(base, params={"min_coverage": 30}).json()
    assert cov["total"] == 9 and all(x["coverage"] >= 30 for x in cov["results"])
    assert all(x["transcript_id"] != "tx_C" for x in cov["results"])

    rate = client.get(base, params={"sort": "rate", "order": "desc", "limit": 3}).json()
    assert [x["probability"] for x in rate["results"]] == [0.75, 0.5, 0.4]

    covs = client.get(base, params={"sort": "coverage", "order": "asc", "limit": 3}).json()
    assert [x["coverage"] for x in covs["results"]] == [10, 10, 35]

    mods = client.get(base, params={"sort": "mod_type", "order": "asc"}).json()
    assert [x["mod_type"] for x in mods["results"]] == sorted(
        x["mod_type"] for x in mods["results"]
    )

    assert client.get(base, params={"limit": 1001}).status_code == 422
    assert client.get(base, params={"sort": "bogus"}).status_code == 422
    empty = client.get(base, params={"transcript_id": ""}).json()  # blank = no filter
    assert empty["total"] == len(SITES)


def test_results_strand_filter(client):
    """`strand` narrows either level to one strand. The regions file lists tx_B on both
    strands, so position 81 carries an m5C call per strand and the read-level drill-down of
    one of them must not list the other strand's reads (same base, different reads)."""
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"

    minus = client.get(base, params={"strand": "-"}).json()
    assert minus["total"] == len(MINUS_SITES) == 2
    assert [(x["position"], x["mod_type"], x["strand"]) for x in minus["results"]] == [
        (81, "m5C", "-"),
        (95, "Psi", "-"),
    ]
    plus = client.get(base, params={"strand": "+"}).json()  # httpx sends '+' as %2B
    assert plus["total"] == len(SITES) - 2 and {x["strand"] for x in plus["results"]} == {"+"}
    both = client.get(base, params={"transcript_id": "tx_B", "position": 81}).json()
    assert [x["strand"] for x in both["results"]] == ["+", "-"]
    one = client.get(base, params={"transcript_id": "tx_B", "position": 81, "strand": "-"}).json()
    assert one["total"] == 1 and one["results"][0]["count"] == 8
    assert client.get(base, params={"min_coverage": 40, "strand": "-"}).json()["total"] == 2

    site = {"level": "read", "transcript_id": "tx_B", "position": 81, "mod_type": "m5C"}
    assert client.get(base, params=site).json()["total"] == 3  # both strands without it
    r = client.get(base, params={**site, "strand": "+"}).json()
    assert r["total"] == 1 and [x["read_id"] for x in r["results"]] == ["r4"]
    r = client.get(base, params={**site, "strand": "-"}).json()
    assert [(x["read_id"], x["strand"]) for x in r["results"]] == [("r5", "-"), ("r6", "-")]
    # the literal query string a browser builds (URLSearchParams encodes '+' as %2B)
    r = client.get(f"{base}?level=read&transcript_id=tx_B&position=81&mod_type=m5C&strand=%2B")
    assert r.status_code == 200 and r.json()["total"] == 1
    # combines with the within-site sort
    r = client.get(base, params={**site, "strand": "-", "sort": "rate", "order": "desc"}).json()
    assert [x["probability"] for x in r["results"]] == [0.85, 0.3]
    # blank = no filter, like the other text filters
    assert client.get(base, params={"strand": ""}).json()["total"] == len(SITES)


def test_results_strand_rejects_anything_else(client):
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"
    detail = "strand must be '+' or '-' (a literal '+' has to be sent as %2B in a query string)."
    for level in ("site", "read"):
        # ' ' is what an unencoded `?strand=+` decodes to: it must not silently mean "all"
        for value in ("x", "plus", "+-", " ", "++"):
            r = client.get(base, params={"level": level, "strand": value})
            assert r.status_code == 422, (level, value, r.text)
            assert r.json()["detail"] == detail
    r = client.get(f"{base}?strand=+")
    assert r.status_code == 422 and r.json()["detail"] == detail
    # the OpenAPI document offers exactly the two values
    spec = client.get("/openapi.json").json()
    params = spec["paths"]["/api/jobs/{job_id}/results"]["get"]["parameters"]
    strand = next(p for p in params if p["name"] == "strand")
    assert strand["schema"]["enum"] == ["+", "-"]


def test_results_read_level_drill_down(client):
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"
    r = client.get(base, params={"level": "read", "transcript_id": "tx_A", "position": 61})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert body["results"][0] == {
        "read_id": "r1",
        "transcript_id": "tx_A",
        "position": 61,
        "strand": "+",
        "mod_type": "Psi",
        "probability": 0.6,
        "source": "signal",
    }
    r = client.get(
        base,
        params={
            "level": "read",
            "transcript_id": "tx_A",
            "position": 61,
            "mod_type": "m6A",
            "sort": "rate",
            "order": "desc",
        },
    ).json()
    assert [x["probability"] for x in r["results"]] == [0.95, 0.8, 0.1]
    assert r["meta"]["source"] == "signal"
    everything = client.get(base, params={"level": "read"}).json()
    assert everything["total"] == len(READS)


# ---------------------------------------------------------------------------------- CSV


def test_download_csv_site_level_streams(client):
    job_id = _done_job(client)
    with client.stream("GET", f"/api/jobs/{job_id}/download.csv") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == (
            f'attachment; filename="rmodhub_signal_{job_id}_sites.csv"'
        )
        assert "content-length" not in r.headers  # streamed, not buffered
        assert r.headers["cache-control"] == "no-store"
        text = "".join(r.iter_text())
    lines = text.splitlines()
    assert lines[0] == SIGNAL_CSV_HEADER
    assert lines[0].startswith(CSV_HEADER)
    assert lines[0] == ",".join(SIGNAL_SITE_COLUMNS)
    assert len(lines) == len(SITES) + 1
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["transcript_id"] == "tx_A" and rows[0]["position"] == "61"
    assert rows[0]["p_value"] == "" and rows[0]["source"] == "signal"
    assert rows[1]["probability"] == "0.5" and rows[1]["count"] == "20"
    assert rows[0]["mod_type"] == "Psi" and rows[1]["mod_type"] == "m6A"
    assert rows[3]["noisyor_prob"] == ""  # NULL -> empty cell
    # the shared seven columns validate as ModSite rows
    for row in rows:
        ModSite.model_validate(
            {k: (None if row[k] == "" else row[k]) for k in CSV_HEADER.split(",")}
        )


def test_download_csv_read_level(client):
    job_id = _done_job(client)
    r = client.get(f"/api/jobs/{job_id}/download.csv", params={"level": "read"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith(f'rmodhub_signal_{job_id}_reads.csv"')
    lines = r.text.splitlines()
    assert lines[0] == READ_CSV_HEADER == ",".join(SIGNAL_READ_COLUMNS)
    assert len(lines) == len(READS) + 1
    assert lines[1] == "r1,tx_A,61,+,Psi,0.6,signal"


def test_download_csv_many_rows_streams_in_chunks(client):
    job_id = _done_job(client)
    many = [("tx_Z", i, "+", "m6A", 0.5, 0.4, 0.6, 40, 20, 0.9, 0.9) for i in range(1, 12001)]
    _write_results_sqlite(_ctx(client).storage.results_path(job_id), sites=many, reads=[])
    from app.jobs.results import csv_stream

    chunks = list(csv_stream(_ctx(client).storage.results_path(job_id), "site"))
    assert len(chunks) > 1  # emitted in batches, never one big string
    assert "".join(chunks).count("\n") == 12001
    with client.stream("GET", f"/api/jobs/{job_id}/download.csv") as r:
        assert r.status_code == 200
        assert "content-length" not in r.headers
        assert b"".join(r.iter_bytes()).count(b"\n") == 12001


# --------------------------------------------------------------------------------- cancel


def test_cancel_queued_job(client):
    r = _post_multipart(client)
    job_id = r.json()["job_id"]
    storage = _ctx(client).storage
    assert storage.job_dir(job_id).exists()
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled" and body["cancel_requested"] is True
    assert body["finished_at"] is not None
    assert _ctx(client).queue.revoked == [
        {"task_id": job_id, "terminate": False, "signal": "SIGTERM"}
    ]
    assert not storage.job_dir(job_id).exists()
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 409
    assert r.json()["detail"] == "The job is already cancelled and cannot be cancelled."


def test_cancel_running_job_revokes_with_terminate(client):
    job_id = _post_multipart(client).json()["job_id"]
    _set_job(client, job_id, status="running", stage="features", started_at=datetime.now(UTC))
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert _ctx(client).queue.revoked[-1] == {
        "task_id": job_id,
        "terminate": True,
        "signal": "SIGTERM",
    }


def test_cancel_done_job_is_conflict(client):
    job_id = _done_job(client)
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 409
    assert r.json()["detail"] == "The job is already done and cannot be cancelled."
    assert client.get(f"/api/jobs/{job_id}/results").status_code == 200  # untouched


# -------------------------------------------------------------------------------- cleanup


def test_cleanup_expires_reaps_and_logs_bytes_freed(client, caplog):
    from app.jobs.cleanup import run_cleanup
    from app.jobs.models import Job, Upload

    ctx = _ctx(client)
    storage = ctx.storage
    now = datetime.now(UTC)

    # (a) done job past expires_at, with results and scratch files on disk
    expired = _done_job(client)
    (storage.work_dir(expired) / "features.npz").write_bytes(b"z" * 4096)
    _set_job(client, expired, expires_at=now - timedelta(hours=1))
    expired_bytes = sum(
        p.stat().st_size for p in storage.job_dir(expired).rglob("*") if p.is_file()
    )

    # (b) done job still within retention: untouched
    fresh = _done_job(client)

    # (c) uploading job older than upload_ttl_h with partial tus files
    stale = _init(client).json()
    stale_url = stale["uploads"]["pod5"]["url"]
    assert _patch(client, stale_url, POD5[:1000], 0).status_code == 204
    _set_job(client, stale["job_id"], created_at=now - timedelta(hours=72))
    stale_upload_id = stale_url.rsplit("/", 1)[1]
    assert storage.tus_path(stale_upload_id).stat().st_size == 1000

    # (d) running job whose worker died 20 minutes ago, (e) one with a fresh heartbeat,
    # (f) a queued job created 3 days ago. Created first: running jobs count against the
    # per-address quota, so the status changes come after the submissions.
    dead = _post_multipart(client).json()["job_id"]
    alive = _post_multipart(client).json()["job_id"]
    old = _post_multipart(client).json()["job_id"]
    _set_job(
        client,
        dead,
        status="running",
        stage="inference",
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(minutes=20),
    )
    dead_bytes = sum(len(v) for v in FILES.values())
    _set_job(
        client, alive, status="running", started_at=now, heartbeat_at=now - timedelta(minutes=2)
    )
    _set_job(client, old, created_at=now - timedelta(hours=72))

    # (g) orphan tus file with no database row, 3 days old
    orphan_id = str(uuid.uuid4())
    orphan_path = storage.create_tus_file(orphan_id, {"job_id": "?", "slot": "pod5"})
    orphan_path.write_bytes(b"o" * 2048)
    old_ts = (now - timedelta(hours=72)).timestamp()
    os.utime(orphan_path, (old_ts, old_ts))

    with caplog.at_level(logging.INFO, logger="app.jobs.cleanup"):
        report = run_cleanup(ctx.sessions, storage, ctx.settings, now=now)

    with ctx.sessions() as session:
        j = session.get(Job, expired)
        assert j.status == "expired" and j.results_deleted_at is not None
        assert session.get(Job, fresh).status == "done"
        s = session.get(Job, stale["job_id"])
        assert s.status == "expired" and s.results_deleted_at is not None
        assert session.get(Upload, stale_upload_id) is None
        d = session.get(Job, dead)
        assert d.status == "failed"
        assert d.error == "The worker stopped responding; please resubmit."
        assert d.finished_at == now and d.results_deleted_at is not None
        assert session.get(Job, alive).status == "running"
        o = session.get(Job, old)  # never picked up: failed, not `queued` forever
        assert o.status == "failed" and o.inputs_deleted_at == now
        assert o.error == (
            "The job waited longer than 48 h for a worker and its input files were removed; "
            "please resubmit."
        )
        assert o.finished_at == now and o.results_deleted_at == now

    assert not storage.job_dir(expired).exists()
    assert storage.results_path(fresh).exists()
    assert not storage.tus_path(stale_upload_id).exists()
    assert not storage.tus_meta_path(stale_upload_id).exists()
    assert not storage.job_dir(dead).exists()
    assert storage.input_path(alive, "pod5").exists()
    assert not storage.job_dir(old).exists()
    assert client.get(f"/api/jobs/{old}").json()["status"] == "failed"
    assert not storage.tus_path(orphan_id).exists()
    assert client.head(stale_url).status_code == 404
    assert client.get(f"/api/jobs/{expired}/results").status_code == 404
    assert client.get(f"/api/jobs/{expired}").json()["status"] == "expired"

    assert report.reaped_workers == 1 and report.expired_uploads == 1 and report.expired_jobs == 2
    assert report.inputs_deleted == 0 and report.timed_out_queued == 1
    assert report.orphan_uploads == 1 and report.purged_rows == 0
    assert report.bytes_freed >= expired_bytes + 1000 + dead_bytes + dead_bytes + 2048
    line = [rec.getMessage() for rec in caplog.records if "freed" in rec.getMessage()][-1]
    assert f"({report.bytes_freed} bytes)" in line
    assert "reaped 1 dead-worker job(s)" in line

    # idempotent: a second pass frees nothing
    again = run_cleanup(ctx.sessions, storage, ctx.settings, now=now)
    assert again.bytes_freed == 0 and again.expired_jobs == 0 and again.reaped_workers == 0


def test_cleanup_cli_runs_one_pass(tmp_path, sample_dir):
    env = {
        **os.environ,
        "RMODHUB_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path}/cli.db",
        "RMODHUB_UPLOAD_DIR": str(tmp_path / "uploads"),
        "RMODHUB_PREDICTOR": "stub",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "app.jobs.cleanup"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "freed" in proc.stdout and "bytes" in proc.stdout
    env.pop("RMODHUB_DATABASE_URL")
    proc = subprocess.run(
        [sys.executable, "-m", "app.jobs.cleanup"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 2


# --------------------------------------------------------------------------------- sample


def test_sample_signal_endpoints(client, sample_dir):
    r = client.get("/api/samples/signal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] and body["kit"] == "RNA004" and body["source"] == "synthetic"
    assert "synthetic" in body["description"].lower()
    assert "about 1.4 MB in total" in body["description"]  # section 11 item 22
    files = {f["slot"]: f for f in body["files"]}
    assert set(files) == {"pod5", "bam", "bai", "reference", "regions"}
    for slot, name in SAMPLE_FILENAMES.items():
        assert files[slot]["filename"] == name
        assert files[slot]["bytes"] == len(SAMPLE_BYTES[slot])
        assert files[slot]["url"] == f"/api/samples/signal/files/{name}"
        f = client.get(files[slot]["url"])
        assert f.status_code == 200 and f.content == SAMPLE_BYTES[slot]
        assert f.headers["content-disposition"].startswith("attachment")
    assert body["regions"] == [
        {"seqnames": "tx_A", "start": 60, "end": 300, "width": 241, "strand": "+"},
        {"seqnames": "tx_B", "start": 80, "end": 320, "width": 241, "strand": "+"},
        {"seqnames": "tx_C", "start": 50, "end": 200, "width": 151, "strand": "+"},
    ]
    assert client.get("/api/samples/signal/files/../../etc/passwd").status_code in (404, 422)
    assert client.get("/api/samples/signal/files/other.pod5").status_code == 404


def test_sample_job(client):
    r = client.post("/api/jobs/signal/sample")
    assert r.status_code == 202, r.text
    body = r.json()
    job_id = body["job_id"]
    assert body["status"] == "queued" and body["input_kind"] == "sample" and body["kit"] == "RNA004"
    assert body["input_bytes"] == {slot: len(FILES[slot]) for slot in FILES}
    input_dir = _ctx(client).storage.input_dir(job_id)
    assert sorted(p.name for p in input_dir.iterdir()) == sorted(INPUT_FILENAMES.values())
    assert (input_dir / "input_sorted.bam.bai").read_bytes() == BAI
    assert _ctx(client).queue.sent[-1] == {
        "task": TASK_NAME,
        "kwargs": {"job_id": job_id},
        "task_id": job_id,
        "queue": QUEUE_NAME,
    }


def test_sample_missing_is_404(make_client, tmp_path):
    client = make_client(sample_dir=tmp_path / "no-such-dir")
    assert client.get("/api/samples/signal").status_code == 404
    r = client.post("/api/jobs/signal/sample")
    assert r.status_code == 404
    assert r.json()["detail"] == "The sample data set is not installed on this server."
    assert _ctx(client).queue.sent == []


# ------------------------------------------------------------------------- misc contract


def test_landing_and_docs_mention_the_signal_branch(client):
    html = client.get("/").text
    assert "DirectRM" in html and "MIT" in html and "MultiRM" in html
    assert "MPL" in html or "Mozilla Public License" in html
    assert "research use only" in html
    assert "14 days" in html and "48 hours" in html
    assert "not stored" not in html
    description = client.get("/openapi.json").json()["info"]["description"]
    assert "planned" not in description.lower()
    assert "/api/jobs/signal" in description


def test_settings_dump_redacts_secrets(caplog, make_client, tmp_path):
    with caplog.at_level(logging.INFO, logger="app.main"):
        make_client(
            ip_hash_secret="super-secret", database_url=f"sqlite+pysqlite:///{tmp_path}/r.db"
        )
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "super-secret" not in text
    assert "'ip_hash_secret': '***'" in text


def test_status_stamps_expires_at_for_finished_jobs(client):
    """The worker only writes finished_at; the API derives expires_at on the first poll."""
    from app.jobs.models import Job

    now = datetime.now(UTC)
    done = _done_job(client)
    _set_job(client, done, expires_at=None, finished_at=now)
    failed = _post_multipart(client).json()["job_id"]
    _set_job(client, failed, status="failed", finished_at=now, error="boom")

    retention = timedelta(days=_ctx(client).settings.results_retention_days)
    for job_id in (done, failed):
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["expires_at"] is not None
        with _ctx(client).sessions() as session:
            assert session.get(Job, job_id).expires_at == now + retention
    # queued / running rows are left alone
    queued = _post_multipart(client).json()["job_id"]
    assert client.get(f"/api/jobs/{queued}").json()["expires_at"] is None


def test_cleanup_stamps_and_expires_unpolled_done_jobs(client):
    from app.jobs.cleanup import run_cleanup
    from app.jobs.models import Job

    ctx = _ctx(client)
    now = datetime.now(UTC)
    retention = timedelta(days=ctx.settings.results_retention_days)
    old = _done_job(client)
    _set_job(client, old, expires_at=None, finished_at=now - retention - timedelta(hours=1))
    recent = _done_job(client)
    _set_job(client, recent, expires_at=None, finished_at=now - timedelta(hours=1))

    report = run_cleanup(ctx.sessions, ctx.storage, ctx.settings, now=now)
    assert report.stamped_expiry == 2 and report.expired_jobs == 1
    with ctx.sessions() as session:
        assert session.get(Job, old).status == "expired"
        r = session.get(Job, recent)
        assert r.status == "done" and r.expires_at == now - timedelta(hours=1) + retention
    assert not ctx.storage.job_dir(old).exists()
    assert ctx.storage.results_path(recent).exists()
