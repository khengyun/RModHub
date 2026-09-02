"""Sequence-branch model picker: registry, `models` request field and `comparison` output.

The app fixtures here load the stub twice over (multirm + stub) only where a second
back-end is actually needed; single-model behaviour must stay byte-identical to before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

URL = "/api/predict/sequence"
SEQ = "ACGT" * 40  # 160 nt, long enough for the 51-nt window; MultiRM finds nothing in it


@pytest.fixture(scope="module")
def two_model_client():
    """Stub as the default plus MultiRM as the extra: two real entries in the picker, and
    the fast one answers the requests that do not name a model."""
    app = create_app(Settings(predictor="stub", sequence_models="multirm", warmup=False))
    with TestClient(app) as client:
        yield client


def test_capabilities_lists_models(two_model_client):
    models = two_model_client.get("/api/capabilities").json()["sequence_models"]
    assert [m["id"] for m in models] == ["stub", "multirm"]
    assert [m["default"] for m in models] == [True, False]
    for m in models:
        assert m["label"] and m["description"] and m["name"] and m["version"]


def test_default_request_runs_one_model_and_omits_comparison(two_model_client):
    body = two_model_client.post(URL, json={"sequence": SEQ}).json()
    assert body["comparison"] is None
    assert body["meta"]["model_name"] == "stub"  # first entry is the default


def test_named_single_model_still_omits_comparison(two_model_client):
    body = two_model_client.post(URL, json={"sequence": SEQ, "models": ["multirm"]}).json()
    assert body["comparison"] is None
    assert body["meta"]["model_name"] == "MultiRM"


def test_two_models_fill_comparison_in_request_order(two_model_client):
    body = two_model_client.post(
        URL, json={"sequence": SEQ, "models": ["multirm", "stub"]}
    ).json()
    assert [run["model"] for run in body["comparison"]] == ["multirm", "stub"]
    # results/meta mirror the first run so old clients keep working.
    assert body["results"] == body["comparison"][0]["results"]
    assert body["meta"] == body["comparison"][0]["meta"]


def test_repeated_model_is_run_once(two_model_client):
    body = two_model_client.post(
        URL, json={"sequence": SEQ, "models": ["stub", "stub", "multirm"]}
    ).json()
    assert [run["model"] for run in body["comparison"]] == ["stub", "multirm"]


def test_unknown_model_is_422_and_names_what_is_offered(two_model_client):
    r = two_model_client.post(URL, json={"sequence": SEQ, "models": ["nope"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "nope" in detail and "stub" in detail and "multirm" in detail


def test_csv_gains_a_model_column_only_for_a_comparison(two_model_client, golden_sequence):
    single = two_model_client.post(f"{URL}?format=csv", json={"sequence": SEQ}).text
    assert (
        single.splitlines()[0]
        == "transcript_id,position,mod_type,probability,p_value,coverage,source"
    )

    # The golden sequence is used here because both back-ends report sites on it.
    multi = two_model_client.post(
        f"{URL}?format=csv", json={"sequence": golden_sequence, "models": ["stub", "multirm"]}
    ).text
    header, *rows = multi.splitlines()
    assert header.endswith(",model")
    assert {row.rsplit(",", 1)[1] for row in rows} == {"stub", "multirm"}
    # Rows stay grouped per model, in the requested order.
    assert rows[0].endswith(",stub") and rows[-1].endswith(",multirm")


def test_settings_reject_an_unknown_model_id():
    with pytest.raises(ValueError, match="unknown sequence model"):
        Settings(sequence_models="multirm,nope")


def test_predictor_stays_the_default_and_extras_follow_it():
    """`sequence_models` adds models; it must never override an explicit `predictor`."""
    assert Settings(predictor="stub", sequence_models="").enabled_sequence_models() == ["stub"]
    assert Settings(predictor="stub", sequence_models=" multirm , stub ").enabled_sequence_models() == [
        "stub",
        "multirm",
    ]
