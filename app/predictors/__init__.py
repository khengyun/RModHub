"""Predictor factory and the sequence-branch model registry.

The API layer calls `create_sequence_predictors(ids)` once at startup and keeps the
result in `app.state.predictors`; `app.state.predictor` stays the default model so the
load-once rule holds for every back-end.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.predictors.base import SequencePredictor


@dataclass(frozen=True)
class ModelInfo:
    """What the UI needs to render one entry of the model picker."""

    id: str
    label: str
    description: str


# Registry of every back-end this build can load. A deployment enables a subset through
# RMODHUB_SEQUENCE_MODELS; adding a model means adding one entry here plus one branch in
# `create_sequence_predictor`.
SEQUENCE_MODELS: dict[str, ModelInfo] = {
    "multirm": ModelInfo(
        id="multirm",
        label="MultiRM",
        description=(
            "Attention-based BiLSTM over 51-nt windows; 12 modification types "
            "(Song et al., Nat Commun 2021)."
        ),
    ),
    "transrnam": ModelInfo(
        id="transrnam",
        label="TransRNAm",
        description=(
            "Transformer + CNN over a 601-nt window; the same 12 modification types, "
            "trained on the same benchmark as MultiRM. Slower, and capped at 2,000 nt."
        ),
    ),
    "stub": ModelInfo(
        id="stub",
        label="Stub (development)",
        description="Torch-free fake used in tests and local development. Not a real model.",
    ),
}


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
    if kind == "transrnam":
        from app.predictors.transrnam import TransRNAmPredictor

        return TransRNAmPredictor.load(**load_kwargs)
    raise ValueError(f"unknown predictor kind: {kind!r} (known: {', '.join(SEQUENCE_MODELS)})")


def create_sequence_predictors(
    kinds: list[str], **load_kwargs
) -> dict[str, SequencePredictor]:
    """Load every back-end in `kinds`, in order. The first one is the deployment default.

    Insertion order is preserved so `next(iter(...))` is the default and the UI lists the
    models the way the operator configured them.
    """
    if not kinds:
        raise ValueError("at least one sequence model must be enabled")
    loaded: dict[str, SequencePredictor] = {}
    for kind in kinds:
        if kind in loaded:  # a repeated id costs a second copy of the weights for nothing
            continue
        loaded[kind] = create_sequence_predictor(kind, **load_kwargs)
    return loaded
