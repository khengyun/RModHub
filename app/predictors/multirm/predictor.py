"""MultiRM sequence predictor: loaded once, kept in RAM, CPU-only, batched inference.

This replaces the upstream `Scripts/main.py` CLI. Everything the CLI did per process
(unpickle embeddings, build the network, load weights, read `neg_prob.csv`) happens once
in `MultiRMPredictor.load()`. A call to `predict`/`predict_matrix` then only encodes the
sequence, runs the network in fixed-size batches under `torch.inference_mode()`, and
post-processes with numpy. There is no subprocess, no `os.chdir`, no per-request I/O.

Numerics are identical to upstream (see `vendor/UPSTREAM.md`); the golden fixture in
`tests/fixtures/golden_multirm_151nt` is the acceptance test.
"""

from __future__ import annotations

import hashlib
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from app.predictors.base import FLANK_NT, MIN_SEQUENCE_NT, WINDOW_NT, SequencePrediction
from app.predictors.multirm.adapter import (
    AttentionWindows,
    MultiRMMatrices,
    matrices_to_sites,
    sites_attention,
)
from app.predictors.multirm.vendor.attention_utils import cal_attention, highest_x
from app.predictors.multirm.vendor.models import model_v3
from app.schemas import MOD_TYPES

DEFAULT_WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
WEIGHTS_FILE = "trained_model_51seqs.pkl"
EMBEDDINGS_FILE = "embeddings_12RM.pkl"
NEG_PROB_FILE = "neg_prob.csv"

KMER = 3
KMERS_PER_WINDOW = WINDOW_NT - KMER + 1  # 49
N_NEGATIVES = 150  # columns of neg_prob.csv
ATT_WINDOW = 3  # upstream `--att_window`: width of the attention windows reported
ATT_TOP = 3  # upstream `--top`: number of attention windows reported per site

_ALPHABET = "ACGT"
# ASCII byte -> base code (A=0, C=1, G=2, T=3); anything else -> 255 (invalid).
_BASE_CODE = np.full(256, 255, dtype=np.uint8)
for _code, _byte in enumerate(_ALPHABET.encode("ascii")):
    _BASE_CODE[_byte] = _code


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_state_dict(path: Path) -> dict:
    try:
        return torch.load(path, map_location="cpu", weights_only=True)
    except (pickle.UnpicklingError, RuntimeError):  # pragma: no cover
        # Fallback for checkpoints pickled with objects the safe unpickler rejects. The
        # vendored checkpoint is a plain OrderedDict of tensors, so this path is not taken.
        return torch.load(path, map_location="cpu", weights_only=False)


def _build_kmer_table(embedding_keys: list[str]) -> np.ndarray:
    """Map each ACGT 3-mer (encoded as 16*b0 + 4*b1 + b2) to its row in the embedding
    matrix, i.e. its position in the upstream dict's key order (what `seq2index` used)."""
    table = np.full(4**KMER, -1, dtype=np.int64)
    for index, kmer in enumerate(embedding_keys):
        if len(kmer) == KMER and set(kmer) <= set(_ALPHABET):
            code = 0
            for base in kmer:
                code = code * 4 + _ALPHABET.index(base)
            table[code] = index
    missing = np.flatnonzero(table < 0)
    if missing.size:
        raise RuntimeError(f"embedding table lacks {missing.size} ACGT 3-mers")
    return table


