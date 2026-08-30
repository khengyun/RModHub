"""Predictor factory. The API layer calls `create_sequence_predictor(kind)` once at startup."""

from __future__ import annotations

from app.predictors.base import SequencePredictor


def create_sequence_predictor(kind: str = "multirm", **load_kwargs) -> SequencePredictor:
    """Build and fully load a sequence predictor. Heavy imports are deferred so the
    stub path never imports torch.

    `load_kwargs` are forwarded to `MultiRMPredictor.load(...)` (e.g. `num_threads=4`)
    and ignored by the stub.
    """
    if kind == "stub":
        from app.predictors.stub import StubPredictor

        return StubPredictor()
    if kind == "multirm":
        from app.predictors.multirm import MultiRMPredictor

        return MultiRMPredictor.load(**load_kwargs)
    raise ValueError(f"unknown predictor kind: {kind!r} (expected 'multirm' or 'stub')")
