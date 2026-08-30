"""HTTP-layer validation, normalisation and formatting.

Runs against the torch-free stub predictor (``stub_client``), so it is fast and independent of
the real model. The stub returns its six fixed sites for any normalised input of >= 104 nt.
"""

from __future__ import annotations

import csv
import io
import json
import re

import pytest

URL = "/api/predict/sequence"
CSV_HEADER = "transcript_id,position,mod_type,probability,p_value,coverage,source"

GOOD = "ACGT" * 40  # 160 nt; long enough for all six stub sites (last one at position 79)
N_STUB_SITES = 6


def _post(client, sequence: str | None = None, *, alpha=None, params=None, body=None):
    if body is None:
        body = {"sequence": sequence}
        if alpha is not None:
            body["alpha"] = alpha
    return client.post(URL, json=body, params=params)


def _detail(resp) -> str:
    """The error body must be JSON with a ``detail`` key; return it as text for substring checks."""
    assert resp.headers["content-type"].startswith("application/json"), resp.text
    body = resp.json()
    assert isinstance(body, dict) and "detail" in body, body
    detail = body["detail"]
    return detail if isinstance(detail, str) else json.dumps(detail)


def _ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= {"results", "meta"}
    return body


# ----------------------------------------------------------------------------- length limits


def test_too_short_50nt_rejected(stub_client):
    r = _post(stub_client, "A" * 50)
    assert r.status_code == 422
    assert "at least 51" in _detail(r)


@pytest.mark.parametrize("seq", ["", "   ", "\n\n", ">header only\n"])
def test_empty_sequence_rejected(stub_client, seq):
    r = _post(stub_client, seq)
    assert r.status_code == 422
    assert _detail(r)


def test_too_long_10001nt_rejected(stub_client):
    r = _post(stub_client, "A" * 10_001)
    assert r.status_code == 422
    assert "at most 10000" in _detail(r)


def test_exactly_51nt_accepted(stub_client):
    seq = "ACGT" * 12 + "ACG"
    assert len(seq) == 51
    body = _ok(_post(stub_client, seq))
    meta = body["meta"]
    assert meta["sequence_length"] == 51
    assert meta["predicted_start"] == 26 == meta["predicted_end"]
    assert meta["n_sites"] == len(body["results"]) == 0  # stub sites all lie beyond position 26


def test_exactly_10000nt_accepted(stub_client):
    body = _ok(_post(stub_client, "ACGT" * 2500))
    meta = body["meta"]
    assert meta["sequence_length"] == 10_000
    assert meta["predicted_start"] == 26
    assert meta["predicted_end"] == 9_975
    assert meta["n_sites"] == len(body["results"]) == N_STUB_SITES


# ----------------------------------------------------------------------------- alphabet


def test_invalid_character_reported_with_offending_char(stub_client):
    r = _post(stub_client, "ACGU" * 13 + "N")
    assert r.status_code == 422
    detail = _detail(r)
    assert "invalid character" in detail
    assert "'N'" in detail


@pytest.mark.parametrize("bad_char", ["1", "-", ".", "*"])
def test_non_nucleotide_characters_rejected(stub_client, bad_char):
    r = _post(stub_client, "ACGT" * 13 + bad_char)
    assert r.status_code == 422
    detail = _detail(r)
    assert "invalid character" in detail
    assert f"'{bad_char}'" in detail


def test_u_accepted_as_rna_alphabet(stub_client):
    rna = _ok(_post(stub_client, GOOD.replace("T", "U")))
    dna = _ok(_post(stub_client, GOOD))
    assert rna["meta"]["sequence_length"] == 160
    assert rna["results"] == dna["results"]


def test_lowercase_accepted(stub_client):
    lower = _ok(_post(stub_client, GOOD.lower()))
    upper = _ok(_post(stub_client, GOOD))
    assert lower["meta"]["sequence_length"] == 160
    assert lower["results"] == upper["results"]
    assert lower["meta"]["n_sites"] == N_STUB_SITES


def test_internal_whitespace_ignored(stub_client):
    chunks = [GOOD[i : i + 60] for i in range(0, len(GOOD), 60)]
    messy = "  " + "\r\n".join(chunks) + "\n"
    messy = messy.replace("ACGTACGT", "ACGT ACGT\t", 1)  # a space and a tab inside a line
    body = _ok(_post(stub_client, messy))
    assert body["meta"]["sequence_length"] == 160  # bases only
    assert body["results"] == _ok(_post(stub_client, GOOD))["results"]


# ----------------------------------------------------------------------------- alpha


@pytest.mark.parametrize("alpha", [0, 0.0, -0.05, 1.5, 2])
def test_alpha_out_of_range_rejected(stub_client, alpha):
    r = _post(stub_client, GOOD, alpha=alpha)
    assert r.status_code == 422
    assert "alpha" in _detail(r).lower()


def test_alpha_one_is_accepted(stub_client):
    body = _ok(_post(stub_client, GOOD, alpha=1.0))
    assert body["meta"]["alpha"] == 1.0


def test_alpha_missing_defaults_to_005(stub_client):
    body = _ok(_post(stub_client, GOOD))
    assert body["meta"]["alpha"] == 0.05
    assert all(row["p_value"] < 0.05 for row in body["results"])


