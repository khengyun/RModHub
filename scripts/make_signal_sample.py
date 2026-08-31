#!/usr/bin/env python
"""Generate the synthetic nanopore (RNA004-like) sample data set for the signal branch.

The output directory (``app/samples/signal`` by default, contract ``docs/signal-branch.md``
section 10) receives:

    sample.pod5                raw signal, 88 reads (pod5, VBZ compressed)
    sample_sorted.bam(.bai)    dorado look-alike alignments with mv/ts/ns/MD tags
    sample_reference.fa(.fai)  3 transcripts: tx_A, tx_B, tx_C
    sample_regions.csv         DirectRM region table (1-based inclusive)
    README.md                  provenance, orientation caveat, licence (CC0)
    MANIFEST.json              bytes + sha256 per file, pod5 content digest, generator args

Everything is derived from ``numpy.random.default_rng(seed)`` with fixed timestamps, so two
runs with the same arguments give byte-identical BAM / FASTA / CSV / README and an identical
pod5 *content* digest (the pod5 container embeds a random file identifier, hence the file
hash itself differs between runs; compare ``content_sha256`` from the manifest instead).

Usage::

    uv run --with "pod5==0.3.35" --with "lib-pod5==0.3.35" python scripts/make_signal_sample.py \
        --out app/samples/signal --seed 20250831 --layout rna_raw \
        --levels worker/directrm_vendor/9mer_levels_v1.txt

Only numpy, pod5 and pysam are needed (all dev dependencies of the root project). The
generator never imports worker code; the 9-mer level table is read as a plain text file.

pod5 writer version: pod5/lib-pod5 0.3.46 introduced POD5 v6 (32-bit ``channel`` column),
which readers older than 0.3.46 reject (``Schema field 'channel' is incorrect type: 'uint32'``).
The RModHub worker (Python 3.10, lib-pod5 0.3.47) reads v5 and v6, so this is not a server
constraint any more; the committed sample is nevertheless kept as a POD5 v5 file (writer
<= 0.3.45) because v5 opens with every pod5 release - including the 0.3.35 pinned in
``tools/Dockerfile.subset`` and older installs users may have - and so that regenerating the
sample does not change its format. The generator therefore refuses to write with pod5 > 0.3.45
unless ``--allow-newer-pod5`` is given; pin the older writer with ``uv run --with`` as shown above.

Move-table layouts
------------------
``rna_raw`` (default) is what dorado emits for direct RNA (verified in the dorado 0.6.2 and
2.1 sources): the raw signal is in sequencing time order, i.e. 3' -> 5' of the read; dorado
reverses only ``seq``/``qual`` so the BAM sequence reads 5' -> 3'; the ``mv`` table stays in
raw-signal time order, so move ``i`` belongs to base ``L-1-i``; ``ts``/``ns`` are raw-signal
coordinates. ``dna_like`` is a diagnostic layout in which the signal time order equals the
5' -> 3' basecall order (as for DNA); it makes a ``reverse_signal=False`` consumer
self-consistent but does not correspond to real RNA output.
"""

from __future__ import annotations

import argparse
import array
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pod5
import pysam

GENERATOR_VERSION = "1.0.0"

# ---- file names fixed by docs/signal-branch.md section 10 and the API --------------------
POD5_NAME = "sample.pod5"
BAM_NAME = "sample_sorted.bam"
BAI_NAME = "sample_sorted.bam.bai"
FASTA_NAME = "sample_reference.fa"
FAI_NAME = "sample_reference.fa.fai"
REGIONS_NAME = "sample_regions.csv"
README_NAME = "README.md"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_FILES = (POD5_NAME, BAM_NAME, BAI_NAME, FASTA_NAME, FAI_NAME, REGIONS_NAME, README_NAME)

# ---- data set design ----------------------------------------------------------------------
# (transcript, number of reads). tx_C is deliberately below DirectRM's default
# --min_coverage 30 so that sampling.py skips its region while the pod5 still holds its reads.
TRANSCRIPTS = (("tx_A", 40), ("tx_B", 36), ("tx_C", 12))
# (seqnames, start, end) 1-based inclusive; width = end - start + 1; strand '+'.
REGIONS = (("tx_A", 60, 300), ("tx_B", 80, 320), ("tx_C", 50, 250))
TX_LEN_MIN, TX_LEN_MAX = 500, 600  # total transcript length incl. the poly(A) tail
POLYA_LEN = 20
MAX_START_OFFSET = 40  # read starts in [0, 40) nt of the transcript
MAX_END_OFFSET = 40  # read ends in (L - 40, L] nt
MAX_SOFT_CLIP = 5  # 0..5 nt random bases clipped at each end
MAX_SUBSTITUTIONS = 2  # 0..2 substitutions per read (MD/NM are computed from them)
QUAL_MIN, QUAL_MAX = 5, 29  # inclusive Phred range

