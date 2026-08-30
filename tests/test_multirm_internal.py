"""Internal tests for the vendored MultiRM predictor: golden fixture reproduction,
batch-size invariance, input validation and the wide -> long adapter.

These load real torch weights (~25 ms) and run real inference; the HTTP-layer tests use
the stub predictor instead.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.predictors.base import FLANK_NT, SequencePredictor
from app.predictors.multirm import MultiRMMatrices, MultiRMPredictor, matrices_to_sites
from app.schemas import MOD_TYPES

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_multirm_151nt"

# Sites the project treats as canonical (mod, 1-based position, p-value).
CANONICAL_SITES = {
    ("Gm", 52, 0.0267),
    ("m5C", 63, 0.0467),
    ("m5U", 68, 0.0467),
    ("m1A", 69, 0.0400),
    ("Cm", 79, 0.0333),
    ("m5C", 79, 0.0200),
}
UPSTREAM_SITE_COUNT = 22  # reported by the unmodified upstream CLI at alpha=0.05


@pytest.fixture(scope="module")
def predictor() -> MultiRMPredictor:
    return MultiRMPredictor.load()


@pytest.fixture(scope="module")
def golden_seq() -> str:
    return (GOLDEN_DIR / "sequence.txt").read_text().strip()


@pytest.fixture(scope="module")
def golden_matrices(predictor: MultiRMPredictor, golden_seq: str) -> MultiRMMatrices:
    return predictor.predict_matrix(golden_seq, alpha=0.05, with_attention=True)


def _golden_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(GOLDEN_DIR / f"{name}.csv", index_col=0)
    assert list(df.index) == list(MOD_TYPES), f"{name}.csv row order != MOD_TYPES"
    return df


def test_implements_protocol(predictor: MultiRMPredictor) -> None:
    assert isinstance(predictor, SequencePredictor)
    assert predictor.name == "MultiRM"
    assert predictor.version == "trained_model_51seqs"
    assert len(predictor.weights_sha256) == 64


def test_golden_probs(golden_matrices: MultiRMMatrices) -> None:
    ref = _golden_csv("probs")
    assert [int(c) for c in ref.columns] == golden_matrices.positions.tolist()
    assert golden_matrices.positions[0] == FLANK_NT + 1
    np.testing.assert_allclose(golden_matrices.probs, ref.to_numpy(), rtol=0, atol=1e-5)


def test_golden_p_values(golden_matrices: MultiRMMatrices) -> None:
    ref = _golden_csv("p_values").to_numpy()
    # p-values are k/150; the CSV round-trip may differ by one ulp from our division.
    np.testing.assert_allclose(golden_matrices.p_values, ref, rtol=0, atol=1e-12)
    scaled = golden_matrices.p_values * 150
    np.testing.assert_allclose(scaled, np.round(scaled), rtol=0, atol=1e-9)


def test_golden_labels_and_attention(golden_matrices: MultiRMMatrices) -> None:
    labels = _golden_csv("pred_labels").to_numpy().astype(np.int64)
    attention = _golden_csv("attention").to_numpy().astype(np.int64)
    np.testing.assert_array_equal(golden_matrices.labels, labels)
    np.testing.assert_array_equal(golden_matrices.attention, attention)
    # Nothing is ever predicted in the 25-nt flanks.
    assert golden_matrices.labels[:, :FLANK_NT].sum() == 0
    assert golden_matrices.labels[:, -FLANK_NT:].sum() == 0


def test_golden_sites(predictor: MultiRMPredictor, golden_seq: str) -> None:
    result = predictor.predict(golden_seq, alpha=0.05)
    assert result.sequence_length == 151
    assert result.predicted_start == 26
    assert result.predicted_end == 126
    assert result.model_name == "MultiRM"
    assert result.model_version == "trained_model_51seqs"
    assert result.extra["n_windows"] == 101
    assert result.extra["batch_size"] == predictor.batch_size
    assert result.extra["weights_sha256"] == predictor.weights_sha256
    assert result.inference_ms > 0

    sites = result.sites
    assert len(sites) == UPSTREAM_SITE_COUNT
    found = {(s.mod_type, s.position, round(s.p_value, 4)) for s in sites}
    assert CANONICAL_SITES <= found
    for s in sites:
        assert s.source == "sequence"
        assert s.transcript_id is None
        assert s.coverage is None
        assert s.p_value < 0.05
        assert 0 < s.probability <= 1
    keys = [(s.position, MOD_TYPES.index(s.mod_type)) for s in sites]
    assert keys == sorted(keys)


def test_batch_size_invariance(golden_seq: str, golden_matrices: MultiRMMatrices) -> None:
    # batch 1 == upstream's per-window inference; 7 exercises a ragged last chunk.
    for batch_size in (1, 7):
        other = MultiRMPredictor.load(batch_size=batch_size)
        m = other.predict_matrix(golden_seq, alpha=0.05, with_attention=True)
        np.testing.assert_allclose(m.probs, golden_matrices.probs, rtol=0, atol=1e-6)
        np.testing.assert_array_equal(m.p_values, golden_matrices.p_values)
        np.testing.assert_array_equal(m.labels, golden_matrices.labels)
        np.testing.assert_array_equal(m.attention, golden_matrices.attention)


def test_with_attention_false(
    predictor: MultiRMPredictor, golden_seq: str, golden_matrices: MultiRMMatrices
) -> None:
    m = predictor.predict_matrix(golden_seq, alpha=0.05, with_attention=False)
    np.testing.assert_array_equal(m.probs, golden_matrices.probs)
    np.testing.assert_array_equal(m.p_values, golden_matrices.p_values)
    np.testing.assert_array_equal(m.labels, golden_matrices.labels)
    assert m.attention.shape == golden_matrices.attention.shape
    assert m.attention.sum() == 0


def test_adapter_stricter_alpha(golden_matrices: MultiRMMatrices) -> None:
    loose = matrices_to_sites(golden_matrices, 0.05)
    strict = matrices_to_sites(golden_matrices, 0.03)
    assert len(loose) == UPSTREAM_SITE_COUNT
    assert 0 < len(strict) < len(loose)
    assert all(s.p_value < 0.03 for s in strict)
    assert {(s.mod_type, s.position) for s in strict} <= {(s.mod_type, s.position) for s in loose}


def test_normalisation_accepts_lowercase_and_u(
    predictor: MultiRMPredictor, golden_seq: str, golden_matrices: MultiRMMatrices
) -> None:
    rna_lower = golden_seq.lower().replace("t", "u")
    m = predictor.predict_matrix(rna_lower, alpha=0.05, with_attention=False)
    np.testing.assert_array_equal(m.probs, golden_matrices.probs)


def test_input_validation(predictor: MultiRMPredictor) -> None:
    with pytest.raises(ValueError, match="at least 51"):
        predictor.predict("ACGT" * 12)  # 48 nt
    with pytest.raises(ValueError, match="outside ACGT"):
        predictor.predict("ACGTN" * 11)
    with pytest.raises(ValueError, match="alpha"):
        predictor.predict("A" * 51, alpha=1.5)
    with pytest.raises(TypeError):
        predictor.predict(None)  # type: ignore[arg-type]


def test_minimum_length_and_warmup(predictor: MultiRMPredictor) -> None:
    result = predictor.predict("A" * 51)
    assert result.extra["n_windows"] == 1
    assert result.predicted_start == result.predicted_end == 26
    predictor.warmup()


def test_load_rejects_missing_assets(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MultiRMPredictor.load(tmp_path)
