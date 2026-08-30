"""Canonical example input, so users (and the phase-2 frontend) can try the server in one click."""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas import SampleSequenceResponse

router = APIRouter(prefix="/api/samples", tags=["samples"])

# First 151 nt of the example sequence from the MultiRM README. This is also the golden
# fixture in tests/fixtures/golden_multirm_151nt/ (hard-coded here so the server never
# reads from tests/ at runtime). Upstream reports 22 significant sites at alpha=0.05.
SAMPLE_SEQUENCE = (
    "GGGGCCGTGGATACCTGCCTTTTAATTCTTTTTTATTCGCCCATCGGGGCCGCGGATACCTGCTTTTTATTTTTTTTTCCTTAGCCC"
    "ATCGGGGTATCGGATACCTGCTGATTCCCTTCCCCTCTGAACCCCCAACACTCTGGCCCATCGG"
)

SAMPLE = SampleSequenceResponse(
    name="multirm_readme_151nt",
    description=(
        "First 151 nt of the example sequence from the MultiRM README; 22 sites at alpha=0.05"
    ),
    sequence=SAMPLE_SEQUENCE,
    length=len(SAMPLE_SEQUENCE),
    source_url="https://github.com/Tsedao/MultiRM",
)


@router.get("/sequence", response_model=SampleSequenceResponse, summary="Example sequence")
def get_sample_sequence() -> SampleSequenceResponse:
    """Return a ready-to-submit example sequence for `POST /api/predict/sequence`."""
    return SAMPLE
