"""Equivalence with the unmodified upstream MultiRM on the 151-nt golden sequence.

Every test here shares the ONE session-scoped real-model app (``app_client``) and the
predictor living in its state, so the weights are loaded once for the whole module.
"""

from __future__ import annotations

import numpy as np

from app.predictors.base import SequencePredictor
from app.schemas import MOD_TYPES, ModSite
from tests.conftest import (
    GOLDEN_LENGTH,
    GOLDEN_N_SITES_AT_005,
    GOLDEN_P_TOL,
    GOLDEN_PREDICTED_END,
    GOLDEN_PREDICTED_START,
)

URL = "/api/predict/sequence"


def _post(client, sequence: str, alpha: float | None = 0.05):
    body: dict = {"sequence": sequence}
    if alpha is not None:
        body["alpha"] = alpha
    return client.post(URL, json=body)


def _keys(results: list[dict]) -> list[tuple[str, int]]:
    return [(row["mod_type"], row["position"]) for row in results]


def test_real_predictor_satisfies_protocol(predictor):
    assert isinstance(predictor, SequencePredictor)
    assert type(predictor).__name__ == "MultiRMPredictor"
    assert isinstance(predictor.name, str) and predictor.name
    assert isinstance(predictor.version, str) and predictor.version


def test_health_reports_real_model(app_client, predictor):
    r = app_client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model_name"] == predictor.name
    assert body["model_version"] == predictor.version


def test_golden_six_sites_via_api(app_client, predictor, golden_sequence, golden_sites):
    r = _post(app_client, golden_sequence, alpha=0.05)
    assert r.status_code == 200, r.text
    body = r.json()
    results, meta = body["results"], body["meta"]
    assert results, "no sites returned for the golden sequence"

    by_key: dict[tuple[str, int], float] = {}
    for row in results:
        ModSite.model_validate(row)  # every row is a valid ModSite on its own
        assert row["source"] == "sequence"
        assert row["coverage"] is None
        assert row["transcript_id"] is None
        assert 0.0 < row["probability"] <= 1.0
        assert row["p_value"] is not None and row["p_value"] < 0.05
        assert GOLDEN_PREDICTED_START <= row["position"] <= GOLDEN_PREDICTED_END
        assert row["mod_type"] in MOD_TYPES
        key = (row["mod_type"], row["position"])
        assert key not in by_key, f"duplicate row for {key}"
        by_key[key] = row["p_value"]

    for mod, pos, expected_p in golden_sites:
        assert (mod, pos) in by_key, f"canonical site {mod}@{pos} missing; got {sorted(by_key)}"
        got_p = by_key[(mod, pos)]
        assert abs(got_p - expected_p) < GOLDEN_P_TOL, (
            f"{mod}@{pos}: p={got_p} expected ~{expected_p}"
        )

    assert meta["n_sites"] == len(results)
    assert meta["predicted_start"] == GOLDEN_PREDICTED_START
    assert meta["predicted_end"] == GOLDEN_PREDICTED_END
    assert meta["sequence_length"] == GOLDEN_LENGTH
    assert meta["alpha"] == 0.05
    assert meta["mod_types"] == list(MOD_TYPES)
    assert meta["source"] == "sequence"
    assert meta["transcript_id"] is None
    assert meta["model_name"] == predictor.name
    assert meta["model_version"] == predictor.version
    assert meta["inference_ms"] > 0
    assert isinstance(meta["extra"], dict)


def test_golden_full_matrix_matches_upstream(
    predictor, golden_sequence, golden_probs, golden_p_values, golden_labels, golden_attention
):
    m = predictor.predict_matrix(golden_sequence, alpha=0.05, with_attention=True)

    positions = np.asarray(m.positions)
    np.testing.assert_array_equal(
        positions, np.arange(GOLDEN_PREDICTED_START, GOLDEN_PREDICTED_END + 1)
    )

    probs = np.asarray(m.probs, dtype=float)
    assert probs.shape == (12, 101)
    np.testing.assert_allclose(probs, golden_probs.values, atol=1e-5, rtol=0)

    p_values = np.asarray(m.p_values, dtype=float)
    assert p_values.shape == (12, 101)
    np.testing.assert_allclose(p_values, golden_p_values.values, atol=1e-9, rtol=0)

    labels = np.asarray(m.labels)
    assert labels.shape == (12, GOLDEN_LENGTH)
    np.testing.assert_array_equal(labels, golden_labels.values.astype(int))

    attention = np.asarray(m.attention)
    assert attention.shape == (12, GOLDEN_LENGTH)
    np.testing.assert_array_equal(attention, golden_attention.values.astype(int))

    assert m.inference_ms > 0