# ---- RNA004 / dorado look-alike constants -------------------------------------------------
SAMPLE_RATE = 4000  # Hz (RNA004)
STRIDE = 5  # rna004_130bps_*@v3.0.1 config.toml [encoder] stride
KMER = 9
CENTER = 3  # dominant position of 9mer_levels_v1.txt (remora SigMapRefiner center_idx)
CAL_OFFSET, CAL_SCALE = -250.0, 0.1462070643901825  # pod5 Calibration: pA = (dac + offset) * scale
ADC_MIN, ADC_MAX = 0, 2047
PA_SHIFT, PA_SCALE, PA_NOISE = 80.0, 15.0, 2.5  # pA = shift + scale * level + N(0, noise)
DWELL_LOG_MEAN, DWELL_LOG_SD = float(np.log(5.5)), 0.45  # blocks/base ~ lognormal, ~30 samples/base
DWELL_MAX_BLOCKS = 40
LEADER_MIN, LEADER_MAX = 200, 800  # ts ~ U(200, 800) samples trimmed by dorado
LEADER_PA, LEADER_NOISE = PA_SHIFT + 40.0, 6.0
TAIL_PA_NOISE = 3.0
MODEL_NAME = "rna004_130bps_hac@v3.0.1"
SEQUENCING_KIT = "sqk-rna004"
FLOW_CELL_PRODUCT_CODE = "FLO-MIN004RA"
T0 = dt.datetime(2025, 8, 31, 0, 0, 0, tzinfo=dt.UTC)  # fixed timestamps

# Newest pod5 that still writes POD5 v5, the format every pod5 reader can open (see the module
# docstring: the worker reads v6 as well, the committed sample stays v5 for older readers).
MAX_POD5_WRITER_VERSION = (0, 3, 45)

LEVELS_ENV = "RMODHUB_KMER_LEVELS"
LEVELS_BASENAME = "9mer_levels_v1.txt"
DEFAULT_LEVELS = (
    Path(__file__).resolve().parents[1] / "worker" / "directrm_vendor" / LEVELS_BASENAME
)

# Result of pushing the default data set through the UNMODIFIED upstream DirectRM scripts
# (sampling.py, feature_extraction.py) in a Python 3.9 environment. Re-measure and update when
# the design constants, the seed or the layout change; the README only quotes these numbers
# when they match the current arguments.
VALIDATION = {
    "seed": 20250831,
    "layout": "rna_raw",
    "n_reads_sampled": 76,
    "n_kmers": 3648,  # 48 k-mers per read (241-nt regions, step 5) x 76 reads
    "failed_reads": 0,
    "environment": "Python 3.9, ont-remora 3.2.0, pod5 0.3.35, pysam 0.24.0, numpy 2.0.2",
    # Pearson(per-base normalised signal mean, expected 9-mer level):
    "pearson_directrm_path": 0.749,  # unmodified DirectRM path (reverse_signal unset + refinement)
    "pearson_remora_reverse_signal": 0.994,  # remora's RNA convention, no refinement
    "pearson_remora_forward": 0.014,  # reverse_signal=False, no refinement
}


# ---- helpers ------------------------------------------------------------------------------
def load_levels(path: Path) -> dict[str, float]:
    """Read an ONT ``kmer_models`` level table (``<kmer>\\t<level>`` per line)."""
    levels: dict[str, float] = {}
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) != 2:
                continue
            kmer, value = parts
            if len(kmer) != KMER:
                raise SystemExit(f"{path}: expected {KMER}-mers, found {kmer!r}")
            levels[kmer] = float(value)
    if len(levels) != 4**KMER:
        raise SystemExit(f"{path}: expected {4**KMER} k-mers, found {len(levels)}")
    return levels


