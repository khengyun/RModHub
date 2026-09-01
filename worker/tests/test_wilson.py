"""Wilson score interval: reference values, edge cases, invariants."""

from __future__ import annotations

import math

import pytest

from rmodhub_worker.wilson import Z_95, wilson_interval

# Reference values (95 %, z = 1.959964) computed by hand from the closed form; they agree with
# the textbook figures for 20/100 (0.1334, 0.2888) and the 0/n upper bound z^2 / (n + z^2).
REFERENCE = [
    (0, 10, 0.0, 0.27753),
    (5, 10, 0.23659, 0.76341),
    (10, 10, 0.72247, 1.0),
    (1, 1, 0.20655, 1.0),
    (0, 1, 0.0, 0.79345),
    (20, 100, 0.13337, 0.28883),
    (1, 2, 0.09453, 0.90547),
]


@pytest.mark.parametrize("count,coverage,low,high", REFERENCE)
def test_reference_values(count, coverage, low, high):
    got_low, got_high = wilson_interval(count, coverage)
    assert got_low == pytest.approx(low, abs=1e-4)
    assert got_high == pytest.approx(high, abs=1e-4)


def test_zero_upper_bound_closed_form():
    for n in (1, 2, 3, 10, 30, 150, 10_000):
        low, high = wilson_interval(0, n)
        assert low == 0.0
        assert high == pytest.approx(Z_95**2 / (n + Z_95**2), rel=1e-12)


def test_full_lower_bound_symmetry():
    for n in (1, 2, 3, 10, 30, 150, 10_000):
        low, high = wilson_interval(n, n)
        assert high == 1.0
        assert low == pytest.approx(1.0 - wilson_interval(0, n)[1], rel=1e-12)


def test_edge_cases_are_exact_and_within_unit_interval():
    assert wilson_interval(0, 5) == (0.0, pytest.approx(0.43445, abs=1e-4))
    assert wilson_interval(5, 5)[1] == 1.0
    for count in range(6):
        low, high = wilson_interval(count, 5)
        assert 0.0 <= low <= count / 5 <= high <= 1.0


def test_symmetry_and_monotonicity():
    n = 37
    for k in range(n + 1):
        low, high = wilson_interval(k, n)
        mlow, mhigh = wilson_interval(n - k, n)
        assert low == pytest.approx(1.0 - mhigh, abs=1e-12)
        assert high == pytest.approx(1.0 - mlow, abs=1e-12)
        if k:
            assert low >= wilson_interval(k - 1, n)[0]
            assert high >= wilson_interval(k - 1, n)[1]


def test_interval_shrinks_with_coverage():
    widths = []
    for n in (2, 10, 100, 1000, 10000):
        low, high = wilson_interval(n // 2, n)
        widths.append(high - low)
    assert widths == sorted(widths, reverse=True)
    assert widths[-1] < 0.02


def test_custom_z():
    low, high = wilson_interval(5, 10, z=0.0)
    assert (low, high) == (0.5, 0.5)
    low99, high99 = wilson_interval(5, 10, z=2.575829)
    low95, high95 = wilson_interval(5, 10)
    assert low99 < low95 and high99 > high95


@pytest.mark.parametrize("count,coverage", [(0, 0), (1, 0), (-1, 5), (6, 5)])
def test_invalid_input(count, coverage):
    with pytest.raises(ValueError):
        wilson_interval(count, coverage)


def test_values_are_finite():
    low, high = wilson_interval(3, 7)
    assert math.isfinite(low) and math.isfinite(high)
