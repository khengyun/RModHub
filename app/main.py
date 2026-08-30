"""FastAPI application factory and ASGI entry point (`uvicorn app.main:app`)."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import health, predict, samples
from app.api.normalize import SequenceValidationError
from app.config import Settings, get_settings
from app.predictors import create_sequence_predictor

log = logging.getLogger(__name__)

LANDING_HTML = (Path(__file__).parent / "landing.html").read_text(encoding="utf-8")
STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """\
RNA modification site prediction web server.

**Sequence branch (live):** `POST /api/predict/sequence` scores a nucleotide sequence for
12 modification types with [MultiRM](https://github.com/Tsedao/MultiRM)
(Song et al. 2021, *Nature Communications*; MIT license). MultiRM predicts the centre of a
51-nt window, so the first and last 25 nt of the input never receive a prediction.

**Signal branch (planned):** nanopore signal input via DirectRM, returning the same
`ModSite` rows with `source="signal"`.

Server code is MIT licensed. No login is required and no cookies are set.
"""

_LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}


def _configure_logging(level: str) -> None:
    # basicConfig is a no-op if the root logger already has handlers (e.g. under uvicorn's
    # own config that is not the case for the root logger, so our lines do get emitted).
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(level.upper())


def _one_line(errors: list[dict]) -> str:
    """Turn pydantic's error list into a single human-readable line, e.g.
    'alpha: Input should be less than or equal to 1'."""
    if not errors:
        return "invalid request"
    err = errors[0]
    loc = list(err.get("loc") or ())
    if len(loc) > 1 and loc[0] in _LOCATION_PREFIXES:
        loc = loc[1:]
    names = [part for part in loc if isinstance(part, str)]
    field = ".".join(names) if names else (str(loc[0]) if loc else "request")
    return f"{field}: {err.get('msg', 'invalid value')}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "starting %s %s with settings %s",
            settings.app_name,
            settings.version,
            settings.model_dump(),
        )
        t0 = perf_counter()
        load_kwargs: dict = {}
        num_threads = settings.effective_torch_threads()
        if num_threads is not None:
            load_kwargs["num_threads"] = num_threads
        predictor = create_sequence_predictor(settings.predictor, **load_kwargs)
        if settings.warmup:
            predictor.warmup()
        log.info(
            "model %s/%s loaded in %.2fs (warmup=%s)",
            predictor.name,
            predictor.version,
            perf_counter() - t0,
            settings.warmup,
        )
        app.state.predictor = predictor
        app.state.started_at = time.monotonic()
        yield
        app.state.predictor = None

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=settings.version,
        lifespan=lifespan,
        # Swagger UI is served from self-hosted assets below (see /docs). FastAPI's default
        # loads it from cdn.jsdelivr.net, which is a third-party asset the NAR Web Server
        # Issue guidelines rule out. ReDoc is disabled for the same reason.
        docs_url=None,
        redoc_url=None,
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    )
    app.state.settings = settings
    app.state.predictor = None
    app.state.started_at = None

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["Content-Disposition"],
        )

    app.include_router(health.router)
    app.include_router(samples.router)
    app.include_router(predict.router)

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def landing() -> HTMLResponse:
        return HTMLResponse(LANDING_HTML)

    # Self-hosted Swagger UI (app/static/swagger, Apache-2.0): no CDN, no third-party
    # cookies, works offline / behind an institutional firewall.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    def swagger_docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{settings.app_name} - docs",
            swagger_js_url="/static/swagger/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger/swagger-ui.css",
            swagger_favicon_url="/static/favicon.svg",
            oauth2_redirect_url=None,
        )

    # --- error mapping -------------------------------------------------------------
    @app.exception_handler(SequenceValidationError)
    async def _sequence_error(request: Request, exc: SequenceValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _request_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Drop the echoed input (could be a 10 kb sequence) and the docs URL; keep loc/msg/type/ctx.
        errors = jsonable_encoder(
            [{k: v for k, v in e.items() if k not in ("input", "url")} for e in exc.errors()]
        )
        return JSONResponse(
            status_code=422, content={"detail": _one_line(errors), "errors": errors}
        )

    return app


app = create_app()
