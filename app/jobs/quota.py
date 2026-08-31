"""Per-address quotas.

The client is identified by `client_key = HMAC-SHA256(RMODHUB_IP_HASH_SECRET, client IP)`;
the raw address never reaches the database or the logs. Behind the reverse proxy uvicorn
runs with `--proxy-headers`, so `request.client.host` is the address from X-Forwarded-For.

`enforce_quota` is a count; the insert that follows it must not interleave with another
request's count for the same key, or N parallel submissions all pass a cap of 3. Callers
wrap count + insert + commit in `quota_guard`.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.jobs.constants import RUNNING, WAITING_STATUSES
from app.jobs.models import Job

# Striped in-process locks: bounded, no bookkeeping, one client key always maps to the same
# stripe. Two different keys sharing a stripe only wait on each other for the few
# milliseconds of a count + insert.
_STRIPES: tuple[threading.Lock, ...] = tuple(threading.Lock() for _ in range(64))


def client_key(request: Request, secret: str) -> str:
    host = request.client.host if request.client and request.client.host else "unknown"
    return hmac.new(secret.encode("utf-8"), host.encode("utf-8"), hashlib.sha256).hexdigest()


def count_jobs(session: Session, key: str, statuses: tuple[str, ...]) -> int:
    stmt = (
        select(func.count())
        .select_from(Job)
        .where(Job.client_key == key, Job.status.in_(statuses))
    )
    return int(session.execute(stmt).scalar_one())


@contextmanager
def quota_guard(session: Session, key: str) -> Iterator[None]:
    """Serialise check-then-insert for one client key; the commit must happen inside.

    Within the process a striped `threading.Lock` makes count + insert + commit one unit
    (the routes that create jobs run in the threadpool, never on the event loop, and the
    Dockerfile runs one uvicorn worker per container). On Postgres a transaction-scoped
    advisory lock on the same key extends the guarantee to several API processes; it is
    released with the commit or rollback. SQLite (tests) has no advisory locks and does not
    need one: its single writer plus the in-process lock suffice.
    """
    stripe = _STRIPES[zlib.crc32(key.encode("utf-8")) % len(_STRIPES)]
    with stripe:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        yield


def enforce_quota(session: Session, key: str, settings: Settings) -> None:
    """Raise 429 when the address already has its share of running or waiting jobs."""
    running = count_jobs(session, key, (RUNNING,))
    if running >= settings.max_running_per_ip:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You already have {running} job running; this server allows at most "
                f"{settings.max_running_per_ip} running job per address. Please wait for it "
                "to finish before submitting another one."
            ),
            headers={"Retry-After": "300"},
        )
    waiting = count_jobs(session, key, WAITING_STATUSES)
    if waiting >= settings.max_queued_per_ip:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You already have {waiting} job(s) uploading or queued; this server allows "
                f"at most {settings.max_queued_per_ip} waiting jobs per address. Cancel one "
                "or wait for it to start."
            ),
            headers={"Retry-After": "60"},
        )
