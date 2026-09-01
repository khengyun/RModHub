"""Celery application (contract section 7).

* broker: ``CELERY_BROKER_URL`` (redis db 0); result backend disabled (status lives in Postgres);
* queue ``signal``; one task per worker at a time (``--concurrency=1 --prefetch-multiplier=1``);
* ``acks_late=False`` / ``reject_on_worker_lost=False``: a job is acknowledged when it starts;
  if the worker dies the API reaper marks it failed via the stale ``heartbeat_at``.
"""

from __future__ import annotations

from celery import Celery

from . import TASK_NAME
from .config import Settings

settings = Settings.from_env()

celery_app = Celery("rmodhub_worker", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_routes={TASK_NAME: {"queue": "signal"}},
    task_default_queue="signal",
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    result_backend=None,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_track_started=False,
    broker_connection_retry_on_startup=True,
    enable_utc=True,
    timezone="UTC",
    imports=("rmodhub_worker.tasks",),
)
