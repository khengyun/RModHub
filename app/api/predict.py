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
from app.schemas import (
    MOD_TYPES,
    ModelRun,
    ModSite,
    PredictionMeta,
    PredictSequenceRequest,
    PredictSequenceResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/predict", tags=["predict"])

# The shared long format (app/csvio.py); the signal branch emits the same seven columns first.
CSV_COLUMNS = MODSITE_COLUMNS
# Comparison exports keep the seven shared columns first and append the model id, the same
# rule the signal branch follows for its extra columns.
CSV_COLUMNS_MULTI = MODSITE_COLUMNS + ("model",)
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


def runs_to_csv(runs: list[ModelRun]) -> str:
    """Serialise several models' sites: shared columns first, then the model id."""
    rows = ([*modsite_cells(s), run.model] for run in runs for s in run.results)
    return "".join(iter_csv(CSV_COLUMNS_MULTI, rows))


def _resolve_models(requested: list[str] | None, loaded: dict) -> list[str]:
    """Requested ids, de-duplicated and order-preserving; the default when none were named."""
    if not requested:
        return [next(iter(loaded))]
    seen: dict[str, None] = {}
    for model_id in requested:
        if model_id not in loaded:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown model {model_id!r}; this server offers: "
                    f"{', '.join(loaded)} (see GET /api/capabilities)"
                ),
            )
        seen.setdefault(model_id, None)
    return list(seen)


def _note(result) -> str:
    """One sentence on the unscored flank, and on alpha when the model does not use it.

    The flank is read back from the result rather than from a constant: it is 25 nt for
    MultiRM's 51-nt window and 300 nt for TransRNAm's 601-nt one.
    """
    flank = result.predicted_start - 1
    note = f"{result.model_name} does not predict the first and last {flank} nt of the input."
    if result.extra.get("alpha_applies") is False:
        threshold = result.extra.get("site_threshold")
        note += (
            f" It reports no empirical p-value, so alpha does not filter these rows: a site is"
            f" listed when its probability reaches {threshold}."
        )
    return note


def _run_model(model_id, predictor, normalized, alpha: float, include_attention: bool) -> ModelRun:
    """Score the normalised sequence with one back-end and wrap it in a `ModelRun`."""
    try:
        result = predictor.predict(
            normalized.sequence, alpha, include_attention=include_attention
        )
    except ValueError as exc:
        # The predictor's own defensive checks. `normalize_sequence` should already have
        # rejected anything that trips them, so this is belt-and-braces, scoped to this
        # call only (a global ValueError handler would disguise server bugs as 422s).
        log.warning("request rejected by predictor %s: %s", model_id, exc)
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
        note=_note(result),
        extra=extra,
        attention=result.attention,
    )
    return ModelRun(model=model_id, results=sites, meta=meta)


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

    The models predict the centre of a 51-nt window, so positions 1..25 and the last
    25 nt never receive a prediction. Only sites with `p_value < alpha` are returned.

    `models` picks the back-ends to run (ids from `sequence_models` in
    `GET /api/capabilities`); omitting it uses the server default. Naming two or more runs
    each of them on the same input: `results`/`meta` then hold the first and `comparison`
    holds them all. `?format=csv` gains a trailing `model` column in that case.
    """
    t0 = perf_counter()
    state = request.app.state
    loaded: dict = getattr(state, "predictors", None) or {}
    default_predictor = getattr(state, "predictor", None)
    if default_predictor is None or not loaded:
        raise HTTPException(status_code=503, detail="model not loaded")
    settings = state.settings

    model_ids = _resolve_models(body.models, loaded)

    normalized = normalize_sequence(
        body.sequence,
        min_nt=settings.min_sequence_nt,
        max_nt=settings.max_sequence_nt,
    )
    alpha = body.alpha if body.alpha is not None else settings.default_alpha

    runs = [
        _run_model(model_id, loaded[model_id], normalized, alpha, body.include_attention)
        for model_id in model_ids
    ]
    primary = runs[0]

    total_ms = (perf_counter() - t0) * 1000
    # Never log the sequence itself: it may be unpublished data.
    log.info(
        "predict/sequence length=%d alpha=%g models=%s n_sites=%s total_ms=%.1f format=%s",
        primary.meta.sequence_length,
        alpha,
        ",".join(model_ids),
        ",".join(str(len(r.results)) for r in runs),
        total_ms,
        fmt,
    )

    if fmt == "csv":
        content = runs_to_csv(runs) if len(runs) > 1 else sites_to_csv(primary.results)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{CSV_FILENAME}"'},
        )
    return PredictSequenceResponse(
        results=primary.results,
        meta=primary.meta,
        comparison=runs if len(runs) > 1 else None,
    )
