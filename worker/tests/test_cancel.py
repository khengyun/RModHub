"""Cancellation, timeouts and progress parsing (no model runs; uses sleeping subprocesses)."""

from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from rmodhub_worker.config import Settings
from rmodhub_worker.db import TERMINAL_STATUSES, NullJobDB
from rmodhub_worker.errors import JobCancelled, StageError
from rmodhub_worker.pipeline import Pipeline, PipelineResult, StageRunner

SLEEPER = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print("child", child.pid, flush=True)
print("started", flush=True)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def sleeper(tmp_path: Path) -> Path:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    return script


def test_stage_runner_kills_process_group(sleeper: Path, tmp_path: Path):
    seen = {}
    started = threading.Event()

    def on_line(line: str) -> None:
        if line.startswith("child "):
            seen["child"] = int(line.split()[1])
        if line.startswith("started"):
            started.set()

    runner = StageRunner(
        "sampling",
        [sys.executable, str(sleeper)],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "logs" / "sampling.log",
        on_line=on_line,
        grace_s=1.0,
    )
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", runner.run()))
    thread.start()
    assert started.wait(15), "sleeper did not start"
    assert runner.proc is not None and runner.proc.poll() is None
    grandchild = seen["child"]
    assert _alive(grandchild)

    t0 = time.monotonic()
    runner.terminate()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert time.monotonic() - t0 < 10
    assert runner.terminated is True
    assert result["rc"] != 0  # killed by signal (-15) rather than a clean exit
    assert _wait_dead(grandchild), "grandchild survived the process-group kill"
    log = (tmp_path / "logs" / "sampling.log").read_text()
    assert log.startswith("$ ") and "started" in log


def test_pipeline_request_cancel_raises_job_cancelled(sleeper: Path, tmp_path: Path):
    vendor = tmp_path / "vendor"
    (vendor / "scripts").mkdir(parents=True)
    (vendor / "scripts" / "sleeper.py").write_text(SLEEPER)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    settings = Settings(
        **{**Settings.from_env().__dict__, "vendor_root": vendor, "child_grace_s": 1.0}
    )
    pipeline = Pipeline(job_dir, "RNA004", settings=settings, db=NullJobDB(), job_id="j1")
    pipeline.logs_dir.mkdir(parents=True, exist_ok=True)
    pipeline._begin_stage("sampling")

    outcome = {}

    def run_stage():
        try:
            pipeline._run_script("sampling", "sleeper.py", [])
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            outcome["exc"] = exc

    thread = threading.Thread(target=run_stage)
    thread.start()
    log_path = pipeline.logs_dir / "sampling.log"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (
        log_path.exists() and "started" in log_path.read_text()
    ):
        time.sleep(0.05)
    assert "started" in log_path.read_text()

    pipeline.request_cancel()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert isinstance(outcome.get("exc"), JobCancelled)
    assert pipeline.cancel_requested


def test_db_cancel_between_stages(tmp_path: Path):
    db = NullJobDB()
    db.cancel_requested = True
    pipeline = Pipeline(tmp_path, "RNA004", settings=Settings.from_env(), db=db, job_id="j2")
    with pytest.raises(JobCancelled):
        pipeline._check_cancel()