def test_alpha_filters_stub_sites(stub_client):
    body = _ok(_post(stub_client, GOOD, alpha=0.03))
    assert {(r["mod_type"], r["position"]) for r in body["results"]} == {("Gm", 52), ("m5C", 79)}


# ----------------------------------------------------------------------------- malformed bodies


@pytest.mark.parametrize("raw", [b"{not json", b"", b"[]", b'"ACGT"', b"null"])
def test_malformed_json_rejected(stub_client, raw):
    r = stub_client.post(URL, content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert _detail(r)


@pytest.mark.parametrize(
    "body",
    [{}, {"seq": GOOD}, {"sequence": None}, {"sequence": 12345}, {"sequence": [GOOD]}],
)
def test_missing_or_wrong_type_sequence_rejected(stub_client, body):
    r = _post(stub_client, body=body)
    assert r.status_code == 422
    assert _detail(r)


# ----------------------------------------------------------------------------- FASTA


@pytest.mark.parametrize("header", [">tx1 my transcript", ">tx1"])
def test_fasta_header_sets_transcript_id(stub_client, header):
    body = _ok(_post(stub_client, header + "\n" + GOOD))
    assert body["meta"]["transcript_id"] == "tx1"
    assert body["meta"]["sequence_length"] == 160
    assert body["meta"]["n_sites"] == N_STUB_SITES
    assert body["results"], "expected stub sites"
    for row in body["results"]:
        assert row["transcript_id"] == "tx1"
    plain = _ok(_post(stub_client, GOOD))
    assert [{**row, "transcript_id": None} for row in body["results"]] == plain["results"]
    assert plain["meta"]["transcript_id"] is None


def test_two_fasta_records_rejected(stub_client):
    r = _post(stub_client, ">a\n" + GOOD + "\n>b\n" + GOOD)
    assert r.status_code == 422
    assert _detail(r)


# ----------------------------------------------------------------------------- CSV output


def test_csv_format(stub_client):
    r = _post(stub_client, GOOD, params={"format": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0] == CSV_HEADER

    json_body = _ok(_post(stub_client, GOOD))
    assert len(lines) - 1 == json_body["meta"]["n_sites"] == N_STUB_SITES

    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(row["mod_type"], int(row["position"])) for row in rows] == [
        (row["mod_type"], row["position"]) for row in json_body["results"]
    ]
    for row in rows:
        assert row["source"] == "sequence"
        assert 0.0 < float(row["probability"]) <= 1.0
        assert 0.0 <= float(row["p_value"]) < 0.05
        assert row["transcript_id"] == ""  # None serialises as an empty cell
        assert row["coverage"] == ""


def test_csv_format_carries_transcript_id(stub_client):
    r = _post(stub_client, ">tx9 desc\n" + GOOD, params={"format": "csv"})
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == N_STUB_SITES
    assert all(row["transcript_id"] == "tx9" for row in rows)


def test_csv_with_no_sites_has_header_only(stub_client):
    r = _post(stub_client, "ACGT" * 12 + "ACG", params={"format": "csv"})  # 51 nt -> 0 stub sites
    assert r.status_code == 200, r.text
    assert r.text.splitlines() == [CSV_HEADER]


def test_csv_validation_errors_still_json(stub_client):
    r = _post(stub_client, "A" * 50, params={"format": "csv"})
    assert r.status_code == 422
    assert "at least 51" in _detail(r)


# ----------------------------------------------------------------------------- health / index


def test_health(stub_client):
    r = stub_client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model_name"] == "stub"
    assert isinstance(body["model_version"], str)


def test_docs_are_self_hosted(stub_client):
    """Swagger UI must not load from a CDN (third-party assets are ruled out by NAR)."""
    r = stub_client.get("/docs")
    assert r.status_code == 200, r.text
    html = r.text
    assert "cdn.jsdelivr.net" not in html
    assert "cdn.jsdelivr" not in html and "unpkg.com" not in html
    assert not re.search(r"(src|href)\s*=\s*[\"']https?://", html, re.IGNORECASE), html
    assert "/static/swagger/swagger-ui-bundle.js" in html
    for asset in ("/static/swagger/swagger-ui-bundle.js", "/static/swagger/swagger-ui.css"):
        a = stub_client.get(asset)
        assert a.status_code == 200, asset
        assert len(a.content) > 10_000, asset
    assert stub_client.get("/redoc").status_code == 404


def test_index_html_is_self_contained(stub_client):
    r = stub_client.get("/")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "MIT" in html
    assert "MultiRM" in html
    # No third-party runtime dependencies: the page must work offline / behind a firewall.
    assert "fonts.googleapis" not in html
    assert '<script src="http' not in html
    assert not re.search(r"<script[^>]*\ssrc\s*=\s*[\"']https?://", html, re.IGNORECASE)
    for link_tag in re.findall(r"<link[^>]*>", html, re.IGNORECASE):
        if "stylesheet" in link_tag.lower():
            assert not re.search(r"href\s*=\s*[\"']https?://", link_tag, re.IGNORECASE), link_tag
