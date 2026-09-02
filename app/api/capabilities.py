"""`GET /api/capabilities`: what this deployment offers, so the UI can show or hide the
nanopore tab and display the upload limits without hard-coding them."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.jobs.schemas import Capabilities, Limits, Retention, SequenceModelInfo
from app.predictors import SEQUENCE_MODELS

router = APIRouter(prefix="/api", tags=["capabilities"])


@router.get("/capabilities", response_model=Capabilities, summary="Enabled branches and limits")
def get_capabilities(request: Request) -> Capabilities:
    s = request.app.state.settings
    loaded = getattr(request.app.state, "predictors", None) or {}
    models = [
        SequenceModelInfo(
            id=model_id,
            label=SEQUENCE_MODELS[model_id].label,
            description=SEQUENCE_MODELS[model_id].description,
            default=i == 0,
            name=predictor.name,
            version=predictor.version,
            min_sequence_nt=getattr(predictor, "min_sequence_nt", s.min_sequence_nt),
            max_sequence_nt=getattr(predictor, "max_sequence_nt", None),
        )
        for i, (model_id, predictor) in enumerate(loaded.items())
        if model_id in SEQUENCE_MODELS
    ]
    return Capabilities(
        sequence=True,
        sequence_models=models,
        signal=s.signal_enabled,
        limits=Limits(
            max_pod5_gb=s.max_pod5_gb,
            max_bam_gb=s.max_bam_gb,
            max_reference_mb=s.max_reference_mb,
            max_regions=s.max_regions,
            max_running_per_ip=s.max_running_per_ip,
            max_queued_per_ip=s.max_queued_per_ip,
            job_timeout_h=round(s.job_timeout_s / 3600, 2),
            tus_chunk_mb=s.tus_chunk_mb,
            upload_ttl_h=s.upload_ttl_h,
        ),
        retention=Retention(
            inputs_deleted=f"after feature extraction, at most {s.inputs_max_age_h} h",
            results_days=s.results_retention_days,
        ),
    )
