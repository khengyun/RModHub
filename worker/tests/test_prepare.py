"""Stage ``preparing``: every user-facing input check, exercised on variants of the sample BAM."""

from __future__ import annotations

import csv
import uuid
from pathlib import Path

import pysam
import pytest
from conftest import stage_sample_inputs

from rmodhub_worker.errors import JobCancelled, SoftTimeLimitExceeded, StageError
from rmodhub_worker.prepare import (
    INPUT_BAI,
    INPUT_BAM,
    INPUT_POD5,
    INPUT_REGIONS,
    NO_MV_MESSAGE,
    NO_OVERLAP_MESSAGE,
    UNSAFE_CONTIG_MESSAGE,
    Region,
    check_pod5,
    find_unsafe_contig_names,
    load_regions,
    prepare_inputs,
    sort_bam,
)

DEFAULTS = {"max_regions": 10000, "min_coverage": 30, "max_coverage": 150, "threads": 1}


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    return stage_sample_inputs(tmp_path / "job")


def rewrite_bam(
    bam_path: Path,
    *,
    edit=None,
    reverse_order: bool = False,
    sort_order: str = "coordinate",
    reindex: bool = True,
) -> None:
    """Rewrite ``bam_path`` in place, applying ``edit(read)`` to every record."""
    tmp = bam_path.with_suffix(".rewrite.bam")
    with pysam.AlignmentFile(str(bam_path), "rb") as src:
        header = src.header.to_dict()
        header.setdefault("HD", {})["SO"] = sort_order
        reads = list(src)
    if reverse_order:
        reads.reverse()
    with pysam.AlignmentFile(str(tmp), "wb", header=header) as dst:
        for read in reads:
            if edit is not None:
                edit(read)
            dst.write(read)
    tmp.replace(bam_path)
    bai = Path(str(bam_path) + ".bai")
    if bai.exists():
        bai.unlink()
    if reindex:
        pysam.index(str(bam_path))


def write_regions(path: Path, rows, header=("seqnames", "start", "end", "width", "strand")):
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


# ----------------------------------------------------------------------------------------------


def test_pristine_sample(job_dir: Path):
    result = prepare_inputs(job_dir, **DEFAULTS)
    assert result.n_reads_pod5 == 88
    assert [r.seqnames for r in result.regions] == ["tx_A", "tx_B", "tx_C"]
    assert result.region_read_counts == [40, 36, 12]
    assert result.regions_skipped_low_coverage == 1
    assert result.regions_subsampled == 0
    assert result.bam_sorted_by_worker is False
    assert result.md_added_by_worker is False
    assert result.n_records_inspected == 88
    assert result.n_overlap_shared == 88
    assert result.reference_lengths == {"tx_A": 560, "tx_B": 516, "tx_C": 579}
    assert result.contig_mapped_reads == {"tx_A": 40, "tx_B": 36, "tx_C": 12}
    assert result.transcripts() == [("tx_A", 560, 40), ("tx_B", 516, 36), ("tx_C", 579, 12)]
    assert (job_dir / "input" / "reference.fa.fai").is_file()
    # regions.csv is rewritten normalised: same content for the already-normal sample file.
    assert (job_dir / "input" / INPUT_REGIONS).read_text().splitlines()[
        0
    ] == "seqnames,start,end,width,strand"
    meta = result.as_meta()
    assert meta["regions_total"] == 3 and meta["n_reads_pod5"] == 88
    # Per-region detail is a results.sqlite table, never a meta value (unbounded with max_regions).
    assert "region_read_counts" not in meta
    assert result.region_rows() == [
        ("tx_A", 60, 300, "+", 40),
        ("tx_B", 80, 320, "+", 36),
        ("tx_C", 50, 250, "+", 12),
    ]


def test_mismatched_pod5_and_bam(job_dir: Path):
    """Swap every query name for a fresh uuid: pod5 and BAM no longer share read ids."""

    def rename(read):
        read.query_name = str(uuid.uuid4())

    rewrite_bam(job_dir / "input" / INPUT_BAM, edit=rename)
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert excinfo.value.user_message == NO_OVERLAP_MESSAGE


