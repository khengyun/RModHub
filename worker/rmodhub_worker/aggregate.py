"""Build ``results.sqlite`` (contract section 5) from the upstream ``read2site`` and ``inference`` outputs.

Inputs (all produced by the unmodified DirectRM scripts):

* ``work/sites/<type>.csv`` -- ``seqnames,pos,strand,max_prob,noisyor_prob,count,coverage``
  (one file per type that has at least one site; missing file == no sites for that type).
* ``work/inference/<type>/*.csv`` -- ``read_id,seqnames,pos,strand,<type>``; one file per
  seqname, or ``<file_id>.csv`` buckets plus ``inference/metadata.json`` when a split has more
  than 200 seqnames. Both layouts carry the ``seqnames`` column, so they are read the same way.

The file is written as ``results.sqlite.tmp`` with ``journal_mode = OFF`` / ``synchronous = OFF``
(SQLite never fsyncs), so before ``os.replace`` publishes it the file is fsynced explicitly and
the job directory is fsynced afterwards: a host crash right after ``status = done`` must not
leave a zero-length ``results.sqlite`` behind a row that says ``done``. Rows are sorted by
``(transcript_id, position, mod_type)`` on insert; sorting happens inside SQLite (staging
tables in a separate attached file) so arbitrarily large jobs never need the whole table in
Python memory.

Besides the four contract tables the file carries ``regions`` (one row per ``regions.csv``
line with the read count the ``preparing`` stage measured), so that per-region detail is not
inlined into ``meta`` -- and therefore not into every paginated results response.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from .wilson import wilson_interval

log = logging.getLogger("rmodhub_worker.aggregate")

#: Upstream lower-case ids -> shared vocabulary (same ids as the sequence branch).
MOD_TYPE_MAP = {
    "ac4c": "ac4C",
    "m1a": "m1A",
    "m5c": "m5C",
    "m6a": "m6A",
    "m7g": "m7G",
    "psi": "Psi",
}
UPSTREAM_TYPES: tuple[str, ...] = ("ac4c", "m1a", "m5c", "m6a", "m7g", "psi")
MOD_TYPES: tuple[str, ...] = tuple(MOD_TYPE_MAP[t] for t in UPSTREAM_TYPES)

RESULTS_NAME = "results.sqlite"
BATCH = 20000

SCHEMA_TABLES = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE transcripts (transcript_id TEXT PRIMARY KEY, length INTEGER, n_reads INTEGER, n_sites INTEGER);
CREATE TABLE sites (
  id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL, position INTEGER NOT NULL, strand TEXT NOT NULL,
  mod_type TEXT NOT NULL, rate REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
  coverage INTEGER NOT NULL, count INTEGER NOT NULL, max_prob REAL, noisyor_prob REAL);
CREATE TABLE reads (
  id INTEGER PRIMARY KEY, read_id TEXT NOT NULL, transcript_id TEXT NOT NULL, position INTEGER NOT NULL,
  strand TEXT NOT NULL, mod_type TEXT NOT NULL, probability REAL NOT NULL);
CREATE TABLE regions (
  id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
  strand TEXT NOT NULL, n_reads INTEGER NOT NULL);
"""

SCHEMA_INDEXES = """
CREATE INDEX sites_tx_pos ON sites (transcript_id, position);
CREATE INDEX sites_mod ON sites (mod_type);
CREATE INDEX sites_cov ON sites (coverage);
CREATE INDEX reads_site ON reads (transcript_id, position, mod_type);
"""

STAGING_SCHEMA = """
CREATE TABLE staging.sites_stage (
  transcript_id TEXT, position INTEGER, strand TEXT, mod_type TEXT, rate REAL, ci_low REAL, ci_high REAL,
  coverage INTEGER, count INTEGER, max_prob REAL, noisyor_prob REAL);
CREATE TABLE staging.reads_stage (
  read_id TEXT, transcript_id TEXT, position INTEGER, strand TEXT, mod_type TEXT, probability REAL);
"""


def normalise_mod_type(upstream_id: str) -> str:
    try:
        return MOD_TYPE_MAP[upstream_id.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown DirectRM modification id {upstream_id!r}") from exc


def _float(value: str) -> float:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"non-finite value {value!r}")
    return number


def _int(value: str) -> int:
    # read2site writes integers, but pandas may emit ``12.0`` after a mixed-dtype concat.
    return int(float(value))


def iter_site_rows(sites_dir: Path) -> Iterator[tuple[Any, ...]]:
    """Yield ``sites`` rows (without ``id``) from ``<sites_dir>/<type>.csv``."""
    for upstream_type in UPSTREAM_TYPES:
        path = Path(sites_dir) / f"{upstream_type}.csv"
        if not path.is_file():
            continue
        mod_type = MOD_TYPE_MAP[upstream_type]
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                coverage = _int(row["coverage"])
                count = _int(row["count"])
                if coverage <= 0:
                    continue
                count = min(count, coverage)
                rate = count / coverage
                low, high = wilson_interval(count, coverage)
                yield (
                    row["seqnames"],
                    _int(row["pos"]),
                    row["strand"],
                    mod_type,
                    rate,
                    low,
                    high,
                    coverage,
                    count,
                    _float(row["max_prob"]),
                    _float(row["noisyor_prob"]),
                )


