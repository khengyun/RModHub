"""Celery producer (docs/signal-branch.md section 7).

The API never imports worker code: jobs are sent **by task name** with `task_id = job_id`
on the `signal` queue, and cancelled with `revoke`. Without a broker URL a `NullQueue`
records the calls instead (used by the test-suite and by a single-container dev setup).
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.jobs.constants import QUEUE_NAME, TASK_NAME

log = logging.getLogger(__name__)


class QueueUnavailable(Exception):
    """The broker could not be reached; the message is safe to show to the user."""

    def __init__(self, message: str = "The job queue is not reachable; please try again later."):
        super().__init__(message)


class JobQueue(Protocol):
    name: str

    def enqueue(self, job_id: str) -> None: ...

    def revoke(self, job_id: str, *, terminate: bool = False) -> None: ...


class NullQueue:
    """Records enqueue/revoke calls; nothing runs. Used when no broker is configured."""

    name = "null"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.revoked: list[dict] = []

    def enqueue(self, job_id: str) -> None:
        call = {
            "task": TASK_NAME,
            "kwargs": {"job_id": job_id},
            "task_id": job_id,
            "queue": QUEUE_NAME,
        }
        self.sent.append(call)
        log.info("null queue: would send %s(job_id=%s) to queue %r", TASK_NAME, job_id, QUEUE_NAME)

    def revoke(self, job_id: str, *, terminate: bool = False) -> None:
        self.revoked.append({"task_id": job_id, "terminate": terminate, "signal": "SIGTERM"})
        log.info("null queue: would revoke %s (terminate=%s)", job_id, terminate)


class CeleryQueue:
    """Lazy Celery client: the app object is built on first use, never at import time."""

    name = "celery"

    def __init__(self, broker_url: str) -> None:
        self._broker_url = broker_url
        self._app = None

    def _celery(self):
        if self._app is None:
            from celery import Celery

            app = Celery("rmodhub", broker=self._broker_url)
            app.conf.update(
                task_ignore_result=True,  # status lives in Postgres, not in a result backend
                task_default_queue=QUEUE_NAME,
                broker_connection_retry_on_startup=True,
                broker_connection_timeout=5,
                broker_connection_max_retries=3,
                task_publish_retry=True,
                task_publish_retry_policy={
                    "max_retries": 2,
                    "interval_start": 0,
                    "interval_step": 0.5,
                    "interval_max": 2,
                },
            )
            self._app = app
        return self._app

    def enqueue(self, job_id: str) -> None:
        try:
            self._celery().send_task(
                TASK_NAME, kwargs={"job_id": job_id}, task_id=job_id, queue=QUEUE_NAME
            )
        except Exception as exc:  # kombu.exceptions.OperationalError and friends
            log.error("could not enqueue job %s: %s", job_id, exc)
            raise QueueUnavailable() from exc

    def revoke(self, job_id: str, *, terminate: bool = False) -> None:
        try:
            self._celery().control.revoke(job_id, terminate=terminate, signal="SIGTERM")
        except Exception as exc:
            log.error("could not revoke job %s: %s", job_id, exc)
            raise QueueUnavailable(
                "The job queue is not reachable; the cancellation was recorded and will be "
                "applied when the worker checks in."
            ) from exc


def make_queue(broker_url: str | None) -> JobQueue:
    if broker_url:
        return CeleryQueue(broker_url)
    log.warning("no CELERY_BROKER_URL configured: jobs will be recorded but never executed")
    return NullQueue()