def base_levels(seq: str, levels: dict[str, float]) -> np.ndarray:
    """Expected level per base with remora ``extract_levels`` semantics.

    ``level[i] = table[seq[i - CENTER : i - CENTER + KMER]]``; bases without a full k-mer
    context (first CENTER, last KMER - CENTER - 1) get 0.
    """
    out = np.zeros(len(seq), dtype=np.float64)
    for i in range(CENTER, len(seq) - (KMER - CENTER - 1)):
        out[i] = levels[seq[i - CENTER : i - CENTER + KMER]]
    return out


def random_seq(rng: np.random.Generator, n: int) -> str:
    return "".join(rng.choice(list("ACGT"), size=n)) if n else ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


POD5_DIGEST_RECIPE = (
    "sha256 over reads sorted by read_id string: for each read, "
    "read_id (36 ASCII chars) + b'\\n' + signal as little-endian int16 bytes + b'\\n'"
)


def pod5_content_digest(path: Path) -> str:
    """Order-independent digest of (read_id, raw signal) pairs; see ``POD5_DIGEST_RECIPE``."""
    with pod5.Reader(path) as reader:
        items = [(str(rec.read_id), rec.signal.astype("<i2").tobytes()) for rec in reader.reads()]
    items.sort(key=lambda item: item[0])
    h = hashlib.sha256()
    for read_id, signal in items:
        h.update(read_id.encode("ascii"))
        h.update(b"\n")
        h.update(signal)
        h.update(b"\n")
    return h.hexdigest()


# ---- synthesis ----------------------------------------------------------------------------
@dataclass
class SynthRead:
    read_id: uuid.UUID
    ref_name: str
    ref_start: int  # 0-based leftmost reference position
    seq: str  # query incl. soft clips, 5' -> 3'
    qual: np.ndarray
    cigar: str
    md: str
    nm: int
    mv: np.ndarray  # moves (without the stride prefix), raw-signal order
    ts: int
    ns: int
    dac: np.ndarray  # int16 raw signal
    channel: int
    read_number: int


def make_reference(rng: np.random.Generator) -> dict[str, str]:
    refs: dict[str, str] = {}
    for name, _ in TRANSCRIPTS:
        length = int(rng.integers(TX_LEN_MIN, TX_LEN_MAX + 1))
        refs[name] = random_seq(rng, length - POLYA_LEN) + "A" * POLYA_LEN
    return refs


def md_and_nm(ref_core: str, query_core: str) -> tuple[str, int]:
    md, run, nm = "", 0, 0
    for r, q in zip(ref_core, query_core):
        if r == q:
            run += 1
        else:
            md += f"{run}{r}"
            run, nm = 0, nm + 1
    return md + str(run), nm


