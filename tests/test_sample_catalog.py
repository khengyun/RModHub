"""The sequence-branch sample catalogue: a second, longer example for TransRNAm.

The 151-nt sample is the golden fixture and the one-click default; nothing here may move
it. The long one exists because TransRNAm's 601-nt window cannot score a 151-nt input.
"""

from __future__ import annotations

import pytest

from app.api.samples import LONG_SAMPLE, SAMPLE, SAMPLES

URL = "/api/samples/sequence"


def test_default_response_is_unchanged(stub_client):
    body = stub_client.get(URL).json()
    assert body["name"] == "multirm_readme_151nt"
    assert body["length"] == 151
    assert body["sequence"] == SAMPLE.sequence


def test_catalog_lists_every_sample_default_first(stub_client):
    body = stub_client.get(f"{URL}/catalog").json()
    assert [s["name"] for s in body] == list(SAMPLES)
    assert body[0]["name"] == SAMPLE.name
    for entry in body:
        assert entry["length"] == len(entry["sequence"])
        assert entry["description"] and entry["source_url"]


@pytest.mark.parametrize("name", list(SAMPLES))
def test_each_sample_can_be_fetched_by_name(stub_client, name):
    body = stub_client.get(URL, params={"name": name}).json()
    assert body["name"] == name
    assert body["sequence"] == SAMPLES[name].sequence


def test_unknown_name_is_404_and_says_what_exists(stub_client):
    r = stub_client.get(URL, params={"name": "nope"})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"] and SAMPLE.name in r.json()["detail"]


def test_long_sample_fits_the_601_nt_window():
    """It has to be long enough for TransRNAm to score more than a handful of positions."""
    from app.predictors.transrnam.predictor import FLANK_NT, MAX_SEQUENCE_NT, MIN_SEQUENCE_NT

    assert MIN_SEQUENCE_NT <= LONG_SAMPLE.length <= MAX_SEQUENCE_NT
    scorable = LONG_SAMPLE.length - 2 * FLANK_NT
    assert scorable >= 400, "too short to be a useful demonstration"


def test_long_sample_is_clean_rna_alphabet():
    assert set(LONG_SAMPLE.sequence) <= set("ACGT")
    assert LONG_SAMPLE.sequence[500] in "ACGT"  # the annotated base sits at position 501