class MultiRMPredictor:
    """Implements `app.predictors.base.SequencePredictor` on top of the vendored MultiRM."""

    min_sequence_nt = MIN_SEQUENCE_NT
    max_sequence_nt = None  # bounded by RMODHUB_MAX_SEQUENCE_NT, not by the model
    name = "MultiRM"
    version = "trained_model_51seqs"

    def __init__(
        self,
        model: model_v3,
        kmer_table: np.ndarray,
        neg_prob_sorted: np.ndarray,
        *,
        batch_size: int,
        weights_sha256: str,
        weights_dir: Path,
    ):
        # All attributes are read-only after construction; `predict*` never mutates them.
        self._model = model
        self._kmer_table = kmer_table
        self._neg_prob_sorted = neg_prob_sorted  # (12, 150) float64, each row ascending
        self._batch_size = int(batch_size)
        self._weights_sha256 = weights_sha256
        self._weights_dir = weights_dir

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(
        cls,
        weights_dir: Path | None = None,
        *,
        batch_size: int = 256,
        num_threads: int | None = None,
    ) -> MultiRMPredictor:
        """Load weights, embeddings and the negative background once and return a
        ready-to-use predictor."""
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        weights_dir = Path(weights_dir) if weights_dir is not None else DEFAULT_WEIGHTS_DIR
        weights_path = weights_dir / WEIGHTS_FILE
        embeddings_path = weights_dir / EMBEDDINGS_FILE
        neg_prob_path = weights_dir / NEG_PROB_FILE
        for path in (weights_path, embeddings_path, neg_prob_path):
            if not path.is_file():
                raise FileNotFoundError(f"MultiRM asset missing: {path}")

        if num_threads is not None:
            torch.set_num_threads(int(num_threads))

        # 3-mer -> 300-d embeddings; the dict's key order defines the token index.
        with embeddings_path.open("rb") as fh:
            embeddings: dict[str, np.ndarray] = pickle.load(fh)  # vendored, trusted asset
        embedding_keys = list(embeddings.keys())
        embedding_matrix = torch.from_numpy(
            np.stack([np.asarray(embeddings[k], dtype=np.float32) for k in embedding_keys])
        )
        kmer_table = _build_kmer_table(embedding_keys)

        model = model_v3(
            num_task=len(MOD_TYPES), use_embedding=True, embedding_weights=embedding_matrix
        )
        model.load_state_dict(_load_state_dict(weights_path), strict=True)
        model.eval()
        for param in model.parameters():
            param.requires_grad_(False)

        # Negative-background probabilities: 12 rows (mod type name, 150 floats). Read with
        # exactly the upstream pandas call: the last row has 149 values plus an empty
        # trailing field, which pandas turns into NaN. Upstream's `neg > p` is False for
        # NaN while the denominator stays 150, so NaN is mapped to -inf (never > p).
        neg_df = pd.read_csv(neg_prob_path, header=None, index_col=0)
        neg_names = [str(x) for x in neg_df.index]
        if tuple(neg_names) != MOD_TYPES:
            raise RuntimeError(f"neg_prob.csv row order {neg_names} != MOD_TYPES {list(MOD_TYPES)}")
        neg_prob = neg_df.to_numpy(dtype=np.float64)
        if neg_prob.shape != (len(MOD_TYPES), N_NEGATIVES):
            raise RuntimeError(f"neg_prob.csv has shape {neg_prob.shape}")
        neg_prob = np.where(np.isnan(neg_prob), -np.inf, neg_prob)
        # Sorted rows let the p-value (count of negatives strictly above p) be a searchsorted.
        neg_prob_sorted = np.sort(neg_prob, axis=1)

        return cls(
            model,
            kmer_table,
            neg_prob_sorted,
            batch_size=batch_size,
            weights_sha256=_sha256(weights_path),
            weights_dir=weights_dir,
        )

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def weights_sha256(self) -> str:
        return self._weights_sha256

    # ---------------------------------------------------------------- encoding
    @staticmethod
    def _normalise(sequence: str) -> str:
        if not isinstance(sequence, str):
            raise TypeError("sequence must be a str")
        sequence = sequence.strip().upper().replace("U", "T")
        n = len(sequence)
        if n < MIN_SEQUENCE_NT:
            raise ValueError(
                f"sequence is {n} nt long; MultiRM needs at least {MIN_SEQUENCE_NT} nt"
            )
        bad = sorted(set(sequence) - set(_ALPHABET))
        if bad:
            raise ValueError(f"sequence contains characters outside ACGT/U: {''.join(bad)[:20]!r}")
        return sequence

    def _windows(self, sequence: str) -> np.ndarray:
        """(N-50, 49) int64 matrix of k-mer token indices, one row per 51-nt window.

        Equivalent to upstream `seq2index(seq[pos:pos+51])` for every `pos`, but computed
        with a lookup table and a strided view instead of O(len(dict)) per k-mer."""
        codes = _BASE_CODE[np.frombuffer(sequence.encode("ascii"), dtype=np.uint8)]
        codes = codes.astype(np.int64)
        kmer_codes = 16 * codes[:-2] + 4 * codes[1:-1] + codes[2:]  # (N-2,)
        kmer_index = self._kmer_table[kmer_codes]
        return np.lib.stride_tricks.sliding_window_view(kmer_index, KMERS_PER_WINDOW)

    # --------------------------------------------------------------- inference
    def _forward(
        self, windows: np.ndarray, *, with_attention: bool
    ) -> tuple[np.ndarray, np.ndarray | None]:
        """Run the network over all windows in chunks of `batch_size`.

        Returns probs (W, 12) float32 and, if requested, attention (W, 49, 12) float32."""
        n_windows = windows.shape[0]
        probs = np.empty((n_windows, len(MOD_TYPES)), dtype=np.float32)
        attention = (
            np.empty((n_windows, KMERS_PER_WINDOW, len(MOD_TYPES)), dtype=np.float32)
            if with_attention
            else None
        )
        with torch.inference_mode():
            for start in range(0, n_windows, self._batch_size):
                stop = min(start + self._batch_size, n_windows)
                # `.copy()` materialises the strided view as a fresh, writable array.
                batch = torch.from_numpy(windows[start:stop].copy())
                out, att = self._model(batch)
                probs[start:stop] = out.numpy()
                if attention is not None:
                    attention[start:stop] = att.numpy()
        return probs, attention

    def _p_values(self, probs: np.ndarray) -> np.ndarray:
        """Empirical p-value per (mod type, window): share of the 150 negatives whose
        probability is strictly greater than the window's probability.

        `probs` is (12, W) float64 (exact upcast of the float32 outputs, as upstream's
        `neg_prob.iloc[k, :] > y_prob[k]` comparison does)."""
        p_values = np.empty_like(probs)
        for k in range(len(MOD_TYPES)):
            n_le = np.searchsorted(self._neg_prob_sorted[k], probs[k], side="right")
            p_values[k] = (N_NEGATIVES - n_le) / N_NEGATIVES
        return p_values

    def predict_matrix(
        self, sequence: str, alpha: float = 0.05, *, with_attention: bool = True
    ) -> MultiRMMatrices:
        """Score every 51-nt window of `sequence` and return the upstream-style matrices.

        `attention` is all zeros when `with_attention=False` (skips the attention
        post-processing, which is the slow, pure-Python part for long inputs)."""
        t0 = time.perf_counter()
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be within [0, 1]")
        sequence = self._normalise(sequence)
        n = len(sequence)
        windows = self._windows(sequence)
        n_windows = windows.shape[0]  # == n - 50

        probs32, att32 = self._forward(windows, with_attention=with_attention)

        probs = probs32.T.astype(np.float64)  # (12, W)
        p_values = self._p_values(probs)
        significant = p_values < alpha  # (12, W)

        labels = np.zeros((len(MOD_TYPES), n), dtype=np.int64)
        labels[:, FLANK_NT : n - FLANK_NT] = significant

        attention = np.zeros((len(MOD_TYPES), n), dtype=np.int64)
        attention_windows: AttentionWindows | None = None
        if with_attention and att32 is not None:
            attention_windows = self._attention_windows(att32, significant)
            for (k, _w), windows in attention_windows.items():
                for start, end, _score in windows:
                    attention[k, start : end + 1] = 1

        positions = np.arange(FLANK_NT + 1, FLANK_NT + 1 + n_windows, dtype=np.int64)
        return MultiRMMatrices(
            positions=positions,
            probs=probs,
            p_values=p_values,
            labels=labels,
            attention=attention,
            inference_ms=(time.perf_counter() - t0) * 1000.0,
            attention_windows=attention_windows,
        )

    @staticmethod
    def _attention_windows(att32: np.ndarray, significant: np.ndarray) -> AttentionWindows:
        """Top-`ATT_TOP` attention windows (width `ATT_WINDOW`) of every significant
        (mod type k, window w) pair, as 0-based absolute nucleotide ranges, best first.
        OR-ing them into a (12, N) mask reproduces upstream's `attention.csv`."""
        out: AttentionWindows = {}
        sig_windows = np.flatnonzero(significant.any(axis=0))
        if sig_windows.size == 0:
            return out
        # (n_sig, 12, 51) float64 per-nucleotide attention for the windows that need it.
        per_nt = cal_attention(att32[sig_windows])
        for row, w in enumerate(sig_windows):
            for k in np.flatnonzero(significant[:, w]):
                ranked = highest_x(per_nt[row, k], w=ATT_WINDOW)
                # Upstream indexes ranked[1..top] unconditionally; a 51-nt window with
                # w=3, p=1 always yields >= 3 windows, the guard only makes it explicit.
                out[(int(k), int(w))] = [
                    (int(start + w), int(end + w), float(score))
                    for score, start, end in (
                        ranked[rank] for rank in range(1, min(ATT_TOP, len(ranked)) + 1)
                    )
                ]
        return out

    # --------------------------------------------------------------- protocol
    def predict(
        self, sequence: str, alpha: float = 0.05, *, include_attention: bool = False
    ) -> SequencePrediction:
        t0 = time.perf_counter()
        matrices = self.predict_matrix(sequence, alpha, with_attention=include_attention)
        sites = matrices_to_sites(matrices, alpha)
        attention = sites_attention(matrices, sites) if include_attention else None
        n = matrices.sequence_length
        return SequencePrediction(
            sites=sites,
            sequence_length=n,
            predicted_start=FLANK_NT + 1,
            predicted_end=n - FLANK_NT,
            alpha=alpha,
            model_name=self.name,
            model_version=self.version,
            inference_ms=(time.perf_counter() - t0) * 1000.0,
            extra={
                "n_windows": matrices.n_windows,
                "batch_size": self._batch_size,
                "weights_sha256": self._weights_sha256,
            },
            attention=attention,
        )

    def warmup(self) -> None:
        self.predict("A" * MIN_SEQUENCE_NT)