def synth_read(
    rng: np.random.Generator,
    refs: dict[str, str],
    levels: dict[str, float],
    ref_name: str,
    read_number: int,
    layout: str,
) -> SynthRead:
    ref = refs[ref_name]
    start = int(rng.integers(0, MAX_START_OFFSET))
    end = int(rng.integers(len(ref) - MAX_END_OFFSET + 1, len(ref) + 1))
    soft5 = int(rng.integers(0, MAX_SOFT_CLIP + 1))
    soft3 = int(rng.integers(0, MAX_SOFT_CLIP + 1))
    n_sub = int(rng.integers(0, MAX_SUBSTITUTIONS + 1))
    channel = int(rng.integers(1, 513))

    core = ref[start:end]
    query_core = list(core)
    for p in sorted(rng.choice(len(core), size=n_sub, replace=False).tolist()) if n_sub else []:
        query_core[p] = str(rng.choice([b for b in "ACGT" if b != core[p]]))
    query_core = "".join(query_core)
    md, nm = md_and_nm(core, query_core)
    clip5, clip3 = random_seq(rng, soft5), random_seq(rng, soft3)
    # The signal comes from the true molecule (reference core + clipped bases); substitutions
    # are basecall errors and therefore do not change the signal.
    molecule = clip5 + core + clip3
    seq = clip5 + query_core + clip3
    cigar = (f"{soft5}S" if soft5 else "") + f"{len(core)}M" + (f"{soft3}S" if soft3 else "")
    length = len(seq)

    lv = base_levels(molecule, levels)
    blocks = np.clip(
        np.round(rng.lognormal(DWELL_LOG_MEAN, DWELL_LOG_SD, size=length)), 1, DWELL_MAX_BLOCKS
    ).astype(int)
    n_blocks = int(blocks.sum())
    # per-base signal in 5' -> 3' base order
    pa_bases = [
        PA_SHIFT + PA_SCALE * lv[i] + rng.normal(0.0, PA_NOISE, size=int(blocks[i]) * STRIDE)
        for i in range(length)
    ]
    if layout == "dna_like":
        time_blocks, pa_body = blocks, np.concatenate(pa_bases)
    else:  # rna_raw: the 3' end enters the pore first -> time order is base L-1, ..., 0
        time_blocks, pa_body = blocks[::-1], np.concatenate(pa_bases[::-1])
    mv = np.zeros(n_blocks, dtype=np.int8)
    mv[np.concatenate([[0], np.cumsum(time_blocks)[:-1]])] = 1

    ts = int(rng.integers(LEADER_MIN, LEADER_MAX))
    tail = int(rng.integers(0, STRIDE))  # < stride samples left after ns
    leader = LEADER_PA + rng.normal(0.0, LEADER_NOISE, size=ts)
    trailer = PA_SHIFT + rng.normal(0.0, TAIL_PA_NOISE, size=tail)
    pa_all = np.concatenate([leader, pa_body, trailer])
    dac = np.clip(np.round(pa_all / CAL_SCALE - CAL_OFFSET), ADC_MIN, ADC_MAX).astype(np.int16)
    ns = ts + n_blocks * STRIDE  # dorado Trimmer.cpp semantics: ns = ts + len(moves) * stride
    assert mv.size == (ns - ts) // STRIDE and int(mv.sum()) == length
    qual = rng.integers(QUAL_MIN, QUAL_MAX + 1, size=length).astype(np.uint8)
    read_id = uuid.UUID(bytes=rng.bytes(16), version=4)
    return SynthRead(
        read_id=read_id,
        ref_name=ref_name,
        ref_start=start,
        seq=seq,
        qual=qual,
        cigar=cigar,
        md=md,
        nm=nm,
        mv=mv,
        ts=ts,
        ns=ns,
        dac=dac,
        channel=channel,
        read_number=read_number,
    )


def synth_reads(
    rng: np.random.Generator, refs: dict[str, str], levels: dict[str, float], layout: str
) -> list[SynthRead]:
    reads: list[SynthRead] = []
    for name, n_reads in TRANSCRIPTS:
        for _ in range(n_reads):
            reads.append(synth_read(rng, refs, levels, name, len(reads) + 1, layout))
    return reads


# ---- writers ------------------------------------------------------------------------------
def write_fasta(path: Path, refs: dict[str, str]) -> None:
    with open(path, "w", newline="\n") as fh:
        for name, seq in refs.items():
            fh.write(f">{name}\n")
            fh.writelines(seq[i : i + 60] + "\n" for i in range(0, len(seq), 60))
    pysam.faidx(str(path))


def write_regions(path: Path) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["seqnames", "start", "end", "width", "strand"])
        for name, start, end in REGIONS:
            writer.writerow([name, start, end, end - start + 1, "+"])


def run_id_for(seed: int) -> str:
    return hashlib.sha1(f"rmodhub synthetic signal sample seed={seed}".encode()).hexdigest()


def write_pod5(path: Path, reads: list[SynthRead], seed: int) -> None:
    run_id = run_id_for(seed)
    run_info = pod5.RunInfo(
        acquisition_id=run_id,
        acquisition_start_time=T0,
        adc_max=ADC_MAX,
        adc_min=ADC_MIN,
        context_tags={
            "sequencing_kit": SEQUENCING_KIT,
            "sample_frequency": str(SAMPLE_RATE),
            "experiment_type": "rna",
            "basecall_config_filename": "rna004_130bps_hac.cfg",
            "synthetic": "true",
        },
        experiment_name="rmodhub_synthetic_sample",
        flow_cell_id="SYNTH001",
        flow_cell_product_code=FLOW_CELL_PRODUCT_CODE,
        protocol_name="SYNTHETIC (no sequencing run): scripts/make_signal_sample.py",
        protocol_run_id=str(uuid.UUID(int=seed)),
        protocol_start_time=T0,
        sample_id="synthetic",
        sample_rate=SAMPLE_RATE,
        sequencing_kit=SEQUENCING_KIT,
        sequencer_position="SYNTH",
        sequencer_position_type="synthetic",
        software=f"rmodhub make_signal_sample.py {GENERATOR_VERSION} (pod5 {pod5.__version__})",
        system_name="synthetic",
        system_type="synthetic",
        tracking_id={"run_id": run_id, "synthetic": "true"},
    )
    median_before = float((PA_SHIFT + 110.0) / CAL_SCALE - CAL_OFFSET)
    if path.exists():
        path.unlink()
    with pod5.Writer(path, software_name=f"rmodhub make_signal_sample.py {GENERATOR_VERSION}") as w:
        w.add_reads(
            [
                pod5.Read(
                    read_id=r.read_id,
                    pore=pod5.Pore(channel=r.channel, well=1, pore_type="not_set"),
                    calibration=pod5.Calibration(offset=CAL_OFFSET, scale=CAL_SCALE),
                    read_number=r.read_number,
                    start_sample=i * 100_000,
                    median_before=median_before,
                    end_reason=pod5.EndReason(pod5.EndReasonEnum.SIGNAL_POSITIVE, False),
                    run_info=run_info,
                    signal=r.dac,
                )
                for i, r in enumerate(reads)
            ]
        )


