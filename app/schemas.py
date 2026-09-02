"""Shared Pydantic schemas.

`ModSite` is the contract shared by BOTH input branches (sequence / MultiRM and
nanopore signal / DirectRM, see app/jobs/schemas.py::SignalSite). Do not change its field
set; docs/signal-branch.md depends on it.
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


class AttentionWindow(BaseModel):
    """One region the model attended to when scoring a site (upstream `--att_window 3`)."""

    start: int = Field(ge=1, description="1-based inclusive start in the input sequence.")
    end: int = Field(ge=1, description="1-based inclusive end in the input sequence.")
    score: float = Field(description="Summed per-nucleotide attention weight of the window.")


class SiteAttention(BaseModel):
    """Top attention windows for one predicted site; parallels one `ModSite` row."""

    position: int = Field(ge=1)
    mod_type: str
    windows: list[AttentionWindow] = Field(
        description="Ranked best-first (upstream `--top 3`), non-overlapping."
    )


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
    include_attention: bool = Field(
        default=False,
        description=(
            "Also return, in meta.attention, the top-3 attention windows the model used for "
            "each reported site (for visualisation). Adds ~10-30% latency on long inputs."
        ),
    )
    models: list[str] | None = Field(
        default=None,
        description=(
            "Sequence models to run, by id (see `sequence_models` in GET /api/capabilities). "
            "Omit for the server default. Naming two or more runs each of them on the same "
            "input and fills `comparison`; `results`/`meta` then hold the first one."
        ),
        json_schema_extra={"example": ["multirm"]},
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
    attention: list[SiteAttention] | None = Field(
        default=None,
        description=(
            "Present only when the request set include_attention=true. One entry per "
            "row of `results`, same order."
        ),
    )


class ModelRun(BaseModel):
    """One model's answer for the input, used when several were requested."""

    model: str = Field(description="Model id, as listed in GET /api/capabilities.")
    results: list[ModSite]
    meta: PredictionMeta


class PredictSequenceResponse(BaseModel):
    results: list[ModSite]
    meta: PredictionMeta
    comparison: list[ModelRun] | None = Field(
        default=None,
        description=(
            "Present only when the request named more than one model. Every requested model "
            "in the requested order, the first repeating `results`/`meta` above."
        ),
    )


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
    signal_enabled: bool = Field(
        default=False, description="Nanopore signal branch (job API, DirectRM worker) enabled."
    )
