"""Deterministic fake predictor for API development and HTTP-layer tests.

Returns the six golden sites for any input long enough to contain them, so the API
layer can be exercised without loading torch.
"""

from __future__ import annotations

import time

from app.predictors.base import FLANK_NT, MIN_SEQUENCE_NT, SequencePrediction
from app.schemas import ModSite

_GOLDEN = [
    ("Gm", 52, 0.0267),
    ("m5C", 63, 0.0467),
    ("m5U", 68, 0.0467),
    ("m1A", 69, 0.0400),
    ("Cm", 79, 0.0333),
    ("m5C", 79, 0.0200),
]


class StubPredictor:
    name = "stub"
    version = "0"

    def predict(self, sequence: str, alpha: float = 0.05) -> SequencePrediction:
        t0 = time.perf_counter()
        n = len(sequence)
        if n < MIN_SEQUENCE_NT or set(sequence) - set("ACGT"):
            raise ValueError("stub predictor expects a normalised ACGT sequence of >= 51 nt")
        sites = [
            ModSite(
                transcript_id=None,
                position=pos,
                mod_type=mod,
                probability=1.0 - p,  # arbitrary but stable
                p_value=p,
                coverage=None,
                source="sequence",
            )
            for mod, pos, p in _GOLDEN
            if p < alpha and pos <= n - FLANK_NT
        ]
        sites.sort(key=lambda s: (s.position, s.mod_type))
        return SequencePrediction(
            sites=sites,
            sequence_length=n,
            predicted_start=FLANK_NT + 1,
            predicted_end=n - FLANK_NT,
            alpha=alpha,
            model_name=self.name,
            model_version=self.version,
            inference_ms=(time.perf_counter() - t0) * 1000,
        )

    def warmup(self) -> None:
        self.predict("A" * MIN_SEQUENCE_NT)