def write_bam(
    path: Path, reads: list[SynthRead], refs: dict[str, str], seed: int, layout: str
) -> None:
    run_id = run_id_for(seed)
    rg_id = f"{run_id}_{MODEL_NAME}"
    synthetic = (
        f"SYNTHETIC data generated by RModHub scripts/make_signal_sample.py {GENERATOR_VERSION} "
        f"(seed={seed}, layout={layout}); no basecaller or sequencer was involved"
    )
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": name, "LN": len(seq)} for name, seq in refs.items()],
        "RG": [
            {
                "ID": rg_id,
                "PU": "SYNTH001",
                "PM": "synthetic",
                "DT": T0.isoformat(),
                "PL": "ONT",
                "DS": f"basecall_model={MODEL_NAME} runid={run_id} {synthetic}",
                "LB": "synthetic",
                "SM": "synthetic",
            }
        ],
        "PG": [
            {
                "ID": "basecaller",
                "PN": "dorado",
                "VN": "0.6.2-lookalike",
                "DS": synthetic,
                "CL": (
                    f"SYNTHETIC: uv run python scripts/make_signal_sample.py --seed {seed} "
                    f"--layout {layout} (emulates: dorado basecaller {MODEL_NAME} "
                    f"{POD5_NAME} --emit-moves --reference {FASTA_NAME})"
                ),
            }
        ],
        "CO": [synthetic],
    }
    tid = {name: i for i, name in enumerate(refs)}
    unsorted = path.with_name(path.stem + ".unsorted.bam")
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as bam:
        for i, r in enumerate(reads):
            a = pysam.AlignedSegment(bam.header)
            a.query_name = str(r.read_id)
            a.flag = 0
            a.reference_id = tid[r.ref_name]
            a.reference_start = r.ref_start
            a.mapping_quality = 60
            a.cigarstring = r.cigar
            a.query_sequence = r.seq
            a.query_qualities = array.array("B", r.qual.tolist())
            pa = (r.dac[r.ts : r.ns].astype(np.float64) + CAL_OFFSET) * CAL_SCALE
            shift = float(np.median(pa))
            scale = float(np.median(np.abs(pa - shift)) * 1.4826)
            a.set_tags(
                [
                    ("qs", round(float(r.qual.mean())), "i"),
                    ("du", float(r.dac.size / SAMPLE_RATE), "f"),
                    ("ns", int(r.ns), "i"),
                    ("ts", int(r.ts), "i"),
                    ("mx", 1, "i"),
                    ("ch", int(r.channel), "i"),
                    (
                        "st",
                        (T0 + dt.timedelta(seconds=60 * i)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                        "Z",
                    ),
                    ("rn", int(r.read_number), "i"),
                    ("fn", POD5_NAME, "Z"),
                    ("sm", shift, "f"),
                    ("sd", scale, "f"),
                    ("sv", "quantile", "Z"),
                    ("dx", 0, "i"),
                    ("RG", rg_id, "Z"),
                    ("mv", array.array("b", [STRIDE] + r.mv.tolist())),
                    ("NM", int(r.nm), "i"),
                    ("MD", r.md, "Z"),
                ]
            )
            bam.write(a)
    # --no-PG keeps the header free of machine-specific paths (byte-identical across runs).
    pysam.sort("--no-PG", "-o", str(path), str(unsorted))
    unsorted.unlink()
    pysam.index(str(path))


def write_readme(
    path: Path,
    refs: dict[str, str],
    reads: list[SynthRead],
    seed: int,
    layout: str,
    levels_sha256: str,
    versions: dict[str, str],
) -> None:
    n_total = len(reads)
    validated = VALIDATION["seed"] == seed and VALIDATION["layout"] == layout
    mean_len = float(np.mean([r.dac.size for r in reads]))
    tx_rows = "\n".join(
        f"| `{name}` | {len(refs[name])} | {n} | `{name},{start},{end},{end - start + 1},+` |"
        f" {'skipped: 12 reads <= --min_coverage 30' if n <= 30 else 'sampled'} |"
        for (name, n), (_, start, end) in zip(TRANSCRIPTS, REGIONS)
    )
    layout_text = {
        "rna_raw": (
            "`rna_raw` (default) - the real dorado convention for direct RNA: raw signal in "
            "sequencing time order (3' -> 5' of the read), BAM `seq`/`qual` reversed to 5' -> 3', "
            "`mv` table in raw-signal order (move *i* <-> base *L-1-i*), `ts`/`ns` in raw coordinates."
        ),
        "dna_like": (
            "`dna_like` - DIAGNOSTIC layout: signal time order equals the 5' -> 3' basecall order "
            "and the `mv` table follows it. This is not what dorado emits for RNA."
        ),
    }[layout]
    if validated:
        validation_text = (
            f"The unmodified upstream DirectRM scripts (`sampling.py`, `feature_extraction.py`; "
            f"{VALIDATION['environment']}) were run on this exact data set: `sampling.py` "
            f"(`--min_coverage 30 --max_coverage 150`) selects {VALIDATION['n_reads_sampled']} read "
            f"ids (tx_A + tx_B; tx_C dropped), `feature_extraction.py` (`--kmer 9 --step 5`) reports "
            f"`{VALIDATION['failed_reads']} failed` and writes `seq` with shape "
            f"(N, 9, 4), N = **{VALIDATION['n_kmers']}** k-mers (all dwell columns have non-zero "
            f"standard deviation, so the later z-scoring stages cannot crash on this input). "
            f"Measured orientation numbers on this data set: Pearson(per-base signal mean, "
            f"expected 9-mer level) = {VALIDATION['pearson_remora_reverse_signal']} with Remora's "
            f"RNA convention (`reverse_signal=True`), {VALIDATION['pearson_remora_forward']} "
            f"without it, and {VALIDATION['pearson_directrm_path']} through the unmodified "
            f"DirectRM path (no `reverse_signal`, after its banded refinement)."
        )
    else:
        validation_text = (
            "This data set was generated with non-default arguments and has NOT been pushed "
            "through the upstream DirectRM scripts; the validation numbers in "
            "`scripts/make_signal_sample.py::VALIDATION` apply to the default seed/layout only."
        )
    text = f"""# Synthetic nanopore sample data (signal branch)

**Everything in this directory is synthetic.** No RNA was sequenced: the reference
transcripts are random ACGT strings with a 20-nt poly(A) tail, and the signal is drawn from a
simple k-mer level model. The files exist so that the DirectRM pipeline can be exercised
end to end (`POST /api/jobs/signal/sample`) without a multi-GB download. Do not use them to
draw biological conclusions.

Generated by `scripts/make_signal_sample.py` version {GENERATOR_VERSION}:

```
uv run --with "pod5=={versions["pod5"]}" --with "lib-pod5=={versions["pod5"]}" \\
    python scripts/make_signal_sample.py --out app/samples/signal --seed {seed} --layout {layout} \\
    --levels worker/directrm_vendor/9mer_levels_v1.txt
```

| setting | value |
|---|---|
| seed | `{seed}` (`numpy.random.default_rng`) |
| layout | `{layout}` |
| k-mer level table | ONT `kmer_models` RNA004 `9mer_levels_v1.txt`, sha256 `{levels_sha256}` |
| generated with | Python {versions["python"]}, numpy {versions["numpy"]}, pod5 {versions["pod5"]}, pysam {versions["pysam"]} |
| pod5 format | written with pod5 <= {".".join(map(str, MAX_POD5_WRITER_VERSION))} (POD5 v5), which every pod5 reader opens, including the 0.3.35 pinned in `tools/Dockerfile.subset`; the worker (lib-pod5 0.3.47) reads v5 and the v6 that pod5 0.3.46+ writes |
| kit emulated | RNA004 (`{SEQUENCING_KIT}`, `{FLOW_CELL_PRODUCT_CODE}`, {SAMPLE_RATE} Hz, model stride {STRIDE}) |
| reads | {n_total} (mean {mean_len:.0f} samples, ~30 samples/base) |

## Files

| file | content |
|---|---|
| `{POD5_NAME}` | raw signal for all {n_total} reads (int16 DAC, `Calibration(offset={CAL_OFFSET:g}, scale={CAL_SCALE})`) |
| `{BAM_NAME}` + `.bai` | coordinate-sorted, dorado look-alike records: flag 0, MAPQ 60, `<S>M<S>` CIGAR, tags `qs du ns ts mx ch st rn fn sm sd sv dx RG mv NM MD` |
| `{FASTA_NAME}` + `.fai` | the 3 reference transcripts |
| `{REGIONS_NAME}` | DirectRM regions (`seqnames,start,end,width,strand`, 1-based inclusive) |
| `{MANIFEST_NAME}` | bytes + sha256 of every file, the pod5 content digest and the generator arguments |

## Transcripts, reads and regions

| transcript | length (nt) | reads | region | DirectRM sampling (`--min_coverage 30`) |
|---|---|---|---|---|
{tx_rows}

Every read spans its whole region (start offset < {MAX_START_OFFSET} nt, end offset < {MAX_END_OFFSET} nt,
0-{MAX_SOFT_CLIP} nt soft clips, 0-{MAX_SUBSTITUTIONS} substitutions with matching `MD`/`NM`,
Phred qualities {QUAL_MIN}-{QUAL_MAX}). Expected read counts per region: **40 / 36 / 12**;
`tx_C` is intentionally below the 30-read threshold so the job report shows the
coverage filter at work.

## Validation against upstream

{validation_text}

## Signal model and move-table orientation

Per base, dwell is a whole number of stride blocks (lognormal, mean ~6 blocks x {STRIDE} samples),
`pA = {PA_SHIFT:g} + {PA_SCALE:g} x level + N(0, {PA_NOISE})` with `level` taken from the 9-mer table at the
dominant position ({CENTER}), preceded by a leader of `ts ~ U({LEADER_MIN}, {LEADER_MAX})` samples. The
BAM satisfies the invariants Remora checks: `len(mv) - 1 == (ns - ts) // stride` and
`sum(moves) == len(query incl. soft clips)`.

Layout: {layout_text}

Orientation caveat: DirectRM's `feature_extraction.py` calls
`remora.io.Read.from_pod5_and_alignment(...)` without `reverse_signal=True`, i.e. it pairs
move *i* with base *i*. On real dorado RNA output (and therefore on the default `rna_raw`
layout) the per-base signal is thus mirrored within each read relative to Remora's own RNA
convention; the data set reproduces that upstream behaviour faithfully instead of hiding it.
The `--layout dna_like` option exists only as a diagnostic to see the pipeline on
self-consistent orientation.

## Determinism

Two runs with the same arguments produce byte-identical BAM, BAI, FASTA, FAI, CSV and
README. The pod5 container embeds a random file identifier, so its file hash differs between
runs; `{MANIFEST_NAME}` therefore also records a **content digest**
({POD5_DIGEST_RECIPE}) which is identical across runs.

## Licence

Generated data, no third-party sequence or signal is included. Released under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain dedication).
The k-mer level table used to synthesise the signal is ONT `kmer_models` (MPL-2.0); it is not
redistributed in this directory.
"""
    with open(path, "w", newline="\n") as fh:
        fh.write(text)


def write_manifest(
    out: Path, seed: int, layout: str, levels_sha256: str, versions: dict[str, str]
) -> dict:
    files: dict[str, dict] = {}
    for name in MANIFEST_FILES:
        p = out / name
        entry = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
        if name == POD5_NAME:
            entry["content_sha256"] = pod5_content_digest(p)
        files[name] = entry
    manifest = {
        "schema": 1,
        "synthetic": True,
        "license": "CC0-1.0",
        "generator": {
            "script": "scripts/make_signal_sample.py",
            "version": GENERATOR_VERSION,
            "args": {
                "seed": seed,
                "layout": layout,
                "levels": LEVELS_BASENAME,
                "levels_sha256": levels_sha256,
            },
            "versions": versions,
        },
        "kit": "RNA004",
        "sample_rate": SAMPLE_RATE,
        "stride": STRIDE,
        "expected_reads": {
            **{name: n for name, n in TRANSCRIPTS},
            "total": sum(n for _, n in TRANSCRIPTS),
        },
        "regions": [
            {"seqnames": name, "start": start, "end": end, "width": end - start + 1, "strand": "+"}
            for name, start, end in REGIONS
        ],
        "pod5_content_digest": {"algorithm": "sha256", "recipe": POD5_DIGEST_RECIPE},
        "files": files,
    }
    with open(out / MANIFEST_NAME, "w", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return manifest


# ---- CLI ----------------------------------------------------------------------------------
def resolve_levels(arg: str | None) -> Path:
    candidates = [arg, os.environ.get(LEVELS_ENV), str(DEFAULT_LEVELS)]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return Path(cand)
    raise SystemExit(
        f"9-mer level table not found. Pass --levels PATH, set ${LEVELS_ENV}, or vendor it at "
        f"{DEFAULT_LEVELS} (ONT kmer_models, rna004/{LEVELS_BASENAME})."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--out", type=Path, default=Path("app/samples/signal"), help="output directory (created)"
    )
    ap.add_argument("--seed", type=int, default=20250831, help="numpy default_rng seed")
    ap.add_argument(
        "--layout",
        choices=("rna_raw", "dna_like"),
        default="rna_raw",
        help="move-table/signal orientation (rna_raw = real dorado RNA output; "
        "dna_like = diagnostic only)",
    )
    ap.add_argument(
        "--levels",
        default=None,
        help=f"RNA004 9-mer level table (default: ${LEVELS_ENV} or {DEFAULT_LEVELS})",
    )
    ap.add_argument(
        "--allow-newer-pod5",
        action="store_true",
        help=f"write even with pod5 > {'.'.join(map(str, MAX_POD5_WRITER_VERSION))} (a POD5 v6 "
        "file: the worker reads it, pod5 < 0.3.46 readers do not; the committed sample stays v5)",
    )
    ap.add_argument(
        "--version", action="version", version=f"make_signal_sample {GENERATOR_VERSION}"
    )
    args = ap.parse_args(argv)

    pod5_version = tuple(int(x) for x in pod5.__version__.split(".")[:3])
    if pod5_version > MAX_POD5_WRITER_VERSION and not args.allow_newer_pod5:
        raise SystemExit(
            f"pod5 {pod5.__version__} writes POD5 v6 files. The worker reads them (lib-pod5 "
            "0.3.47), but the committed sample is kept as a v5 file so that every pod5 reader "
            "(e.g. the 0.3.35 pinned in tools/Dockerfile.subset) can open it and so that the "
            "sample's format never changes. Re-run with a v5 writer, e.g.\n"
            f'  uv run --with "pod5==0.3.35" --with "lib-pod5==0.3.35" python '
            f"scripts/make_signal_sample.py ...\n"
            f"or pass --allow-newer-pod5 if you really want a v6 file."
        )

    levels_path = resolve_levels(args.levels)
    levels = load_levels(levels_path)
    levels_sha256 = sha256_file(levels_path)
    versions = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pod5": pod5.__version__,
        "pysam": pysam.__version__,
    }
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(args.seed)
    refs = make_reference(rng)
    reads = synth_reads(rng, refs, levels, args.layout)

    write_fasta(out / FASTA_NAME, refs)
    write_regions(out / REGIONS_NAME)
    write_pod5(out / POD5_NAME, reads, args.seed)
    write_bam(out / BAM_NAME, reads, refs, args.seed, args.layout)
    write_readme(out / README_NAME, refs, reads, args.seed, args.layout, levels_sha256, versions)
    manifest = write_manifest(out, args.seed, args.layout, levels_sha256, versions)

    total = sum(entry["bytes"] for entry in manifest["files"].values())
    print(f"wrote {len(reads)} reads ({args.layout}, seed {args.seed}) to {out}")
    for name, entry in manifest["files"].items():
        print(f"  {name:26s} {entry['bytes']:>9,d} B  sha256 {entry['sha256'][:16]}...")
    print(f"  pod5 content digest {manifest['files'][POD5_NAME]['content_sha256']}")
    print(f"  total {total / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
