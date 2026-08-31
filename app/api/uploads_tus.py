"""tus 1.0.0 core + termination on `/api/uploads/{upload_id}` (docs/signal-branch.md section 6).

Uploads are created by `POST /api/jobs/signal/init` (which declares the four file sizes),
so there is no tus *creation* extension here: HEAD reports the offset, PATCH appends a chunk
streamed straight from the request body, DELETE terminates. The declared length is a hard
cap and each PATCH body is capped at `RMODHUB_TUS_CHUNK_MB`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app.api.jobs import Ctx, Db, get_signal
from app.jobs.constants import SIGNAL_DISABLED_DETAIL, TUS_CONTENT_TYPE, TUS_VERSION, UPLOADING
from app.jobs.models import Upload
from app.jobs.service import SignalContext, cancel_job, fmt_size, list_uploads_for
from app.jobs.storage import FileTooLarge, write_stream

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

TUS_HEADERS = {"Tus-Resumable": TUS_VERSION}
NO_STORE = {"Cache-Control": "no-store"}
UPLOAD_NOT_FOUND = "No upload with this id exists (it may have expired or already been used)."

_DISABLED = {
    503: {
        "description": "Signal branch not enabled",
        "content": {"application/json": {"example": {"detail": SIGNAL_DISABLED_DETAIL}}},
    }
}


class UploadLocks:
    """One `asyncio.Lock` per upload id, kept for as long as any request holds or waits on it.

    Two PATCHes for the same upload must never interleave (offset check + append + database
    update form one unit). The entry is reference-counted: `Lock.release()` marks the lock
    free *before* the woken waiter runs, so "drop the entry when `locked()` is False" would
    hand the next request a fresh lock while a waiter is still queued on the old one, and
    the two would append to the same file at once. Everything here runs on the event loop
    with no await between the dictionary reads and writes, so the counts are exact. One
    uvicorn worker per container (Dockerfile), so in-process locks are enough.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def waiting_on(self, key: str) -> int:
        entry = self._entries.get(key)
        return entry[1] if entry else 0

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, refs = self._entries.get(key) or (asyncio.Lock(), 0)
        self._entries[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock, refs = self._entries[key]
            if refs <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock, refs - 1)


_locks = UploadLocks()


def _tus_error(status_code: int, detail: str, **headers: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={**TUS_HEADERS, **headers})


def _check_version(request: Request) -> None:
    version = request.headers.get("tus-resumable")
    if version is not None and version.strip() != TUS_VERSION:
        raise _tus_error(
            412,
            f"Unsupported Tus-Resumable version {version.strip()!r}; this server speaks {TUS_VERSION}.",
            **{"Tus-Version": TUS_VERSION},
        )


def _upload_or_404(session: Session, upload_id: str) -> Upload:
    upload = list_uploads_for(session, upload_id)
    if upload is None:
        raise _tus_error(404, UPLOAD_NOT_FOUND)
    return upload


def _offset_mismatch(current: int) -> HTTPException:
    return _tus_error(
        409,
        f"Upload offset mismatch: the server has {current} bytes; resume from that offset.",
        **{"Upload-Offset": str(current)},
    )


@router.options(
    "",
    summary="tus capabilities",
    status_code=204,
    responses={204: {"description": "Tus-Version / Tus-Extension / Tus-Max-Size headers"}, **_DISABLED},
)
def tus_options(ctx: Ctx) -> Response:
    settings = ctx.settings
    return Response(
        status_code=204,
        headers={
            **TUS_HEADERS,
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": "termination",
            "Tus-Max-Size": str(max(settings.max_pod5_bytes, settings.max_bam_bytes)),
        },
    )


@router.head(
    "/{upload_id}",
    summary="Current offset of an upload",
    responses={200: {"description": "Upload-Offset / Upload-Length headers"}, 404: {}, **_DISABLED},
)
def tus_head(upload_id: str, request: Request, session: Db) -> Response:
    _check_version(request)
    upload = _upload_or_404(session, upload_id)
    return Response(
        status_code=200,
        headers={
            **TUS_HEADERS,
            **NO_STORE,
            "Upload-Offset": str(upload.offset),
            "Upload-Length": str(upload.length),
        },
    )