def test_golden_site_count_matches_upstream(
    app_client, predictor, golden_sequence, golden_label_sites
):
    assert len(golden_label_sites) == GOLDEN_N_SITES_AT_005  # fixture sanity

    r = _post(app_client, golden_sequence, alpha=0.05)
    assert r.status_code == 200, r.text
    body = r.json()
    api_keys = _keys(body["results"])
    assert len(api_keys) == GOLDEN_N_SITES_AT_005
    assert set(api_keys) == golden_label_sites
    assert body["meta"]["n_sites"] == GOLDEN_N_SITES_AT_005

    # The predictor object agrees with the HTTP layer (no extra filtering on either side).
    pred = predictor.predict(golden_sequence, alpha=0.05)
    assert {(s.mod_type, s.position) for s in pred.sites} == golden_label_sites
    assert len(pred.sites) == GOLDEN_N_SITES_AT_005


def test_results_sorted_by_position_then_mod_order(app_client, golden_sequence):
    # alpha = 1.0 yields the most rows (everything with p < 1), the strongest ordering check.
    r = _post(app_client, golden_sequence, alpha=1.0)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) > GOLDEN_N_SITES_AT_005
    keys = [(row["position"], MOD_TYPES.index(row["mod_type"])) for row in results]
    assert keys == sorted(keys), "results are not sorted by (position, MOD_TYPES order)"
    assert len(set(keys)) == len(keys), "duplicate (position, mod_type) rows"


def test_alpha_filter_is_monotonic(app_client, golden_sequence, golden_p_values, golden_probs):
    pv = golden_p_values.values
    pr = golden_probs.values
    assert (pr > 0).all(), "fixture sanity: no zero probabilities expected"

    sites_at: dict[float, set[tuple[str, int]]] = {}
    for alpha in (0.01, 0.05, 0.2, 1.0):
        r = _post(app_client, golden_sequence, alpha=alpha)
        assert r.status_code == 200, r.text
        body = r.json()
        rows = body["results"]
        assert body["meta"]["alpha"] == alpha
        assert body["meta"]["n_sites"] == len(rows)
        assert all(row["p_value"] < alpha for row in rows), f"row with p >= alpha at alpha={alpha}"
        sites_at[alpha] = set(_keys(rows))
        assert len(sites_at[alpha]) == len(rows)

        # Expected count from the fixture. p-values are multiples of 1/150, so an alpha that
        # lands exactly on such a value (0.2 == 30/150) is a floating-point boundary: accept
        # either side of it, but nothing else.
        lo = int((pv < alpha - 1e-9).sum())
        hi = int((pv < alpha + 1e-9).sum())
        assert lo <= len(rows) <= hi, f"alpha={alpha}: {len(rows)} rows, fixture says {lo}..{hi}"

    assert sites_at[0.01] <= sites_at[0.05] <= sites_at[0.2] <= sites_at[1.0]
    assert len(sites_at[0.05]) == GOLDEN_N_SITES_AT_005

    # alpha = 1.0 returns every cell except those with p_value == 1.0 (probability is never 0 here).
    n_p_equal_one = int((pv == 1.0).sum())
    assert len(sites_at[1.0]) == 12 * 101 - n_p_equal_one
    assert len(sites_at[1.0]) == int((pv < 1.0).sum())


def test_sample_endpoint_is_the_golden_sequence(app_client, golden_sequence, golden_label_sites):
    r = app_client.get("/api/samples/sequence")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("name", "description", "sequence", "length", "source_url"):
        assert key in body, f"sample response lacks {key!r}"
        assert body[key] not in (None, ""), f"sample response has empty {key!r}"
    assert body["sequence"] == golden_sequence
    assert body["length"] == GOLDEN_LENGTH == len(body["sequence"])

    # Feeding the sample straight back gives the 22 upstream sites.
    r2 = _post(app_client, body["sequence"], alpha=0.05)
    assert r2.status_code == 200, r2.text
    keys = _keys(r2.json()["results"])
    assert len(keys) == GOLDEN_N_SITES_AT_005
    assert set(keys) == golden_label_sites
