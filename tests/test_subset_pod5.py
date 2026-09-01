"""Tests for ``tools/subset_pod5.py`` on the synthetic signal sample (``app/samples/signal``).

The sample has 3 transcripts with 40 / 36 / 12 reads (``tx_A`` / ``tx_B`` / ``tx_C``); every
read is a single primary alignment on the ``+`` strand spanning its region.  The tests check
the read selection against an independent pysam fetch, that every POD5 field round-trips
unchanged, the subset BAM, the dry run, the exit codes of the validation errors and that a
read overlapping two regions is written once.  Run with ``uv run pytest tests/test_subset_pod5.py -q``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pod5
import pysam
import pytest

# pod5 >= 0.3.47 deprecates the (unused) scaling fields that ReadRecord.to_read() still copies;
# the tool has to copy them to keep the round-trip complete, so silence that one warning.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Call to deprecated function.*Scaling fields:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO / "app" / "samples" / "signal"
TOOL = REPO / "tools" / "subset_pod5.py"
POD5 = SAMPLE_DIR / "sample.pod5"
BAM = SAMPLE_DIR / "sample_sorted.bam"
REGIONS = SAMPLE_DIR / "sample_regions.csv"

EXPECTED_READS = {"tx_A": 40, "tx_B": 36, "tx_C": 12}
# The POD5 container (footer, schema metadata, software name) is not free: allow this much
# on top of the input size when all 88 reads are kept; the signal itself is compared read by
# read via the compressed byte counts.
CONTAINER_OVERHEAD_BYTES = 16 * 1024

HEADER = "seqnames,start,end,width,strand\n"
ROW = {
    "tx_A": "tx_A,60,300,241,+\n",
    "tx_B": "tx_B,80,320,241,+\n",
    "tx_C": "tx_C,50,250,201,+\n",
}


def _load_tool():
    spec = importlib.util.spec_from_file_location("subset_pod5", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


@pytest.fixture(scope="module")
def input_records() -> dict[str, dict]:
    """Every field of every input read, keyed by read id, plus the file order."""
    out: dict[str, dict] = {}
    with pod5.Reader(POD5) as reader:
        for i, rec in enumerate(reader.reads()):
            out[str(rec.read_id)] = _snapshot(rec, i)
    return out


def _snapshot(rec: pod5.ReadRecord, index: int) -> dict:
    return {
        "index": index,
        "signal": np.array(rec.signal, copy=True),
        "byte_count": rec.byte_count,
        "calibration": rec.calibration,
        "run_info": rec.run_info,
        "pore": rec.pore,
        "end_reason": rec.end_reason,
        "read_number": rec.read_number,
        "start_sample": rec.start_sample,
        "median_before": rec.median_before,
        "num_minknow_events": rec.num_minknow_events,
        "tracked_scaling": rec.tracked_scaling,
        "predicted_scaling": rec.predicted_scaling,
        "num_reads_since_mux_change": rec.num_reads_since_mux_change,
        "time_since_mux_change": rec.time_since_mux_change,
    }


def _write_regions(path: Path, *rows: str) -> Path:
    path.write_text(HEADER + "".join(rows))
    return path


def _bam_ids(contig: str, start1: int, end1: int, strand: str = "+") -> set[str]:
    """Parent read ids of the alignments overlapping a 1-based inclusive region (no flank)."""
    with pysam.AlignmentFile(str(BAM), "rb") as bam:
        return {
            (rec.get_tag("pi") if rec.has_tag("pi") else rec.query_name)
            for rec in bam.fetch(contig, start1 - 1, end1)
            if rec.is_reverse == (strand == "-")
        }


def _pod5_ids(path: Path) -> list[str]:
    with pod5.Reader(path) as reader:
        return [str(r) for r in reader.read_ids]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, check=False
    )


def _base_args(out: Path, regions: Path) -> list[str]:
    return ["-i", str(POD5), "-b", str(BAM), "-r", str(regions), "-o", str(out)]


# ----------------------------------------------------------------------------------------
# 1. one region: read set == independent pysam fetch, every field round-trips
# ----------------------------------------------------------------------------------------


def test_single_region_selects_exactly_the_overlapping_reads_and_round_trips(
    tool, tmp_path, input_records, capsys
):
    regions = _write_regions(tmp_path / "txA.csv", ROW["tx_A"])
    out = tmp_path / "txA.pod5"
    assert tool.main(_base_args(out, regions)) == 0
    stdout = capsys.readouterr().out
    assert "selected   : 40 unique read ids" in stdout
    assert "found      : 40 / 40" in stdout

    expected = _bam_ids("tx_A", 60, 300)
    assert len(expected) == EXPECTED_READS["tx_A"]
    got = _pod5_ids(out)
    assert set(got) == expected
    assert len(got) == len(expected)

    # input file order is preserved
    assert got == sorted(got, key=lambda rid: input_records[rid]["index"])

    with pod5.Reader(out) as reader:
        for rec in reader.reads():
            ref = input_records[str(rec.read_id)]
            assert np.array_equal(rec.signal, ref["signal"])
            assert rec.signal.dtype == np.int16
            assert rec.calibration == ref["calibration"]
            assert rec.run_info == ref["run_info"]
            assert rec.pore == ref["pore"]
            assert rec.end_reason == ref["end_reason"]
            for key in (
                "read_number",
                "start_sample",
                "median_before",
                "num_minknow_events",
                "tracked_scaling",
                "predicted_scaling",
                "num_reads_since_mux_change",
                "time_since_mux_change",
            ):
                assert getattr(rec, key) == ref[key], key


# ----------------------------------------------------------------------------------------
# 2. all regions: 88 reads, no size blow-up
# ----------------------------------------------------------------------------------------


def test_all_regions_keep_every_read_without_growing(tool, tmp_path, input_records):
    out = tmp_path / "all.pod5"
    assert tool.main(_base_args(out, REGIONS)) == 0
    ids = _pod5_ids(out)
    assert len(ids) == sum(EXPECTED_READS.values()) == 88
    assert set(ids) == set(input_records)
    assert out.stat().st_size <= POD5.stat().st_size + CONTAINER_OVERHEAD_BYTES
    with pod5.Reader(out) as reader:
        out_bytes = {str(r.read_id): r.byte_count for r in reader.reads()}
    assert sum(out_bytes.values()) == sum(v["byte_count"] for v in input_records.values())


# ----------------------------------------------------------------------------------------
# 3. --bam-out: valid, indexed, same reads, mv tags intact
# ----------------------------------------------------------------------------------------


def test_bam_out_is_indexed_and_keeps_move_tables(tool, tmp_path):
    out = tmp_path / "all.pod5"
    bam_out = tmp_path / "all.bam"
    assert tool.main(_base_args(out, REGIONS) + ["--bam-out", str(bam_out), "--threads", "2"]) == 0
    assert bam_out.is_file() and (tmp_path / "all.bam.bai").is_file()

    with pysam.AlignmentFile(str(BAM), "rb") as src:
        original = {rec.query_name: rec for rec in src.fetch(until_eof=True)}
    with pysam.AlignmentFile(str(bam_out), "rb") as bam:
        assert bam.check_index()
        assert bam.header.to_dict()["HD"]["SO"] == "coordinate"
        assert any(pg.get("PN") == "subset_pod5" for pg in bam.header.to_dict().get("PG", []))
        records = list(bam.fetch(until_eof=True))
    assert len(records) == 88
    assert {r.query_name for r in records} == set(_pod5_ids(out))
    for rec in records:
        ref = original[rec.query_name]
        assert rec.get_tag("mv") == ref.get_tag("mv")
        assert rec.get_tag("ts") == ref.get_tag("ts")
        assert rec.get_tag("ns") == ref.get_tag("ns")
        assert rec.cigarstring == ref.cigarstring
        assert rec.query_sequence == ref.query_sequence
    # coordinate order, as the .bai requires
    keys = [(r.reference_id, r.reference_start) for r in records]
    assert keys == sorted(keys)


# ----------------------------------------------------------------------------------------
# 4. --dry-run writes nothing and prints the counts
# ----------------------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_prints_counts(tool, tmp_path, capsys):
    out = tmp_path / "dry.pod5"
    bam_out = tmp_path / "dry.bam"
    rc = tool.main(_base_args(out, REGIONS) + ["--bam-out", str(bam_out), "--dry-run"])
    assert rc == 0
    assert not out.exists() and not bam_out.exists()
    assert list(tmp_path.iterdir()) == []
    stdout = capsys.readouterr().out
    assert "selected   : 88 unique read ids (88 alignment records)" in stdout
    assert "found      : 88 / 88 read ids in pod5" in stdout
    assert "estimate   : ~" in stdout
    assert "dry run    : nothing written" in stdout
    for tx, n in EXPECTED_READS.items():
        assert f"{tx}:" in stdout and f"reads {n:>8}" in stdout


# ----------------------------------------------------------------------------------------
# 5. validation errors -> exit code 2 with a message on stderr
# ----------------------------------------------------------------------------------------


def test_unknown_seqname_exits_2(tmp_path):
    regions = _write_regions(tmp_path / "bad.csv", "chrX,60,300,241,+\n")
    proc = _run_cli(*_base_args(tmp_path / "x.pod5", regions))
    assert proc.returncode == 2
    assert "seqname 'chrX' is not a contig of the BAM" in proc.stderr
    assert not (tmp_path / "x.pod5").exists()


def test_missing_column_exits_2(tmp_path):
    regions = tmp_path / "cols.csv"
    regions.write_text("seqnames,start,strand\ntx_A,60,+\n")
    proc = _run_cli(*_base_args(tmp_path / "x.pod5", regions))
    assert proc.returncode == 2
    assert "missing required column(s) end" in proc.stderr


def test_no_overlapping_reads_exits_2(tool, tmp_path, capsys):
    # nothing is aligned to the '-' strand in the sample
    regions = _write_regions(tmp_path / "minus.csv", "tx_A,60,300,241,-\n")
    assert tool.main(_base_args(tmp_path / "x.pod5", regions)) == 2
    assert "no alignments overlap any region" in capsys.readouterr().err
    assert not (tmp_path / "x.pod5").exists()


def test_bad_coordinates_and_strand_exit_2(tool, tmp_path, capsys):
    regions = _write_regions(tmp_path / "coords.csv", "tx_A,300,60,241,+\n")
    assert tool.main(_base_args(tmp_path / "x.pod5", regions)) == 2
    assert "1 <= start <= end" in capsys.readouterr().err
    regions = _write_regions(tmp_path / "strand.csv", "tx_A,60,300,241,*\n")
    assert tool.main(_base_args(tmp_path / "x.pod5", regions)) == 2
    assert "strand must be '+' or '-'" in capsys.readouterr().err


def test_existing_output_needs_force(tool, tmp_path, capsys):
    out = tmp_path / "exists.pod5"
    out.write_bytes(b"not a pod5")
    assert tool.main(_base_args(out, REGIONS)) == 2
    assert "use --force" in capsys.readouterr().err
    assert out.read_bytes() == b"not a pod5"
    assert tool.main(_base_args(out, REGIONS) + ["--force"]) == 0
    assert len(_pod5_ids(out)) == 88


# ----------------------------------------------------------------------------------------
# 6. a read overlapping two regions is written once
# ----------------------------------------------------------------------------------------


def test_read_overlapping_two_regions_is_written_once(tool, tmp_path, capsys):
    regions = _write_regions(tmp_path / "overlap.csv", "tx_A,60,150,91,+\n", "tx_A,140,300,161,+\n")
    out = tmp_path / "ov.pod5"
    bam_out = tmp_path / "ov.bam"
    assert tool.main(_base_args(out, regions) + ["--bam-out", str(bam_out)]) == 0
    stdout = capsys.readouterr().out
    assert "selected   : 40 unique read ids (80 alignment records)" in stdout
    ids = _pod5_ids(out)
    assert len(ids) == 40 and len(set(ids)) == 40
    assert set(ids) == _bam_ids("tx_A", 60, 300)
    with pysam.AlignmentFile(str(bam_out), "rb") as bam:
        names = [r.query_name for r in bam.fetch(until_eof=True)]
    assert len(names) == 40 and len(set(names)) == 40


# ----------------------------------------------------------------------------------------
# helpers: flank / window arithmetic
# ----------------------------------------------------------------------------------------


def test_flank_widens_windows_and_clamps_to_the_contig(tool):
    region = tool.Region("tx_A", 60, 300, "+", 2)
    assert tool.region_window(region, 560, 20) == (39, 320)
    assert tool.region_window(region, 560, 0) == (59, 300)
    edge = tool.Region("tx_A", 1, 560, "+", 2)
    assert tool.region_window(edge, 560, 20) == (0, 560)
    stats = [
        tool.RegionStats(tool.Region("tx_A", 60, 150, "+", 2), 39, 170),
        tool.RegionStats(tool.Region("tx_A", 140, 300, "+", 3), 119, 320),
        tool.RegionStats(tool.Region("tx_A", 400, 450, "+", 4), 379, 470),
        tool.RegionStats(tool.Region("tx_A", 60, 150, "-", 5), 39, 170),
    ]
    assert tool.merged_windows(stats) == [
        ("tx_A", "+", 39, 320),
        ("tx_A", "+", 379, 470),
        ("tx_A", "-", 39, 170),
    ]


def test_flank_zero_still_selects_every_sample_read(tool, tmp_path):
    out = tmp_path / "noflank.pod5"
    assert tool.main(_base_args(out, REGIONS) + ["--flank", "0"]) == 0
    assert len(_pod5_ids(out)) == 88
