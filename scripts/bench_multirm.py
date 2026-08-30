#!/usr/bin/env python
"""Benchmark the in-process MultiRM predictor: load time, RSS, per-call latency.

Run from the repo root:

    uv run python scripts/bench_multirm.py [--batch-size 256] [--threads N] [--length 10000]
"""

from __future__ import annotations

import argparse
import os
import random
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GOLDEN_SEQ = ROOT / "tests" / "fixtures" / "golden_multirm_151nt" / "sequence.txt"


def rss_mb() -> float:
    """Current resident set size in MiB (Linux /proc)."""
    with open("/proc/self/statm") as fh:
        resident_pages = int(fh.read().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / 2**20


def peak_rss_mb() -> float:
    """Peak resident set size of this process in MiB (ru_maxrss is KiB on Linux)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--threads", type=int, default=None, help="torch.set_num_threads")
    parser.add_argument("--length", type=int, default=10_000, help="random sequence length")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    print(f"RSS at start:                 {rss_mb():8.1f} MB")

    _, import_ms = timed(lambda: __import__("torch"))
    import torch

    from app.predictors.multirm import MultiRMPredictor

    print(f"import torch + app:           {import_ms:8.1f} ms   RSS {rss_mb():8.1f} MB")

    rss_before_load = rss_mb()
    predictor, load_ms = timed(
        lambda: MultiRMPredictor.load(batch_size=args.batch_size, num_threads=args.threads)
    )
    print(
        f"MultiRMPredictor.load():      {load_ms:8.1f} ms   RSS {rss_mb():8.1f} MB "
        f"(+{rss_mb() - rss_before_load:.1f} MB)   torch threads={torch.get_num_threads()} "
        f"batch_size={args.batch_size}"
    )

    seq = GOLDEN_SEQ.read_text().strip()
    for label in ("first", "second", "third"):
        result, ms = timed(lambda: predictor.predict(seq))
        print(
            f"predict(golden {len(seq)} nt) {label:6s}: {ms:8.1f} ms   "
            f"({result.inference_ms:.1f} ms internal, {len(result.sites)} sites)"
        )
    matrices, ms = timed(lambda: predictor.predict_matrix(seq, with_attention=True))
    print(f"predict_matrix(golden, attention=True): {ms:8.1f} ms")

    rng = random.Random(args.seed)
    long_seq = "".join(rng.choices("ACGT", k=args.length))
    rss_before = rss_mb()
    peak_before = peak_rss_mb()
    result, ms = timed(lambda: predictor.predict(long_seq))
    rss_after = rss_mb()
    peak_after = peak_rss_mb()
    print(
        f"predict(random {args.length} nt):   {ms:8.1f} ms   {len(result.sites)} sites   "
        f"RSS before {rss_before:.1f} MB -> after {rss_after:.1f} MB, "
        f"peak {peak_after:.1f} MB (growth vs before-call: {peak_after - rss_before:.1f} MB, "
        f"peak before call {peak_before:.1f} MB)"
    )
    result, ms = timed(lambda: predictor.predict(long_seq))
    print(f"predict(random {args.length} nt) again: {ms:8.1f} ms   RSS {rss_mb():.1f} MB")

    rss_before = rss_mb()
    matrices, ms = timed(lambda: predictor.predict_matrix(long_seq, with_attention=True))
    print(
        f"predict_matrix(random {args.length} nt, attention=True): {ms:8.1f} ms   "
        f"{int(matrices.labels.sum())} significant (k,pos) pairs   "
        f"peak RSS {peak_rss_mb():.1f} MB (growth vs before-call: "
        f"{peak_rss_mb() - rss_before:.1f} MB)"
    )


if __name__ == "__main__":
    main()
