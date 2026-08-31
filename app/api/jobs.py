"""Signal branch job API: `/api/jobs/*` (docs/signal-branch.md section 6).

Uploads never touch memory: the multipart handler feeds `request.stream()` through
python-multipart's incremental parser straight into files under `jobs/<id>/input/`, with a
hard byte counter per part; the tus flow lives in `app.api.uploads_tus`.

When the branch is disabled (no DATABASE_URL) every route here still appears in /docs but
answers 503 through the `get_signal` dependency.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import session_scope
from app.jobs.constants import (
    DEFAULT_KIT,
    DONE,
    EXPIRED,
    INPUT_KIND_UPLOAD,
    KITS,
    SIGNAL_DISABLED_DETAIL,
    SLOTS,
    UPLOADING,
)
from app.jobs.models import Job
from app.jobs.quota import client_key, enforce_quota, quota_guard
from app.jobs.results import (
    MAX_LIMIT,
    STRANDS,
    ResultFilters,
    check_sort,
    count_rows,
    csv_filename,
    csv_stream,
    fetch_page,
    open_results,
    results_meta,
)
from app.jobs.schemas import JobInit, JobStatus, ResultLevel, ResultsPage, SortKey, SortOrder
from app.jobs.service import (
    KIT_FIELD_MAX_BYTES,
    SignalContext,
    cancel_job,
    cap_bytes,
    create_sample_job,
    discard_job,
    finalize_multipart_job,
    fmt_size,
    init_job,
    new_job,
    require_job,
    stamp_expiry,
    start_job,
    to_status,
    too_large_error,
    total_cap_bytes,
)
from app.jobs.storage import CappedWriter, FileTooLarge, JobStorage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_DISABLED = {
    503: {
        "description": "Signal branch not enabled",
        "content": {"application/json": {"example": {"detail": SIGNAL_DISABLED_DETAIL}}},
    }
}
_QUOTA = {
    429: {
        "description": "Per-address quota reached (Retry-After header set)",
        "content": {
            "application/json": {"example": {"detail": "You already have 1 job running; ..."}}
        },
    }
}
_NOT_FOUND = {404: {"description": "Unknown job id"}}
_CONFLICT = {409: {"description": "The job is not in a state that allows this"}}
_INVALID = {422: {"description": "Invalid input or over a cap"}}


# ---------------------------------------------------------------------------- dependencies


def get_signal(request: Request) -> SignalContext:
    ctx = getattr(request.app.state, "signal", None)
    if ctx is None:
        raise HTTPException(status_code=503, detail=SIGNAL_DISABLED_DETAIL)
    return ctx


def get_session(ctx: Annotated[SignalContext, Depends(get_signal)]) -> Iterator[Session]:
    yield from session_scope(ctx.sessions)


def get_client_key(request: Request, ctx: Annotated[SignalContext, Depends(get_signal)]) -> str:
    return client_key(request, ctx.settings.ip_hash_secret.get_secret_value())


Ctx = Annotated[SignalContext, Depends(get_signal)]
Db = Annotated[Session, Depends(get_session)]
ClientKey = Annotated[str, Depends(get_client_key)]


class NoStoreMiddleware:
    """Adds `Cache-Control: no-store` to every response under the given path prefixes."""

    def __init__(self, app: ASGIApp, prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefixes):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_no_store)


# ------------------------------------------------------------------ multipart streaming


class MultipartReceiver:
    """python-multipart callbacks that stream file parts to `input/` with per-part caps."""

    def __init__(self, storage: JobStorage, job_id: str, settings) -> None:
        self.storage = storage
        self.job_id = job_id
        self.settings = settings
        self.received: dict[str, int] = {}
        self.fields: dict[str, str] = {}
        self._writer: CappedWriter | None = None
        self._slot: str | None = None
        self._field: str | None = None
        self._field_buf = bytearray()
        self._hname = bytearray()
        self._hvalue = bytearray()
        self._headers: dict[bytes, bytes] = {}

    # -- header assembly -------------------------------------------------------------
    def on_part_begin(self) -> None:
        self._headers = {}
        self._hname.clear()
        self._hvalue.clear()
        self._slot = None
        self._field = None
        self._field_buf.clear()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hvalue += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._hname).lower()] = bytes(self._hvalue)
        self._hname.clear()
        self._hvalue.clear()

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        filename = params.get(b"filename")
        if name in SLOTS and filename is not None:
            if name in self.received:
                raise HTTPException(status_code=422, detail=f"The part {name!r} was sent twice.")
            self._slot = name
            self._writer = CappedWriter(
                self.storage.input_path(self.job_id, name), cap_bytes(name, self.settings)
            )
        elif filename is None and name:
            self._field = name
        # anything else (unknown file part) is read and discarded

    # -- body ----------------------------------------------------------------------------
    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._writer is not None:
            try:
                self._writer.write(data[start:end])
            except FileTooLarge:
                raise too_large_error(self._slot or "pod5", None, self.settings) from None
        elif self._field is not None:
            self._field_buf += data[start:end]
            if len(self._field_buf) > KIT_FIELD_MAX_BYTES:
                raise HTTPException(
                    status_code=422, detail=f"The form field {self._field!r} is too long."
                )

    def on_part_end(self) -> None:
        if self._writer is not None:
            self.received[self._slot] = self._writer.commit()
            self._writer = None
            self._slot = None
        elif self._field is not None:
            self.fields[self._field] = bytes(self._field_buf).decode("utf-8", "replace").strip()
            self._field = None
            self._field_buf.clear()

    def on_end(self) -> None:
        pass

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def abort(self) -> None:
        if self._writer is not None:
            self._writer.abort()
            self._writer = None


_MULTIPART_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["pod5", "bam", "reference", "regions"],
                    "properties": {
                        "pod5": {
                            "type": "string",
                            "format": "binary",
                            "description": "Raw signal (single .pod5 file).",
                        },
                        "bam": {
                            "type": "string",
                            "format": "binary",
                            "description": "Alignments basecalled with `dorado --emit-moves`, sorted.",
                        },
                        "reference": {
                            "type": "string",
                            "format": "binary",
                            "description": "Transcript FASTA the BAM is aligned to.",
                        },
                        "regions": {
                            "type": "string",
                            "format": "binary",
                            "description": "CSV: seqnames,start,end,width,strand (1-based inclusive).",
                        },
                        "kit": {"type": "string", "enum": list(KITS), "default": DEFAULT_KIT},
                    },
                }
            }
        },
    }
}


@router.post(
    "/signal",
    status_code=202,
    response_model=JobStatus,
    summary="Submit a nanopore signal job (multipart upload)",
    openapi_extra=_MULTIPART_SCHEMA,
    responses={**_INVALID, **_QUOTA, **_DISABLED},
)
async def create_signal_job(request: Request, ctx: Ctx, key: ClientKey) -> JobStatus:
    """One-shot submission for scripts and curl: the four files are streamed to disk and
    the job is queued. Browsers use the resumable flow (`/init` + tus) instead.
    """
    settings = ctx.settings
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise HTTPException(
            status_code=422,
            detail=(
                "Expected a multipart/form-data body with the file parts pod5, bam, reference "
                "and regions."
            ),
        )
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit():
        declared = int(content_length)
        if declared > total_cap_bytes(settings):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"The request body is {fmt_size(declared)}; this server accepts at most "
                    f"{fmt_size(total_cap_bytes(settings))} in one request "
                    f"(pod5 {fmt_size(settings.max_pod5_bytes)}, BAM {fmt_size(settings.max_bam_bytes)}, "
                    f"reference {fmt_size(settings.max_reference_bytes)})."
                ),
            )

    def _prepare() -> Job:
        with ctx.sessions() as session, quota_guard(session, key):
            enforce_quota(session, key, settings)
            job = new_job(
                session,
                kit=DEFAULT_KIT,
                input_kind=INPUT_KIND_UPLOAD,
                client_key=key,
                status=UPLOADING,
                stage="uploading",
            )
            session.commit()
            return job

    job = await run_in_threadpool(_prepare)
    ctx.storage.create_job_dir(job.id)
    receiver = MultipartReceiver(ctx.storage, job.id, settings)

    def _discard() -> None:
        with ctx.sessions() as session:
            row = session.get(Job, job.id)
            if row is not None:
                discard_job(ctx, session, row)
            else:
                ctx.storage.remove_job_dir(job.id)

    def _finalize() -> JobStatus:
        with ctx.sessions() as session:
            row = session.get(Job, job.id)
            kit = receiver.fields.get("kit") or DEFAULT_KIT
            return finalize_multipart_job(ctx, session, row, kit, receiver.received)

    try:
        parser = MultipartParser(boundary, receiver.callbacks())
        async for chunk in request.stream():
            if chunk:
                parser.write(chunk)
        parser.finalize()
        return await run_in_threadpool(_finalize)
    except HTTPException as exc:
        receiver.abort()
        if exc.status_code == 422:
            await run_in_threadpool(_discard)
        raise
    except ClientDisconnect:
        receiver.abort()
        await run_in_threadpool(_discard)
        raise
    except Exception as exc:
        receiver.abort()
        await run_in_threadpool(_discard)
        if exc.__class__.__module__.startswith("python_multipart"):
            raise HTTPException(status_code=422, detail="The multipart body is malformed.") from exc
        raise


@router.post(
    "/signal/init",
    status_code=201,
    response_model=JobStatus,
    summary="Declare a resumable upload job",
    responses={**_INVALID, **_QUOTA, **_DISABLED},
)
def init_signal_job(body: JobInit, ctx: Ctx, session: Db, key: ClientKey) -> JobStatus:
    """Creates the job in `uploading` state and one tus upload per file. Sizes are checked
    against the server caps here, before any byte is sent; PATCH the bytes to the returned
    `uploads[slot].url`, then `POST /api/jobs/{job_id}/start`.
    """
    return init_job(ctx, session, key, body)


@router.post(
    "/signal/sample",
    status_code=202,
    response_model=JobStatus,
    summary="Run the built-in synthetic sample",
    responses={**_NOT_FOUND, **_QUOTA, **_DISABLED},
)
def create_sample_signal_job(ctx: Ctx, session: Db, key: ClientKey) -> JobStatus:
    """Copies the synthetic RNA004 sample (`GET /api/samples/signal`) into a new job and
    queues it. Quotas apply as for any other job.
    """
    return create_sample_job(ctx, session, key)


@router.post(
    "/{job_id}/start",
    status_code=202,
    response_model=JobStatus,
    summary="Queue a job once all uploads are complete",
    responses={**_NOT_FOUND, **_CONFLICT, **_INVALID, **_DISABLED},
)
def start_signal_job(job_id: str, ctx: Ctx, session: Db) -> JobStatus:
    return start_job(ctx, session, job_id)


@router.get(
    "/{job_id}",
    response_model=JobStatus,
    summary="Job status",
    responses={**_NOT_FOUND, **_DISABLED},
)
def get_job_status(job_id: str, ctx: Ctx, session: Db) -> JobStatus:
    """Poll this while the job is `queued` or `running`. `uploads` is present only while
    the job is `uploading`.
    """
    job = require_job(session, job_id)
    if stamp_expiry(job, ctx.settings):
        session.commit()
    return to_status(job)


def _require_results(job: Job) -> None:
    if job.status == EXPIRED or job.results_deleted_at is not None:
        raise HTTPException(
            status_code=404, detail="The results of this job have expired and were deleted."
        )
    if job.status != DONE:
        raise HTTPException(
            status_code=409,
            detail=f"The job is {job.status}; results are available once it is done.",
        )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


STRAND_DETAIL = "strand must be '+' or '-' (a literal '+' has to be sent as %2B in a query string)."


def _parse_strand(value: str | None) -> str | None:
    """'+' / '-' or None (no filter); anything else is a 422.

    A bare `?strand=+` reaches the server as a space (form decoding), which must not turn
    into "no filter" silently: only the empty string means "no filter" here.
    """
    if value is None or value == "":
        return None
    if value in STRANDS:
        return value
    raise HTTPException(status_code=422, detail=STRAND_DETAIL)


@router.get(
    "/{job_id}/results",
    response_model=ResultsPage,
    summary="Paged results (site or read level)",
    responses={**_NOT_FOUND, **_CONFLICT, **_DISABLED},
)
def get_job_results(
    job_id: str,
    ctx: Ctx,
    session: Db,
    level: Annotated[
        ResultLevel, Query(description="'site' rows are ModSite-compatible.")
    ] = "site",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    transcript_id: Annotated[str | None, Query()] = None,
    mod_type: Annotated[str | None, Query()] = None,
    position: Annotated[int | None, Query(ge=1)] = None,
    strand: Annotated[
        str | None,
        Query(
            description="Only rows on this strand: '+' or '-' (send '+' as %2B).",
            json_schema_extra={"enum": ["+", "-"]},
        ),
    ] = None,
    min_coverage: Annotated[int | None, Query(ge=0, description="Site level only.")] = None,
    sort: Annotated[SortKey, Query()] = "position",
    order: Annotated[SortOrder, Query()] = "asc",
) -> ResultsPage:
    """Site rows carry the shared `ModSite` fields first (`probability` = modification
    rate, `p_value` = null, `source` = "signal") plus strand, count, the Wilson 95 %
    interval and the per-read maxima. `level=read` with `transcript_id` + `position`
    (+ `mod_type`, + `strand` when the regions file lists both strands of that contig)
    drills down to the per-read probabilities of one site.

    `sort=position` (the default) is the canonical order of the CSV download: rows of one
    transcript are contiguous, then position, modification type; `order=desc` reverses it.
    Read-level rows can be sorted by `rate` / `mod_type` only within one site
    (`transcript_id` + `position`), and have no `coverage` (422 otherwise).
    """
    job = require_job(session, job_id)
    _require_results(job)
    filters = ResultFilters(
        transcript_id=_blank_to_none(transcript_id),
        mod_type=_blank_to_none(mod_type),
        position=position,
        strand=_parse_strand(strand),
        min_coverage=min_coverage,
    )
    problem = check_sort(level, sort, filters)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)
    try:
        conn = open_results(ctx.storage.results_path(job.id))
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail="The results file of this job is missing."
        ) from None
    with closing(conn):
        total = count_rows(conn, level, filters)
        rows = fetch_page(conn, level, filters, sort=sort, order=order, offset=offset, limit=limit)
        meta = results_meta(job, conn)
    return ResultsPage(results=rows, meta=meta, total=total, offset=offset, limit=limit)


@router.get(
    "/{job_id}/download.csv",
    summary="Download all results as CSV",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {}}, "description": "Streamed CSV attachment"},
        **_NOT_FOUND,
        **_CONFLICT,
        **_DISABLED,
    },
)
def download_job_csv(
    job_id: str,
    ctx: Ctx,
    session: Db,
    level: Annotated[ResultLevel, Query()] = "site",
) -> StreamingResponse:
    """Site CSV header: `transcript_id,position,mod_type,probability,p_value,coverage,source`
    (the shared columns) then `strand,count,ci_low,ci_high,max_prob,noisyor_prob`.
    Read CSV header: `read_id,transcript_id,position,strand,mod_type,probability,source`.
    Rows come in the canonical (transcript_id, position, mod_type) order. A transcript or
    read id that starts with `=`, `+`, `-` or `@` is written with a leading `'` so a
    spreadsheet shows it instead of evaluating it as a formula.
    """
    job = require_job(session, job_id)
    _require_results(job)
    path = ctx.storage.results_path(job.id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The results file of this job is missing.")
    return StreamingResponse(
        csv_stream(path, level),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{csv_filename(job.id, level)}"'},
    )


@router.post(
    "/{job_id}/cancel",
    response_model=JobStatus,
    summary="Cancel a job",
    responses={**_NOT_FOUND, **_CONFLICT, **_DISABLED},
)
def cancel_signal_job(job_id: str, ctx: Ctx, session: Db) -> JobStatus:
    """`uploading`/`queued` jobs are removed immediately; a `running` job is sent SIGTERM
    through the worker queue and its files are deleted by the worker.
    """
    return cancel_job(ctx, session, job_id)