def test_bam_without_move_table(job_dir: Path):
    def strip_mv(read):
        read.set_tag("mv", None)

    rewrite_bam(job_dir / "input" / INPUT_BAM, edit=strip_mv)
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert excinfo.value.user_message == NO_MV_MESSAGE
    assert "--emit-moves" in excinfo.value.user_message


def test_unsorted_bam_is_sorted_and_indexed(job_dir: Path):
    bam_path = job_dir / "input" / INPUT_BAM
    rewrite_bam(bam_path, reverse_order=True, sort_order="unsorted", reindex=False)
    assert not (job_dir / "input" / INPUT_BAI).exists()
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        assert bam.header.to_dict()["HD"]["SO"] == "unsorted"

    result = prepare_inputs(job_dir, **DEFAULTS)
    assert result.bam_sorted_by_worker is True
    assert result.bam_indexed_by_worker is True
    assert (job_dir / "input" / INPUT_BAI).is_file()
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        assert bam.header.to_dict()["HD"]["SO"] == "coordinate"
        positions = [(r.reference_id, r.reference_start) for r in bam]
    assert positions == sorted(positions)
    assert result.region_read_counts == [40, 36, 12]


def test_bam_without_md_gets_md_via_calmd(job_dir: Path):
    bam_path = job_dir / "input" / INPUT_BAM
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        original_md = {r.query_name: r.get_tag("MD") for r in bam}

    def strip_md(read):
        read.set_tag("MD", None)
        read.set_tag("NM", None)

    rewrite_bam(bam_path, edit=strip_md)
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        assert not any(r.has_tag("MD") for r in bam)

    result = prepare_inputs(job_dir, **DEFAULTS)
    assert result.md_added_by_worker is True
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        recomputed = {r.query_name: r.get_tag("MD") for r in bam}
        assert all(r.has_tag("mv") for r in bam.fetch(until_eof=True))
    assert (
        recomputed == original_md
    )  # calmd against the same reference reproduces the generator's MD
    assert (job_dir / "input" / INPUT_BAI).is_file()


def test_unknown_region_seqname(job_dir: Path):
    write_regions(
        job_dir / "input" / INPUT_REGIONS,
        [("tx_A", 60, 300, 241, "+"), ("tx_Z", 1, 100, 100, "+")],
    )
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert "tx_Z" in excinfo.value.user_message


def test_too_many_regions(job_dir: Path):
    write_regions(
        job_dir / "input" / INPUT_REGIONS,
        [("tx_A", 60, 300, 241, "+"), ("tx_B", 80, 320, 241, "+"), ("tx_C", 50, 250, 201, "+")],
    )
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **{**DEFAULTS, "max_regions": 2})
    assert "at most 2 regions" in excinfo.value.user_message


@pytest.mark.parametrize(
    "rows,header,fragment",
    [
        ([("tx_A", 300, 60, 241, "+")], None, "smaller than start"),
        ([("tx_A", 0, 60, 61, "+")], None, "start must be >= 1"),
        ([("tx_A", 60, 300, 241, "*")], None, "strand must be"),
        ([("tx_A", "sixty", 300, 241, "+")], None, "must be integers"),
        ([("tx_A", 60, 300)], ("seqnames", "start", "end"), "missing: strand"),
        ([], None, "no data rows"),
    ],
)
def test_bad_regions(tmp_path: Path, rows, header, fragment):
    path = tmp_path / "regions.csv"
    write_regions(path, rows, header=header or ("seqnames", "start", "end", "width", "strand"))
    with pytest.raises(StageError) as excinfo:
        load_regions(path, 10000)
    assert fragment in excinfo.value.user_message


def test_regions_width_is_optional_and_recomputed(tmp_path: Path):
    path = tmp_path / "regions.csv"
    path.write_text("seqnames,start,end,strand\n tx_A , 60 , 300 , + \n\n")
    regions = load_regions(path, 10)
    assert regions == [Region("tx_A", 60, 300, "+")]
    assert regions[0].width == 241


def test_missing_input_file(job_dir: Path):
    (job_dir / "input" / INPUT_POD5).unlink()
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert "pod5" in excinfo.value.user_message


