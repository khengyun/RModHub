#!/usr/bin/env python
"""Vendor DirectRM into ``worker/directrm_vendor/`` from a local clone.

Usage (from the repository root or anywhere)::

    uv run --project worker python worker/scripts/vendor_directrm.py <path-to-DirectRM-clone> \
        [--dest worker/directrm_vendor] [--expect-commit bc7a08573dfe7629e808256fa6ade6e4111ed1f9]

What it does (see ``docs/signal-branch.md`` section 2 and ``directrm_vendor/UPSTREAM.md``):

* copies ``scripts/*.py``, ``utils/*.py``, ``5mer_levels_v1.txt``, ``9mer_levels_v1.txt`` and
  ``LICENSE`` byte-for-byte (``__pycache__``, ``.DS_Store``, ``figure_reproduce.Rmd``,
  ``README.md`` and the environment yml files are not vendored);
* copies ``model/RNA002/**`` and ``model/RNA004/**`` where every ``model.pt`` is re-serialised
  to CPU storage (``torch.save(torch.load(p, map_location="cpu"), q)``) because the upstream
  scripts call ``torch.load`` without ``map_location`` and the shipped files carry ``cuda:0``
  storage tags. Values are verified identical (``torch.equal`` on every tensor, same key order)
  and stray files (``train_perf.txt``, ``.DS_Store``) are skipped;
* writes ``WEIGHTS_MANIFEST.json`` with, per weight file, the sha256 of the upstream file, the
  sha256 of the vendored file, the number of tensors and the verification flag, plus the sha256 of
  every verbatim-copied file under ``"verbatim"``.

The script refuses to run if the clone's HEAD is not the expected commit (override with
``--expect-commit ''``). Re-running is idempotent for the verbatim files and for the manifest; the
re-serialised ``.pt`` files are byte-stable for a given torch version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

EXPECTED_COMMIT = "bc7a08573dfe7629e808256fa6ade6e4111ed1f9"
VERBATIM_TOP = ("5mer_levels_v1.txt", "9mer_levels_v1.txt", "LICENSE")
VERBATIM_DIRS = ("scripts", "utils")
MODEL_KITS = ("RNA002", "RNA004")
SKIP_NAMES = {".DS_Store", "__pycache__", "train_perf.txt"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(clone: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def reserialise_weights(src: Path, dst: Path) -> dict:
    """Load ``src`` on CPU, save to ``dst`` and verify the round trip is value-identical."""
    import torch

    original = torch.load(src, map_location="cpu")
    if not isinstance(original, (dict, OrderedDict)):
        raise TypeError(f"{src}: expected a state_dict, got {type(original).__name__}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    torch.save(original, dst)
    # The vendored file must load WITHOUT map_location on a CPU-only box (what upstream does).
    reloaded = torch.load(dst)
    if list(reloaded.keys()) != list(original.keys()):
        raise RuntimeError(f"{src}: key order changed on re-serialisation")
    n_tensors = 0
    for key, value in original.items():
        other = reloaded[key]
        if torch.is_tensor(value):
            n_tensors += 1
            if value.dtype != other.dtype or value.shape != other.shape:
                raise RuntimeError(f"{src}: tensor {key} changed dtype/shape")
            if not torch.equal(value, other):
                raise RuntimeError(f"{src}: tensor {key} is not equal after re-serialisation")
            if other.device.type != "cpu":
                raise RuntimeError(f"{src}: tensor {key} is not on CPU after re-serialisation")
        elif value != other:
            raise RuntimeError(f"{src}: non-tensor entry {key} differs")
    return {
        "original_sha256": sha256_file(src),
        "vendored_sha256": sha256_file(dst),
        "n_tensors": n_tensors,
        "verified_equal": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "clone", type=Path, help="path to a checkout of github.com/yuxinPenny/DirectRM"
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "directrm_vendor",
        help="destination directory (default: worker/directrm_vendor)",
    )
    parser.add_argument(
        "--expect-commit",
        default=EXPECTED_COMMIT,
        help="refuse to vendor unless the clone's HEAD is this commit ('' to skip the check)",
    )
    args = parser.parse_args(argv)

    clone: Path = args.clone.resolve()
    dest: Path = args.dest.resolve()
    if not (clone / "scripts" / "feature_extraction.py").is_file():
        print(f"error: {clone} does not look like a DirectRM checkout", file=sys.stderr)
        return 2
    head = git_head(clone)
    if args.expect_commit:
        if head is None:
            print(
                "error: cannot determine the clone's commit (not a git checkout?)", file=sys.stderr
            )
            return 2
        if head != args.expect_commit:
            print(f"error: clone is at {head}, expected {args.expect_commit}", file=sys.stderr)
            return 2

    dest.mkdir(parents=True, exist_ok=True)
    verbatim: dict[str, str] = {}
    weights: dict[str, dict] = {}

    def copy_one(rel: Path) -> None:
        src = clone / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        digest = sha256_file(src)
        if sha256_file(dst) != digest:
            raise RuntimeError(f"copy of {rel} is not byte-identical")
        verbatim[rel.as_posix()] = digest

    for name in VERBATIM_TOP:
        copy_one(Path(name))
    for dirname in VERBATIM_DIRS:
        for src in sorted((clone / dirname).glob("*.py")):
            copy_one(src.relative_to(clone))

    for kit in MODEL_KITS:
        kit_dir = clone / "model" / kit
        if not kit_dir.is_dir():
            raise RuntimeError(f"missing {kit_dir}")
        for src in sorted(kit_dir.rglob("*")):
            if not src.is_file():
                continue
            if src.name in SKIP_NAMES or any(part in SKIP_NAMES for part in src.parts):
                continue
            rel = src.relative_to(clone)
            if src.suffix == ".pt":
                weights[rel.as_posix()] = reserialise_weights(src, dest / rel)
            else:
                copy_one(rel)

    import torch

    manifest = {
        "upstream": {
            "repository": "https://github.com/yuxinPenny/DirectRM",
            "commit": head or "unknown",
            "license": "MIT (c) 2025 Yuxin Zhang",
        },
        "weights_transform": (
            "torch.save(torch.load(p, map_location='cpu'), q); verified with torch.equal on every "
            "tensor and identical key order"
        ),
        "torch_version_used": torch.__version__,
        "weights": dict(sorted(weights.items())),
        "verbatim": dict(sorted(verbatim.items())),
    }
    (dest / "WEIGHTS_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"vendored {len(verbatim)} verbatim files and {len(weights)} weight files into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
