"""TransRNAm back-end: weights integrity, window geometry, and the reported rows.

The upstream repository publishes no per-site reference output, so there is no golden
fixture here as there is for MultiRM. What is pinned instead is everything that would
silently corrupt the served predictions: the checkpoint loads `strict=True`, the k-mer
window is centred where the checkpoint expects it, and the head order matches MOD_TYPES.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pytest
import torch

from app.predictors.base import SequencePredictor
from app.predictors.transrnam.predictor import (
    FLANK_NT,
    KMERS_PER_WINDOW,
    MAX_SEQUENCE_NT,
    MIN_SEQUENCE_NT,
    SITE_THRESHOLD,
    TransRNAmPredictor,
)
from app.schemas import MOD_TYPES

WEIGHTS = Path("app/predictors/transrnam/weights")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def predictor() -> TransRNAmPredictor:
    return TransRNAmPredictor.load()


def _seq(n: int, seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    return "".join(rng.choice(list("ACGT"), size=n))


# ------------------------------------------------------------------ weights and manifest
def test_manifest_matches_the_served_files():
    manifest = json.loads((WEIGHTS / "WEIGHTS_MANIFEST.json").read_text())["files"]
    for name, info in manifest.items():
        assert _sha256(WEIGHTS / name) == info["sha256"], name


def test_embedding_table_is_byte_identical_to_the_multirm_copy():
    """The manifest claims it; both back-ends must keep using the same Word2Vec table."""
    assert _sha256(WEIGHTS / "embeddings_12RM.pkl") == _sha256(
        Path("app/predictors/multirm/weights/embeddings_12RM.pkl")
    )


def test_checkpoint_holds_tensors_only():
    """It is loadable with weights_only=True, i.e. it cannot execute code on load."""
    state = torch.load(WEIGHTS / "transrnam.pt", map_location="cpu", weights_only=True)
    assert len(state) == 70
    assert all(torch.is_tensor(v) for v in state.values())


def test_every_acgt_3mer_is_in_the_vocabulary():
    with (WEIGHTS / "embeddings_12RM.pkl").open("rb") as fh:
        emb = pickle.load(fh)
    for a in "ACGT":
        for b in "ACGT":
            for c in "ACGT":
                assert a + b + c in emb


# ------------------------------------------------------------------ protocol
def test_satisfies_the_predictor_protocol(predictor):
    assert isinstance(predictor, SequencePredictor)
    assert predictor.name == "TransRNAm" and predictor.version


# ------------------------------------------------------------------ window geometry
def test_window_is_centred_on_the_site(predictor):
    """The checkpoint expects the site's own 3-mer at offset 299 of the 599-wide window."""
    with (WEIGHTS / "embeddings_12RM.pkl").open("rb") as fh:
        vocab = list(pickle.load(fh))
    seq = _seq(MIN_SEQUENCE_NT + 20, seed=1)
    windows = predictor._windows(predictor._kmer_indices(seq), len(seq))
    assert windows.shape == (len(seq) - 2 * FLANK_NT, KMERS_PER_WINDOW)
    for row in range(windows.shape[0]):
        centre_kmer = vocab[windows[row, FLANK_NT - 1]]
        site = row + FLANK_NT  # 0-based index of the scored base
        assert centre_kmer == seq[site - 1 : site + 2]
        assert centre_kmer[1] == seq[site]


def test_scored_range_and_window_count(predictor):
    n = MIN_SEQUENCE_NT + 9
    result = predictor.predict(_seq(n, seed=2))
    assert result.predicted_start == FLANK_NT + 1
    assert result.predicted_end == n - FLANK_NT
    assert result.extra["n_windows"] == n - 2 * FLANK_NT
    assert result.sequence_length == n


# ------------------------------------------------------------------ input bounds
def test_short_input_is_rejected_with_the_window_size(predictor):
    with pytest.raises(ValueError, match=str(MIN_SEQUENCE_NT)):
        predictor.predict(_seq(MIN_SEQUENCE_NT - 1))


def test_long_input_is_rejected_with_the_cap(predictor):
    with pytest.raises(ValueError, match="at most"):
        predictor.predict(_seq(MAX_SEQUENCE_NT + 1))


# ------------------------------------------------------------------ reported rows
def test_rows_are_thresholded_on_probability_and_carry_no_p_value(predictor):
    result = predictor.predict(_seq(MIN_SEQUENCE_NT + 40, seed=3))
    assert result.extra["alpha_applies"] is False
    assert result.extra["site_threshold"] == SITE_THRESHOLD
    for site in result.sites:
        assert site.p_value is None
        assert site.coverage is None
        assert site.source == "sequence"
        assert site.probability >= SITE_THRESHOLD
        assert site.mod_type in MOD_TYPES
        assert result.predicted_start <= site.position <= result.predicted_end


def test_rows_are_sorted_by_position_then_mod_type(predictor):
    result = predictor.predict(_seq(MIN_SEQUENCE_NT + 40, seed=4))
    keys = [(s.position, MOD_TYPES.index(s.mod_type)) for s in result.sites]
    assert keys == sorted(keys)


def test_alpha_does_not_change_the_rows(predictor):
    """No null distribution upstream, so alpha is echoed but must not filter."""
    seq = _seq(MIN_SEQUENCE_NT + 20, seed=5)
    strict = predictor.predict(seq, alpha=1e-9)
    loose = predictor.predict(seq, alpha=1.0)
    assert [s.model_dump() for s in strict.sites] == [s.model_dump() for s in loose.sites]
    assert strict.alpha == 1e-9 and loose.alpha == 1.0


def test_inference_is_deterministic(predictor):
    """Dropout must be off: the same input has to give the same numbers every time."""
    seq = _seq(MIN_SEQUENCE_NT + 10, seed=6)
    first = predictor.predict(seq)
    second = predictor.predict(seq)
    assert [s.model_dump() for s in first.sites] == [s.model_dump() for s in second.sites]
