"""CSV writer shared by both branches.

The sequence branch serialises a whole result list at once (`app.api.predict.sites_to_csv`);
the signal branch streams millions of read-level rows from SQLite. Both go through
`iter_csv`, so the shared seven `ModSite` columns come out byte-identical.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Sequence

from app.schemas import ModSite

# The shared long format: identical for `source="sequence"` and `source="signal"` rows.
MODSITE_COLUMNS: tuple[str, ...] = (
    "transcript_id",
    "position",
    "mod_type",
    "probability",
    "p_value",
    "coverage",
    "source",
)

# Signal-branch site rows: the shared columns first, then the DirectRM extras.
SIGNAL_SITE_COLUMNS: tuple[str, ...] = MODSITE_COLUMNS + (
    "strand",
    "count",
    "ci_low",
    "ci_high",
    "max_prob",
    "noisyor_prob",
)

SIGNAL_READ_COLUMNS: tuple[str, ...] = (
    "read_id",
    "transcript_id",
    "position",
    "strand",
    "mod_type",
    "probability",
    "source",
)


def cell(value: object) -> object:
    """None -> empty cell; everything else is left to `csv` (numbers via str())."""
    return "" if value is None else value


# Characters that make Excel / LibreOffice evaluate a cell (DDE, =HYPERLINK exfiltration).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def text_cell(value: object) -> object:
    """A user-supplied identifier as an inert spreadsheet cell.

    Transcript ids come from the uploader's FASTA / regions file and read ids from the
    BAM; result pages are public by job id, so a shared download must not carry a
    formula. A leading `'` (the usual neutraliser) keeps the value visible and prevents
    evaluation; ordinary identifiers never start with one of these characters.
    """
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def modsite_cells(site: ModSite) -> list[object]:
    """The seven shared cells of one `ModSite` (or subclass) row, in `MODSITE_COLUMNS` order."""
    return [
        text_cell(site.transcript_id),
        site.position,
        site.mod_type,
        site.probability,
        cell(site.p_value),
        cell(site.coverage),
        site.source,
    ]


def iter_csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> Iterator[str]:
    """Yield CSV text: the header line first, then one line per row (LF line endings)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate()
        writer.writerow(row)
        yield buf.getvalue()


def iter_csv_batched(
    header: Sequence[str], rows: Iterable[Sequence[object]], batch_rows: int = 2000
) -> Iterator[str]:
    """Like `iter_csv` but yields ~`batch_rows` lines per chunk (fewer ASGI messages)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    n = 0
    for row in rows:
        writer.writerow(row)
        n += 1
        if n >= batch_rows:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
            n = 0
    yield buf.getvalue()