def test_execute_job_marks_cancelled(tmp_path: Path):
    from rmodhub_worker.tasks import execute_job

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()
    db.cancel_requested = True  # the API set cancel_requested_at while the job was queued/running
    summary = execute_job(
        "j3",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "cancelled"
    assert db.last["status"] == "cancelled" and db.last["finished_at"] is not None
    statuses = [u["status"] for u in db.updates if "status" in u]
    assert statuses == ["running", "cancelled"]


def test_execute_job_soft_time_limit(tmp_path: Path, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    from rmodhub_worker import tasks

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)

    def boom(self):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(tasks.Pipeline, "run", boom)
    db = NullJobDB()
    summary = tasks.execute_job(
        "j4",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "failed"
    assert "6 h limit" in summary["error"]
    assert db.last["status"] == "failed" and "6 h limit" in db.last["error"]


def test_execute_job_stage_error_is_user_safe(tmp_path: Path):
    """A job dir without inputs fails in ``preparing`` with a one-sentence error, never 'running'."""
    from rmodhub_worker.tasks import execute_job

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "regions.csv").write_text("seqnames,start,end,strand\ntx,1,2,+\n")
    db = NullJobDB()
    summary = execute_job(
        "j5",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "failed"
    assert summary["stage"] == "preparing"
    assert summary["error"].endswith(".") and "Traceback" not in summary["error"]
    assert db.last["status"] == "failed" and db.last["error"] == summary["error"]


def test_features_progress_parser(tmp_path: Path):
    pipeline = Pipeline(
        tmp_path, "RNA004", settings=Settings.from_env(), db=NullJobDB(), job_id="j6"
    )
    pipeline._begin_stage("features")
    pipeline._features_total = 4
    pipeline._features_done = 0
    pipeline._on_features_line("Indexing BAM by parent read id: 100%\n")
    assert pipeline.progress == 0.0
    for expected in (0.25, 0.5, 0.75, 1.0):
        pipeline._on_features_line("signal refinement by remora\n")
        assert pipeline.progress == pytest.approx(expected)
        assert pipeline.eta_s is not None and pipeline.eta_s >= 0.0
    pipeline._on_features_line("signal refinement by remora\n")  # never above 1.0
    assert pipeline.progress == 1.0 and pipeline.eta_s == 0.0


# ----------------------------------------------------------------------------------------------
# Start gate: the Celery task reads the jobs row before doing anything (contract section 7)
# ----------------------------------------------------------------------------------------------


def _row(status: str, cancel_requested_at=None) -> dict:
    return {
        "id": "gate",
        "status": status,
        "stage": None,
        "kit": "RNA004",
        "input_kind": "sample",
        "params": {},
        "cancel_requested_at": cancel_requested_at,
    }


@pytest.mark.parametrize(
    "status", ["uploading", "running", "done", "failed", "cancelled", "expired"]
)
def test_task_skips_a_job_that_is_not_queued(tmp_path: Path, status: str):
    """Any status but ``queued`` makes the delivery a no-op: the row and the files stay as they are."""
    from rmodhub_worker.tasks import execute_job

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB(job=_row(status))
    summary = execute_job(
        "gate", settings=Settings.from_env(), db=db, job_dir=job_dir, remove_dir_on_abort=True
    )
    assert summary == {"job_id": "gate", "status": status, "skipped": True}
    assert db.updates == []
    assert (job_dir / "input").is_dir()


def test_task_marks_a_queued_job_with_cancel_request_cancelled(tmp_path: Path):
    """``cancel_requested_at`` set while queued -> ``cancelled`` + job dir removed, nothing runs."""
    from rmodhub_worker.tasks import execute_job

    settings = Settings(**{**Settings.from_env().__dict__, "upload_dir": tmp_path})
    job_dir = settings.job_dir("gate")
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB(job=_row("queued", cancel_requested_at=dt.datetime.now(dt.timezone.utc)))
    summary = execute_job("gate", settings=settings, db=db, remove_dir_on_abort=True)
    assert summary == {"job_id": "gate", "status": "cancelled"}
    assert [u["status"] for u in db.updates if "status" in u] == ["cancelled"]
    assert db.last["finished_at"] is not None
    assert not job_dir.exists()


def test_task_runs_a_queued_job_from_the_row(tmp_path: Path):
    """A queued row is taken: status goes to ``running`` (kit/params read from the row) and on."""
    from rmodhub_worker.tasks import MISSING_INPUT_ERROR, execute_job

    job_dir = tmp_path / "job"  # no input/ -> fails right after the gate with a clear message
    db = NullJobDB(job=_row("queued"))
    summary = execute_job(
        "gate", settings=Settings.from_env(), db=db, job_dir=job_dir, remove_dir_on_abort=False
    )
    assert summary["status"] == "failed" and summary["error"] == MISSING_INPUT_ERROR
    assert [u["status"] for u in db.updates if "status" in u] == ["running", "failed"]
    assert db.updates[0]["stage"] == "preparing" and db.updates[0]["worker_hostname"]


def test_task_skips_a_job_that_is_missing_from_the_database(tmp_path: Path):
    from rmodhub_worker.tasks import execute_job

    db = NullJobDB()  # get_job -> None
    summary = execute_job("gate", settings=Settings.from_env(), db=db, job_dir=tmp_path / "job")
    assert summary == {"job_id": "gate", "status": "missing", "skipped": True}
    assert db.updates == []


def test_sigterm_during_child_wait_finalises_before_the_grace_period(sleeper: Path, tmp_path: Path):
    """SIGTERM handled on the main thread while it sits in Popen.wait() must not stall.

    Regression: the handler used to call terminate() from inside the interrupted wait(),
    whose _waitpid_lock made proc.poll() report "still running" until both grace periods
    had elapsed (2 x child_grace_s). Now the handler only flags the cancel and the child is
    killed once wait() has unwound.
    """
    import signal

    from rmodhub_worker.tasks import _install_sigterm, _restore_sigterm, _SigtermState

    vendor = tmp_path / "vendor"
    (vendor / "scripts").mkdir(parents=True)
    (vendor / "scripts" / "sleeper.py").write_text(SLEEPER)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    settings = Settings(
        **{**Settings.from_env().__dict__, "vendor_root": vendor, "child_grace_s": 3.0}
    )
    pipeline = Pipeline(job_dir, "RNA004", settings=settings, db=NullJobDB(), job_id="j7")
    pipeline.logs_dir.mkdir(parents=True, exist_ok=True)
    pipeline._begin_stage("sampling")
    state = _SigtermState()
    previous = _install_sigterm(pipeline, state)
    assert previous is not None  # pytest runs tests on the main thread
    log_path = pipeline.logs_dir / "sampling.log"
    sent_at: dict[str, float] = {}

    def fire() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (
            log_path.exists() and "started" in log_path.read_text()
        ):
            time.sleep(0.05)
        sent_at["t"] = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=fire, daemon=True).start()
    try:
        with pytest.raises(JobCancelled):
            pipeline._run_script("sampling", "sleeper.py", [])
        unwound = time.monotonic()
        pipeline.kill_child()  # what the task's outcome handling does; a no-op by now
    finally:
        _restore_sigterm(previous)
    assert state.received and pipeline.cancel_requested
    grandchild = int(
        next(l for l in log_path.read_text().splitlines() if l.startswith("child ")).split()[1]
    )
    assert _wait_dead(grandchild, timeout=2.0), "process group not killed"
    assert unwound - sent_at["t"] < settings.child_grace_s, "kill stalled through the grace period"


# ----------------------------------------------------------------------------------------------
# Interruptions must never be swallowed by a broad ``except Exception`` on the main thread
# ----------------------------------------------------------------------------------------------


def test_job_cancelled_is_not_an_exception_subclass():
    """Like KeyboardInterrupt: no ``except Exception`` in a stage helper can eat a cancel."""
    assert issubclass(JobCancelled, BaseException) and not issubclass(JobCancelled, Exception)


def test_sigterm_inside_the_cancel_check_db_call_propagates(tmp_path: Path):
    """Regression: JobCancelled raised by the SIGTERM handler while ``_check_cancel`` waited on
    Postgres was swallowed by its ``except Exception`` (logged as "could not read
    cancel_requested_at: SIGTERM") and the cancelled job went on to run a whole further stage."""
    import signal

    from rmodhub_worker.tasks import _install_sigterm, _restore_sigterm, _SigtermState

    class SlowDB(NullJobDB):
        def get_cancel_requested(self, job_id: str) -> bool:
            time.sleep(5.0)  # SIGTERM lands here
            return False

    pipeline = Pipeline(tmp_path, "RNA004", settings=Settings.from_env(), db=SlowDB(), job_id="j8")
    state = _SigtermState()
    previous = _install_sigterm(pipeline, state)
    assert previous is not None  # pytest runs tests on the main thread
    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(JobCancelled):
            pipeline._check_cancel()
    finally:
        timer.cancel()
        _restore_sigterm(previous)
    assert state.received and pipeline.cancel_requested
    assert time.monotonic() - t0 < 3.0, "unwound only after the DB call finished"


def test_soft_time_limit_inside_db_calls_propagates(tmp_path: Path):
    from celery.exceptions import SoftTimeLimitExceeded

    class BoomDB(NullJobDB):
        def get_cancel_requested(self, job_id: str) -> bool:
            raise SoftTimeLimitExceeded()

        def update_job(self, job_id: str, *, if_status=None, **cols) -> int:
            raise SoftTimeLimitExceeded()

    pipeline = Pipeline(tmp_path, "RNA004", settings=Settings.from_env(), db=BoomDB(), job_id="j9")
    with pytest.raises(SoftTimeLimitExceeded):
        pipeline._check_cancel()
    with pytest.raises(SoftTimeLimitExceeded):
        pipeline._update(progress=0.5)


def test_execute_job_reports_cancelled_even_through_a_broad_handler(tmp_path: Path, monkeypatch):
    """A cancel that unwinds through some ``except Exception`` must still end as ``cancelled``."""
    from rmodhub_worker import tasks

    def run(self):
        try:
            raise JobCancelled("SIGTERM")
        except Exception as exc:  # noqa: BLE001 - the kind of handler the stage helpers have
            raise StageError("relabelled", detail=str(exc)) from None

    monkeypatch.setattr(tasks.Pipeline, "run", run)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()
    summary = tasks.execute_job(
        "j10",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "cancelled"
    assert [u["status"] for u in db.updates if "status" in u] == ["running", "cancelled"]


# ----------------------------------------------------------------------------------------------
# Claim, params validation and terminal-write retry
# ----------------------------------------------------------------------------------------------


def test_claim_refuses_a_job_cancelled_between_the_gate_and_the_claim(tmp_path: Path):
    """The API cancels a queued job right after the gate read it (row -> ``cancelled``, job dir
    removed): the conditional claim fails and nothing is written over the API's outcome."""
    from rmodhub_worker.tasks import execute_job

    class RacyDB(NullJobDB):
        def get_job(self, job_id: str):
            row = super().get_job(job_id)
            if row and row["status"] == "queued":
                self.job.update(
                    status="cancelled", cancel_requested_at=dt.datetime.now(dt.timezone.utc)
                )
            return row

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = RacyDB(job=_row("queued"))
    summary = execute_job(
        "gate", settings=Settings.from_env(), db=db, job_dir=job_dir, remove_dir_on_abort=True
    )
    assert summary == {"job_id": "gate", "status": "cancelled", "skipped": True}
    assert db.updates == []


@pytest.mark.parametrize(
    "params,fragment",
    [
        ({"model_id": 9}, "model_id must be an integer from 1 to 8"),
        ({"model_id": True}, "model_id must be an integer"),
        ({"model_id": 2.5}, "model_id must be an integer"),
        ({"min_coverage": "thirty"}, "min_coverage must be an integer"),
        ({"min_coverage": -1}, "min_coverage must be >= 0"),
        ({"max_coverage": 0}, "max_coverage must be >= 1"),
        ({"min_coverage": 200, "max_coverage": 150}, "greater than min_coverage"),
    ],
)
def test_invalid_params_fail_the_job_before_it_is_claimed(tmp_path: Path, params, fragment):
    """``jobs.params`` is validated up front: a bad value never leaves the row ``running``."""
    from rmodhub_worker.tasks import INVALID_PARAMS_ERROR, execute_job

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB(job={**_row("queued"), "params": params})
    summary = execute_job(
        "gate", settings=Settings.from_env(), db=db, job_dir=job_dir, remove_dir_on_abort=False
    )
    assert summary["status"] == "failed"
    assert fragment in summary["error"]
    prefix = INVALID_PARAMS_ERROR.split("{problem}")[0]
    assert summary["error"].startswith(prefix) and summary["error"].endswith(".")
    assert [u["status"] for u in db.updates if "status" in u] == ["failed"]
    assert db.last["error"] == summary["error"]


def test_params_are_coerced_and_defaulted():
    from rmodhub_worker.tasks import coerce_params

    settings = Settings.from_env()
    assert coerce_params(
        {"model_id": "3", "min_coverage": " 10 ", "max_coverage": 20}, settings
    ) == {
        "model_id": 3,
        "min_coverage": 10,
        "max_coverage": 20,
    }
    defaults = {
        "model_id": settings.directrm_model_id,
        "min_coverage": settings.min_coverage,
        "max_coverage": settings.max_coverage,
    }
    assert coerce_params({}, settings) == defaults
    assert coerce_params(None, settings) == defaults
    assert coerce_params({"model_id": None, "unknown": "ignored"}, settings) == defaults


def test_pipeline_setup_failure_after_the_claim_marks_the_job_failed(tmp_path: Path, monkeypatch):
    from rmodhub_worker import tasks

    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("cannot build")

    monkeypatch.setattr(tasks, "Pipeline", Broken)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB(job=_row("queued"))
    summary = tasks.execute_job(
        "gate", settings=Settings.from_env(), db=db, job_dir=job_dir, remove_dir_on_abort=False
    )
    assert summary["status"] == "failed" and summary["error"] == tasks.GENERIC_ERROR
    assert [u["status"] for u in db.updates if "status" in u] == ["running", "failed"]


class _FlakyDB(NullJobDB):
    """Fails the first ``failures`` terminal-status writes (as a Postgres restart would)."""

    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def update_job(self, job_id: str, *, if_status=None, **cols) -> int:
        if cols.get("status") in TERMINAL_STATUSES:
            self.attempts += 1
            if self.failures > 0:
                self.failures -= 1
                raise ConnectionError("connection timeout expired")
        return super().update_job(job_id, if_status=if_status, **cols)


def _fake_run(self) -> PipelineResult:
    return PipelineResult(
        n_sites=1,
        n_reads=2,
        n_transcripts=3,
        n_read_rows=4,
        results_path=self.job_dir / "results.sqlite",
        stage_seconds={},
    )


def test_terminal_status_write_is_retried(tmp_path: Path, monkeypatch):
    """A transient DB error on the final ``done`` UPDATE must not leave the row ``running``."""
    from rmodhub_worker import tasks

    monkeypatch.setattr(tasks, "TERMINAL_WRITE_BACKOFF_S", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(tasks.Pipeline, "run", _fake_run)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = _FlakyDB(failures=2)
    summary = tasks.execute_job(
        "j11",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "done" and "db_write_failed" not in summary
    assert db.attempts == 3
    assert db.last["status"] == "done" and db.last["n_sites"] == 1
    finished = [u["finished_at"] for u in db.updates if u.get("status") == "done"]
    assert len(finished) == 1  # the retried UPDATE is one identical write, not three


def test_terminal_status_write_gives_up_after_bounded_attempts(tmp_path: Path, monkeypatch, caplog):
    from rmodhub_worker import tasks

    monkeypatch.setattr(tasks, "TERMINAL_WRITE_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(tasks.Pipeline, "run", _fake_run)
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = _FlakyDB(failures=99)
    with caplog.at_level("ERROR", logger="rmodhub_worker.tasks"):
        summary = tasks.execute_job(
            "j12",
            settings=Settings.from_env(),
            db=db,
            job_dir=job_dir,
            kit="RNA004",
            remove_dir_on_abort=False,
        )
    assert summary["status"] == "done" and summary["db_write_failed"] is True
    assert db.attempts == 3  # len(backoff) + 1
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert any("could not write status=done" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------------------------------
# Conditional writes: after the claim the worker only ever touches a row that is still
# ``running``; the API owns every other status (cancel, reaper) and must never be overwritten
# ----------------------------------------------------------------------------------------------

SKIPPED_MESSAGE = "terminal write skipped, row no longer running (status changed by the API)"


def _api_cancel(db: NullJobDB) -> None:
    """What ``POST /api/jobs/{id}/cancel`` does to a running job's row (contract 11.5)."""
    now = dt.datetime.now(dt.timezone.utc)
    db.job.update(status="cancelled", cancel_requested_at=now, finished_at=now)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(**{**Settings.from_env().__dict__, "upload_dir": tmp_path, **overrides})


def _heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "heartbeat" and t.is_alive()]


def test_null_db_mirrors_the_status_guard():
    """``NullJobDB`` behaves like the Postgres ``WHERE status IN (...)``: a row in another
    status is left alone (rowcount 0), a row that never got a status matches (``--no-db``)."""
    db = NullJobDB(job=_row("cancelled"))
    assert db.update_job("gate", if_status=("running",), status="done", n_sites=1) == 0
    assert db.updates == [] and db.job["status"] == "cancelled"
    assert db.update_job("gate", if_status=("queued", "cancelled"), progress=None) == 1
    assert db.job["status"] == "cancelled" and db.last == {"job_id": "gate", "progress": None}
    assert db.update_job("gate", status="failed") == 1  # unconditional: the caller owns the row

    fresh = NullJobDB()
    assert fresh.update_job("x", if_status=("running",), stage="sampling") == 1
    assert fresh.update_job("x", if_status=("running",), status="running") == 1
    assert fresh.update_job("x", if_status=("queued",), status="cancelled") == 0
    with pytest.raises(ValueError):
        fresh.update_job("x", if_status=("bogus",), stage="sampling")
    with pytest.raises(ValueError):
        fresh.update_job("x", if_status=(), stage="sampling")


def test_postgres_update_carries_the_status_guard_as_bound_parameters(monkeypatch):
    from rmodhub_worker.db import PostgresJobDB

    calls: list[tuple[str, list]] = []

    class Cursor:
        rowcount = 0

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            calls.append((sql, list(params)))
            return Cursor()

    db = PostgresJobDB("postgresql+psycopg://u:p@h/d")
    monkeypatch.setattr(db, "_connect", lambda: Conn())
    assert db.update_job("j", if_status=("running",), status="done", n_sites=7) == 0
    assert calls[-1] == (
        "UPDATE jobs SET status = %s, n_sites = %s WHERE id = %s AND status IN (%s)",
        ["done", 7, "j", "running"],
    )
    db.update_job("j", if_status=("queued", "running"), status="cancelled")
    assert calls[-1] == (
        "UPDATE jobs SET status = %s WHERE id = %s AND status IN (%s, %s)",
        ["cancelled", "j", "queued", "running"],
    )
    db.update_job("j", status="failed")
    assert calls[-1] == ("UPDATE jobs SET status = %s WHERE id = %s", ["failed", "j"])
    with pytest.raises(ValueError):
        db.update_job("j", if_status=("bogus",), status="failed")
    assert len(calls) == 3


def test_api_cancel_before_the_done_write_is_never_overwritten(tmp_path: Path, monkeypatch, caplog):
    """``POST /cancel`` lands while the last stage runs and the revoke never arrives (or arrives
    too late): the row is already ``cancelled``. The worker's ``done`` UPDATE
    (``WHERE status = 'running'``) changes nothing, the row keeps the API's status, no
    ``done`` is ever written and the worker removes the job directory (the API backstop would
    remove it too; removing twice is harmless)."""
    from rmodhub_worker import tasks

    settings = _settings(tmp_path, heartbeat_interval_s=0.05)
    job_dir = settings.job_dir("j13")
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()

    def run(self):
        _api_cancel(db)  # the API cancels while the pipeline is in its last stage
        return _fake_run(self)

    monkeypatch.setattr(tasks.Pipeline, "run", run)
    with caplog.at_level("WARNING", logger="rmodhub_worker.tasks"):
        summary = tasks.execute_job("j13", settings=settings, db=db, job_dir=job_dir, kit="RNA004")
    assert summary["status"] == "done" and summary["db_write_skipped"] is True
    assert "db_write_failed" not in summary
    assert db.job["status"] == "cancelled" and db.job["cancel_requested_at"] is not None
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert not any("n_sites" in u for u in db.updates)  # nothing of the done write landed
    assert not job_dir.exists()
    assert any(f"job j13: {SKIPPED_MESSAGE}" in r.getMessage() for r in caplog.records)
    tasks._safe_remove(job_dir, settings)  # the API cleanup backstop, a second time: harmless
    assert not job_dir.exists()


def test_api_cancel_during_the_last_stage_with_the_real_heartbeat(tmp_path: Path, monkeypatch):
    """Same race through the real ``Pipeline.run`` (heartbeat thread, stage bookkeeping): the
    heartbeat thread stops cleanly, the ``done`` write is skipped, the directory goes."""
    from rmodhub_worker import tasks

    settings = _settings(tmp_path, heartbeat_interval_s=0.02)
    job_dir = settings.job_dir("j15")
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()

    def aggregating(self, prep):
        _api_cancel(db)
        time.sleep(0.2)  # a few heartbeats land on the cancelled row: all rowcount 0
        return _fake_run(self)

    for stage in ("_stage_preparing", "_stage_sampling", "_stage_denovo", "_stage_inference"):
        monkeypatch.setattr(tasks.Pipeline, stage, lambda self, *a: None)
    monkeypatch.setattr(tasks.Pipeline, "_stage_features", lambda self, n: None)
    monkeypatch.setattr(tasks.Pipeline, "_stage_aggregating", aggregating)
    summary = tasks.execute_job(
        "j15", settings=settings, db=db, job_dir=job_dir, kit="RNA004", delete_inputs=False
    )
    assert summary["status"] == "done" and summary["db_write_skipped"] is True
    assert db.job["status"] == "cancelled"
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert not job_dir.exists()
    assert _heartbeat_threads() == []
    assert not any(u.get("stage") is None and "n_sites" in u for u in db.updates)


def test_row_lost_during_a_stage_stops_the_child_and_skips_the_terminal_write(
    sleeper: Path, tmp_path: Path, monkeypatch, caplog
):
    """The API cancels a running job but the revoke never reaches the worker: the next
    heartbeat UPDATE (``WHERE status = 'running'``) changes no row, so the worker kills the
    child process group itself, unwinds as a cancel, skips its own ``cancelled`` write (the
    row already says so) and removes the job directory -- within one heartbeat interval plus
    the kill, not at the next stage boundary."""
    from rmodhub_worker import tasks

    vendor = tmp_path / "vendor"
    (vendor / "scripts").mkdir(parents=True)
    (vendor / "scripts" / "sleeper.py").write_text(SLEEPER)
    settings = _settings(tmp_path, vendor_root=vendor, child_grace_s=1.0, heartbeat_interval_s=0.1)
    job_dir = settings.job_dir("j14")
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()
    monkeypatch.setattr(tasks.Pipeline, "_stage_preparing", lambda self: None)
    monkeypatch.setattr(
        tasks.Pipeline,
        "_stage_sampling",
        lambda self: self._run_script("sampling", "sleeper.py", []),
    )
    log_path = job_dir / "work" / "logs" / "sampling.log"
    seen: dict[str, float | int] = {}

    def api_cancel() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (
            log_path.exists() and "started" in log_path.read_text()
        ):
            time.sleep(0.05)
        text = log_path.read_text()
        seen["grandchild"] = int(
            next(line for line in text.splitlines() if line.startswith("child ")).split()[1]
        )
        seen["t"] = time.monotonic()
        _api_cancel(db)

    threading.Thread(target=api_cancel, daemon=True).start()
    with caplog.at_level("WARNING"):
        summary = tasks.execute_job("j14", settings=settings, db=db, job_dir=job_dir, kit="RNA004")
    stopped = time.monotonic()
    assert "grandchild" in seen, "the sleeper never started"
    assert stopped - seen["t"] < 5.0, "the lost row was not noticed within a heartbeat"
    assert summary["status"] == "cancelled" and summary["db_write_skipped"] is True
    assert db.job["status"] == "cancelled"
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert _wait_dead(int(seen["grandchild"]), timeout=5.0), "process group not killed"
    assert not job_dir.exists()
    assert _heartbeat_threads() == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("heartbeat skipped, row no longer running" in m for m in messages)
    assert any(f"job j14: {SKIPPED_MESSAGE}" in m for m in messages)


def test_stage_write_on_a_closed_row_sets_the_cancel_flag(tmp_path: Path):
    """A stage/progress UPDATE from the main thread that changes no row flags the cancel (it
    never raises: the same path runs on the features log pump thread) and the next cancel
    check unwinds the job."""
    db = NullJobDB(job=_row("cancelled"))
    pipeline = Pipeline(tmp_path, "RNA004", settings=Settings.from_env(), db=db, job_id="gate")
    assert not pipeline.cancel_requested
    pipeline._begin_stage("denovo")
    assert pipeline.cancel_requested
    assert db.updates == [] and db.job["status"] == "cancelled"
    with pytest.raises(JobCancelled):
        pipeline._check_cancel()


def test_heartbeat_stops_after_the_row_is_lost():
    from rmodhub_worker.pipeline import Heartbeat

    db = NullJobDB(job=_row("running"))
    lost = threading.Event()
    beat = Heartbeat(db, "gate", 0.01, lambda: {"progress": 0.5, "eta_s": 1.0}, on_lost=lost.set)
    beat.start()
    time.sleep(0.1)
    assert not lost.is_set() and beat.is_alive()
    assert db.updates and all(u["progress"] == 0.5 for u in db.updates)
    n_before = len(db.updates)
    _api_cancel(db)
    assert lost.wait(2.0), "on_lost was not called"
    beat.join(2.0)
    assert not beat.is_alive() and beat.lost is True
    assert len(db.updates) == n_before  # nothing landed on the cancelled row
    assert db.job["status"] == "cancelled"


def test_stage_failure_after_the_api_closed_the_job_keeps_the_api_status(
    tmp_path: Path, monkeypatch, caplog
):
    from rmodhub_worker import tasks

    settings = _settings(tmp_path)
    job_dir = settings.job_dir("j16")
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB()

    def run(self):
        _api_cancel(db)
        raise StageError("Feature extraction failed.", detail="rc 1", stage="features")

    monkeypatch.setattr(tasks.Pipeline, "run", run)
    with caplog.at_level("WARNING", logger="rmodhub_worker.tasks"):
        summary = tasks.execute_job("j16", settings=settings, db=db, job_dir=job_dir, kit="RNA004")
    assert summary["status"] == "failed" and summary["db_write_skipped"] is True
    assert db.job["status"] == "cancelled" and db.job.get("error") is None
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert not job_dir.exists()  # a cancelled job's directory has no reader
    assert any(f"job j16: {SKIPPED_MESSAGE}" in r.getMessage() for r in caplog.records)


def test_gate_cancelled_write_only_converts_a_queued_row(tmp_path: Path):
    """Queued row with ``cancel_requested_at`` at the gate, but the API finishes its own cancel
    (row -> ``cancelled``) before the worker's write: the worker's UPDATE is limited to
    ``status = 'queued'`` and changes nothing."""
    from rmodhub_worker.tasks import execute_job

    class RacyDB(NullJobDB):
        def get_job(self, job_id: str):
            row = super().get_job(job_id)
            if row and row["status"] == "queued":
                self.job["status"] = "cancelled"
            return row

    settings = _settings(tmp_path)
    job_dir = settings.job_dir("gate")
    (job_dir / "input").mkdir(parents=True)
    db = RacyDB(job=_row("queued", cancel_requested_at=dt.datetime.now(dt.timezone.utc)))
    summary = execute_job("gate", settings=settings, db=db)
    assert summary == {"job_id": "gate", "status": "cancelled"}
    assert db.updates == [] and db.job["status"] == "cancelled"
    assert not job_dir.exists()


def test_run_local_style_caller_still_yields_to_an_api_cancel(tmp_path: Path, monkeypatch):
    """An explicit-kit caller claims unconditionally (it owns the row) but its writes after the
    claim are guarded like the task's: an API cancel in between still wins."""
    from rmodhub_worker import tasks

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    db = NullJobDB(job=_row("queued"))

    def run(self):
        _api_cancel(db)
        return _fake_run(self)

    monkeypatch.setattr(tasks.Pipeline, "run", run)
    summary = tasks.execute_job(
        "gate",
        settings=Settings.from_env(),
        db=db,
        job_dir=job_dir,
        kit="RNA004",
        remove_dir_on_abort=False,
    )
    assert summary["status"] == "done" and summary["db_write_skipped"] is True
    assert db.job["status"] == "cancelled"
    assert [u["status"] for u in db.updates if "status" in u] == ["running"]
    assert (job_dir / "input").is_dir()  # remove_dir_on_abort=False: run_local keeps its files
