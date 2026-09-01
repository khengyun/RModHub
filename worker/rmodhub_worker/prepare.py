"""Stage ``preparing``: validate and normalise the four inputs before any upstream script runs.

Upstream DirectRM has no input checks at all (a missing ``mv`` tag or a mismatched pod5 just
produces "0 reads" or a crash three stages later), so everything a user can get wrong is caught
here with a one-sentence message. Checks, in order:

1. all four files exist (``input.pod5``, ``input_sorted.bam``, ``reference.fa``, ``regions.csv``);
2. ``regions.csv``: required columns ``seqnames,start,end,strand`` (``width`` optional and
   recomputed), at most ``max_regions`` data rows, ``1 <= start <= end``, strand ``+``/``-``;
   contig names that upstream's ``pd.read_csv`` would turn into a number, a boolean or NaN
   (``1``, ``1e5``, ``True``, ``NA``, ``nan``, ``null``, ...) are rejected, because every later
   stage passes the parsed value to ``pysam.fetch`` or builds a file path from it;
   rewritten normalised as ``seqnames,start,end,width,strand`` for upstream's ``pd.read_csv``;
3. pod5 opens (``pod5.DatasetReader``) and has reads; the read ids are kept for step 8;
4. reference: ``pysam.faidx`` (creates ``reference.fa.fai``);
5. BAM opens, has ``@SQ`` lines, is coordinate-sorted (else ``pysam.sort`` into place) and
   indexed (``pysam.index`` -> ``input_sorted.bam.bai``);
6. the first 500 mapped primary records must all carry ``mv``; if any lacks ``MD`` the BAM is
   rewritten with ``pysam.calmd`` against the reference and re-indexed;
7. every region contig exists in the BAM header and the FASTA with the same length;
8. reads overlapping the regions (Remora ``fetch`` semantics: every alignment record on the
   requested strand) are counted per region -- this reproduces ``sampling.py``'s skip / subsample
   decisions -- and their names must intersect the pod5 read ids.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import INTERRUPTS, StageError

INPUT_POD5 = "input.pod5"
INPUT_BAM = "input_sorted.bam"
INPUT_BAI = "input_sorted.bam.bai"
INPUT_REFERENCE = "reference.fa"
INPUT_REGIONS = "regions.csv"

REGION_COLUMNS = ("seqnames", "start", "end", "width", "strand")
REQUIRED_REGION_COLUMNS = ("seqnames", "start", "end", "strand")
INSPECT_RECORDS = 500
#: Per-region alignment records counted before giving up (only the ``<= min`` / ``>= max``
#: decisions matter; exact counts far above ``max_coverage`` are not worth the time).
REGION_COUNT_CAP = 50000
OVERLAP_MAX_NAMES = 50000

NO_MV_MESSAGE = (
    "The BAM has no move table (mv tag). Re-run dorado basecaller with --emit-moves "
    "(and --reference) and upload the new BAM."
)
NO_OVERLAP_MESSAGE = (
    "The pod5 and BAM do not share any read IDs: the BAM was not basecalled from this pod5 "
    "(or the pod5 was subset with the wrong regions)."
)
UNSAFE_CONTIG_MESSAGE = (
    "Region {index}: the contig name '{name}' is read as {kind} rather than a name by DirectRM's "
    "CSV reader; rename the contig in the reference FASTA, the BAM and the regions file "
    "(for example by prefixing it with 'chr')."
)
#: lib-pod5 raises ``Schema field '<x>' is incorrect type`` for a file written by a newer pod5
#: that changed the read-table schema (as 0.3.46 did with the 32-bit ``channel`` column, POD5 v6).
#: The worker's lib-pod5 (0.3.47) reads v6; this message is for whatever comes after it.
NEWER_POD5_MESSAGE = (
    "The pod5 file uses a newer POD5 format than this server can read (its reader is "
    "lib-pod5 {version}); please re-write it with pod5 {version} or older (pod5 convert / "
    "pod5 subset) or contact the maintainers."
)

log = logging.getLogger("rmodhub_worker.prepare")


@dataclass(frozen=True)
class Region:
    seqnames: str
    start: int
    end: int
    strand: str

    @property
    def width(self) -> int:
        return self.end - self.start + 1


@dataclass
class PrepareResult:
    n_reads_pod5: int
    regions: list[Region]
    region_read_counts: list[int]
    regions_skipped_low_coverage: int
    regions_subsampled: int
    bam_sorted_by_worker: bool
    bam_indexed_by_worker: bool
    md_added_by_worker: bool
    n_records_inspected: int
    n_overlap_checked: int
    n_overlap_shared: int
    reference_lengths: dict[str, int] = field(default_factory=dict)
    contig_mapped_reads: dict[str, int] = field(default_factory=dict)

    @property
    def regions_total(self) -> int:
        return len(self.regions)

    def transcripts(self) -> list[tuple[str, int, int]]:
        """``(transcript_id, length, mapped reads)`` for every contig named in the regions."""
        names = sorted({r.seqnames for r in self.regions})
        return [
            (name, self.reference_lengths.get(name, 0), self.contig_mapped_reads.get(name, 0))
            for name in names
        ]

    def region_rows(self) -> list[tuple[str, int, int, str, int]]:
        """``(transcript_id, start, end, strand, n_reads)`` per region, in ``regions.csv`` order.

        Stored in the ``regions`` table of ``results.sqlite`` (one row per region, so it is
        never inlined into ``meta`` -- 10,000 regions would be ~1 MB on every results page).
        """
        return [
            (r.seqnames, r.start, r.end, r.strand, int(n))
            for r, n in zip(self.regions, self.region_read_counts)
        ]

    def as_meta(self) -> dict[str, Any]:
        """Aggregates only; the per-region table goes to ``results.sqlite`` via ``region_rows``."""
        return {
            "n_reads_pod5": self.n_reads_pod5,
            "regions_total": self.regions_total,
            "regions_skipped_low_coverage": self.regions_skipped_low_coverage,
            "regions_subsampled": self.regions_subsampled,
            "bam_sorted_by_worker": self.bam_sorted_by_worker,
            "bam_indexed_by_worker": self.bam_indexed_by_worker,
            "md_added_by_worker": self.md_added_by_worker,
            "n_bam_records_inspected": self.n_records_inspected,
            "n_overlap_checked": self.n_overlap_checked,
            "n_overlap_shared": self.n_overlap_shared,
        }


# ----------------------------------------------------------------------------------------------
# regions.csv
# ----------------------------------------------------------------------------------------------


def find_unsafe_contig_names(names: Iterable[str]) -> dict[str, str]:
    """Contig names that upstream's ``pd.read_csv`` would not keep as strings -> what they become.

    Every DirectRM stage reads ``regions.csv`` (and later the per-k-mer feature CSV, whose
    ``seqnames`` column is an arbitrary subset of these names) with ``pandas.read_csv`` and
    default dtype inference. A column made only of number-like names (``1``, ``1e5``, ``inf``)
    becomes int64/float64, one made only of ``True``/``False`` becomes bool, and an NA string
    (``NA``, ``nan``, ``null``, ``None``, ``<NA>``, ...) becomes NaN whatever the other rows
    are; ``pysam.fetch`` then rejects the non-string contig and ``inference.py`` cannot build
    ``<type>/<seqname>.csv``. Each distinct name is therefore parsed on its own (one-row CSV
    with one column per name, a single ``read_csv`` call) with the very pandas the child
    processes use, so the verdict matches upstream for any subset of the regions.
    """
    import pandas as pd

    distinct = list(dict.fromkeys(names))
    if not distinct:
        return {}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([f"c{i}" for i in range(len(distinct))])
    writer.writerow(distinct)
    frame = pd.read_csv(io.StringIO(buf.getvalue()))
    unsafe: dict[str, str] = {}
    for i, name in enumerate(distinct):
        value = frame.iloc[0, i]
        if isinstance(value, str) and value == name:
            continue
        if pd.isna(value):
            unsafe[name] = "a missing value"
        elif frame.dtypes.iloc[i].kind == "b":
            unsafe[name] = "a boolean"
        else:
            unsafe[name] = "a number"
    return unsafe


def load_regions(path: Path, max_regions: int) -> list[Region]:
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise StageError("The regions file is empty.")
            fieldnames = [name.strip() for name in reader.fieldnames]
            missing = [c for c in REQUIRED_REGION_COLUMNS if c not in fieldnames]
            if missing:
                raise StageError(
                    "The regions file must have the columns seqnames,start,end,strand "
                    f"(width is optional); missing: {', '.join(missing)}."
                )
            rows = []
            for raw in reader:
                row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
                if not any(row.values()):
                    continue  # blank line
                rows.append(row)
                if len(rows) > max_regions:
                    raise StageError(
                        f"The regions file has more than {max_regions} rows; at most "
                        f"{max_regions} regions are allowed per job."
                    )
    except UnicodeDecodeError:
        raise StageError("The regions file is not a UTF-8 text file.") from None
    except OSError as exc:
        raise StageError("The regions file could not be read.", detail=str(exc)) from None

    if not rows:
        raise StageError("The regions file has no data rows.")

    regions: list[Region] = []
    for index, row in enumerate(rows, start=1):
        name = row.get("seqnames", "")
        if not name:
            raise StageError(f"Region {index} has an empty seqnames value.")
        if "/" in name or os.sep in name:
            # inference.py writes <outdir>/<type>/<seqname>.csv
            raise StageError(f"Region {index}: the contig name '{name}' may not contain '/'.")
        try:
            start = int(row.get("start", ""))
            end = int(row.get("end", ""))
        except ValueError:
            raise StageError(
                f"Region {index} ('{name}'): start and end must be integers (1-based, inclusive)."
            ) from None
        if start < 1:
            raise StageError(
                f"Region {index} ('{name}'): start must be >= 1 (1-based coordinates)."
            )
        if end < start:
            raise StageError(
                f"Region {index} ('{name}'): end ({end}) is smaller than start ({start})."
            )
        strand = row.get("strand", "")
        if strand not in ("+", "-"):
            raise StageError(
                f"Region {index} ('{name}'): strand must be '+' or '-', got '{strand}'."
            )
        regions.append(Region(name, start, end, strand))

    unsafe = find_unsafe_contig_names(r.seqnames for r in regions)
    if unsafe:
        index, region = next((i, r) for i, r in enumerate(regions, start=1) if r.seqnames in unsafe)
        raise StageError(
            UNSAFE_CONTIG_MESSAGE.format(
                index=index, name=region.seqnames, kind=unsafe[region.seqnames]
            ),
            detail=f"{len(unsafe)} unsafe contig name(s): {sorted(unsafe)[:10]}",
        )
    return regions


def write_regions(path: Path, regions: Sequence[Region]) -> None:
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(REGION_COLUMNS)
        for r in regions:
            writer.writerow([r.seqnames, r.start, r.end, r.width, r.strand])
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------------------
# pod5
# ----------------------------------------------------------------------------------------------


def check_pod5(path: Path) -> tuple[int, set[str]]:
    import pod5

    try:
        with pod5.DatasetReader(Path(path)) as reader:
            read_ids = {str(read_id) for read_id in reader.read_ids}
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable  # lib-pod5 raises a mix of RuntimeError / pod5 errors
        text = str(exc)
        if "Schema field" in text or "incorrect type" in text:
            raise StageError(
                NEWER_POD5_MESSAGE.format(version=pod5.__version__), detail=text
            ) from None
        raise StageError(
            "The pod5 file could not be opened; is it a valid POD5 file?", detail=text
        ) from None
    if not read_ids:
        raise StageError("The pod5 file contains no reads.")
    return len(read_ids), read_ids


# ----------------------------------------------------------------------------------------------
# reference
# ----------------------------------------------------------------------------------------------


def check_reference(path: Path) -> dict[str, int]:
    import pysam

    try:
        pysam.faidx(str(path))
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        raise StageError(
            "The reference FASTA could not be indexed; is it a valid (uncompressed or bgzip) "
            "FASTA file?",
            detail=str(exc),
        ) from None
    try:
        with pysam.FastaFile(str(path)) as fasta:
            lengths = dict(zip(fasta.references, fasta.lengths))
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        raise StageError("The reference FASTA could not be read.", detail=str(exc)) from None
    if not lengths:
        raise StageError("The reference FASTA contains no sequences.")
    return lengths


# ----------------------------------------------------------------------------------------------
# BAM
# ----------------------------------------------------------------------------------------------


def _open_bam(path: Path):
    import pysam

    try:
        return pysam.AlignmentFile(str(path), "rb", check_sq=False)
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        raise StageError(
            "The BAM file could not be opened; is it a valid BAM file?", detail=str(exc)
        ) from None


def _is_coordinate_sorted(bam) -> bool:
    header = bam.header.to_dict()
    return header.get("HD", {}).get("SO") == "coordinate"


def sort_bam(path: Path, threads: int, work_dir: Path) -> None:
    import pysam

    tmp_out = Path(str(path) + ".sorting")
    tmp_prefix = Path(work_dir) / "sort_tmp"
    try:
        pysam.sort("-@", str(max(1, threads)), "-T", str(tmp_prefix), "-o", str(tmp_out), str(path))
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        if tmp_out.exists():
            tmp_out.unlink()
        raise StageError(
            "The BAM could not be coordinate-sorted; is it a complete, valid BAM file?",
            detail=str(exc),
        ) from None
    os.replace(tmp_out, path)


def index_bam(path: Path) -> None:
    import pysam

    try:
        pysam.index(str(path))
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        raise StageError(
            "The BAM could not be indexed; is it coordinate-sorted and complete?", detail=str(exc)
        ) from None


def _bai_is_fresh(bam_path: Path) -> bool:
    bai = Path(str(bam_path) + ".bai")
    if not bai.is_file():
        return False
    try:
        return bai.stat().st_mtime >= bam_path.stat().st_mtime
    except OSError:
        return False


def inspect_records(bam, limit: int = INSPECT_RECORDS) -> tuple[int, int, int]:
    """Return ``(n_inspected, n_missing_mv, n_missing_md)`` over the first mapped primaries."""
    n = missing_mv = missing_md = 0
    for read in bam.fetch(until_eof=False):
        if read.is_unmapped or read.is_secondary or read.is_supplementary:
            continue
        n += 1
        if not read.has_tag("mv"):
            missing_mv += 1
        if not read.has_tag("MD"):
            missing_md += 1
        if n >= limit:
            break
    return n, missing_mv, missing_md


def add_md_tags(bam_path: Path, reference: Path, threads: int) -> None:
    """Rewrite ``bam_path`` with ``MD``/``NM`` computed by ``samtools calmd`` and re-index."""
    import pysam

    tmp_out = Path(str(bam_path) + ".calmd")
    try:
        pysam.calmd(
            "-b",
            "-@",
            str(max(1, threads)),
            str(bam_path),
            str(reference),
            save_stdout=str(tmp_out),
        )
    except INTERRUPTS:
        raise
    except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable
        if tmp_out.exists():
            tmp_out.unlink()
        raise StageError(
            "MD tags could not be added to the BAM (samtools calmd failed); check that the "
            "reference matches the BAM.",
            detail=str(exc),
        ) from None
    os.replace(tmp_out, bam_path)
    bai = Path(str(bam_path) + ".bai")
    if bai.exists():
        bai.unlink()
    index_bam(bam_path)


def _strands_match(strand: str, read) -> bool:
    # Mirrors remora.io.strands_match for strand in {"+", "-"}.
    return (strand == "+" and not read.is_reverse) or (strand == "-" and read.is_reverse)


def count_region_reads(
    bam, regions: Sequence[Region], cap: int = REGION_COUNT_CAP, max_names: int = OVERLAP_MAX_NAMES
) -> tuple[list[int], set[str]]:
    """Count alignment records per region the way ``sampling.py`` does (via Remora ``fetch``).

    The CSV ``start``/``end`` are passed to ``pysam.fetch`` unchanged (upstream treats the
    1-based start as 0-based, so the window is 1-based ``start+1..end``); every record on the
    requested strand counts, primary or not. Also returns a sample of the query names seen.
    """
    counts: list[int] = []
    names: set[str] = set()
    for region in regions:
        n = 0
        try:
            for read in bam.fetch(region.seqnames, region.start, region.end):
                if not _strands_match(region.strand, read):
                    continue
                n += 1
                if len(names) < max_names:
                    names.add(read.query_name)
                if n >= cap:
                    break
        except ValueError as exc:
            # Same outcome as upstream (exception printed, region contributes no reads).
            log.warning("region %s:%d-%d: %s", region.seqnames, region.start, region.end, exc)
            n = 0
        counts.append(n)
    return counts, names


# ----------------------------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------------------------


def prepare_inputs(
    job_dir: Path,
    *,
    max_regions: int,
    min_coverage: int,
    max_coverage: int,
    threads: int = 1,
    logger: logging.Logger | None = None,
) -> PrepareResult:
    logger = logger or log
    job_dir = Path(job_dir)
    input_dir = job_dir / "input"
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    pod5_path = input_dir / INPUT_POD5
    bam_path = input_dir / INPUT_BAM
    ref_path = input_dir / INPUT_REFERENCE
    regions_path = input_dir / INPUT_REGIONS
    for path, label in (
        (pod5_path, "pod5"),
        (bam_path, "BAM"),
        (ref_path, "reference"),
        (regions_path, "regions"),
    ):
        if not path.is_file():
            raise StageError(f"The {label} input file is missing from the job directory.")
        if path.stat().st_size == 0:
            raise StageError(f"The {label} input file is empty.")

    regions = load_regions(regions_path, max_regions)
    write_regions(regions_path, regions)
    logger.info("regions: %d rows", len(regions))

    n_reads_pod5, pod5_ids = check_pod5(pod5_path)
    logger.info("pod5: %d reads", n_reads_pod5)

    reference_lengths = check_reference(ref_path)
    logger.info("reference: %d contigs", len(reference_lengths))

    bam = _open_bam(bam_path)
    try:
        if not bam.references:
            raise StageError(
                "The BAM has no reference sequences in its header (it is unaligned). Basecall "
                "with dorado --reference (or align with minimap2) and upload the aligned BAM."
            )
        sorted_by_worker = not _is_coordinate_sorted(bam)
    finally:
        bam.close()
    if sorted_by_worker:
        logger.info("BAM is not coordinate-sorted: sorting")
        sort_bam(bam_path, threads, work_dir)
    indexed_by_worker = not _bai_is_fresh(bam_path)
    if indexed_by_worker:
        index_bam(bam_path)

    bam = _open_bam(bam_path)
    try:
        n_inspected, missing_mv, missing_md = inspect_records(bam)
    finally:
        bam.close()
    if n_inspected == 0:
        raise StageError("The BAM contains no mapped primary alignments.")
    if missing_mv:
        raise StageError(
            NO_MV_MESSAGE, detail=f"{missing_mv}/{n_inspected} inspected records lack mv"
        )
    md_added = False
    if missing_md:
        logger.info("%d/%d inspected records lack MD: running calmd", missing_md, n_inspected)
        add_md_tags(bam_path, ref_path, threads)
        md_added = True

    bam = _open_bam(bam_path)
    try:
        bam_lengths = dict(zip(bam.references, bam.lengths))
        seen: set[str] = set()
        for region in regions:
            name = region.seqnames
            if name in seen:
                continue
            seen.add(name)
            if name not in bam_lengths:
                raise StageError(
                    f"Region contig '{name}' is not in the BAM header; the BAM was aligned to a "
                    "different reference."
                )
            if name not in reference_lengths:
                raise StageError(f"Region contig '{name}' is not in the reference FASTA.")
            if bam_lengths[name] != reference_lengths[name]:
                raise StageError(
                    f"Contig '{name}' is {reference_lengths[name]} nt in the reference FASTA but "
                    f"{bam_lengths[name]} nt in the BAM header; the BAM was not aligned to this "
                    "reference."
                )
        counts, names = count_region_reads(bam, regions)
        if not names:
            raise StageError(
                "None of the requested regions is covered by any read in the BAM (check contig "
                "names, coordinates and strand)."
            )
        shared = len(names & pod5_ids)
        if shared == 0:
            raise StageError(
                NO_OVERLAP_MESSAGE,
                detail=f"0 of {len(names)} BAM read names found among {n_reads_pod5} pod5 reads",
            )
        try:
            index_stats = {s.contig: int(s.mapped) for s in bam.get_index_statistics()}
        except INTERRUPTS:
            raise
        except Exception as exc:  # noqa: BLE001 - htslib/lib-pod5 error types are not enumerable  # pragma: no cover - index just built
            logger.warning("get_index_statistics failed: %s", exc)
            index_stats = {}
    finally:
        bam.close()

    skipped = sum(1 for n in counts if n <= min_coverage)
    subsampled = sum(1 for n in counts if n >= max_coverage)
    region_contigs = {r.seqnames for r in regions}
    return PrepareResult(
        n_reads_pod5=n_reads_pod5,
        regions=regions,
        region_read_counts=counts,
        regions_skipped_low_coverage=skipped,
        regions_subsampled=subsampled,
        bam_sorted_by_worker=sorted_by_worker,
        bam_indexed_by_worker=indexed_by_worker,
        md_added_by_worker=md_added,
        n_records_inspected=n_inspected,
        n_overlap_checked=len(names),
        n_overlap_shared=shared,
        reference_lengths={k: v for k, v in reference_lengths.items() if k in region_contigs},
        contig_mapped_reads={k: v for k, v in index_stats.items() if k in region_contigs},
    )
