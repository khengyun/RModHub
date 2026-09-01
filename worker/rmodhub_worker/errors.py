"""Exceptions shared by the pipeline, the Celery task and the CLI."""

from __future__ import annotations

try:
    from celery.exceptions import SoftTimeLimitExceeded
except ImportError:  # pragma: no cover - celery is a hard dependency; keeps the module importable

    class SoftTimeLimitExceeded(Exception):  # type: ignore[no-redef]
        """Stand-in when Celery is not installed."""


class WorkerError(Exception):
    """Base class for the *failure* errors raised by this package (not for interruptions)."""


class StageError(WorkerError):
    """A pipeline stage failed.

    ``user_message`` is the one user-safe sentence written to ``jobs.error``; ``detail`` (log
    excerpt, exception text, log path) goes to the worker log only.
    """

    def __init__(self, user_message: str, detail: str | None = None, stage: str | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.stage = stage

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        stage = f"[{self.stage}] " if self.stage else ""
        detail = f" ({self.detail})" if self.detail else ""
        return f"{stage}{self.user_message}{detail}"


class JobCancelled(BaseException):
    """The job was cancelled (API request seen between stages, or SIGTERM).

    Deliberately a ``BaseException`` (like ``KeyboardInterrupt``): it is raised asynchronously
    by the SIGTERM handler on the main thread, wherever that thread happens to be, and no
    ``except Exception`` in a stage helper may swallow it or re-label it as a stage failure.
    Only the outcome handling in ``tasks.execute_job`` catches it, by name.
    """

    def __init__(self, reason: str = "cancelled"):
        super().__init__(reason)
        self.reason = reason


#: Exceptions that interrupt a job from outside (SIGTERM -> ``JobCancelled``, Celery's soft
#: time limit). Every broad ``except Exception`` on the main thread must re-raise these first:
#: ``JobCancelled`` is a ``BaseException`` already, ``SoftTimeLimitExceeded`` is not.
INTERRUPTS: tuple[type[BaseException], ...] = (JobCancelled, SoftTimeLimitExceeded)
