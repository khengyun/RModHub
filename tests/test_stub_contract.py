"""The torch-free stub predictor honours the ``SequencePredictor`` contract.

These tests need nothing but ``app/predictors/{base,stub,__init__}.py`` and ``app/schemas.py``
and therefore run (and pass) before the real model or the HTTP layer exist.
"""

from __future__ import annotations

import pytest

from app.predictors import create_sequence_predictor
from app.predictors.base import (
    FLANK_NT,
    MIN_SEQUENCE_NT,
    SequencePrediction,
    SequencePredictor,
)
from app.predictors.stub import StubPredictor
from app.schemas import MOD_TYPES, ModSite

# Long enough to contain all six stub sites (last one at 79 needs n - FLANK_NT >= 79).
_SEQ = "ACGT" * 40  # 160 nt


def test_stub_satisfies_runtime_checkable_protocol():
    stub = StubPredictor()
    assert isinstance(stub, SequencePredictor)
    assert isinstance(stub.name, str) and stub.name
    assert isinstance(stub.version, str) and stub.version


def test_factory_builds_stub_without_torch():
    import sys

    torch_loaded_before = "torch" in sys.modules
    multirm_loaded_before = "app.predictors.multirm" in sys.modules

    pred = create_sequence_predictor("stub")
    assert isinstance(pred, StubPredictor)
    assert isinstance(pred, SequencePredictor)
    # The stub path must not drag in torch or the real backend (that is its whole point).
    # Only meaningful if nothing else in this process imported them first.
    if not torch_loaded_before:
        assert "torch" not in sys.modules
    if not multirm_loaded_before:
        assert "app.predictors.multirm" not in sys.modules


def test_factory_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown predictor kind"):
        create_sequence_predictor("nope")


def test_stub_prediction_geometry_and_types():
    result = StubPredictor().predict(_SEQ, alpha=0.05)
    assert isinstance(result, SequencePrediction)
    assert result.sequence_length == len(_SEQ)
    assert result.predicted_start == FLANK_NT + 1 == 26
    assert result.predicted_end == len(_SEQ) - FLANK_NT
    assert result.alpha == 0.05
    assert result.model_name == "stub"
    assert result.model_version == "0"
    assert result.inference_ms >= 0
    assert isinstance(result.extra, dict)


def test_stub_rows_are_valid_modsites():
    result = StubPredictor().predict(_SEQ, alpha=0.05)
    assert len(result.sites) == 6
    for site in result.sites:
        assert isinstance(site, ModSite)
        # Round-trip through the schema: the row must validate as a ModSite on its own.
        ModSite.model_validate(site.model_dump())
        assert site.mod_type in MOD_TYPES
        assert site.source == "sequence"
        assert site.coverage is None
        assert site.transcript_id is None
        assert 0.0 < site.probability <= 1.0
        assert site.p_value is not None and site.p_value < 0.05
        assert result.predicted_start <= site.position <= result.predicted_end


def test_stub_returns_the_six_golden_sites():
    result = StubPredictor().predict(_SEQ, alpha=0.05)
    got = {(s.mod_type, s.position) for s in result.sites}
    assert got == {
        ("Gm", 52),
        ("m5C", 63),
        ("m5U", 68),
        ("m1A", 69),
        ("Cm", 79),
        ("m5C", 79),
    }


def test_stub_is_sorted_by_position_then_mod_type():
    result = StubPredictor().predict(_SEQ, alpha=0.05)
    keys = [(s.position, MOD_TYPES.index(s.mod_type)) for s in result.sites]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys), "duplicate (position, mod_type) rows"


def test_stub_applies_alpha_filter():
    result = StubPredictor().predict(_SEQ, alpha=0.03)
    assert {(s.mod_type, s.position) for s in result.sites} == {("Gm", 52), ("m5C", 79)}
    assert all(s.p_value < 0.03 for s in result.sites)


def test_stub_drops_sites_outside_predicted_range():
    # 60 nt: predicted_end = 35, so no golden site (all at >= 52) can be reported.
    result = StubPredictor().predict("ACGT" * 15, alpha=0.05)
    assert result.predicted_end == 35
    assert result.sites == []


@pytest.mark.parametrize(
    "bad",
    [
        "A" * (MIN_SEQUENCE_NT - 1),  # too short
        "ACGT" * 12 + "ACN",  # invalid character
        "acgt" * 13,  # not normalised (lower-case)
        "ACGU" * 13,  # not normalised (U)
    ],
)
def test_stub_rejects_unnormalised_input(bad: str):
    with pytest.raises(ValueError):
        StubPredictor().predict(bad)


def test_stub_warmup_runs():
    assert StubPredictor().warmup() is None
