"""Runtime configuration. Every field can be set as an environment variable with the
`RMODHUB_` prefix (e.g. `RMODHUB_PREDICTOR=stub`), or in a `.env` file in the working directory."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app import __version__
from app.predictors.base import MIN_SEQUENCE_NT

LogLevel = Literal["critical", "error", "warning", "info", "debug"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RMODHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RModHub API"
    version: str = __version__

    predictor: Literal["multirm", "stub"] = Field(
        default="multirm",
        description="'multirm' loads the real model; 'stub' is a torch-free fake for development.",
    )
    min_sequence_nt: int = Field(
        default=MIN_SEQUENCE_NT,
        ge=MIN_SEQUENCE_NT,
        description="Shortest accepted input. Cannot go below the model's 51-nt window.",
    )
    max_sequence_nt: int = Field(
        default=10_000,
        ge=MIN_SEQUENCE_NT,
        description="Longest accepted input; bounds per-request CPU time and memory.",
    )
    default_alpha: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        description="p-value threshold used when the request omits alpha.",
    )
    warmup: bool = Field(default=True, description="Run one dummy inference at startup.")
    torch_threads: int | None = Field(
        default=None,
        ge=1,
        description=(
            "torch intra-op threads for inference. Unset: honour OMP_NUM_THREADS if present, "
            "otherwise min(4, cpu_count) — torch's own default (all cores) oversubscribes a "
            "shared box and makes single requests slower, not faster."
        ),
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Allowed CORS origins. JSON list or comma-separated. Empty = CORS middleware disabled.",
    )
    log_level: LogLevel = "info"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_log_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    def effective_torch_threads(self) -> int | None:
        """Thread count to pass to the model loader; None means "leave torch alone"."""
        if self.torch_threads is not None:
            return self.torch_threads
        if os.environ.get("OMP_NUM_THREADS"):
            return None  # torch already reads it; e.g. the Dockerfile sets 1
        return min(4, os.cpu_count() or 1)

    @model_validator(mode="after")
    def _check_length_bounds(self) -> Settings:
        if self.max_sequence_nt < self.min_sequence_nt:
            raise ValueError(
                f"max_sequence_nt ({self.max_sequence_nt}) must be >= "
                f"min_sequence_nt ({self.min_sequence_nt})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()
