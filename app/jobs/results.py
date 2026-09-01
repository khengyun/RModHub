"""Read-only access to a job's `results.sqlite` (docs/signal-branch.md sections 5 and 6).

The worker publishes the file atomically and never modifies it afterwards, so the API opens
it in SQLite URI read-only mode. Paging, filtering and sorting run inside SQLite (indexes on
(transcript_id, position), mod_type and coverage); CSV downloads stream in id order, which
is the canonical (transcript_id, position, mod_type) order by construction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.csvio import SIGNAL_READ_COLUMNS, SIGNAL_SITE_COLUMNS, cell, iter_csv_batched, text_cell
from app.jobs.models import Job
from app.jobs.schemas import (
    SignalRead,
    SignalResultsMeta,
    SignalSite,
    TranscriptInfo,
)

CSV_CHUNK_ROWS = 5000
MAX_LIMIT = 1000
STRANDS = ("+", "-")

# `sort=position` is the canonical order (rows of one transcript contiguous, then position,
# then modification type, then insertion id) or, with `order=desc`, its exact reverse. Both
# walk the (transcript_id, position, mod_type) index; the `reads` table of a real job has
# 10^7-10^8 rows and no index on `probability`, so an unscoped sort by another key would be
# a full-table sort on every page request.
CANONICAL_KEYS = ("transcript_id", "position", "mod_type", "id")
SITE_SORT = {"rate": "rate", "coverage": "coverage", "mod_type": "mod_type"}
READ_SORT = {"rate": "probability", "mod_type": "mod_type"}


def open_results(path: Path) -> sqlite3.Connection:
    """Open `path` read-only (`mode=ro`); raises FileNotFoundError if it is not there."""
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class ResultFilters:
    transcript_id: str | None = None
    mod_type: str | None = None
    position: int | None = None
    strand: str | None = None
    min_coverage: int | None = None

    def where(self, level: str) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if self.transcript_id is not None:
            clauses.append("transcript_id = ?")
            params.append(self.transcript_id)
        if self.mod_type is not None:
            clauses.append("mod_type = ?")
            params.append(self.mod_type)
        if self.position is not None:
            clauses.append("position = ?")
            params.append(self.position)
        if self.strand is not None:  # both tables carry the strand
            clauses.append("strand = ?")
            params.append(self.strand)
        if self.min_coverage is not None and level == "site":
            clauses.append("coverage >= ?")
            params.append(self.min_coverage)
        sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return sql, params


def _table(level: str) -> str:
    return "sites" if level == "site" else "reads"


def count_rows(conn: sqlite3.Connection, level: str, filters: ResultFilters) -> int:
    where, params = filters.where(level)
    row = conn.execute(f"SELECT COUNT(*) FROM {_table(level)}{where}", params).fetchone()
    return int(row[0])


def check_sort(level: str, sort: str, filters: ResultFilters) -> str | None:
    """One sentence when `sort` cannot be served for this request, else None.

    Read-level rows are only sortable by rate / modification type within one site
    (`transcript_id` + `position`, the drill-down of section 6): anything wider would sort
    millions of rows in a temp b-tree for every page. `coverage` is a site-level column.
    """
    if level == "site" or sort == "position":
        return None
    if sort not in READ_SORT:
        return f"sort={sort} applies to site-level rows only; read-level rows have no coverage."
    if filters.transcript_id is None or filters.position is None:
        return (
            f"Read-level rows can be sorted by {sort} only within one site; pass "
            "transcript_id and position (or use the default position order)."
        )
    return None


def _order(level: str, sort: str, order: str) -> str:
    direction = "DESC" if order == "desc" else "ASC"
    column = (SITE_SORT if level == "site" else READ_SORT).get(sort)
    if column is None:  # position (default) or a key this level does not have
        return " ORDER BY " + ", ".join(f"{key} {direction}" for key in CANONICAL_KEYS)
    ties = ", ".join(f"{key} ASC" for key in CANONICAL_KEYS)
    return f" ORDER BY {column} {direction}, {ties}"


def site_from_row(row: sqlite3.Row) -> SignalSite:
    return SignalSite(
        transcript_id=row["transcript_id"],
        position=row["position"],
        mod_type=row["mod_type"],
        probability=row["rate"],
        p_value=None,
        coverage=row["coverage"],
        source="signal",
        strand=row["strand"],
        count=row["count"],
        ci_low=row["ci_low"],
        ci_high=row["ci_high"],
        max_prob=row["max_prob"],
        noisyor_prob=row["noisyor_prob"],
    )


def read_from_row(row: sqlite3.Row) -> SignalRead:
    return SignalRead(
        read_id=row["read_id"],
        transcript_id=row["transcript_id"],
        position=row["position"],
        strand=row["strand"],
        mod_type=row["mod_type"],
        probability=row["probability"],
        source="signal",
    )


def fetch_page(
    conn: sqlite3.Connection,
    level: str,
    filters: ResultFilters,
    *,
    sort: str,
    order: str,
    offset: int,
    limit: int,
) -> list[SignalSite | SignalRead]:
    where, params = filters.where(level)
    sql = f"SELECT * FROM {_table(level)}{where}{_order(level, sort, order)} LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    convert = site_from_row if level == "site" else read_from_row
    return [convert(r) for r in rows]


def read_meta(conn: sqlite3.Connection) -> dict:
    """The `meta` table as a dict; values are JSON-decoded when possible."""
    out: dict = {}
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        return out
    for key, value in rows:
        try:
            out[key] = json.loads(value) if value is not None else None
        except (TypeError, ValueError):
            out[key] = value
    return out


def read_transcripts(conn: sqlite3.Connection) -> list[TranscriptInfo]:
    try:
        rows = conn.execute(
            "SELECT transcript_id, length, n_reads, n_sites FROM transcripts ORDER BY transcript_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        TranscriptInfo(
            transcript_id=r["transcript_id"], length=r["length"], n_reads=r["n_reads"], n_sites=r["n_sites"]
        )
        for r in rows
    ]


def results_meta(job: Job, conn: sqlite3.Connection) -> SignalResultsMeta:
    extra = read_meta(conn)
    transcripts = read_transcripts(conn)
    n_sites = job.n_sites
    if n_sites is None:
        n_sites = count_rows(conn, "site", ResultFilters())
    n_transcripts = job.n_transcripts if job.n_transcripts is not None else len(transcripts)
    n_reads = job.n_reads
    if n_reads is None and isinstance(extra.get("n_reads_features"), int):
        n_reads = extra["n_reads_features"]
    return SignalResultsMeta(
        job_id=job.id,
        model_name=job.model_name,
        model_version=job.model_version,
        kit=job.kit,
        n_sites=n_sites,
        n_reads=n_reads,
        n_transcripts=n_transcripts,
        transcripts=transcripts,
        extra=extra,
    )


# --------------------------------------------------------------------------- CSV export


def site_row_cells(row: sqlite3.Row) -> list[object]:
    """Cells of one `sites` row in `SIGNAL_SITE_COLUMNS` order (shared seven first)."""
    return [
        text_cell(row["transcript_id"]),
        row["position"],
        row["mod_type"],
        row["rate"],
        "",  # p_value is always null for the signal branch
        row["coverage"],
        "signal",
        row["strand"],
        row["count"],
        row["ci_low"],
        row["ci_high"],
        cell(row["max_prob"]),
        cell(row["noisyor_prob"]),
    ]


def read_row_cells(row: sqlite3.Row) -> list[object]:
    return [
        text_cell(row["read_id"]),
        text_cell(row["transcript_id"]),
        row["position"],
        row["strand"],
        row["mod_type"],
        row["probability"],
        "signal",
    ]


def _iter_rows(conn: sqlite3.Connection, table: str, chunk: int = CSV_CHUNK_ROWS) -> Iterator[sqlite3.Row]:
    """Keyset-paged walk over `table` in id order (no OFFSET cost on large tables)."""
    last = 0
    while True:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (last, chunk)
        ).fetchall()
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]


def csv_stream(path: Path, level: str) -> Iterator[str]:
    """Generator of CSV text for `StreamingResponse`; opens and closes its own connection."""
    conn = open_results(path)
    try:
        if level == "site":
            rows = (site_row_cells(r) for r in _iter_rows(conn, "sites"))
            yield from iter_csv_batched(SIGNAL_SITE_COLUMNS, rows)
        else:
            rows = (read_row_cells(r) for r in _iter_rows(conn, "reads"))
            yield from iter_csv_batched(SIGNAL_READ_COLUMNS, rows)
    finally:
        conn.close()


def csv_filename(job_id: str, level: str) -> str:
    return f"rmodhub_signal_{job_id}_{level}s.csv"
