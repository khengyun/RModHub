#!/usr/bin/env python3
"""Subset a large POD5 data set to the reads DirectRM will actually use.

DirectRM (the RModHub nanopore signal branch) only ever touches the reads that its
``sampling.py`` step selects from the regions of interest: every alignment that overlaps a
region on the requested strand.  A whole-flowcell POD5 (50-500 GB) is therefore mostly dead
weight for a run restricted to a few transcripts.  This tool extracts exactly those reads
(plus a safety flank) into a small POD5 that can be uploaded instead, and optionally the
matching subset BAM so the server-side ``sampling.py`` sees the same alignments.

Selection semantics (mirrors ``sampling.py`` / Remora ``ReadIndexedBam.fetch``):

* regions CSV: ``seqnames,start,end[,width],strand`` with 1-based inclusive coordinates;
* for every region, fetch all alignments overlapping ``[start-1-flank, end+flank)`` (0-based
  half-open) whose strand matches the region strand; primary, secondary and supplementary
  records all count (that is what Remora's ``fetch`` yields), ``--min-mapq`` filters on
  ``MAPQ`` (default 0 = keep everything);
* the read id used for the POD5 lookup is the parent read id (``pi`` tag when present, else
  ``query_name``), exactly like Remora's ``get_parent_id``;
* the selected reads are streamed from the input POD5 file(s) into the output with
  ``pod5.Writer`` in input order (file order within a file, files in command-line / sorted
  directory order), one batch at a time, so memory stays flat whatever the input size.

``--flank`` exists because ``sampling.py`` passes the 1-based CSV numbers straight to pysam
(effectively querying 1-based ``start+1..end``) while other tools use ``start-1``; a
20-nt flank makes the subset a superset of what any of these conventions fetch, so the
subset can never miss a read that the full-size run would have used.  Extra reads are
harmless: DirectRM only processes reads that ``sampling.py`` selects for the regions.

Exit codes: 0 success; 2 usage or validation error (bad CSV, unknown contig, no reads found,
missing files); 1 unexpected error (traceback on stderr).

Requires Python >= 3.10 with ``pod5`` and ``pysam`` only.  Run it with the repository
environment (``uv run python tools/subset_pod5.py ...``, pod5 0.3.47) or through
``tools/Dockerfile.subset`` (``rmodhub/subset:local``, pod5 0.3.35).  The RModHub worker reads
POD5 v5 and v6, i.e. every file written by pod5 <= 0.3.47 (its reader is lib-pod5 0.3.47), so
both environments produce uploadable output; with a pod5 newer than ``WORKER_POD5_VERSION`` the
tool warns that the server may not be able to open the result yet.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pod5
import pysam
import pysam.utils

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2

REQUIRED_COLUMNS = ("seqnames", "start", "end", "strand")
DEFAULT_FLANK = 20
# lib-pod5 version of the RModHub worker (worker/pyproject.toml). A pod5 reader opens every file
# written by pod5 <= its own version; a newer writer may use a read-table schema the worker's
# reader rejects ("Schema field '<x>' is incorrect type", as pod5 0.3.46 did with the 32-bit
# channel column, POD5 "v6"). Bump this together with the worker pin.
WORKER_POD5_VERSION = (0, 3, 47)
WORKER_MAX_POD5 = WORKER_POD5_VERSION  # newest pod5 whose output the worker is known to read
WORKER_POD5_HINT = (
    "run the tool through the Docker image (tools/Dockerfile.subset) or with `uv run --with "
    '"pod5==0.3.47" --with "lib-pod5==0.3.47" python tools/subset_pod5.py ...`'
)


class UsageError(Exception):
    """A problem the user can fix (bad arguments or inputs) -> exit code 2."""


# ----------------------------------------------------------------------------------------
# Regions
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    seqnames: str
    start: int  # 1-based inclusive
    end: int  # 1-based inclusive
    strand: str  # "+" or "-"
    line_no: int

    def label(self) -> str:
        return f"{self.seqnames}:{self.start}-{self.end}({self.strand})"


def read_regions(path: Path) -> list[Region]:
    """Parse a DirectRM regions CSV (``seqnames,start,end[,width],strand``)."""
    if not path.is_file():
        raise UsageError(f"regions file not found: {path}")
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise UsageError(
                f"{path}: missing required column(s) {', '.join(missing)}; the header must "
                f"contain {', '.join(REQUIRED_COLUMNS)} (found: {', '.join(header) or 'nothing'})"
            )
        regions: list[Region] = []
        for line_no, row in enumerate(reader, start=2):
            values = {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}
            if not any(values.values()):
                continue  # blank line
            seqnames = values["seqnames"]
            if not seqnames:
                raise UsageError(f"{path} line {line_no}: empty seqnames")
            try:
                start = int(values["start"])
                end = int(values["end"])
            except ValueError:
                raise UsageError(
                    f"{path} line {line_no}: start/end must be integers "
                    f"(got {values['start']!r}, {values['end']!r})"
                ) from None
            if start < 1 or end < start:
                raise UsageError(
                    f"{path} line {line_no}: need 1 <= start <= end (got {start}, {end}); "
                    "coordinates are 1-based inclusive"
                )
            strand = values["strand"]
            if strand not in ("+", "-"):
                raise UsageError(
                    f"{path} line {line_no}: strand must be '+' or '-' (got {strand!r}); "
                    "DirectRM matches reads to the region strand"
                )
            regions.append(Region(seqnames, start, end, strand, line_no))
    if not regions:
        raise UsageError(f"{path}: no regions (only a header)")
    return regions


# ----------------------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------------------


def collect_pod5_files(inputs: Sequence[str]) -> list[Path]:
    """Expand files/directories into an ordered, de-duplicated list of .pod5 files."""
    files: list[Path] = []
    seen: set[Path] = set()
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            found = sorted(q for q in p.rglob("*.pod5") if q.is_file())
            if not found:
                raise UsageError(f"no .pod5 files found under directory {p}")
            candidates = found
        elif p.is_file():
            candidates = [p]
        else:
            raise UsageError(f"pod5 input not found: {p}")
        for c in candidates:
            key = c.resolve()
            if key not in seen:
                seen.add(key)
                files.append(c)
    return files


def open_pod5(path: Path) -> pod5.Reader:
    try:
        return pod5.Reader(path)
    except (RuntimeError, OSError) as exc:  # lib-pod5 reports format problems as RuntimeError
        msg = str(exc)
        if "Schema field" in msg or "incorrect type" in msg:
            worker_v = ".".join(map(str, WORKER_POD5_VERSION))
            raise UsageError(
                f"{path}: written in a newer POD5 format than this pod5 library "
                f"({pod5.__version__}) can read; install a newer pod5 (`pip install -U pod5`) "
                f"to subset it. The RModHub worker reads files written by pod5 <= {worker_v} "
                "(POD5 v5 and v6); after upgrading, the tool warns if its pod5 is newer than that."
            ) from None
        raise UsageError(f"{path}: cannot open as POD5 ({msg})") from None


def ensure_bam_index(bam_path: Path) -> None:
    if not bam_path.is_file():
        raise UsageError(f"BAM not found: {bam_path}")
    candidates = [Path(str(bam_path) + ".bai"), bam_path.with_suffix(".bai")]
    if any(c.is_file() for c in candidates):
        return
    print(f"note: no index next to {bam_path}, creating {bam_path}.bai", file=sys.stderr)
    try:
        pysam.index(str(bam_path))
    except (OSError, pysam.utils.SamtoolsError) as exc:
        raise UsageError(
            f"could not index {bam_path} ({exc}); the BAM must be coordinate-sorted and its "
            "directory writable (or run `samtools index` yourself)"
        ) from None


def validate_regions_against_bam(regions: Sequence[Region], bam: pysam.AlignmentFile) -> None:
    contigs = set(bam.references)
    unknown = [r for r in regions if r.seqnames not in contigs]
    if unknown:
        preview = ", ".join(list(bam.references)[:8])
        more = "" if len(bam.references) <= 8 else f", ... ({len(bam.references)} contigs)"
        first = unknown[0]
        raise UsageError(
            f"regions line {first.line_no}: seqname {first.seqnames!r} is not a contig of the "
            f"BAM ({len(unknown)} such region(s)); BAM contigs: {preview}{more}. "
            "Make sure the regions and the BAM use the same reference."
        )


# ----------------------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------------------


@dataclass
class RegionStats:
    region: Region
    window_start: int  # 0-based
    window_end: int  # 0-based exclusive
    n_records: int = 0
    n_reads: int = 0


@dataclass
class Selection:
    read_ids: list[str]  # unique parent read ids, first-seen order
    n_records: int
    per_region: list[RegionStats]
    ids_without_primary: int = 0
    id_set: set[str] = field(default_factory=set)


def strand_matches(strand: str, rec: pysam.AlignedSegment) -> bool:
    return rec.is_reverse if strand == "-" else not rec.is_reverse


def parent_read_id(rec: pysam.AlignedSegment) -> str:
    # dorado writes pi:Z:<parent id> on split (child) reads; the POD5 holds the parent.
    if rec.has_tag("pi"):
        return str(rec.get_tag("pi"))
    return rec.query_name


def region_window(region: Region, contig_len: int, flank: int) -> tuple[int, int]:
    start0 = max(0, region.start - 1 - flank)
    end0 = min(contig_len, region.end + flank)
    return start0, end0


def select_reads(
    bam: pysam.AlignmentFile, regions: Sequence[Region], flank: int, min_mapq: int
) -> Selection:
    lengths = dict(zip(bam.references, bam.lengths))
    ids: dict[str, None] = {}
    with_primary: set[str] = set()
    per_region: list[RegionStats] = []
    n_records = 0
    for region in regions:
        contig_len = lengths[region.seqnames]
        s0, e0 = region_window(region, contig_len, flank)
        stats = RegionStats(region, s0, e0)
        if s0 >= e0:
            print(
                f"warning: {region.label()} lies beyond the end of {region.seqnames} "
                f"(length {contig_len}); no reads can overlap it",
                file=sys.stderr,
            )
            per_region.append(stats)
            continue
        local: set[str] = set()
        for rec in bam.fetch(region.seqnames, s0, e0):
            if rec.is_unmapped or not strand_matches(region.strand, rec):
                continue
            if rec.mapping_quality < min_mapq:
                continue
            rid = parent_read_id(rec)
            stats.n_records += 1
            local.add(rid)
            ids.setdefault(rid, None)
            if not (rec.is_secondary or rec.is_supplementary):
                with_primary.add(rid)
        stats.n_reads = len(local)
        n_records += stats.n_records
        per_region.append(stats)
    id_list = list(ids)
    return Selection(
        read_ids=id_list,
        n_records=n_records,
        per_region=per_region,
        ids_without_primary=sum(1 for r in id_list if r not in with_primary),
        id_set=set(id_list),
    )


def merged_windows(
    stats: Iterable[RegionStats],
) -> list[tuple[str, str, int, int]]:
    """Merge overlapping fetch windows per (contig, strand) so a BAM record is written once."""
    by_key: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for st in stats:
        if st.window_start < st.window_end:
            by_key.setdefault((st.region.seqnames, st.region.strand), []).append(
                (st.window_start, st.window_end)
            )
    out: list[tuple[str, str, int, int]] = []
    for (ctg, strand), spans in by_key.items():
        spans.sort()
        cur_s, cur_e = spans[0]
        for s, e in spans[1:]:
            if s <= cur_e:
                cur_e = max(cur_e, e)
            else:
                out.append((ctg, strand, cur_s, cur_e))
                cur_s, cur_e = s, e
        out.append((ctg, strand, cur_s, cur_e))
    return out


# ----------------------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------------------


def write_bam_subset(
    bam_in_path: Path,
    windows: Sequence[tuple[str, str, int, int]],
    selected: Selection,
    min_mapq: int,
    out_path: Path,
    threads: int,
    argv: Sequence[str],
) -> int:
    """Write every fetched alignment of the selected reads, coordinate-sorted and indexed."""
    tmp_unsorted = out_path.with_name(out_path.name + ".unsorted.tmp.bam")
    tmp_prefix = out_path.with_name(out_path.name + ".sort.tmp")
    n = 0
    try:
        with pysam.AlignmentFile(str(bam_in_path), "rb", threads=threads) as bam:
            header = bam.header.to_dict()
            pg_entries = header.setdefault("PG", [])
            pg_id = "subset_pod5"
            existing = {e.get("ID") for e in pg_entries}
            k = 1
            while pg_id in existing:
                k += 1
                pg_id = f"subset_pod5.{k}"
            entry = {
                "ID": pg_id,
                "PN": "subset_pod5",
                "VN": __version__,
                "CL": " ".join(argv),
            }
            if pg_entries and pg_entries[-1].get("ID"):
                entry["PP"] = pg_entries[-1]["ID"]
            pg_entries.append(entry)
            with pysam.AlignmentFile(
                str(tmp_unsorted), "wb", header=header, threads=threads
            ) as out:
                for ctg, strand, s0, e0 in windows:
                    for rec in bam.fetch(ctg, s0, e0):
                        if rec.is_unmapped or not strand_matches(strand, rec):
                            continue
                        if rec.mapping_quality < min_mapq:
                            continue
                        if parent_read_id(rec) not in selected.id_set:
                            continue
                        out.write(rec)
                        n += 1
        pysam.sort(
            "--no-PG",
            "-@",
            str(threads),
            "-T",
            str(tmp_prefix),
            "-o",
            str(out_path),
            str(tmp_unsorted),
        )
        pysam.index("-@", str(threads), str(out_path))
    finally:
        if tmp_unsorted.exists():
            tmp_unsorted.unlink()
    return n


def pod5_inventory(files: Sequence[Path]) -> tuple[int, int]:
    """Total bytes and total reads of the input POD5 files (metadata only, no signal read)."""
    total_bytes = 0
    total_reads = 0
    for f in files:
        total_bytes += f.stat().st_size
        with open_pod5(f) as reader:
            total_reads += reader.num_reads
    return total_bytes, total_reads


def count_found(files: Sequence[Path], read_ids: Sequence[str]) -> int:
    """How many of ``read_ids`` exist in the POD5 inputs (read-table lookups only)."""
    found: set[str] = set()
    wanted = list(read_ids)
    for f in files:
        with open_pod5(f) as reader:
            for batch in reader.read_batches(selection=wanted, missing_ok=True):
                for rec in batch.reads():
                    found.add(str(rec.read_id))
    return len(found)


def write_pod5_subset(files: Sequence[Path], read_ids: Sequence[str], out_path: Path) -> int:
    """Stream the selected reads into ``out_path`` preserving input order; returns #written."""
    written: set[str] = set()
    remaining = list(read_ids)
    writer = pod5.Writer(out_path, software_name=f"rmodhub subset_pod5 {__version__}")
    try:
        for f in files:
            if not remaining:
                break
            with open_pod5(f) as reader:
                # pod5 walks the selection in file order, one record batch at a time; the
                # signal of a read is only decompressed when to_read() is called.
                for batch in reader.read_batches(selection=remaining, missing_ok=True):
                    for rec in batch.reads():
                        rid = str(rec.read_id)
                        if rid in written:
                            continue  # same read id in two input files: keep the first
                        writer.add_read(rec.to_read())
                        written.add(rid)
            if written:
                remaining = [r for r in remaining if r not in written]
    finally:
        writer.close()
    return len(written)