@router.patch(
    "/{upload_id}",
    status_code=204,
    summary="Append a chunk",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/offset+octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
    responses={
        204: {"description": "Chunk stored; Upload-Offset is the new offset"},
        404: {},
        409: {"description": "Upload-Offset does not match the server offset"},
        413: {"description": "Chunk larger than the configured maximum or past the declared length"},
        415: {"description": "Content-Type is not application/offset+octet-stream"},
        **_DISABLED,
    },
)
async def tus_patch(
    upload_id: str, request: Request, ctx: Annotated[SignalContext, Depends(get_signal)]
) -> Response:
    _check_version(request)
    settings = ctx.settings
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != TUS_CONTENT_TYPE:
        raise _tus_error(415, f"Content-Type must be {TUS_CONTENT_TYPE}.")
    raw_offset = request.headers.get("upload-offset")
    if raw_offset is None or not raw_offset.strip().isdigit():
        raise _tus_error(422, "The Upload-Offset header (a non-negative integer) is required.")
    offset = int(raw_offset)
    chunk_cap = settings.tus_chunk_bytes
    content_length: int | None = None
    raw_length = request.headers.get("content-length")
    if raw_length is not None and raw_length.isdigit():
        content_length = int(raw_length)
        if content_length > chunk_cap:
            raise _tus_error(
                413,
                f"The chunk is {fmt_size(content_length)}; send chunks of at most "
                f"{settings.tus_chunk_mb} MB.",
            )

    def _load() -> tuple[str, int, int]:
        with ctx.sessions() as session:
            upload = _upload_or_404(session, upload_id)
            if upload.job.status != UPLOADING:
                raise _tus_error(
                    409, f"This job is {upload.job.status} and no longer accepts uploads."
                )
            return upload.id, upload.offset, upload.length

    def _resync(up_id: str, expected: int, on_disk: int) -> int:
        """The file is behind the row (a write was lost): the row follows the file."""
        with ctx.sessions() as session:
            session.execute(
                update(Upload)
                .where(Upload.id == up_id, Upload.offset == expected)
                .values(offset=on_disk, complete=False)
            )
            session.commit()
            meta = ctx.storage.read_tus_meta(up_id) or {}
            meta["offset"] = on_disk
            ctx.storage.write_tus_meta(up_id, meta)
            return on_disk

    def _committed_offset(up_id: str) -> int | None:
        with ctx.sessions() as session:
            return session.execute(
                select(Upload.offset).where(Upload.id == up_id)
            ).scalar_one_or_none()

    async with _locks.hold(upload_id.lower()):
        up_id, current, length = await run_in_threadpool(_load)
        on_disk = ctx.storage.tus_size(up_id)
        if on_disk < current:
            # Never pad the gap (F4): what is on disk is the truth, the client resends.
            log.warning(
                "upload %s: file has %d bytes but the row says %d; offset reset",
                up_id,
                on_disk,
                current,
            )
            current = await run_in_threadpool(_resync, up_id, current, on_disk)
        if offset != current:
            raise _offset_mismatch(current)
        if content_length is not None and offset + content_length > length:
            raise _tus_error(
                413, f"The chunk would exceed the declared upload length of {length} bytes."
            )
        limit = min(chunk_cap, length - offset)
        path = ctx.storage.tus_path(up_id)
        ctx.storage.truncate_tus(up_id, offset)  # drop bytes of a PATCH that never committed
        try:
            n = await write_stream(request.stream(), path, limit, append=True)
        except FileTooLarge as exc:
            ctx.storage.truncate_tus(up_id, offset)
            if exc.received > chunk_cap:
                raise _tus_error(
                    413,
                    f"The chunk exceeds {settings.tus_chunk_mb} MB; send smaller chunks.",
                ) from None
            raise _tus_error(
                413, f"The chunk would exceed the declared upload length of {length} bytes."
            ) from None
        except ClientDisconnect:
            # Keep what arrived: the client resumes from HEAD's offset, which stays `current`.
            ctx.storage.truncate_tus(up_id, offset)
            raise
        new_offset = offset + n
        complete = new_offset >= length

        def _commit() -> int:
            with ctx.sessions() as session:
                result = session.execute(
                    update(Upload)
                    .where(Upload.id == up_id, Upload.offset == offset)
                    .values(offset=new_offset, complete=complete)
                )
                session.commit()
                return result.rowcount

        if await run_in_threadpool(_commit) != 1:
            # Cannot happen under the per-upload lock; kept as a guard against a second API
            # process. Reconcile the file with what *is* committed, never with our stale view.
            committed = await run_in_threadpool(_committed_offset, up_id)
            if committed is None:
                raise _tus_error(404, UPLOAD_NOT_FOUND)
            ctx.storage.truncate_tus(up_id, committed)
            raise _tus_error(
                409,
                "The upload changed concurrently; resume from the offset returned by HEAD.",
                **{"Upload-Offset": str(committed)},
            )
        meta = ctx.storage.read_tus_meta(up_id) or {}
        meta["offset"] = new_offset
        ctx.storage.write_tus_meta(up_id, meta)
    return Response(
        status_code=204,
        headers={**TUS_HEADERS, **NO_STORE, "Upload-Offset": str(new_offset)},
    )


@router.delete(
    "/{upload_id}",
    status_code=204,
    summary="Terminate an upload (cancels the job)",
    responses={204: {"description": "Upload and its job removed"}, 404: {}, 409: {}, **_DISABLED},
)
def tus_delete(upload_id: str, request: Request, ctx: Ctx, session: Db) -> Response:
    """A job needs all four files, so terminating one upload cancels the whole job and
    removes every file received so far.
    """
    _check_version(request)
    upload = _upload_or_404(session, upload_id)
    job = upload.job
    if job.status != UPLOADING:
        raise _tus_error(
            409, f"The job is {job.status}; its uploads are in use - cancel the job instead."
        )
    cancel_job(ctx, session, job.id)
    return Response(status_code=204, headers={**TUS_HEADERS, **NO_STORE})
