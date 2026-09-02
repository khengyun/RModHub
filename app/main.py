"""FastAPI application factory and ASGI entry point (`uvicorn app.main:app`)."""

from __future__ import annotations

import asyncio
import contextlib
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
from sqlalchemy.exc import InterfaceError, OperationalError

from app.api import capabilities, health, jobs, predict, samples, uploads_tus
from app.api.jobs import NoStoreMiddleware
from app.api.normalize import SequenceValidationError
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.jobs.cleanup import cleanup_loop
from app.jobs.queue import make_queue
from app.jobs.service import SignalContext
from app.jobs.storage import JobStorage
from app.predictors import create_sequence_predictors

log = logging.getLogger(__name__)

LANDING_TEMPLATE = (Path(__file__).parent / "landing.html").read_text(encoding="utf-8")
STATIC_DIR = Path(__file__).parent / "static"

# The landing page and the OpenAPI description state the retention periods of *this*
# deployment: `{{name}}` placeholders are filled from the settings (the same values
# `GET /api/capabilities` publishes), never from literals.
RETENTION_PLACEHOLDERS = ("inputs_max_age_h", "results_retention_days", "upload_ttl_h")

DESCRIPTION_TEMPLATE = """\
RNA modification site prediction web server. No login, no cookies; the server code is MIT.

**Sequence branch:** `POST /api/predict/sequence` scores a nucleotide sequence for
12 modification types with [MultiRM](https://github.com/Tsedao/MultiRM)
(Song et al. 2021, *Nature Communications*; MIT). MultiRM predicts the centre of a
51-nt window, so the first and last 25 nt of the input never receive a prediction.

**Nanopore signal branch:** direct-RNA reads (pod5 + BAM basecalled with
`dorado --emit-moves`, a reference FASTA and a regions CSV) are scored per read and per
site for ac4C, m1A, m5C, m6A, m7G and Ψ with
[DirectRM](https://github.com/yuxinPenny/DirectRM) (Zhang et al. 2025, *Nature
Communications*; MIT). Jobs run asynchronously:

* `POST /api/jobs/signal` — one-shot multipart submission (curl, scripts) → `202` job status.
* `POST /api/jobs/signal/init` → resumable tus 1.0.0 uploads on `/api/uploads/{id}`
  (HEAD / PATCH / DELETE) → `POST /api/jobs/{job_id}/start`.
* `POST /api/jobs/signal/sample` — run the built-in synthetic sample.
* `GET /api/jobs/{job_id}` — poll status, stage, progress; `POST …/cancel`.
* `GET /api/jobs/{job_id}/results?level=site|read` — paged rows; site rows use the same
  `ModSite` fields as the sequence branch (`source="signal"`); `…/download.csv` streams them.
* `GET /api/capabilities` — whether the branch is enabled on this deployment and its limits.

The pod5 and BAM are deleted right after feature extraction (at most {{inputs_max_age_h}} h);
the reference and regions file stay with the job; results are kept {{results_retention_days}}
days; unfinished uploads expire after {{upload_ttl_h}} h (see `GET /api/capabilities`).
"""

_LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}
_NO_STORE_PREFIXES = ("/api/jobs", "/api/uploads")
DB_CONNECT_ATTEMPTS = 10
DB_CONNECT_DELAY_S = 3.0
DB_DOWN_DETAIL = "The job database is not reachable; please try again later."


def render_retention(template: str, settings: Settings) -> str:
    """Fill the `{{...}}` retention placeholders of a landing / description template."""
    for name in RETENTION_PLACEHOLDERS:
        template = template.replace("{{" + name + "}}", str(getattr(settings, name)))
    return template


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