def output_pod5_version(path: Path) -> str:
    try:
        with pod5.Reader(path) as reader:
            v = getattr(reader, "file_version_pre_migration", None) or reader.file_version
            return str(v)
    except (RuntimeError, OSError):
        return "unknown"


def pod5_version_tuple() -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(pod5.__version__).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


# ----------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subset_pod5",
        description=(
            "Extract the reads overlapping DirectRM regions from a large POD5 into a small "
            "POD5 (and optionally the matching BAM) for upload to RModHub."
        ),
        epilog=(
            "Coordinates in the regions CSV are 1-based inclusive (DirectRM convention). "
            "Exit codes: 0 ok, 2 usage/validation error, 1 unexpected error."
        ),
    )
    p.add_argument(
        "-i",
        "--pod5",
        nargs="+",
        required=True,
        metavar="POD5",
        help="input .pod5 file(s) and/or directories (directories are searched recursively)",
    )
    p.add_argument(
        "-b",
        "--bam",
        required=True,
        metavar="BAM",
        help="aligned, coordinate-sorted BAM (dorado --emit-moves); indexed if no .bai exists",
    )
    p.add_argument(
        "-r",
        "--regions",
        required=True,
        metavar="CSV",
        help="DirectRM regions CSV: seqnames,start,end[,width],strand (1-based inclusive)",
    )
    p.add_argument("-o", "--out", required=True, metavar="POD5", help="output .pod5 path")
    p.add_argument(
        "--bam-out",
        metavar="BAM",
        help="also write the matching subset BAM (coordinate-sorted, indexed) to this path",
    )
    p.add_argument(
        "--flank",
        type=int,
        default=DEFAULT_FLANK,
        metavar="NT",
        help=(
            "widen every region by this many nt on both sides so the subset is a superset "
            "of what DirectRM sampling.py fetches (default %(default)s)"
        ),
    )
    p.add_argument(
        "--min-mapq",
        type=int,
        default=0,
        metavar="Q",
        help="drop alignments with MAPQ below Q (default %(default)s = keep all, like DirectRM)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="only count the selected reads, check them against the POD5 and estimate the size",
    )
    p.add_argument(
        "--threads",
        type=int,
        default=1,
        metavar="N",
        help="threads for BAM (de)compression, sorting and indexing (default %(default)s)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing output files",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def fmt_int(n: int) -> str:
    return f"{n:,}"


def run(args: argparse.Namespace, argv: Sequence[str]) -> int:
    t0 = time.monotonic()
    if args.flank < 0:
        raise UsageError("--flank must be >= 0")
    if args.min_mapq < 0:
        raise UsageError("--min-mapq must be >= 0")
    if args.threads < 1:
        raise UsageError("--threads must be >= 1")

    regions = read_regions(Path(args.regions))
    pod5_files = collect_pod5_files(args.pod5)
    bam_path = Path(args.bam)
    ensure_bam_index(bam_path)
    out_path = Path(args.out)
    bam_out = Path(args.bam_out) if args.bam_out else None
    if not args.dry_run:
        for target in [out_path] + ([bam_out] if bam_out else []):
            if target.exists() and not args.force:
                raise UsageError(f"output exists: {target} (use --force to overwrite)")
            if target.resolve() in {f.resolve() for f in pod5_files} or target.resolve() == (
                bam_path.resolve()
            ):
                raise UsageError(f"output {target} would overwrite an input file")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if bam_out:
            bam_out.parent.mkdir(parents=True, exist_ok=True)

    print(f"subset_pod5 {__version__} (pod5 {pod5.__version__}, pysam {pysam.__version__})")
    with pysam.AlignmentFile(str(bam_path), "rb", threads=args.threads) as bam:
        validate_regions_against_bam(regions, bam)
        n_contigs = len(bam.references)
        selected = select_reads(bam, regions, args.flank, args.min_mapq)

    print(f"BAM        : {bam_path} ({n_contigs} contigs)")
    print(
        f"regions    : {len(regions)} (flank {args.flank} nt, min MAPQ {args.min_mapq}, "
        "strand-matched, primary+secondary+supplementary)"
    )
    for st in selected.per_region:
        print(
            f"  {st.region.label():<32} window {st.region.seqnames}:{st.window_start + 1}-"
            f"{st.window_end:<10} records {st.n_records:>8}  reads {st.n_reads:>8}"
        )
        if st.n_records == 0:
            print(f"warning: no alignments overlap {st.region.label()}", file=sys.stderr)
    if not selected.read_ids:
        raise UsageError(
            "no alignments overlap any region on the requested strand; check that the "
            "regions use the BAM's contig names/coordinates and the right strand"
        )
    print(
        f"selected   : {fmt_int(len(selected.read_ids))} unique read ids "
        f"({fmt_int(selected.n_records)} alignment records)"
    )
    if selected.ids_without_primary:
        print(
            f"note: {fmt_int(selected.ids_without_primary)} selected read id(s) have only "
            "secondary/supplementary alignments inside the regions; their primary alignment "
            "lies elsewhere and is not part of the subset BAM",
            file=sys.stderr,
        )

    total_bytes, total_reads = pod5_inventory(pod5_files)
    mean_bytes = total_bytes / total_reads if total_reads else 0.0
    estimate = int(mean_bytes * len(selected.read_ids))
    print(
        f"input pod5 : {len(pod5_files)} file(s), {fmt_int(total_reads)} reads, "
        f"{fmt_int(total_bytes)} bytes (mean {fmt_int(int(mean_bytes))} bytes/read)"
    )
    print(
        f"estimate   : ~{fmt_int(estimate)} bytes output "
        f"({fmt_int(len(selected.read_ids))} reads x {fmt_int(int(mean_bytes))} bytes/read)"
    )

    if args.dry_run:
        found = count_found(pod5_files, selected.read_ids)
        print(f"found      : {fmt_int(found)} / {fmt_int(len(selected.read_ids))} read ids in pod5")
        if found == 0:
            raise UsageError(
                "none of the selected read ids exist in the pod5 input(s); is the BAM from "
                "the same sequencing run (same read ids)?"
            )
        if found < len(selected.read_ids):
            print(
                f"warning: {fmt_int(len(selected.read_ids) - found)} selected read id(s) are "
                "missing from the pod5 input(s)",
                file=sys.stderr,
            )
        print(f"dry run    : nothing written ({time.monotonic() - t0:.1f} s)")
        return EXIT_OK

    if out_path.exists():
        out_path.unlink()
    ok = False
    try:
        found = write_pod5_subset(pod5_files, selected.read_ids, out_path)
        if found == 0:
            raise UsageError(
                "none of the selected read ids exist in the pod5 input(s); is the BAM from "
                "the same sequencing run (same read ids)?"
            )
        n_bam_records = 0
        if bam_out:
            n_bam_records = write_bam_subset(
                bam_path,
                merged_windows(selected.per_region),
                selected,
                args.min_mapq,
                bam_out,
                args.threads,
                argv,
            )
        ok = True
    finally:
        if not ok:
            for target in [out_path] + ([bam_out] if bam_out else []):
                for suffix in ("", ".bai"):
                    p = Path(str(target) + suffix)
                    if p.exists():
                        p.unlink()

    out_bytes = out_path.stat().st_size
    print(f"found      : {fmt_int(found)} / {fmt_int(len(selected.read_ids))} read ids in pod5")
    if found < len(selected.read_ids):
        print(
            f"warning: {fmt_int(len(selected.read_ids) - found)} selected read id(s) are missing "
            "from the pod5 input(s); they were skipped (DirectRM would skip them as well)",
            file=sys.stderr,
        )
    print(
        f"output pod5: {out_path} ({fmt_int(out_bytes)} bytes, {fmt_int(found)} reads, "
        f"POD5 format {output_pod5_version(out_path)})"
    )
    if bam_out:
        print(f"output BAM : {bam_out} (+ .bai, {fmt_int(n_bam_records)} records)")
    if pod5_version_tuple() > WORKER_MAX_POD5:
        worker_v = ".".join(map(str, WORKER_POD5_VERSION))
        print(
            f"WARNING: written with pod5 {pod5.__version__}, which is newer than the RModHub "
            f"worker's reader (lib-pod5 {worker_v}, POD5 v5/v6). If this pod5 release changed "
            "the POD5 file format the server will not be able to open the subset; to be safe "
            f"{WORKER_POD5_HINT}.",
            file=sys.stderr,
        )
    print(f"elapsed    : {time.monotonic() - t0:.1f} s")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # keep the stdout summary and stderr warnings interleaved in order when piped
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    try:
        return run(args, ["subset_pod5.py", *argv])
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - anything else is a bug: traceback + exit code 1
        print("unexpected error:", file=sys.stderr)
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