def iter_read_rows(inference_dir: Path) -> Iterator[tuple[Any, ...]]:
    """Yield ``reads`` rows (without ``id``) from ``<inference_dir>/<type>/*.csv``."""
    for upstream_type in UPSTREAM_TYPES:
        type_dir = Path(inference_dir) / upstream_type
        if not type_dir.is_dir():
            continue
        mod_type = MOD_TYPE_MAP[upstream_type]
        for path in sorted(type_dir.glob("*.csv")):
            with path.open(newline="") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None or upstream_type not in reader.fieldnames:
                    continue
                for row in reader:
                    raw = row[upstream_type]
                    if raw in ("", "nan", "NaN"):
                        continue
                    yield (
                        row["read_id"],
                        row["seqnames"],
                        _int(row["pos"]),
                        row["strand"],
                        mod_type,
                        _float(raw),
                    )


def _batched(rows: Iterable[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _fsync_file(path: Path) -> None:
    """Flush ``path``'s data blocks to disk (the writer ran with ``synchronous = OFF``)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Make the rename in ``path`` durable; best effort (some filesystems refuse it on a dir)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        log.warning("could not open %s for fsync: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.warning("could not fsync directory %s: %s", path, exc)
    finally:
        os.close(fd)


def build_results(
    job_dir: Path,
    *,
    meta: dict[str, Any],
    transcripts: Sequence[tuple[str, int, int]],
    sites_dir: Path,
    inference_dir: Path,
    regions: Sequence[tuple[str, int, int, str, int]] = (),
) -> tuple[int, int, int]:
    """Write ``<job_dir>/results.sqlite`` atomically and durably.

    ``transcripts`` is ``[(transcript_id, length, n_reads), ...]``; ``regions`` is
    ``[(transcript_id, start, end, strand, n_reads), ...]`` in ``regions.csv`` order. Returns
    ``(n_sites, n_read_rows, n_transcripts)``.
    """
    job_dir = Path(job_dir)
    final_path = job_dir / RESULTS_NAME
    tmp_path = job_dir / (RESULTS_NAME + ".tmp")
    staging_path = job_dir / "results.staging.sqlite"
    for stale in (tmp_path, staging_path):
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = FILE")
        conn.executescript(SCHEMA_TABLES)
        conn.execute("ATTACH DATABASE ? AS staging", (str(staging_path),))
        conn.executescript(STAGING_SCHEMA)

        for batch in _batched(iter_site_rows(sites_dir), BATCH):
            conn.executemany(
                "INSERT INTO staging.sites_stage VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )
        conn.execute(
            "INSERT INTO sites (transcript_id, position, strand, mod_type, rate, ci_low, ci_high, "
            "coverage, count, max_prob, noisyor_prob) "
            "SELECT transcript_id, position, strand, mod_type, rate, ci_low, ci_high, coverage, "
            "count, max_prob, noisyor_prob FROM staging.sites_stage "
            "ORDER BY transcript_id, position, mod_type, strand"
        )

        for batch in _batched(iter_read_rows(inference_dir), BATCH):
            conn.executemany("INSERT INTO staging.reads_stage VALUES (?,?,?,?,?,?)", batch)
        conn.execute(
            "INSERT INTO reads (read_id, transcript_id, position, strand, mod_type, probability) "
            "SELECT read_id, transcript_id, position, strand, mod_type, probability "
            "FROM staging.reads_stage ORDER BY transcript_id, position, mod_type, read_id"
        )
        conn.commit()
        conn.execute("DETACH DATABASE staging")
        conn.executescript(SCHEMA_INDEXES)

        for transcript_id, length, n_reads in sorted(transcripts):
            n_sites_tx = conn.execute(
                "SELECT COUNT(*) FROM sites WHERE transcript_id = ?", (transcript_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO transcripts (transcript_id, length, n_reads, n_sites) VALUES (?,?,?,?)",
                (transcript_id, int(length), int(n_reads), int(n_sites_tx)),
            )

        n_sites = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        n_read_rows = conn.execute("SELECT COUNT(*) FROM reads").fetchone()[0]
        n_transcripts = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]

        conn.executemany(
            "INSERT INTO regions (transcript_id, start, end, strand, n_reads) VALUES (?,?,?,?,?)",
            [
                (str(transcript_id), int(start), int(end), str(strand), int(n_reads))
                for transcript_id, start, end, strand, n_reads in regions
            ],
        )
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in meta.items()],
        )
        conn.commit()
    finally:
        conn.close()
        if staging_path.exists():
            staging_path.unlink()

    _fsync_file(tmp_path)
    os.replace(tmp_path, final_path)
    _fsync_dir(job_dir)
    return int(n_sites), int(n_read_rows), int(n_transcripts)
