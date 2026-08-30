"""Shared fixtures for the RModHub test suite.

Two tiers of HTTP clients keep the suite fast:

* ``stub_client``  -- the FastAPI app built on the torch-free ``StubPredictor``. Used by the
  validation / HTTP-layer tests, which never need real model numbers.
* ``app_client``   -- ONE session-scoped app built on the real MultiRM predictor. The model is
  loaded exactly once, inside the app's lifespan, and every real-model test shares it. The
  ``predictor`` fixture is derived from ``app_client.app.state.predictor`` so the suite never
  loads the weights twice by accident (the single ``slow`` cold-start test builds its own app
  on purpose and is the only other place a model is loaded).

Imports of agent-owned modules (``app.main``, ``app.config``, ``app.predictors.multirm``) are
deferred into the fixtures / tests so ``pytest --collect-only`` works before those modules
exist, and tests that need them fail with a clean ImportError at fixture setup instead of
breaking collection of the whole file.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# --------------------------------------------------------------------------------------
# Golden fixture (produced by unmodified upstream MultiRM; see the README in GOLDEN_DIR)
# --------------------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_multirm_151nt"

GOLDEN_LENGTH = 151
GOLDEN_PREDICTED_START = 26
GOLDEN_PREDICTED_END = 126
GOLDEN_N_SITES_AT_005 = 22  # upstream reports 22 significant sites at alpha = 0.05

# The six sites the product owner verified by hand: (mod_type, 1-based position, p_value).
GOLDEN_SITES: list[tuple[str, int, float]] = [
    ("Gm", 52, 0.0267),
    ("m5C", 63, 0.0467),
    ("m5U", 68, 0.0467),
    ("m1A", 69, 0.0400),
    ("Cm", 79, 0.0333),
    ("m5C", 79, 0.0200),
]
# p-values in the fixture are multiples of 1/150; the hand-verified values above are rounded
# to 4 decimals, so 5e-5 is the right tolerance (1/150 rounding error is <= 3.4e-5).
GOLDEN_P_TOL = 5e-5

# Wall-clock timings recorded by session fixtures, read by tests/test_perf.py.
LOAD_TIMES: dict[str, float] = {}


def load_golden_matrix(name: str) -> pd.DataFrame:
    """Load ``<name>.csv`` from the golden directory as a 12-row DataFrame.

    Rows are the 12 modification types in canonical order. Column labels are strings:
    1-based positions ("26".."126") for probs / p_values, 0-based nt indices ("0".."150")
    for pred_labels / attention.
    """
    return pd.read_csv(GOLDEN_DIR / f"{name}.csv", index_col=0)


# --------------------------------------------------------------------------------------
# pytest configuration
# --------------------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: loads a second model instance or runs a 10 kb inference; deselect with -m 'not slow'",
    )


# Settings the tests pin via ``Settings(...)`` kwargs or rely on at their defaults. They are
# removed from the environment for the whole session so a developer's shell (or CI env) cannot
# silently change what the assertions mean. Other RMODHUB_* variables (weights dir, threads)
# are deliberately left alone.
_PINNED_ENV_VARS = (
    "RMODHUB_PREDICTOR",
    "RMODHUB_WARMUP",
    "RMODHUB_MIN_SEQUENCE_NT",
    "RMODHUB_MAX_SEQUENCE_NT",
    "RMODHUB_DEFAULT_ALPHA",
)


@pytest.fixture(scope="session", autouse=True)
def _pinned_env():
    with pytest.MonkeyPatch.context() as mp:
        for name in _PINNED_ENV_VARS:
            if name in os.environ:
                mp.delenv(name)
        yield


# --------------------------------------------------------------------------------------
# Golden data fixtures (cheap, session scoped)
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def golden_sequence() -> str:
    seq = (GOLDEN_DIR / "sequence.txt").read_text().strip()
    assert len(seq) == GOLDEN_LENGTH, "golden fixture sequence.txt is not 151 nt"
    return seq


@pytest.fixture(scope="session")
def golden_sites() -> list[tuple[str, int, float]]:
    return list(GOLDEN_SITES)


@pytest.fixture(scope="session")
def golden_probs() -> pd.DataFrame:
    return load_golden_matrix("probs")


@pytest.fixture(scope="session")
def golden_p_values() -> pd.DataFrame:
    return load_golden_matrix("p_values")


@pytest.fixture(scope="session")
def golden_labels() -> pd.DataFrame:
    return load_golden_matrix("pred_labels")


@pytest.fixture(scope="session")
def golden_attention() -> pd.DataFrame:
    return load_golden_matrix("attention")


@pytest.fixture(scope="session")
def golden_label_sites(golden_labels: pd.DataFrame) -> set[tuple[str, int]]:
    """``{(mod_type, 1-based position)}`` for every 1 in ``pred_labels.csv``.

    The label matrix is indexed by 0-based nucleotide index, so position = column + 1.
    """
    rows, cols = np.where(golden_labels.values == 1)
    return {
        (str(golden_labels.index[r]), int(golden_labels.columns[c]) + 1)
        for r, c in zip(rows, cols, strict=True)
    }


# --------------------------------------------------------------------------------------
# Application clients
# --------------------------------------------------------------------------------------


def _build_app(**settings_kwargs):
    """Build a FastAPI app from explicit ``Settings`` kwargs (never from env vars).

    Deferred imports: ``app.main`` / ``app.config`` are owned by another agent and may not
    exist yet; failing here yields a clean fixture error rather than a collection error.
    """
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(**settings_kwargs))


@pytest.fixture(scope="session")
def stub_client():
    """App on the torch-free stub predictor. Lifespan is entered (context manager)."""
    from fastapi.testclient import TestClient

    with TestClient(_build_app(predictor="stub")) as client:
        yield client


@pytest.fixture(scope="session")
def app_client():
    """The ONE real-model app for the whole session.

    ``warmup=False`` so the lifespan cost recorded in ``LOAD_TIMES`` is the weight load alone.
    """
    from fastapi.testclient import TestClient

    app = _build_app(predictor="multirm", warmup=False)
    t0 = time.perf_counter()
    with TestClient(app) as client:
        LOAD_TIMES["app_client_startup_s"] = time.perf_counter() - t0
        yield client


@pytest.fixture(scope="session")
def predictor(app_client):
    """The real ``MultiRMPredictor`` instance living inside ``app_client``'s app state.

    Taken from the app rather than built separately so the session loads the weights once.
    """
    pred = getattr(app_client.app.state, "predictor", None)
    if pred is None:
        pytest.fail("app lifespan did not set app.state.predictor")
    return pred


@pytest.fixture
def cold_app_factory():
    """Factory for a *fresh*, not-yet-started real-model app (used by the cold-start perf test).

    Each call builds a new app; entering ``TestClient(app)`` loads the model. Overrides are
    passed straight to ``Settings``.
    """

    def factory(**overrides):
        kwargs: dict = {"predictor": "multirm", "warmup": False}
        kwargs.update(overrides)
        return _build_app(**kwargs)

    return factory


@pytest.fixture(scope="session")
def load_times() -> dict[str, float]:
    """Timings recorded by session fixtures (see ``LOAD_TIMES``)."""
    return LOAD_TIMES
