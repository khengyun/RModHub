"""Latency, memory and load-once evidence for the real model.

Numbers are printed (run with ``-s``) and attached to the junit report via ``record_property``.
Budgets are deliberately generous: these tests guard against regressions of kind (model
reloaded per request, non-deterministic output, 10 kb blowing up), not against noise.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

URL = "/api/predict/sequence"


def _timed_post(client, sequence: str, alpha: float = 0.05):
    t0 = time.perf_counter()
    r = client.post(URL, json={"sequence": sequence, "alpha": alpha})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    return r, elapsed


def _maxrss_mb() -> float:
    # ru_maxrss is KiB on Linux (bytes on macOS; this suite targets Linux/CPU containers).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def test_session_app_startup_time(app_client, load_times, record_property):
    """Report the one-off weight-load cost of the shared session app."""
    t_startup = load_times["app_client_startup_s"]
    print(f"\n[perf] session app startup (weight load, warmup=False) = {t_startup:.3f}s")
    record_property("session_startup_s", round(t_startup, 4))
    assert t_startup < 60.0


# Run in a *fresh interpreter* so "startup" is what a cold container actually pays: importing
# torch and the vendored model code, unpickling the weights and entering the lifespan. Inside the
# pytest process torch is already imported and the weight file is page-cached, so a second
# in-process load costs ~30 ms and cannot be told apart from inference noise.
_COLD_PROCESS_SCRIPT = r"""
import json, sys, time

t0 = time.perf_counter()
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
t_import = time.perf_counter() - t0

sequence = sys.argv[1]
app = create_app(Settings(predictor="multirm", warmup=False))


def post(client):
    t = time.perf_counter()
    r = client.post("/api/predict/sequence", json={"sequence": sequence, "alpha": 0.05})
    dt = time.perf_counter() - t
    assert r.status_code == 200, r.text
    return r.json()["results"], dt


t0 = time.perf_counter()
with TestClient(app) as client:
    t_lifespan = time.perf_counter() - t0
    res1, t1 = post(client)
    # "Second request" = warm steady state: best of three so scheduler noise on a loaded
    # box does not turn the comparison into a coin flip.
    warm = [post(client) for _ in range(3)]
    res2, t2 = min(warm, key=lambda item: item[1])

