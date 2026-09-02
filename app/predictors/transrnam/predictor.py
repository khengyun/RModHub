"""TransRNAm sequence predictor: loaded once, kept in RAM, CPU-only, batched inference.

Same 12 modifications and the same frozen 3-mer embedding table as MultiRM (the file is
byte-identical, see `weights/WEIGHTS_MANIFEST.json`), but a 601-nt context window and a
transformer+CNN trunk instead of MultiRM's 51-nt BiLSTM. That makes it the natural
head-to-head partner in the model picker, and also much slower per site, which is why the
accepted input is capped well below the API's own 10,000-nt limit.

Upstream ships no null distribution, so there is no empirical p-value to threshold on:
sites are reported when the model's own sigmoid clears `SITE_THRESHOLD`, `p_value` is left
empty, and the request's `alpha` is echoed but does not filter. `meta.note` says so.
"""

from __future__ import annotations

import hashlib
import pickle
import time
from pathlib import Path

import numpy as np
import torch

from app.predictors.base import SequencePrediction
from app.schemas import MOD_TYPES, ModSite

DEFAULT_WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
WEIGHTS_FILE = "transrnam.pt"
EMBEDDINGS_FILE = "embeddings_12RM.pkl"

KMER = 3
FLANK_NT = 300
WINDOW_NT = 2 * FLANK_NT + 1  # 601
KMERS_PER_WINDOW = WINDOW_NT - KMER + 1  # 599, the width the checkpoint was trained on
MIN_SEQUENCE_NT = WINDOW_NT

# ~18 ms per site on four CPU threads: 2,000 nt is about 25 s, which is the most a
# synchronous request should cost. Longer inputs are refused with a message naming the cap.
MAX_SEQUENCE_NT = 2_000
DEFAULT_BATCH_SIZE = 32
SITE_THRESHOLD = 0.5


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TransRNAmPredictor:
    """Implements `app.predictors.base.SequencePredictor`."""

    name = "TransRNAm"
    version = "Best_weights"
    min_sequence_nt = MIN_SEQUENCE_NT
    max_sequence_nt = MAX_SEQUENCE_NT

    def __init__(self, model, kmer_index: dict[str, int], weights_sha256: str, batch_size: int):
        self._model = model
        self._kmer_index = kmer_index
        self._weights_sha256 = weights_sha256
        self._batch_size = batch_size
        # Lookup table over the 64 ACGT 3-mers: base codes (A=0..T=3) -> vocabulary index.
        self._table = np.full(64, -1, dtype=np.int64)
        for kmer, idx in kmer_index.items():
            if all(b in "ACGT" for b in kmer):
                a, b, c = ("ACGT".index(x) for x in kmer)
                self._table[a * 16 + b * 4 + c] = idx
        if (self._table < 0).any():
            raise ValueError("the embedding table does not cover every ACGT 3-mer")

    @classmethod
    def load(
        cls,
        weights_dir: Path | str = DEFAULT_WEIGHTS_DIR,
        *,
        num_threads: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> TransRNAmPredictor:
        from app.predictors.transrnam.vendor.models import model_v11

        if num_threads is not None:
            torch.set_num_threads(num_threads)
        weights_dir = Path(weights_dir)
        with (weights_dir / EMBEDDINGS_FILE).open("rb") as fh:
            embeddings = pickle.load(fh)
        kmer_index = {kmer: i for i, kmer in enumerate(embeddings)}

        model = model_v11(num_embeddings=len(embeddings))
        weights_path = weights_dir / WEIGHTS_FILE
        # weights_only=True: the served file holds tensors only, never executable pickle.
        state = torch.load(weights_path, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)
        model.eval()
        return cls(model, kmer_index, _sha256(weights_path), batch_size)

    # --------------------------------------------------------------- encoding
    def _kmer_indices(self, sequence: str) -> np.ndarray:
        """Sequence -> one vocabulary index per 3-mer (length n-2)."""
        codes = np.frombuffer(sequence.encode("ascii"), dtype=np.uint8)
        lut = np.full(256, 255, dtype=np.uint8)
        for code, byte in enumerate(b"ACGT"):
            lut[byte] = code
        base = lut[codes].astype(np.int64)
        if (base > 3).any():
            raise ValueError("sequence must contain only A, C, G, T (U already mapped to T)")
        return self._table[base[:-2] * 16 + base[1:-1] * 4 + base[2:]]

    def _windows(self, kmers: np.ndarray, n: int) -> np.ndarray:
        """(n_sites, 599) view: row s is the window centred on 0-based base FLANK_NT+s."""
        n_sites = n - 2 * FLANK_NT
        starts = np.arange(n_sites)[:, None]
        return kmers[starts + np.arange(KMERS_PER_WINDOW)[None, :]]

    # --------------------------------------------------------------- protocol
    def predict(
        self, sequence: str, alpha: float = 0.05, *, include_attention: bool = False
    ) -> SequencePrediction:
        t0 = time.perf_counter()
        n = len(sequence)
        if n < MIN_SEQUENCE_NT:
            raise ValueError(
                f"sequence is {n} nt long; TransRNAm scores a {WINDOW_NT}-nt window and "
                f"needs at least {MIN_SEQUENCE_NT} nt"
            )
        if n > MAX_SEQUENCE_NT:
            raise ValueError(
                f"sequence is {n:,} nt long; TransRNAm accepts at most {MAX_SEQUENCE_NT:,} nt "
                "(it scores a 601-nt window per site and is far slower than MultiRM)"
            )

        windows = self._windows(self._kmer_indices(sequence), n)
        probs = np.empty((len(windows), len(MOD_TYPES)), dtype=np.float32)
        with torch.inference_mode():
            for i in range(0, len(windows), self._batch_size):
                chunk = torch.from_numpy(windows[i : i + self._batch_size])
                probs[i : i + self._batch_size] = self._model(chunk).numpy()

        rows, cols = np.nonzero(probs >= SITE_THRESHOLD)
        order = np.lexsort((cols, rows))  # by position, then MOD_TYPES order
        sites = [
            ModSite(
                transcript_id=None,
                position=int(rows[k]) + FLANK_NT + 1,  # 1-based
                mod_type=MOD_TYPES[int(cols[k])],
                probability=float(probs[rows[k], cols[k]]),
                p_value=None,
                coverage=None,
                source="sequence",
            )
            for k in order
        ]
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
                "n_windows": len(windows),
                "batch_size": self._batch_size,
                "weights_sha256": self._weights_sha256,
                "site_threshold": SITE_THRESHOLD,
                "max_sequence_nt": MAX_SEQUENCE_NT,
                "alpha_applies": False,
            },
            attention=None,  # upstream exposes attention maps only through its notebook
        )

    def warmup(self) -> None:
        self.predict("A" * MIN_SEQUENCE_NT)