def _start_signal_branch(settings: Settings) -> SignalContext:
    """Engine + tables + storage layout + queue client. Called once from the lifespan."""
    url = settings.database_url.get_secret_value()  # type: ignore[union-attr]
    engine = make_engine(url)
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            init_db(engine)
            break
        except OperationalError as exc:
            if attempt == DB_CONNECT_ATTEMPTS:
                raise
            log.warning(
                "database not ready (attempt %d/%d): %s", attempt, DB_CONNECT_ATTEMPTS, exc
            )
            time.sleep(DB_CONNECT_DELAY_S)
    storage = JobStorage(settings.upload_dir)
    storage.ensure_layout()
    broker = settings.celery_broker_url
    queue = make_queue(broker.get_secret_value() if broker is not None else None)
    if settings.ip_hash_secret_is_default:
        log.warning(
            "RMODHUB_IP_HASH_SECRET is the development default; set a random value in production"
        )
    log.info(
        "signal branch enabled: db=%s upload_dir=%s queue=%s",
        engine.url.render_as_string(hide_password=True),
        settings.upload_dir,
        queue.name,
    )
    return SignalContext(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        storage=storage,
        queue=queue,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "starting %s %s with settings %s",
            settings.app_name,
            settings.version,
            settings.for_log(),
        )
        t0 = perf_counter()
        load_kwargs: dict = {}
        num_threads = settings.effective_torch_threads()
        if num_threads is not None:
            load_kwargs["num_threads"] = num_threads
        model_ids = settings.enabled_sequence_models()
        predictors = create_sequence_predictors(model_ids, **load_kwargs)
        if settings.warmup:
            for p in predictors.values():
                p.warmup()
        log.info(
            "sequence models %s loaded in %.2fs (warmup=%s)",
            ", ".join(f"{i}={p.name}/{p.version}" for i, p in predictors.items()),
            perf_counter() - t0,
            settings.warmup,
        )
        app.state.predictors = predictors
        # The default model. Kept as its own attribute so /health, the CSV writer and every
        # single-model caller stay unchanged.
        app.state.predictor = predictors[model_ids[0]]
        app.state.started_at = time.monotonic()

        cleanup_task: asyncio.Task | None = None
        if settings.signal_enabled:
            ctx = _start_signal_branch(settings)
            app.state.signal = ctx
            cleanup_task = asyncio.create_task(
                cleanup_loop(ctx.sessions, ctx.storage, settings), name="rmodhub-cleanup"
            )
        else:
            app.state.signal = None
            log.info("signal branch disabled (DATABASE_URL not set)")
        try:
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup_task
            ctx = app.state.signal
            app.state.signal = None
            if ctx is not None:
                ctx.engine.dispose()
            app.state.predictor = None
            app.state.predictors = {}

    landing_html = render_retention(LANDING_TEMPLATE, settings)
    app = FastAPI(
        title=settings.app_name,
        description=render_retention(DESCRIPTION_TEMPLATE, settings),
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
    app.state.predictors = {}
    app.state.started_at = None
    app.state.signal = None

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "HEAD", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Upload-Offset", "Upload-Length", "Tus-Resumable"],
            expose_headers=[
                "Content-Disposition",
                "Location",
                "Upload-Offset",
                "Upload-Length",
                "Tus-Resumable",
                "Tus-Version",
                "Tus-Extension",
                "Tus-Max-Size",
            ],
        )
    # Job and upload responses must never be cached (status changes, offsets move).
    app.add_middleware(NoStoreMiddleware, prefixes=_NO_STORE_PREFIXES)

    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(samples.router)
    app.include_router(predict.router)
    app.include_router(jobs.router)
    app.include_router(uploads_tus.router)

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def landing() -> HTMLResponse:
        return HTMLResponse(landing_html)

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

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def _database_down(request: Request, exc: Exception) -> JSONResponse:
        # Postgres restarting, its connection limit hit, the SQLite file unreadable: every
        # job / upload route goes through a session, so answer the contract's one-sentence
        # JSON (503, retryable) instead of Starlette's text/plain 500.
        log.error("job database unavailable on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=503, content={"detail": DB_DOWN_DETAIL}, headers={"Retry-After": "10"}
        )

    return app


app = create_app()
