"""Wilson score interval (pure Python; no scipy/numpy)."""

from __future__ import annotations

import math

#: z for a two-sided 95 % interval, as fixed in docs/signal-branch.md section 5.
Z_95 = 1.959964


def wilson_interval(count: int, coverage: int, z: float = Z_95) -> tuple[float, float]:
    """Return the Wilson score interval ``(low, high)`` for ``count`` successes in ``coverage`` trials.

    ``count == 0`` gives ``low == 0.0`` exactly and ``count == coverage`` gives ``high == 1.0``
    exactly (the closed form lands there up to rounding; it is clamped so the stored values are
    clean). Raises ``ValueError`` for ``coverage <= 0`` or a count outside ``0..coverage``.
    """
    if coverage <= 0:
        raise ValueError("coverage must be positive")
    if count < 0 or count > coverage:
        raise ValueError("count must be within 0..coverage")
    n = float(coverage)
    p = count / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    low = 0.0 if count == 0 else max(0.0, centre - half)
    high = 1.0 if count == coverage else min(1.0, centre + half)
    return low, high
