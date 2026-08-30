"""Shared Pydantic schemas.

`ModSite` is the contract shared by BOTH input branches (sequence / MultiRM today,
nanopore signal / DirectRM later). Do not change its field set without updating the
signal branch design notes in README.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Canonical order of the 12 modification types predicted by MultiRM.
# Row order of every MultiRM output matrix follows this tuple.
MOD_TYPES: tuple[str, ...] = (
    "Am",
    "Cm",
    "Gm",
    "Um",
    "m1A",
    "m5C",
    "m5U",
    "m6A",
    "m6Am",
    "m7G",
    "Psi",
    "AtoI",
)

Source = Literal["sequence", "signal"]


class ModSite(BaseModel):
    """One predicted modification at one position. Long (tidy) format, one row per (position, mod_type)."""

    transcript_id: str | None = Field(
        default=None, description="Transcript / read reference id. None for a pasted sequence."
    )
    position: int = Field(
        ge=1, description="1-based position in the sequence as entered by the user."
    )
    mod_type: str = Field(description="Modification type, one of MOD_TYPES.")
    probability: float = Field(ge=0.0, le=1.0, description="Model probability for this site.")
    p_value: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Empirical p-value against the negative background.",
    )
    coverage: int | None = Field(
        default=None, ge=0, description="Read coverage. Always None for the sequence branch."
    )
    source: Source = Field(description="'sequence' (MultiRM) or 'signal' (DirectRM).")


# --------------------------------------------------------------------------- API models
# Request/response envelopes for the HTTP layer. `ModSite` and `MOD_TYPES` above are the
# shared contract and must not change; everything below is owned by the API layer.


class PredictSequenceRequest(BaseModel):
    """Body of `POST /api/predict/sequence`."""

    sequence: str = Field(
        description=(
            "Nucleotide sequence, 51-10000 nt. Raw nucleotides or a single FASTA record; "
            "DNA (T) or RNA (U) alphabet; case and whitespace are ignored."
        ),
        json_schema_extra={
            "example": "GGGGCCGTGGATACCTGCCTTTTAATTCTTTTTTATTCGCCCATCGGGGCCGCGGATACC"
        },
    )
    alpha: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Significance threshold on the empirical p-value (0 < alpha <= 1). "
            "Sites with p_value < alpha are returned. Defaults to the server default (0.05)."
        ),
        json_schema_extra={"example": 0.05},
    )


class PredictionMeta(BaseModel):
    """Run-level information accompanying the site list."""

    sequence_length: int
    predicted_start: int = Field(
        description="First position (1-based) that can receive a prediction."
    )
    predicted_end: int = Field(description="Last position (1-based) that can receive a prediction.")
    alpha: float
    n_sites: int
    model_name: str
    model_version: str
    inference_ms: float
    source: Literal["sequence"] = "sequence"
    transcript_id: str | None = Field(
        default=None, description="Taken from the FASTA header, if the input had one."
    )
    mod_types: list[str] = Field(default_factory=lambda: list(MOD_TYPES))
    note: str = "MultiRM does not predict the first and last 25 nt of the input."
    extra: dict = Field(default_factory=dict, description="Backend-specific extras.")


class PredictSequenceResponse(BaseModel):
    results: list[ModSite]
    meta: PredictionMeta


class SampleSequenceResponse(BaseModel):
    name: str
    description: str
    sequence: str
    length: int
    source_url: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    model_name: str
    model_version: str
    model_loaded: bool
    uptime_s: float
    version: str = Field(description="RModHub server version.")
