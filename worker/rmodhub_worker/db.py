"""Postgres access with plain SQL (psycopg 3), restricted to what the contract lets the worker touch.

* ``update_job`` accepts only the columns listed in ``docs/signal-branch.md`` section 4
  ("Worker -> Postgres writes").
* ``update_job(..., if_status=("running",))`` adds ``AND status IN (...)`` to that UPDATE and
  returns the rowcount, so a write meant for a running job cannot land on a row the API has
  closed in the meantime (``POST /cancel`` sets ``cancelled`` immediately; the reaper sets
  ``failed``). Every write after the claim -- stage, progress, heartbeat, ``inputs_deleted_at``
  and the terminal ``done`` / ``failed`` / ``cancelled`` -- goes through this guard; a rowcount
  of 0 means "the API changed the status" and the caller skips, it never overwrites.
* ``claim_job`` is the same UPDATE guarded by ``status = 'queued' AND cancel_requested_at IS
  NULL``: the start gate reads the row and then claims it atomically, so a cancel that lands
  between the two cannot be overwritten with ``running``.
* One short-lived connection per call (``connect_timeout`` + ``statement_timeout``), so a stuck
  database can never wedge the pipeline for long, and there is no connection to keep alive across
  a multi-hour job.
* ``NullJobDB`` is the in-memory stand-in used by ``run_local --no-db`` and the tests.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

ALLOWED_COLUMNS = frozenset(
    {
        "status",
        "stage",
        "progress",
        "eta_s",
        "started_at",
        "finished_at",
        "inputs_deleted_at",
        "error",
        "n_sites",
        "n_reads",
        "n_transcripts",
        "worker_hostname",
        "heartbeat_at",
    }
)

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "expired"})
JOB_STATUSES = frozenset({"uploading", "queued", "running"}) | TERMINAL_STATUSES

#: ``if_status`` value for every write the worker makes after it claimed the row.
RUNNING_ONLY: tuple[str, ...] = ("running",)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalise_dsn(url: str) -> str:
    """Turn a SQLAlchemy-style URL (``postgresql+psycopg://``) into a libpq one."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base = scheme.split("+", 1)[0]
    if base == "postgres":
        base = "postgresql"
    return f"{base}://{rest}"


class JobDB(Protocol):
    def update_job(
        self, job_id: str, *, if_status: tuple[str, ...] | None = None, **cols: Any
    ) -> int:
        """UPDATE the allowed columns; with ``if_status`` only when ``status`` is one of them.

        Returns the number of rows changed (0 or 1). A 0 with ``if_status`` set means the API
        moved the row to another status meanwhile; callers must not fall back to an
        unconditional write.
        """
        ...

    def claim_job(self, job_id: str, **cols: Any) -> bool: ...

    def get_cancel_requested(self, job_id: str) -> bool: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...


def _check_columns(cols: dict[str, Any]) -> None:
    unknown = set(cols) - ALLOWED_COLUMNS
    if unknown:
        raise ValueError(f"worker may not write jobs column(s): {sorted(unknown)}")


def _check_statuses(if_status: tuple[str, ...] | None) -> None:
    if if_status is None:
        return
    unknown = set(if_status) - JOB_STATUSES
    if not if_status or unknown:
        raise ValueError(f"if_status must name known job statuses, got {if_status!r}")


class NullJobDB:
    """Records every update in memory; never touches a database."""

    def __init__(self, job: dict[str, Any] | None = None):
        self.updates: list[dict[str, Any]] = []
        self.cancel_requested = False
        self.job: dict[str, Any] = dict(job or {})

    def update_job(
        self, job_id: str, *, if_status: tuple[str, ...] | None = None, **cols: Any
    ) -> int:
        _check_columns(cols)
        _check_statuses(if_status)
        # The guard mirrors Postgres on the in-memory row. A row that has never been given a
        # status (a Pipeline driven directly, without a claim) is treated as matching, like the
        # ``--no-db`` runs it stands in for.
        current = self.job.get("status")
        if if_status is not None and current is not None and current not in if_status:
            return 0
        self.updates.append({"job_id": job_id, **cols})
        self.job.update(cols)
        return 1

    def claim_job(self, job_id: str, **cols: Any) -> bool:
        _check_columns(cols)
        if self.job and (
            self.job.get("status") != "queued" or self.job.get("cancel_requested_at") is not None
        ):
            return False
        return self.update_job(job_id, **cols) == 1

    def get_cancel_requested(self, job_id: str) -> bool:
        return self.cancel_requested

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return dict(self.job) if self.job else None

    @property
    def last(self) -> dict[str, Any]:
        return self.updates[-1] if self.updates else {}


class PostgresJobDB:
    """psycopg 3 implementation. ``url`` may be a libpq or SQLAlchemy URL."""

    def __init__(self, url: str, connect_timeout_s: int = 5, statement_timeout_ms: int = 10000):
        if not url:
            raise ValueError("DATABASE_URL is not set")
        self.dsn = normalise_dsn(url)
        self.connect_timeout_s = connect_timeout_s
        self.statement_timeout_ms = statement_timeout_ms

    def _connect(self):
        import psycopg

        return psycopg.connect(
            self.dsn,
            connect_timeout=self.connect_timeout_s,
            options=f"-c statement_timeout={self.statement_timeout_ms}",
            autocommit=True,
        )

    def update_job(
        self, job_id: str, *, if_status: tuple[str, ...] | None = None, **cols: Any
    ) -> int:
        _check_columns(cols)
        _check_statuses(if_status)
        if not cols:
            return 0
        # Column names come from the allow-list above, never from user input; the status
        # guard is a plain ``IN (%s, ...)`` so every value travels as a bound parameter.
        assignments = ", ".join(f"{name} = %s" for name in cols)
        sql = f"UPDATE jobs SET {assignments} WHERE id = %s"
        params: list[Any] = [*cols.values(), job_id]
        if if_status is not None:
            sql += " AND status IN (" + ", ".join("%s" for _ in if_status) + ")"
            params.extend(if_status)
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def claim_job(self, job_id: str, **cols: Any) -> bool:
        """``update_job`` restricted to a still-queued, not-cancelled row; True if it was taken."""
        _check_columns(cols)
        if not cols:
            return False
        assignments = ", ".join(f"{name} = %s" for name in cols)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = %s "
                "AND status = 'queued' AND cancel_requested_at IS NULL",
                [*cols.values(), job_id],
            )
            return cur.rowcount == 1

    def get_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested_at, status FROM jobs WHERE id = %s", [job_id]
            ).fetchone()
        if row is None:
            # The job vanished (deleted/expired): stop working on it.
            return True
        cancel_requested_at, status = row
        return cancel_requested_at is not None or status == "cancelled"

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, status, stage, kit, input_kind, params, cancel_requested_at "
                "FROM jobs WHERE id = %s",
                [job_id],
            ).fetchone()
        if row is None:
            return None
        keys = ("id", "status", "stage", "kit", "input_kind", "params", "cancel_requested_at")
        job = dict(zip(keys, row))
        if isinstance(job.get("params"), str):
            import json

            try:
                job["params"] = json.loads(job["params"])
            except ValueError:
                job["params"] = {}
        return job
