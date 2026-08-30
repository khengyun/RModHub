"""Wide (matrix) MultiRM output and its conversion to the long `ModSite` format.

`MultiRMMatrices` mirrors the four CSVs the upstream CLI writes (`probs`, `p_values`,
`pred_labels`, `attention`). `matrices_to_sites` is a pure function so the signal branch
can copy the same wide -> long pattern later.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.schemas import MOD_TYPES, ModSite


@dataclass(frozen=True)
class MultiRMMatrices:
    """Per-position outputs for one sequence of length N (W = N - 50 windows).

    Row order of every matrix is `MOD_TYPES`.
    """

    positions: np.ndarray  # (W,) int64, 1-based centre position of each window: 26 .. N-25
    probs: np.ndarray  # (12, W) float64, sigmoid output per (mod type, window)
    p_values: np.ndarray  # (12, W) float64, share of the 150 negatives scoring above probs
    labels: np.ndarray  # (12, N) int64 0/1, 1 where p_value < alpha (index = 0-based nt)
    attention: np.ndarray  # (12, N) int64 0/1, top-3 attention windows of significant sites
    inference_ms: float  # wall time of the model pass + post-processing

    @property
    def sequence_length(self) -> int:
        return int(self.labels.shape[1])

    @property
    def n_windows(self) -> int:
        return int(self.probs.shape[1])


def matrices_to_sites(matrices: MultiRMMatrices, alpha: float) -> list[ModSite]:
    """Reshape wide matrices to one `ModSite` per (position, mod_type) with
    p_value < alpha and probability > 0, sorted by (position, MOD_TYPES order)."""
    mask = (matrices.p_values < alpha) & (matrices.probs > 0)
    ks, ws = np.nonzero(mask)
    order = np.lexsort((ks, ws))  # primary key: window (position), secondary: mod index
    sites: list[ModSite] = []
    for k, w in zip(ks[order], ws[order]):
        sites.append(
            ModSite(
                transcript_id=None,
                position=int(matrices.positions[w]),
                mod_type=MOD_TYPES[k],
                probability=float(matrices.probs[k, w]),
                p_value=float(matrices.p_values[k, w]),
                coverage=None,
                source="sequence",
            )
        )
    return sites