print("COLD_START_RESULT " + json.dumps({
    "t_import": t_import, "t_lifespan": t_lifespan, "t1": t1, "t2": t2,
    "n_sites": len(res1), "identical": res1 == res2,
}))
"""


@pytest.mark.slow
def test_second_request_faster_than_first_cold(golden_sequence, record_property):
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", _COLD_PROCESS_SCRIPT, golden_sequence],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,  # the return code is asserted below, with stderr attached
    )
    assert proc.returncode == 0, f"cold-start subprocess failed:\n{proc.stderr[-4000:]}"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("COLD_START_RESULT ")), None
    )
    assert line is not None, f"no result line in subprocess output:\n{proc.stdout[-2000:]}"
    data = json.loads(line.split(" ", 1)[1])

    t_import, t_lifespan, t1, t2 = data["t_import"], data["t_lifespan"], data["t1"], data["t2"]
    t_startup = t_import + t_lifespan  # time to a ready app in a fresh process
    ratio = t1 / t2 if t2 > 0 else float("inf")
    summary = (
        f"startup={t_startup:.3f}s (imports={t_import:.3f}s lifespan={t_lifespan:.3f}s) "
        f"first={t1:.3f}s second={t2:.3f}s ratio={ratio:.2f}"
    )
    print("\n[perf] cold process: " + summary)
    record_property("cold_startup_s", round(t_startup, 4))
    record_property("cold_import_s", round(t_import, 4))
    record_property("cold_lifespan_s", round(t_lifespan, 4))
    record_property("cold_first_request_s", round(t1, 4))
    record_property("cold_second_request_s", round(t2, 4))
    record_property("cold_first_over_second", round(ratio, 3))

    assert data["identical"], "first and second responses differ"
    assert data["n_sites"] == 22
    assert t1 < 5.0, summary
    assert t2 < 5.0, summary
    assert t2 < t1, summary
    # If weights were re-loaded per request, request 2 would cost about startup + inference.
    assert t2 < 0.25 * (t_startup + t1), summary


def test_fresh_in_process_app_serves_independently(
    cold_app_factory, app_client, predictor, golden_sequence, record_property
):
    """A second ``create_app`` in the same process gets its own model and the same answers.

    Guards against process-global state (settings caches, logging handlers, module singletons)
    leaking between apps. Timings are reported only; in-process the second load is too cheap to
    support a ratio assertion.
    """
    from fastapi.testclient import TestClient

    app = cold_app_factory()
    t0 = time.perf_counter()
    with TestClient(app) as client:
        t_startup = time.perf_counter() - t0
        assert app.state.predictor is not predictor  # its own instance, not the session one
        r1, t1 = _timed_post(client, golden_sequence)
        r2, t2 = _timed_post(client, golden_sequence)
    reference, _ = _timed_post(app_client, golden_sequence)

    print(
        f"\n[perf] in-process second app: startup={t_startup:.3f}s first={t1:.3f}s second={t2:.3f}s"
    )
    record_property("inproc_startup_s", round(t_startup, 4))
    record_property("inproc_first_request_s", round(t1, 4))
    record_property("inproc_second_request_s", round(t2, 4))

    assert r1.json()["results"] == r2.json()["results"] == reference.json()["results"]
    assert t1 < 5.0 and t2 < 5.0


def test_prediction_is_deterministic(predictor, app_client, golden_sequence):
    a = predictor.predict_matrix(golden_sequence, alpha=0.05)
    b = predictor.predict_matrix(golden_sequence, alpha=0.05)
    assert np.array_equal(np.asarray(a.probs), np.asarray(b.probs))
    assert np.array_equal(np.asarray(a.p_values), np.asarray(b.p_values))
    assert np.array_equal(np.asarray(a.labels), np.asarray(b.labels))
    assert np.array_equal(np.asarray(a.attention), np.asarray(b.attention))

    s1 = [s.model_dump() for s in predictor.predict(golden_sequence, alpha=0.05).sites]
    s2 = [s.model_dump() for s in predictor.predict(golden_sequence, alpha=0.05).sites]
    assert s1 == s2 and s1

    r1, _ = _timed_post(app_client, golden_sequence)
    r2, _ = _timed_post(app_client, golden_sequence)
    assert r1.json()["results"] == r2.json()["results"]


@pytest.mark.slow
def test_long_sequence_10k_within_budget(predictor, record_property):
    seq = "ACGT" * 2500
    rss_before = _maxrss_mb()
    t0 = time.perf_counter()
    result = predictor.predict(seq, alpha=0.05)
    elapsed = time.perf_counter() - t0
    rss_after = _maxrss_mb()
    growth = rss_after - rss_before

    print(
        f"\n[perf] 10 kb: elapsed={elapsed:.2f}s inference_ms={result.inference_ms:.0f} "
        f"n_sites={len(result.sites)} rss_growth={growth:.1f}MB maxrss={rss_after:.0f}MB"
    )
    record_property("long_10k_elapsed_s", round(elapsed, 3))
    record_property("long_10k_inference_ms", round(result.inference_ms, 1))
    record_property("long_10k_rss_growth_mb", round(growth, 1))
    record_property("long_10k_maxrss_mb", round(rss_after, 1))

    assert result.sequence_length == 10_000
    assert result.predicted_start == 26
    assert result.predicted_end == 9_975
    assert all(26 <= s.position <= 9_975 for s in result.sites)
    assert elapsed < 60.0, f"10 kb inference took {elapsed:.1f}s"


def test_predictor_loaded_once_per_app(app_client, predictor, golden_sequence, monkeypatch):
    from app.predictors.multirm import MultiRMPredictor

    app = app_client.app
    assert app.state.predictor is predictor
    id_before = id(app.state.predictor)

    # Count calls through the class so it works whether or not the instance allows setattr.
    cls = type(predictor)
    original_predict = cls.predict
    seen: list[object] = []

    def counting_predict(self, sequence, alpha=0.05, *args, **kwargs):
        seen.append(self)
        return original_predict(self, sequence, alpha, *args, **kwargs)

    def reload_forbidden(*args, **kwargs):
        raise AssertionError("MultiRMPredictor.load() was called while serving requests")

    monkeypatch.setattr(cls, "predict", counting_predict)
    monkeypatch.setattr(MultiRMPredictor, "load", reload_forbidden)

    for _ in range(3):
        r = app_client.post(URL, json={"sequence": golden_sequence, "alpha": 0.05})
        assert r.status_code == 200, r.text

    assert len(seen) == 3, f"predict() called {len(seen)} times for 3 requests"
    assert all(obj is predictor for obj in seen), "requests were served by a different predictor"
    assert app.state.predictor is predictor
    assert id(app.state.predictor) == id_before
