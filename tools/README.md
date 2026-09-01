# `subset_pod5` — shrink a whole-flowcell POD5 to the reads DirectRM will use

RModHub's nanopore signal branch (DirectRM) only ever reads the reads that overlap the
regions listed in your `regions.csv`: `sampling.py` fetches the alignments in each region
and `feature_extraction.py` then loads *those* reads — and nothing else — from the POD5.
A whole run (50–500 GB of POD5) is therefore mostly dead weight for an analysis restricted
to a few transcripts, and it does not fit the server's upload cap.

`tools/subset_pod5.py` extracts exactly those reads (plus a safety flank) into a small POD5
you can upload instead, and optionally the matching subset BAM. It is a single Python file
depending on `pod5` and `pysam` only.

## Copy-paste commands

### Docker (recommended: nothing to install)

```bash
# build once (from the RModHub checkout)
docker build -f tools/Dockerfile.subset -t rmodhub/subset:local tools

# run inside the directory that holds your files
docker run --rm -v "$PWD:/data" rmodhub/subset:local \
    -i /data/big.pod5 -b /data/in.bam -r /data/reg.csv \
    -o /data/small.pod5 --bam-out /data/small.bam
```

The container runs as uid/gid 1000; if that is not your user, add
`--user "$(id -u):$(id -g)"` so the output files belong to you. `-i` also accepts a
directory (`-i /data/pod5_pass`), searched recursively for `*.pod5`. Add `--dry-run` first
to see the read counts and the size estimate without writing anything.

### uv (RModHub checkout) or plain pip

```bash
# inside the RModHub checkout (its environment has pod5 0.3.47 and pysam)
uv run python tools/subset_pod5.py -i big.pod5 -b in.bam -r reg.csv -o small.pod5 --bam-out small.bam

# anywhere else (Python >= 3.10)
python -m venv subset-env && . subset-env/bin/activate
pip install "pod5==0.3.47" "lib-pod5==0.3.47" "pysam>=0.22"
python tools/subset_pod5.py -i big.pod5 -b in.bam -r reg.csv -o small.pod5 --bam-out small.bam
```

Then upload `small.pod5`, `small.bam` (its `.bai` is created next to it), your reference
FASTA and the same `regions.csv` on the **Nanopore signal** tab.

### Options

| option | default | meaning |
|---|---|---|
| `-i/--pod5 POD5 [POD5 ...]` | required | input `.pod5` file(s) and/or directories (recursive) |
| `-b/--bam BAM` | required | aligned, coordinate-sorted BAM (dorado `--emit-moves`); a `.bai` is created if missing |
| `-r/--regions CSV` | required | DirectRM regions CSV `seqnames,start,end[,width],strand`, 1-based inclusive |
| `-o/--out POD5` | required | output POD5 |
| `--bam-out BAM` | – | also write the matching subset BAM + `.bai` |
| `--flank NT` | 20 | widen every region on both sides (see below) |
| `--min-mapq Q` | 0 | drop alignments below this MAPQ (0 = keep all, like DirectRM) |
| `--dry-run` | – | count reads, check them against the POD5, estimate the size; write nothing |
| `--threads N` | 1 | threads for BAM (de)compression, sorting and indexing |
| `--force` | – | overwrite existing outputs |

Exit codes: `0` success, `2` usage/validation error (bad CSV, unknown contig, no reads
found, missing input), `1` unexpected error (traceback on stderr).

## What it does

1. Parses `regions.csv` and checks every `seqnames` against the BAM contigs.
2. For each region fetches every alignment overlapping `[start-1-flank, end+flank)`
   (0-based half-open) whose strand matches the region's strand — primary, secondary and
   supplementary records alike, exactly what Remora's `ReadIndexedBam.fetch` (used by
   DirectRM's `sampling.py`) yields — and records the parent read id (`pi` tag when present,
   else the query name). Each read id is kept once, in first-seen order.
3. Prints the per-region counts and a size estimate
   (`mean bytes per read of the input POD5 × selected reads`).
4. Streams the selected reads from the input POD5(s) into the output with `pod5.Writer`,
   one record batch at a time, **preserving input order** (file order within a file, files
   in command-line / sorted directory order) and every read field (signal, calibration,
   run info, pore, end reason, read number, start sample, ...). Memory stays flat
   whatever the input size; runtime scales with the number of selected reads plus one
   read-id index lookup per input file, not with the total signal volume.
5. With `--bam-out`, writes every fetched alignment of the selected reads (each record
   once, even if it overlaps several regions) to a coordinate-sorted, indexed BAM with a
   `@PG` line recording the command.

## Size estimate — worked example

`--dry-run` prints `input pod5 : N files, R reads, B bytes (mean b bytes/read)` and
`estimate : ~E bytes`, where `E = b × selected reads`. For instance:

* a 480 GB run with 12 M reads → **~40 kB per read** (vbz-compressed RNA004 signal);
* 2,000 reads overlap your regions → **~80 MB** output POD5;
* the matching subset BAM is typically a few MB (2,000 records with move tables).

The estimate assumes the selected reads have average length. Regions on very long
transcripts select longer-than-average reads, so allow some headroom. On the bundled
synthetic sample (88 reads, 1.27 MB) the estimate for all three regions is 1,272,752 bytes
and the actual output is 1,272,672 bytes.

## Why `--flank` (default 20 nt)

DirectRM's `sampling.py` passes the 1-based CSV `start`/`end` straight to pysam's 0-based
half-open `fetch`, so it effectively queries 1-based `start+1..end`; the DirectRM README
describes the file as 1-based inclusive, and other tools use `start-1..end`. The flank
widens each region so the subset is a **superset** of what any of these conventions fetch:
a read that the full-size run would have used can never be missing from the subset. The
extra reads are harmless — DirectRM only processes what `sampling.py` selects for the
regions — and they cost a few kilobytes each. `--flank 0` reproduces the literal
1-based-inclusive window.