def test_reference_length_mismatch(job_dir: Path):
    ref = job_dir / "input" / "reference.fa"
    records = ref.read_text().split(">")[1:]
    out = []
    for rec in records:
        name, _, seq = rec.partition("\n")
        seq = seq.replace("\n", "")
        if name.split()[0] == "tx_A":
            seq = seq[:-10]
        out.append(f">{name}\n{seq}\n")
    ref.write_text("".join(out))
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert "not aligned to this reference" in excinfo.value.user_message


# ----------------------------------------------------------------------------------------------
# Contig names upstream's pandas would not keep as strings (numeric / boolean / NA-like)
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,kind",
    [
        ("1", "a number"),
        ("-3", "a number"),
        ("+7", "a number"),
        ("1e5", "a number"),
        (".5", "a number"),
        ("1.", "a number"),
        ("inf", "a number"),
        ("Infinity", "a number"),
        ("True", "a boolean"),
        ("false", "a boolean"),
        ("NA", "a missing value"),
        ("NULL", "a missing value"),
        ("nan", "a missing value"),
        ("-NaN", "a missing value"),
        ("null", "a missing value"),
        ("None", "a missing value"),
        ("<NA>", "a missing value"),
        ("#NA", "a missing value"),
        # "N/A", "#N/A", "n/a" are NA strings too, but the earlier "no '/'" check reports them.
    ],
)
def test_contig_names_pandas_would_misread_are_rejected(tmp_path: Path, name: str, kind: str):
    """``pd.read_csv`` turns these into int64/float64/bool/NaN and every upstream stage breaks."""
    path = tmp_path / "regions.csv"
    write_regions(path, [("tx_A", 60, 300, 241, "+"), (name, 1, 100, 100, "+")])
    with pytest.raises(StageError) as excinfo:
        load_regions(path, 10)
    assert excinfo.value.user_message == UNSAFE_CONTIG_MESSAGE.format(index=2, name=name, kind=kind)
    assert "chr" in excinfo.value.user_message


def test_contig_names_pandas_keeps_as_strings_are_accepted():
    names = [
        "chr1",
        "1X",
        "X",
        "MT",
        "tx_A",
        "ENST00000456328",
        "Null",  # only pandas' exact NA spellings are NA
        "1_000",
        "0x10",
        "1,000",
        "9999999999999999999999",  # overflows int64 -> stays a string
        "e5",
        "--1",
    ]
    assert find_unsafe_contig_names(names) == {}


def test_unsafe_contig_verdict_is_per_name_not_per_column():
    """A mixed column (``1`` + ``X``) stays object dtype in regions.csv, but any subset of the
    regions can end up alone in a later per-k-mer CSV, so the rule is per name."""
    assert find_unsafe_contig_names(["1", "X"]) == {"1": "a number"}
    assert find_unsafe_contig_names([]) == {}


def test_unsafe_contig_name_is_reported_before_the_bam_header_check(job_dir: Path):
    write_regions(job_dir / "input" / INPUT_REGIONS, [("1", 60, 300, 241, "+")])
    with pytest.raises(StageError) as excinfo:
        prepare_inputs(job_dir, **DEFAULTS)
    assert excinfo.value.user_message == UNSAFE_CONTIG_MESSAGE.format(
        index=1, name="1", kind="a number"
    )


# ----------------------------------------------------------------------------------------------
# Interruptions (SIGTERM -> JobCancelled, Celery soft time limit) inside htslib / pod5 calls
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("interrupt", [JobCancelled("SIGTERM"), SoftTimeLimitExceeded()])
def test_interrupts_inside_c_calls_are_not_relabelled_as_stage_errors(
    job_dir: Path, monkeypatch, interrupt: BaseException
):
    """Python delivers a signal when a long ``pysam.sort`` / ``pod5`` call returns, i.e. inside
    the ``try`` whose ``except Exception`` used to turn a cancel into "The BAM could not be
    coordinate-sorted" (and ``failed`` over the API's ``cancelled``)."""

    def interrupted(*args, **kwargs):
        raise interrupt

    monkeypatch.setattr(pysam, "sort", interrupted)
    with pytest.raises(type(interrupt)):
        sort_bam(job_dir / "input" / INPUT_BAM, 1, job_dir / "work")

    import pod5

    monkeypatch.setattr(pod5, "DatasetReader", interrupted)
    with pytest.raises(type(interrupt)):
        check_pod5(job_dir / "input" / INPUT_POD5)
