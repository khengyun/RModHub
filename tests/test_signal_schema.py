"""`SignalSite` is a `ModSite`; both branches share one CSV writer.

Pure schema / serialisation tests: no app, no database, no model.
"""

from __future__ import annotations

import csv
import io
import sqlite3

import pytest
from pydantic import ValidationError

from app.api.predict import CSV_COLUMNS, sites_to_csv
from app.csvio import (
    MODSITE_COLUMNS,
    SIGNAL_READ_COLUMNS,
    SIGNAL_SITE_COLUMNS,
    iter_csv,
    iter_csv_batched,
    modsite_cells,
)
from app.jobs.constants import SIGNAL_MOD_TYPES
from app.jobs.results import read_row_cells, site_row_cells
from app.jobs.schemas import SignalRead, SignalSite
from app.predictors.stub import StubPredictor
from app.schemas import ModSite

CSV_HEADER = "transcript_id,position,mod_type,probability,p_value,coverage,source"


def _site(**overrides) -> SignalSite:
    base = {
        "transcript_id": "tx_A",
        "position": 42,
        "mod_type": "m6A",
        "probability": 0.5,
        "coverage": 40,
        "strand": "+",
        "count": 20,
        "ci_low": 0.35,
        "ci_high": 0.65,
        "max_prob": 0.97,
        "noisyor_prob": 0.999,
    }
    base.update(overrides)
    return SignalSite(**base)


# ----------------------------------------------------------------------------- SignalSite


def test_signal_site_is_a_modsite():
    site = _site()
    assert isinstance(site, ModSite)
    assert site.source == "signal"
    assert site.p_value is None


def test_signal_site_round_trips_as_plain_modsite():
    dumped = _site().model_dump()
    plain = ModSite.model_validate(dumped)  # extras are ignored by ModSite
    assert plain.model_dump() == {
        "transcript_id": "tx_A",
        "position": 42,
        "mod_type": "m6A",
        "probability": 0.5,
        "p_value": None,
        "coverage": 40,
        "source": "signal",
    }
    # and back: the shared seven fields are enough to rebuild the ModSite part of a SignalSite
    again = SignalSite.model_validate({**dumped})
    assert again == _site()


def test_signal_site_shared_fields_come_first():
    keys = list(_site().model_dump())
    assert tuple(keys[: len(MODSITE_COLUMNS)]) == MODSITE_COLUMNS
    assert tuple(keys) == SIGNAL_SITE_COLUMNS
    json_keys = list(_site().model_dump(mode="json"))
    assert tuple(json_keys[:7]) == MODSITE_COLUMNS


def test_signal_site_requires_its_extras_and_signal_source():
    with pytest.raises(ValidationError):
        SignalSite(transcript_id="tx", position=1, mod_type="m6A", probability=0.5, coverage=10)
    with pytest.raises(ValidationError):
        _site(source="sequence")
    with pytest.raises(ValidationError):
        _site(ci_low=1.5)


def test_signal_mod_vocabulary_is_the_shared_one():
    assert SIGNAL_MOD_TYPES == ("ac4C", "m1A", "m5C", "m6A", "m7G", "Psi")
    for mod in SIGNAL_MOD_TYPES:
        assert _site(mod_type=mod).mod_type == mod


def test_signal_read_field_order_matches_csv_columns():
    read = SignalRead(
        read_id="r1", transcript_id="tx_A", position=42, strand="+", mod_type="m6A", probability=0.9
    )
    assert tuple(read.model_dump()) == SIGNAL_READ_COLUMNS
    assert read.source == "signal"


# ------------------------------------------------------------------------ shared CSV writer


def test_shared_columns_are_the_sequence_branch_columns():
    assert CSV_COLUMNS == MODSITE_COLUMNS
    assert ",".join(CSV_COLUMNS) == CSV_HEADER
    assert SIGNAL_SITE_COLUMNS[:7] == MODSITE_COLUMNS
    assert SIGNAL_SITE_COLUMNS[7:] == ("strand", "count", "ci_low", "ci_high", "max_prob", "noisyor_prob")


def test_sites_to_csv_header_is_byte_identical():
    text = sites_to_csv([])
    assert text == CSV_HEADER + "\n"
    assert "\r" not in text


def test_sites_to_csv_accepts_signal_sites_unchanged():
    text = sites_to_csv([_site(), _site(position=43, mod_type="Psi", transcript_id=None)])
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == list(CSV_COLUMNS)
    assert rows[0] == {
        "transcript_id": "tx_A",
        "position": "42",
        "mod_type": "m6A",
        "probability": "0.5",
        "p_value": "",
        "coverage": "40",
        "source": "signal",
    }
    assert rows[1]["transcript_id"] == ""  # None -> empty cell, as for the sequence branch


def test_iter_csv_matches_sites_to_csv_for_stub_output():
    sites = StubPredictor().predict("ACGT" * 40, alpha=0.05).sites
    assert sites, "stub returned no sites"
    streamed = "".join(iter_csv(CSV_COLUMNS, (modsite_cells(s) for s in sites)))
    batched = "".join(iter_csv_batched(CSV_COLUMNS, (modsite_cells(s) for s in sites), batch_rows=2))
    assert streamed == sites_to_csv(sites) == batched
    assert streamed.splitlines()[0] == CSV_HEADER


def _sqlite_rows() -> tuple[sqlite3.Row, sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sites (id INTEGER PRIMARY KEY, transcript_id TEXT, position INTEGER,
          strand TEXT, mod_type TEXT, rate REAL, ci_low REAL, ci_high REAL, coverage INTEGER,
          count INTEGER, max_prob REAL, noisyor_prob REAL);
        CREATE TABLE reads (id INTEGER PRIMARY KEY, read_id TEXT, transcript_id TEXT,
          position INTEGER, strand TEXT, mod_type TEXT, probability REAL);
        INSERT INTO sites VALUES (1, 'tx_A', 42, '+', 'm6A', 0.5, 0.35, 0.65, 40, 20, 0.97, 0.999);
        INSERT INTO reads VALUES (1, 'r1', 'tx_A', 42, '+', 'm6A', 0.9);
        """
    )
    site = conn.execute("SELECT * FROM sites").fetchone()
    read = conn.execute("SELECT * FROM reads").fetchone()
    return site, read


def test_sqlite_site_row_cells_share_the_modsite_prefix():
    site_row, read_row = _sqlite_rows()
    cells = site_row_cells(site_row)
    assert len(cells) == len(SIGNAL_SITE_COLUMNS)
    assert cells[:7] == modsite_cells(_site())
    assert cells[7:] == ["+", 20, 0.35, 0.65, 0.97, 0.999]
    read_cells = read_row_cells(read_row)
    assert len(read_cells) == len(SIGNAL_READ_COLUMNS)
    assert read_cells == ["r1", "tx_A", 42, "+", "m6A", 0.9, "signal"]


def test_site_csv_line_parses_back_to_the_same_signal_site():
    site_row, _ = _sqlite_rows()
    text = "".join(iter_csv(SIGNAL_SITE_COLUMNS, [site_row_cells(site_row)]))
    (row,) = csv.DictReader(io.StringIO(text))
    row = {k: (None if v == "" else v) for k, v in row.items()}
    assert SignalSite.model_validate(row) == _site()