## Results are the same as with the full POD5

DirectRM never looks at reads outside `reads.txt` (written by `sampling.py`), and every
feature is **read-local**: Remora maps a read's own signal to its own alignment and the
9-mer statistics are computed from that read alone. Therefore:

* **Same read set.** The subset BAM contains every alignment overlapping the (flanked)
  regions on the region strand, so `sampling.py` sees the same records in the same
  coordinate order as on the full BAM, counts the same coverage, drops/keeps the same
  regions and writes the same `reads.txt`. (For regions with `>= --max_coverage` reads
  upstream subsamples with an **unseeded** `random.sample`; that is not reproducible with
  the full POD5 either.)
* **Bit-identical features.** Each read's signal, move table, calibration and alignment are
  copied unchanged, so `feature_extraction.py` produces the same numbers per read.
* **Same k-mer batching order.** DirectRM orders k-mers by
  `list(set(pod5_read_ids) & set(reads.txt))` — a set whose iteration order depends only on
  the sampled read ids and `PYTHONHASHSEED` (fixed to `0` in the RModHub worker), not on
  the other reads in the POD5. Because the model LSTMs are batch-composition sensitive
  (`batch_first=False`, see `docs/signal-branch.md`), this matters: as long as the sampled
  read set is unchanged and every sampled read exists in the POD5 (the subset is then a
  superset of `reads.txt`, which is what makes CPython iterate the intersection in the
  same order; the tool warns when reads are missing), the batching order is unchanged and
  the site-level output is identical to a run on the full POD5. If you subset with a
  *different* region file or a MAPQ filter that changes the sampled set, the results change
  for the same reason they would change with the full POD5.

Upload the subset BAM together with the subset POD5: if you upload the full BAM instead,
`sampling.py` may select reads that are not in the subset POD5 and DirectRM will skip them
(it prints them as failed reads), which lowers coverage.

## POD5 format versions

pod5 0.3.46 changed the read-table schema (32-bit `channel` column, "POD5 v6"). Files written
by pod5 >= 0.3.46 — what MinKNOW, dorado and the `pod5` CLI produce today — are rejected by
readers older than 0.3.46 with `Schema field 'channel' is incorrect type: 'uint32'`. The
RModHub worker runs Python 3.10 with lib-pod5 **0.3.47** and therefore reads v5 and v6, i.e.
every file written by pod5 <= 0.3.47. Both ways of running the tool produce uploadable output:
the RModHub environment writes v6 (pod5 0.3.47) and the Docker image writes v5 (pod5 0.3.35,
readable by every pod5 release, which is also why the bundled sample is kept at v5). Should
you run the tool with a pod5 newer than 0.3.47, it prints a `WARNING` that the server may not
be able to open the output yet; if your *input* is newer than the tool's pod5, opening it
fails with a validation error telling you to upgrade pod5 (`pip install -U pod5`).

## Troubleshooting

* **`seqname 'X' is not a contig of the BAM`** — `regions.csv` and the BAM must use the same
  reference (transcript ids as in the FASTA header before the first space).
* **`no alignments overlap any region`** — check the coordinates (1-based inclusive) and the
  `strand` column (`+`/`-`; DirectRM matches reads to the region strand, and dorado RNA reads
  align to the `+` strand of a transcriptome).
* **`N selected read id(s) are missing from the pod5`** — the BAM was basecalled from a
  different run, or `-i` does not cover every POD5 file of the run (pass the whole
  directory), or reads were split by dorado without a `pi` tag. DirectRM would skip those
  reads as well.
* **The BAM must be the dorado `--emit-moves` BAM aligned to the same reference** —
  DirectRM needs the `mv`, `ts`, `ns` tags (move table) and an `MD` tag
  (`dorado aligner` writes it; `samtools calmd` can add it). The subset BAM keeps every tag
  unchanged.
* **`could not index ... `** — the BAM must be coordinate-sorted (`samtools sort`); with
  Docker the directory must be mounted read-write for the `.bai` to be created, or create
  it beforehand with `samtools index`.
* **`Schema field 'channel' is incorrect type`** — the input was written by a newer pod5 than
  the one running the tool (see *POD5 format versions* above); use the `uv run` command or
  `pip install -U pod5` rather than the 0.3.35 Docker image.
* **Output owned by root / permission denied with Docker** — run with
  `--user "$(id -u):$(id -g)"`.
* **Estimate vs. reality** — the estimate is the input's mean bytes per read times the number
  of selected reads; long reads in your regions make the real file larger.

## Measured on the bundled sample

`app/samples/signal` (88 synthetic RNA004-like reads, 1,272,752 bytes POD5, 3 regions):

* `uv run python tools/subset_pod5.py ... --bam-out` — 0.36 s wall (0.26 s with pod5 0.3.35),
  138 MB peak RSS, 88/88 reads found, output 1,272,672 bytes (pod5 0.3.35) or 1,273,904 bytes
  (pod5 0.3.47 writes slightly more container metadata).
* Docker image `rmodhub/subset:local` (python:3.12-slim + pod5 0.3.35 + pysam 0.24.0):
  ~390 MB unpacked (base image 134 MB + one 254 MB dependency layer; `docker images` reports
  509 MB with the containerd snapshotter, ~120 MB compressed). pyarrow (114 MB, required by
  pod5's reader) and pysam (63 MB) are the floor; Flight, C headers, tests and pip are
  removed. The same run through `docker run` takes 1.0 s wall including container start-up.
