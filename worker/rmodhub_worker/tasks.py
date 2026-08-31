"""The Celery task ``rmodhub.signal.run_job(job_id)`` and the job-level outcome handling.

Start gate (the API relies on it, see ``docs/signal-branch.md`` section 7): when the task is
delivered it reads the ``jobs`` row first and only runs a job whose ``status`` is ``queued`` and
whose ``cancel_requested_at`` is null. A row with ``cancel_requested_at`` set (cancel while queued)
is marked ``cancelled`` and its directory removed; any other status (terminal, ``uploading``,
``running`` = another delivery of the same job id) makes the task return without touching the row.
The row is then *claimed* with a conditional UPDATE (``status = 'queued' AND cancel_requested_at
IS NULL``), so a cancel that lands between the gate read and the claim is not overwritten with
``running``. ``jobs.params`` is validated before the claim so a bad ``model_id`` fails the job
with a specific sentence instead of leaving the row ``running`` for the reaper.

Outcome rules (contract sections 4 and 7):

* success       -> ``done`` + ``n_sites``/``n_reads``/``n_transcripts`` + ``finished_at``;
* stage failure -> ``failed`` + one user-safe sentence in ``error`` (details in the worker log);
* soft timeout  -> child process group killed, ``failed`` ("exceeded the 6 h limit"), job dir removed;
* SIGTERM       -> child killed, ``cancelled`` when the API asked for it (``cancel_requested_at``)
  else ``failed`` ("worker stopped"), job dir removed; the process then re-raises SIGTERM so the
  prefork pool sees the child terminate exactly as it expects after ``revoke(terminate=True)``.

Every terminal write (``done`` / ``failed`` / ``cancelled``) is conditional: ``UPDATE ... WHERE
id = %s AND status = 'running'`` (``status = 'queued'`` for the start gate's ``cancelled``). The
API owns every other transition -- ``POST /cancel`` writes ``cancelled`` at once and only then
revokes, the cleanup reaper writes ``failed`` -- so when the UPDATE changes no row the API has
closed the job meanwhile; the worker logs a warning, never overwrites, and removes the job
directory (nobody will read it; the API's cleanup backstop removes it too, and removing twice is
harmless). The write is retried with a bounded backoff (``TERMINAL_WRITE_BACKOFF_S``) on database
errors: a Postgres hiccup at the very end of a multi-hour job must not lose the outcome (the row
would otherwise stay ``running`` until the API reaper declares the worker dead although
``results.sqlite`` is there).

``execute_job`` contains all of this without Celery so ``run_local`` and the tests reuse it.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from . import TASK_NAME
from .celery_app import celery_app
from .celery_app import settings as _import_settings
from .config import Settings, validate_kit
from .db import RUNNING_ONLY, TERMINAL_STATUSES, JobDB, PostgresJobDB, utcnow
from .errors import JobCancelled, StageError
from .lifecycle import remove_job_dir
from .pipeline import Pipeline

log = logging.getLogger("rmodhub_worker.tasks")

GENERIC_ERROR = "The worker hit an unexpected error while processing the job."
TIMEOUT_ERROR = "The job exceeded the {hours:g} h limit and was stopped."
WORKER_STOPPED_ERROR = "The worker was stopped while the job was running."
MISSING_INPUT_ERROR = "The job's input files were not found on the shared volume."
INVALID_PARAMS_ERROR = "The job's parameters are invalid: {problem}."

#: Sleeps between attempts to write a terminal status; ``len + 1`` attempts in total (about
#: 75 s of waiting plus the per-call connect/statement timeouts, i.e. well under 3 minutes).
TERMINAL_WRITE_BACKOFF_S: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0)

#: Outcomes of ``_write_terminal``.
WRITE_DONE = "written"
WRITE_SKIPPED = "skipped"  # the row was not in the expected status: the API closed the job
WRITE_FAILED = "db_error"  # every attempt raised

#: ``if_status`` of the start gate's ``cancelled`` write (the row was read as ``queued``).
QUEUED_ONLY: tuple[str, ...] = ("queued",)


class _SigtermState:
    def __init__(self) -> None:
        self.received = False
        self.finalizing = False


def _install_sigterm(pipeline: Pipeline, state: _SigtermState):
    """Install a SIGTERM handler (main thread only); returns the previous handler or None."""
    if threading.current_thread() is not threading.main_thread():
        return None

    def handler(signum, frame):
        state.received = True
        # Flag only: killing here would run inside the interrupted Popen.wait() (see
        # Pipeline.request_cancel); the child dies when the JobCancelled below unwinds it.
        pipeline.request_cancel(kill=False)
        if not state.finalizing:
            raise JobCancelled("SIGTERM")

    try:
        return signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not in main thread / unsupported
        return None


def _restore_sigterm(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError):
        pass


def _safe_remove(job_dir: Path, settings: Settings) -> None:
    """Remove the job directory; a directory that is already gone (API backstop) is fine."""
    try:
        if remove_job_dir(job_dir, uploads_root=settings.upload_dir):
            log.info("removed job dir %s", job_dir)
        else:
            log.info("job dir %s already removed", job_dir)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        log.warning("could not remove job dir %s: %s", job_dir, exc)


# ----------------------------------------------------------------------------------------------
# jobs.params
# ----------------------------------------------------------------------------------------------


def _as_int(value: Any, name: str) -> int:
    # bool is an int subclass; ``True`` must not silently become model_id 1.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{name} must be an integer")
    try:
        return int(value.strip()) if isinstance(value, str) else value
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def coerce_params(params: dict[str, Any] | None, settings: Settings) -> dict[str, int]:
    """Validate ``jobs.params`` (``model_id``, ``min_coverage``, ``max_coverage``) up front.

    Returns the three effective values (the settings' defaults fill in absent / null keys);
    unknown keys are ignored (the column is free-form). Raises ``ValueError`` (bad value) or
    ``TypeError`` (wrong type) whose text is a user-safe sentence fragment.
    ``Pipeline.__init__`` validates ``model_id`` too, but by then the row is already ``running``.
    """
    params = dict(params or {})

    def pick(key: str, default: int) -> int:
        value = params.get(key)
        return _as_int(default if value is None else value, key)

    model_id = pick("model_id", settings.directrm_model_id)
    if not 1 <= model_id <= 8:
        raise ValueError("model_id must be an integer from 1 to 8")
    min_coverage = pick("min_coverage", settings.min_coverage)
    max_coverage = pick("max_coverage", settings.max_coverage)
    if min_coverage < 0:
        raise ValueError("min_coverage must be >= 0")
    if max_coverage < 1:
        raise ValueError("max_coverage must be >= 1")
    if max_coverage <= min_coverage:
        raise ValueError("max_coverage must be greater than min_coverage")
    return {"model_id": model_id, "min_coverage": min_coverage, "max_coverage": max_coverage}


# ----------------------------------------------------------------------------------------------
# terminal status writes
# ----------------------------------------------------------------------------------------------


def _row_status(db: JobDB, job_id: str) -> str:
    """Current ``jobs.status`` for a log line; never raises."""
    try:
        row = db.get_job(job_id)
    except Exception:  # noqa: BLE001 - diagnostic only
        return "unknown"
    return "missing" if row is None else str(row.get("status"))


def _write_terminal(
    db: JobDB, job_id: str, *, if_status: tuple[str, ...] | None = RUNNING_ONLY, **cols: Any
) -> str:
    """Write a terminal status to a row that is still in ``if_status`` (default ``running``).

    Returns ``WRITE_DONE``; ``WRITE_SKIPPED`` (logged at warning level) when the UPDATE changed
    no row because the API moved the job to another status meanwhile -- the row is left exactly
    as the API wrote it; or ``WRITE_FAILED`` (logged at error level) when every attempt raised.
    Database errors are retried with a bounded backoff; the column values (``finished_at``
    included) are fixed before the first attempt, so a retry after a reply lost in transit
    repeats an identical UPDATE. Callers still return their summary in every case.
    """
    attempts = len(TERMINAL_WRITE_BACKOFF_S) + 1
    for attempt in range(1, attempts + 1):
        try:
            changed = db.update_job(job_id, if_status=if_status, **cols)
        except Exception as exc:  # noqa: BLE001 - any driver/connection error is worth a retry
            if attempt == attempts:
                log.error(
                    "job %s: could not write status=%s after %d attempts: %s",
                    job_id,
                    cols.get("status"),
                    attempts,
                    exc,
                )
                return WRITE_FAILED
            delay = TERMINAL_WRITE_BACKOFF_S[attempt - 1]
            log.warning(
                "job %s: status=%s write failed (attempt %d/%d, retrying in %.0f s): %s",
                job_id,
                cols.get("status"),
                attempt,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)
            continue
        if changed == 0:
            expected = " / ".join(if_status) if if_status else "present"
            log.warning(
                "job %s: terminal write skipped, row no longer %s (status changed by the API); "
                "wanted status=%s, the row is now %s",
                job_id,
                expected,
                cols.get("status"),
                _row_status(db, job_id),
            )
            return WRITE_SKIPPED
        return WRITE_DONE
    return WRITE_FAILED  # pragma: no cover - unreachable


def _fail_early(
    db: JobDB, job_id: str, error: str, *, if_status: tuple[str, ...] | None
) -> dict[str, Any]:
    outcome = _write_terminal(
        db, job_id, if_status=if_status, status="failed", error=error, finished_at=utcnow()
    )
    summary = {"job_id": job_id, "status": "failed", "error": error}
    if outcome == WRITE_SKIPPED:
        summary["db_write_skipped"] = True
    elif outcome == WRITE_FAILED:
        summary["db_write_failed"] = True
    return summary


# ----------------------------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------------------------


def execute_job(
    job_id: str,
    *,
    settings: Settings,
    db: JobDB,
    job_dir: Path | None = None,
    kit: str | None = None,
    params: dict[str, Any] | None = None,
    remove_dir_on_abort: bool = True,
    delete_inputs: bool = True,
) -> dict[str, Any]:
    """Run one job end to end and record the outcome in ``db``. Returns a summary dict."""
    job_dir = Path(job_dir) if job_dir is not None else settings.job_dir(job_id)
    params = dict(params or {})

    gated = kit is None
    if gated:
        # Start gate: only a queued, not-cancelled row is run (see the module docstring).
        job = db.get_job(job_id)
        if job is None:
            log.error("job %s not found in the database; nothing to do", job_id)
            return {"job_id": job_id, "status": "missing", "skipped": True}
        status = job.get("status")
        if status in TERMINAL_STATUSES:
            log.info("job %s is already %s; skipping", job_id, status)
            return {"job_id": job_id, "status": status, "skipped": True}
        if job.get("cancel_requested_at") is not None:
            log.info("job %s was cancelled before it started", job_id)
            # The row was read as ``queued``: only that row may become ``cancelled`` here.
            _write_terminal(
                db,
                job_id,
                if_status=QUEUED_ONLY,
                status="cancelled",
                finished_at=utcnow(),
                progress=None,
                eta_s=None,
            )
            if remove_dir_on_abort:
                _safe_remove(job_dir, settings)
            return {"job_id": job_id, "status": "cancelled"}
        if status != "queued":
            # ``uploading``: the API never started it; ``running``: another delivery of the same
            # job id is (or was) working on it. Never touch the row in either case.
            log.warning("job %s is %r, not queued; skipping this delivery", job_id, status)
            return {"job_id": job_id, "status": status, "skipped": True}
        kit = job.get("kit") or "RNA004"
        params = dict(job.get("params") or {})

    # Before the claim the gated row is ``queued``; an explicit-kit caller (``run_local``) owns
    # its row whatever the state, so its early failure is written unconditionally.
    pre_claim: tuple[str, ...] | None = QUEUED_ONLY if gated else None
    try:
        kit = validate_kit(kit)
    except ValueError as exc:
        return _fail_early(db, job_id, str(exc), if_status=pre_claim)
    try:
        effective = coerce_params(params, settings)
    except (TypeError, ValueError) as exc:
        log.error("job %s: invalid params %r: %s", job_id, params, exc)
        return _fail_early(
            db, job_id, INVALID_PARAMS_ERROR.format(problem=exc), if_status=pre_claim
        )

    hostname = socket.gethostname()
    now = utcnow()
    claim: dict[str, Any] = {
        "status": "running",
        "stage": "preparing",
        "progress": None,
        "eta_s": None,
        "error": None,
        "started_at": now,
        "heartbeat_at": now,
        "worker_hostname": hostname,
    }
    if gated:
        # Atomic claim: the row must still be queued and not cancelled *now*, not only when
        # the gate read it. An API cancel in between has already written ``cancelled`` and
        # removed the job dir; overwriting that with ``running`` would end as a bogus
        # ``failed`` ("input files were not found").
        if not db.claim_job(job_id, **claim):
            current = db.get_job(job_id) or {}
            status = current.get("status") or "missing"
            log.info("job %s became %r before it could be claimed; skipping", job_id, status)
            return {"job_id": job_id, "status": status, "skipped": True}
    else:
        # ``run_local`` / explicit kit: the caller owns the row, whatever its state.
        db.update_job(job_id, **claim)

    if not (job_dir / "input").is_dir():
        return _fail_early(db, job_id, MISSING_INPUT_ERROR, if_status=RUNNING_ONLY)

    try:
        pipeline = Pipeline(
            job_dir,
            kit,
            settings=settings,
            db=db,
            job_id=job_id,
            model_id=effective["model_id"],
            min_coverage=effective["min_coverage"],
            max_coverage=effective["max_coverage"],
            delete_inputs_after_features=delete_inputs,
        )
    except Exception:
        # The row is ``running`` by now: whatever went wrong, it must not stay that way.
        log.exception("job %s: could not set up the pipeline", job_id)
        return _fail_early(db, job_id, GENERIC_ERROR, if_status=RUNNING_ONLY)

    sig_state = _SigtermState()
    previous_handler = _install_sigterm(pipeline, sig_state)
    summary: dict[str, Any] = {"job_id": job_id, "kit": kit, "job_dir": str(job_dir)}

    def finish(**cols: Any) -> str:
        """Terminal write for the claimed (``running``) row; records the outcome in the summary."""
        outcome = _write_terminal(db, job_id, if_status=RUNNING_ONLY, **cols)
        if outcome == WRITE_FAILED:
            summary["db_write_failed"] = True
        elif outcome == WRITE_SKIPPED:
            summary["db_write_skipped"] = True
        return outcome

    try:
        try:
            result = pipeline.run()
        except SoftTimeLimitExceeded:
            sig_state.finalizing = True
            pipeline.request_cancel()
            hours = settings.job_timeout_s / 3600.0
            error = TIMEOUT_ERROR.format(hours=hours)
            log.error("job %s: soft time limit exceeded in stage %s", job_id, pipeline.stage)
            finish(
                status="failed",
                error=error,
                finished_at=utcnow(),
                progress=None,
                eta_s=None,
            )
            if remove_dir_on_abort:
                _safe_remove(job_dir, settings)
            summary.update(status="failed", error=error, stage=pipeline.stage)
        except JobCancelled as exc:
            sig_state.finalizing = True
            pipeline.kill_child()
            cancelled_by_api = True
            if sig_state.received:
                try:
                    cancelled_by_api = db.get_cancel_requested(job_id)
                except Exception:  # noqa: BLE001 - DB unreachable: SIGTERM most likely came from a revoke
                    cancelled_by_api = True
            if cancelled_by_api:
                log.info("job %s cancelled in stage %s (%s)", job_id, pipeline.stage, exc.reason)
                finish(
                    status="cancelled",
                    error=None,
                    finished_at=utcnow(),
                    progress=None,
                    eta_s=None,
                )
                summary.update(status="cancelled", stage=pipeline.stage)
            else:
                log.error("job %s: worker stopped in stage %s", job_id, pipeline.stage)
                finish(
                    status="failed",
                    error=WORKER_STOPPED_ERROR,
                    finished_at=utcnow(),
                    progress=None,
                    eta_s=None,
                )
                summary.update(status="failed", error=WORKER_STOPPED_ERROR, stage=pipeline.stage)
            if remove_dir_on_abort:
                _safe_remove(job_dir, settings)
        except StageError as exc:
            sig_state.finalizing = True
            stage = exc.stage or pipeline.stage
            log.error(
                "job %s failed in stage %s: %s | %s | log dir %s",
                job_id,
                stage,
                exc.user_message,
                exc.detail,
                pipeline.logs_dir,
            )
            finish(
                status="failed",
                error=exc.user_message,
                finished_at=utcnow(),
                progress=None,
                eta_s=None,
            )
            summary.update(status="failed", error=exc.user_message, stage=stage, detail=exc.detail)
        except Exception:
            sig_state.finalizing = True
            pipeline.kill_child()
            log.exception("job %s: unexpected error in stage %s", job_id, pipeline.stage)
            finish(
                status="failed",
                error=GENERIC_ERROR,
                finished_at=utcnow(),
                progress=None,
                eta_s=None,
            )
            summary.update(status="failed", error=GENERIC_ERROR, stage=pipeline.stage)
        else:
            sig_state.finalizing = True
            finish(
                status="done",
                stage=None,
                progress=None,
                eta_s=None,
                error=None,
                finished_at=utcnow(),
                n_sites=result.n_sites,
                n_reads=result.n_reads,
                n_transcripts=result.n_transcripts,
            )
            summary.update(
                status="done",
                n_sites=result.n_sites,
                n_reads=result.n_reads,
                n_transcripts=result.n_transcripts,
                n_read_rows=result.n_read_rows,
                results=str(result.results_path),
            )
        summary["stage_seconds"] = dict(pipeline.stage_seconds)
        summary["meta"] = dict(pipeline.meta)
        if summary.get("db_write_skipped") and remove_dir_on_abort:
            # The API closed the job (cancelled it, or the reaper failed it) while the worker
            # was still at work, so the row keeps the API's status and nobody will ever read
            # this directory: remove it now rather than leave it to the cleanup backstop.
            # In the cancel/timeout branches above this is the second removal, harmless.
            log.warning(
                "job %s: finished as %s but the API had already closed the job; removing %s",
                job_id,
                summary.get("status"),
                job_dir,
            )
            _safe_remove(job_dir, settings)
    finally:
        _restore_sigterm(previous_handler)

    if sig_state.received and previous_handler is not None:
        # Let the prefork pool see the child die from the signal it sent (revoke terminate).
        log.info("job %s: re-raising SIGTERM after cleanup", job_id)
        logging.shutdown()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.raise_signal(signal.SIGTERM)
    return summary


@celery_app.task(
    name=TASK_NAME,
    bind=True,
    max_retries=0,
    ignore_result=True,
    soft_time_limit=_import_settings.job_timeout_s,
    time_limit=_import_settings.job_timeout_s + 300,
)
def run_job(self, job_id: str) -> dict[str, Any]:
    settings = Settings.from_env()
    db = PostgresJobDB(settings.database_url)
    return execute_job(job_id, settings=settings, db=db)
