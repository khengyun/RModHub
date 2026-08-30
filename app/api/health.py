"""Liveness / readiness endpoint."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Server and model status",
    responses={503: {"description": "Model not loaded"}},
)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    predictor = getattr(state, "predictor", None)
    if predictor is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    started_at = getattr(state, "started_at", None)
    uptime_s = time.monotonic() - started_at if started_at is not None else 0.0
    return HealthResponse(
        status="ok",
        model_name=predictor.name,
        model_version=predictor.version,
        model_loaded=True,
        uptime_s=round(uptime_s, 1),
        version=state.settings.version,
    )
