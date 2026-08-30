"""Opt-in attention windows (`include_attention`) for the track view."""

from __future__ import annotations

import numpy as np

from app.schemas import MOD_TYPES

URL = "/api/predict/sequence"


def test_attention_absent_by_default(app_client, golden_sequence):
    r = app_client.post(URL, json={"sequence": golden_sequence, "alpha": 0.05})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["attention"] is None


def test_attention_parallels_results(app_client, golden_sequence):
    r = app_client.post(
        URL, json={"sequence": golden_sequence, "alpha": 0.05, "include_attention": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    results, attention = body["results"], body["meta"]["attention"]
    assert len(results) == 22 == len(attention)
    for site, att in zip(results, attention, strict=True):
        assert (att["position"], att["mod_type"]) == (site["position"], site["mod_type"])
        assert 1 <= len(att["windows"]) <= 3
        # Windows lie inside the 51-nt window centred on the site and are best-first.
        lo, hi = site["position"] - 25, site["position"] + 25
        scores = [w["score"] for w in att["windows"]]
        assert scores == sorted(scores, reverse=True)
        for w in att["windows"]:
            assert lo <= w["start"] <= w["end"] <= hi
            assert w["end"] - w["start"] == 2  # upstream --att_window 3
    # Results themselves are unchanged by the flag.
    plain = app_client.post(URL, json={"sequence": golden_sequence, "alpha": 0.05}).json()
    assert plain["results"] == results


def test_attention_windows_union_equals_upstream_mask(predictor, golden_sequence, golden_attention):
    """OR-ing the per-site windows must give exactly upstream's attention.csv."""
    m = predictor.predict_matrix(golden_sequence, alpha=0.05, with_attention=True)
    assert m.attention_windows is not None
    mask = np.zeros_like(m.attention)
    for (k, _w), windows in m.attention_windows.items():
        for start, end, _score in windows:
            mask[k, start : end + 1] = 1
    assert np.array_equal(mask, np.asarray(golden_attention.to_numpy(), dtype=mask.dtype))
    assert np.array_equal(mask, m.attention)


def test_attention_via_predict_matches_matrix(predictor, golden_sequence):
    pred = predictor.predict(golden_sequence, alpha=0.05, include_attention=True)
    assert pred.attention is not None and len(pred.attention) == len(pred.sites) == 22
    m = predictor.predict_matrix(golden_sequence, alpha=0.05, with_attention=True)
    for site, att in zip(pred.sites, pred.attention, strict=True):
        k, w = MOD_TYPES.index(site.mod_type), site.position - 26
        expected = m.attention_windows[(k, w)]
        got = [(a.start - 1, a.end - 1, a.score) for a in att.windows]
        assert got == expected


def test_stub_attention_shape(stub_client, golden_sequence):
    r = stub_client.post(URL, json={"sequence": golden_sequence, "include_attention": True})
    assert r.status_code == 200
    att = r.json()["meta"]["attention"]
    assert len(att) == 6 and all(len(a["windows"]) == 3 for a in att)
