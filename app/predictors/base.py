"""Predictor interface shared by all model back-ends.

The HTTP layer only talks to `SequencePredictor`; concrete implementations
(`multirm.MultiRMPredictor`, `stub.StubPredictor`) live in sibling modules.
The signal branch (DirectRM) will add a `SignalPredictor` protocol next to this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.schemas import ModSite

# MultiRM scans 51-nt windows and predicts the centre nucleotide, so the first and
# last 25 nt of any input never receive a prediction.
FLANK_NT = 25
WINDOW_NT = 2 * FLANK_NT + 1  # 51
MIN_SEQUENCE_NT = WINDOW_NT


@dataclass(frozen=True)
class SequencePrediction:
    """Result of one sequence-branch prediction.

    `sites` contains only rows that pass the caller's filters
    (p_value < alpha and probability > 0), sorted by (position, mod_type order).
    """

    sites: list[ModSite]
    sequence_length: int
    predicted_start: int  # 1-based, inclusive (== FLANK_NT + 1)
    predicted_end: int  # 1-based, inclusive (== sequence_length - FLANK_NT)
    alpha: float
    model_name: str
    model_version: str
    inference_ms: float
    extra: dict = field(
        default_factory=dict
    )  # backend-specific extras for `meta`, JSON-serialisable


@runtime_checkable
class SequencePredictor(Protocol):
    """A loaded-once, in-memory model that scores a single nucleotide sequence.

    Implementations must be safe to call from multiple threads (FastAPI runs sync
    endpoints in a threadpool) and must NOT reload weights per call.
    """

    name: str
    version: str

    def predict(self, sequence: str, alpha: float = 0.05) -> SequencePrediction:
        """Score `sequence`.

        `sequence` is already normalised by the API layer: upper-case, only A/C/G/T
        (U already mapped to T), length >= MIN_SEQUENCE_NT. Implementations should
        still validate defensively and raise ValueError on bad input.
        """
        ...

    def warmup(self) -> None:
        """Run one dummy inference so the first real request is not slower than the rest."""
        ...
