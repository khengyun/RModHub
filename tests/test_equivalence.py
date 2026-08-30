"""Input normalisation must not change the numbers.

U/T alphabet, letter case, FASTA wrapping and stray whitespace are all presentation details;
the model must see byte-identical input and produce byte-identical output. Real model, shared
session app.
"""

from __future__ import annotations

import pytest

URL = "/api/predict/sequence"


def _post(client, sequence: str, alpha: float = 0.05) -> dict:
    r = client.post(URL, json={"sequence": sequence, "alpha": alpha})
    assert r.status_code == 200, r.text
    return r.json()


def _comparable_meta(meta: dict) -> dict:
    """``meta`` minus the fields that legitimately vary between spellings of the same input.

    ``inference_ms`` is wall-clock. ``extra["input_*"]`` flags (``input_had_u``,
    ``input_had_fasta_header``) describe how the input was written, not what the model saw,
    so they are checked separately where relevant and excluded here.
    """
    m = dict(meta)
    m.pop("inference_ms", None)
    m["extra"] = {k: v for k, v in meta.get("extra", {}).items() if not k.startswith("input_")}
    return m


def _input_flag(meta: dict, name: str):
    """Optional input-descriptor flag from ``meta.extra`` (None when the API does not emit it)."""
    return meta.get("extra", {}).get(name)


@pytest.fixture(scope="module")
def reference(app_client, golden_sequence) -> dict:
    """Response for the plain upper-case DNA-alphabet golden sequence."""
    body = _post(app_client, golden_sequence)
    assert body["results"], "reference call returned no sites"
    return body


def test_u_and_t_give_identical_results(app_client, golden_sequence, reference):
    rna = golden_sequence.replace("T", "U")
    assert rna != golden_sequence and "T" not in rna
    body = _post(app_client, rna)
    # Exact equality: same floats in, same floats out (no re-computation, no rounding drift).
    assert body["results"] == reference["results"]
    assert _comparable_meta(body["meta"]) == _comparable_meta(reference["meta"])
    if _input_flag(body["meta"], "input_had_u") is not None:  # optional descriptor
        assert _input_flag(body["meta"], "input_had_u") is True
        assert _input_flag(reference["meta"], "input_had_u") is False


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(str.lower, id="lowercase"),
        pytest.param(
            lambda s: "".join(c.lower() if i % 2 else c for i, c in enumerate(s)), id="mixed-case"
        ),
        pytest.param(lambda s: s.lower().replace("t", "u"), id="lowercase-rna"),
    ],
)
def test_case_variants_identical(app_client, golden_sequence, reference, transform):
    variant = transform(golden_sequence)
    assert variant != golden_sequence
    body = _post(app_client, variant)
    assert body["results"] == reference["results"]
    assert _comparable_meta(body["meta"]) == _comparable_meta(reference["meta"])


def test_fasta_wrapped_identical(app_client, golden_sequence, reference):
    lines = [golden_sequence[i : i + 60] for i in range(0, len(golden_sequence), 60)]
    fasta = ">golden_151nt hand-verified fixture\n" + "\n".join(lines) + "\n"
    body = _post(app_client, fasta)

    assert body["meta"]["transcript_id"] == "golden_151nt"
    if _input_flag(body["meta"], "input_had_fasta_header") is not None:  # optional descriptor
        assert _input_flag(body["meta"], "input_had_fasta_header") is True
        assert _input_flag(reference["meta"], "input_had_fasta_header") is False
    assert body["results"], "expected sites"
    for row in body["results"]:
        assert row["transcript_id"] == "golden_151nt"
    assert [{**row, "transcript_id": None} for row in body["results"]] == reference["results"]

    got_meta = _comparable_meta(body["meta"])
    ref_meta = _comparable_meta(reference["meta"])
    got_meta.pop("transcript_id")
    ref_meta.pop("transcript_id")
    assert got_meta == ref_meta


def test_crlf_and_internal_whitespace_identical(app_client, golden_sequence, reference):
    lines = [golden_sequence[i : i + 50] for i in range(0, len(golden_sequence), 50)]
    messy = " \t" + "\r\n".join(lines) + "\r\n"
    messy = messy[:70] + " " + messy[70:]  # a stray space inside a line
    body = _post(app_client, messy)
    assert body["meta"]["sequence_length"] == len(golden_sequence)
    assert body["results"] == reference["results"]
    assert _comparable_meta(body["meta"]) == _comparable_meta(reference["meta"])
