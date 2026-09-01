"""Sequence branch: `POST /api/predict/sequence`.

The handler is a plain `def` on purpose: FastAPI runs sync endpoints in its threadpool,
so the CPU-bound model call does not block the event loop (and /health stays responsive
while a long sequence is being scored).
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.api.normalize import normalize_sequence
from app.csvio import MODSITE_COLUMNS, iter_csv, modsite_cells
from app.predictors.base import FLANK_NT
from app.schemas import (
    MOD_TYPES,
    ModSite,
    PredictionMeta,
    PredictSequenceRequest,
    PredictSequenceResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/predict", tags=["predict"])

# The shared long format (app/csvio.py); the signal branch emits the same seven columns first.
CSV_COLUMNS = MODSITE_COLUMNS
CSV_FILENAME = "rmodhub_sites.csv"

_ERROR_RESPONSES = {
    422: {
        "description": "Invalid input (bad sequence, bad alpha, ...)",
        "content": {"application/json": {"example": {"detail": "sequence too short: 50 nt ..."}}},
    },
    503: {
        "description": "Model not loaded",
        "content": {"application/json": {"example": {"detail": "model not loaded"}}},
    },
}


def sites_to_csv(sites: list[ModSite]) -> str:
    """Serialise sites in the shared long format. None -> empty cell."""
    return "".join(iter_csv(CSV_COLUMNS, (modsite_cells(s) for s in sites)))


@router.post(
    "/sequence",
    response_model=PredictSequenceResponse,
    summary="Predict RNA modification sites from a nucleotide sequence",
    responses={
        200: {
            "description": "Predicted sites (JSON by default, CSV with ?format=csv)",
            "content": {
                "application/json": {},
                "text/csv": {"example": ",".join(CSV_COLUMNS) + "\n"},
            },
        },
        **_ERROR_RESPONSES,
    },
)
def predict_sequence(
    request: Request,
    body: PredictSequenceRequest,
    fmt: Literal["json", "csv"] = Query(
        "json",
        alias="format",
        description="Response format. 'csv' returns the result rows as a downloadable file.",
    ),
) -> PredictSequenceResponse | Response:
    """Score one sequence (raw nucleotides or a single FASTA record, DNA or RNA alphabet).

    MultiRM predicts the centre of a 51-nt window, so positions 1..25 and the last
    25 nt never receive a prediction. Only sites with `p_value < alpha` are returned.
    """
    t0 = perf_counter()
    state = request.app.state
    predictor = getattr(state, "predictor", None)
    if predictor is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    settings = state.settings

    normalized = normalize_sequence(
        body.sequence,
        min_nt=settings.min_sequence_nt,
        max_nt=settings.max_sequence_nt,
    )
    alpha = body.alpha if body.alpha is not None else settings.default_alpha

    try:
        result = predictor.predict(
            normalized.sequence, alpha, include_attention=body.include_attention
        )
    except ValueError as exc:
        # The predictor's own defensive checks. `normalize_sequence` should already have
        # rejected anything that trips them, so this is belt-and-braces, scoped to this
        # call only (a global ValueError handler would disguise server bugs as 422s).
        log.warning("request rejected by predictor: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sites = result.sites
    if normalized.transcript_id is not None:
        sites = [s.model_copy(update={"transcript_id": normalized.transcript_id}) for s in sites]

    extra = dict(result.extra)
    extra.setdefault("input_had_u", normalized.had_u)
    extra.setdefault("input_had_fasta_header", normalized.had_fasta_header)

    meta = PredictionMeta(
        sequence_length=result.sequence_length,
        predicted_start=result.predicted_start,
        predicted_end=result.predicted_end,
        alpha=result.alpha,
        n_sites=len(sites),
        model_name=result.model_name,
        model_version=result.model_version,
        inference_ms=round(result.inference_ms, 2),
        source="sequence",
        transcript_id=normalized.transcript_id,
        mod_types=list(MOD_TYPES),
        note=f"MultiRM does not predict the first and last {FLANK_NT} nt of the input.",
        extra=extra,
        attention=result.attention,
    )

    total_ms = (perf_counter() - t0) * 1000
    # Never log the sequence itself: it may be unpublished data.
    log.info(
        "predict/sequence length=%d alpha=%g n_sites=%d inference_ms=%.1f total_ms=%.1f format=%s",
        result.sequence_length,
        alpha,
        len(sites),
        result.inference_ms,
        total_ms,
        fmt,
    )

    if fmt == "csv":
        return Response(
            content=sites_to_csv(sites),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{CSV_FILENAME}"'},
        )
    return PredictSequenceResponse(results=sites, meta=meta)
