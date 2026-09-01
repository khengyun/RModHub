"""Data-lifecycle reaper (docs/signal-branch.md section 8).

One idempotent pass, `run_cleanup(now)`:

0. `done` / `failed` jobs without `expires_at` get `finished_at + results_retention_days`
   (the worker writes only `finished_at`; the status route stamps this too).
1. `running` jobs whose worker heartbeat is older than 10 min -> `failed`
   ("The worker stopped responding; please resubmit.").
2. `uploading` jobs older than `upload_ttl_h` -> `expired`, tus files removed.
3. jobs past `expires_at` -> directory removed, `results_deleted_at` set, `done` -> `expired`.
   A `cancelled` job whose worker is still heartbeating is left alone until it is gone.
4. any `input/` older than `inputs_max_age_h` -> removed (backstop for the worker's own
   deletion after feature extraction). A job still `queued` at that point can never run
   any more -> `failed` with a message saying so.
5. terminal rows whose files are long gone are deleted (the table stays bounded; the row
   of a job that never started goes after `upload_ttl_h`, any other after
   `results_retention_days`).
6. orphans on disk (job dirs / tus files with no matching database row) older than the
   same limits -> removed.

The number of bytes freed is logged per run. The pass runs inside the API process every
`cleanup_interval_s` and can be run from cron: `python -m app.jobs.cleanup`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import utcnow
from app.jobs.constants import (
    CANCELLED,
    DEAD_WORKER_AFTER_S,
    DONE,
    EXPIRED,
    FAILED,
    QUEUED,
    RUNNING,
    TERMINAL_STATUSES,
    UPLOADING,
)
from app.jobs.models import Job, Upload
from app.jobs.service import stamp_expiry
from app.jobs.storage import JobStorage, dir_size

log = logging.getLogger(__name__)

DEAD_WORKER_DETAIL = "The worker stopped responding; please resubmit."
UPLOAD_EXPIRED_DETAIL = "The upload was not completed in time and has expired."
QUEUED_TIMEOUT_DETAIL = (
    "The job waited longer than {hours} h for a worker and its input files were removed; "
    "please resubmit."
)


@dataclass
class CleanupReport:
    stamped_expiry: int = 0
    reaped_workers: int = 0
    expired_uploads: int = 0
    expired_jobs: int = 0
    deferred_jobs: int = 0
    inputs_deleted: int = 0
    timed_out_queued: int = 0
    purged_rows: int = 0
    orphan_dirs: int = 0
    orphan_uploads: int = 0
    bytes_freed: int = 0

    def summary(self) -> str:
        return (
            f"cleanup: stamped {self.stamped_expiry} expiry date(s), reaped "
            f"{self.reaped_workers} dead-worker job(s), expired "
            f"{self.expired_uploads} unfinished upload(s) and {self.expired_jobs} job(s) "
            f"({self.deferred_jobs} deferred: worker still alive), deleted "
            f"{self.inputs_deleted} input dir(s), failed {self.timed_out_queued} job(s) "
            f"never picked up, purged {self.purged_rows} old row(s), removed "
            f"{self.orphan_dirs} orphan dir(s) and {self.orphan_uploads} orphan upload "
            f"file(s); freed {self.bytes_freed / 1024**2:.1f} MB ({self.bytes_freed} bytes)"
        )


def _mtime(path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _stamp_expiry(
    session: Session, now: datetime, settings: Settings, report: CleanupReport
) -> None:
    stmt = select(Job).where(Job.status.in_((DONE, FAILED)), Job.expires_at.is_(None))
    for job in session.execute(stmt).scalars():
        if stamp_expiry(job, settings, now):
            report.stamped_expiry += 1


def _reap_dead_workers(session: Session, now: datetime, report: CleanupReport) -> None:
    cutoff = now - timedelta(seconds=DEAD_WORKER_AFTER_S)
    for job in session.execute(select(Job).where(Job.status == RUNNING)).scalars():
        last_seen = job.heartbeat_at or job.started_at or job.created_at
        if last_seen is not None and last_seen > cutoff:
            continue
        job.status = FAILED
        job.error = DEAD_WORKER_DETAIL
        job.finished_at = now
        job.expires_at = now  # the directory goes in step 3 of this same pass
        report.reaped_workers += 1
        log.warning("job %s: worker heartbeat last seen %s; marked failed", job.id, last_seen)


def _expire_uploads(
    session: Session, storage: JobStorage, now: datetime, settings: Settings, report: CleanupReport
) -> None:
    cutoff = now - timedelta(hours=settings.upload_ttl_h)
    stmt = select(Job).where(Job.status == UPLOADING, Job.created_at < cutoff)
    for job in session.execute(stmt).scalars():
        for upload in list(job.uploads):
            report.bytes_freed += storage.remove_tus(upload.id)
        job.uploads.clear()
        report.bytes_freed += storage.remove_job_dir(job.id)
        job.status = EXPIRED
        job.error = UPLOAD_EXPIRED_DETAIL
        job.finished_at = now
        job.inputs_deleted_at = now
        job.results_deleted_at = now
        report.expired_uploads += 1


def _expire_jobs(
    session: Session, storage: JobStorage, now: datetime, report: CleanupReport
) -> None:
    alive_after = now - timedelta(seconds=DEAD_WORKER_AFTER_S)
    stmt = select(Job).where(
        Job.expires_at.is_not(None),
        Job.expires_at <= now,
        Job.results_deleted_at.is_(None),
        Job.status != UPLOADING,
    )
    for job in session.execute(stmt).scalars():
        if (
            job.status == CANCELLED
            and job.heartbeat_at is not None
            and job.heartbeat_at > alive_after
        ):
            # The cancel's expires_at (+1 h) is only a backstop for the worker's own
            # removal. A worker that missed the revoke keeps heartbeating until the next
            # stage boundary; pulling the directory from under it would turn the cancel
            # into a misleading "feature extraction failed". Wait until it is provably gone.
            report.deferred_jobs += 1
            continue
        for upload in list(job.uploads):
            report.bytes_freed += storage.remove_tus(upload.id)
        job.uploads.clear()
        report.bytes_freed += storage.remove_job_dir(job.id)
        job.results_deleted_at = now
        if job.inputs_deleted_at is None:
            job.inputs_deleted_at = now
        if job.status == DONE:
            job.status = EXPIRED
        report.expired_jobs += 1


def _delete_old_inputs(
    session: Session, storage: JobStorage, now: datetime, settings: Settings, report: CleanupReport
) -> None:
    cutoff = now - timedelta(hours=settings.inputs_max_age_h)
    stmt = select(Job).where(
        Job.created_at < cutoff, Job.inputs_deleted_at.is_(None), Job.status != UPLOADING
    )
    for job in session.execute(stmt).scalars():
        if job.status == QUEUED:
            # Never picked up (a backlog longer than the input TTL, a lost broker message,
            # no broker at all). Without its input the job can no longer run: say so now
            # rather than leaving it `queued` forever, holding a quota slot.
            report.bytes_freed += storage.remove_job_dir(job.id)
            job.status = FAILED
            job.error = QUEUED_TIMEOUT_DETAIL.format(hours=settings.inputs_max_age_h)
            job.finished_at = now
            job.expires_at = now
            job.inputs_deleted_at = now
            job.results_deleted_at = now
            report.timed_out_queued += 1
            log.warning("job %s: still queued after %d h; marked failed", job.id, settings.inputs_max_age_h)
            continue
        freed = storage.remove_input_dir(job.id)
        report.bytes_freed += freed
        job.inputs_deleted_at = now
        if freed:
            report.inputs_deleted += 1


def _purge_rows(session: Session, now: datetime, settings: Settings, report: CleanupReport) -> None:
    """Delete terminal rows whose files are long gone, so `jobs` does not grow forever.

    After `results_deleted_at` the row only serves `GET /api/jobs/{id}` -> `expired` /
    `cancelled`; keeping it one more retention period is plenty. Rows of jobs that never
    reached a worker (cancelled or expired while uploading; an init/cancel loop creates
    one per request) go after `upload_ttl_h`. The uploads table follows by FK cascade.
    """
    long_ago = now - timedelta(days=settings.results_retention_days)
    never_ran = now - timedelta(hours=settings.upload_ttl_h)
    stmt = delete(Job).where(
        Job.status.in_(tuple(TERMINAL_STATUSES)),
        Job.results_deleted_at.is_not(None),
        or_(
            Job.results_deleted_at < long_ago,
            and_(Job.started_at.is_(None), Job.results_deleted_at < never_ran),
        ),
    )
    result = session.execute(stmt.execution_options(synchronize_session=False))
    report.purged_rows += max(int(result.rowcount or 0), 0)


def _sweep_orphans(
    session: Session, storage: JobStorage, now: datetime, settings: Settings, report: CleanupReport
) -> None:
    # One lookup per directory on disk (bounded by live jobs) rather than every job id ever
    # created materialised in a set.
    dir_cutoff = now - timedelta(hours=settings.inputs_max_age_h)
    for job_id, path in storage.list_job_dirs():
        try:
            if _mtime(path) > dir_cutoff:
                continue
        except FileNotFoundError:
            continue
        if session.get(Job, job_id) is not None:
            continue
        report.bytes_freed += dir_size(path)
        storage.remove_job_dir(job_id)
        report.orphan_dirs += 1

    tus_cutoff = now - timedelta(hours=settings.upload_ttl_h)
    for upload_id, path in storage.list_tus_files():
        try:
            if _mtime(path) > tus_cutoff:
                continue
        except FileNotFoundError:
            continue
        live = session.execute(
            select(Upload.id)
            .join(Job, Upload.job_id == Job.id)
            .where(Upload.id == upload_id, Job.status == UPLOADING)
        ).scalar_one_or_none()
        if live is not None:
            continue
        report.bytes_freed += storage.remove_tus(upload_id)
        report.orphan_uploads += 1


def run_cleanup(
    sessions: sessionmaker[Session],
    storage: JobStorage,
    settings: Settings,
    now: datetime | None = None,
) -> CleanupReport:
    """One idempotent cleanup pass; safe to run concurrently with the API and the worker."""
    now = now or utcnow()
    report = CleanupReport()
    with sessions() as session:
        _stamp_expiry(session, now, settings, report)
        _reap_dead_workers(session, now, report)
        _expire_uploads(session, storage, now, settings, report)
        _expire_jobs(session, storage, now, report)
        _delete_old_inputs(session, storage, now, settings, report)
        _purge_rows(session, now, settings, report)
        session.commit()
        _sweep_orphans(session, storage, now, settings, report)
    log.info("%s", report.summary())
    return report


async def cleanup_loop(
    sessions: sessionmaker[Session], storage: JobStorage, settings: Settings
) -> None:
    """Periodic in-process task started by the application lifespan.

    The first pass runs one interval after start-up: a freshly (re)started API should not
    begin by deleting data, and `python -m app.jobs.cleanup` exists for an immediate pass.
    """
    while True:
        await asyncio.sleep(settings.cleanup_interval_s)
        try:
            await asyncio.to_thread(run_cleanup, sessions, storage, settings)
        except Exception:  # never let one bad pass kill the loop
            log.exception("cleanup pass failed")


def main(argv: list[str] | None = None) -> int:
    """`python -m app.jobs.cleanup` for cron; reads the same RMODHUB_* / DATABASE_URL env."""
    parser = argparse.ArgumentParser(description="Run one RModHub signal-branch cleanup pass.")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from app.config import get_settings
    from app.db import init_db, make_engine, make_sessionmaker

    settings = get_settings()
    if not settings.signal_enabled:
        print("DATABASE_URL is not set: the signal branch is disabled, nothing to clean up.")
        return 2
    engine = make_engine(settings.database_url.get_secret_value())
    init_db(engine)
    storage = JobStorage(settings.upload_dir)
    storage.ensure_layout()
    report = run_cleanup(make_sessionmaker(engine), storage, settings)
    engine.dispose()
    print(report.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
