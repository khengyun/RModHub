"""Job state machine (docs/signal-branch.md sections 3, 6, 8).

    init  ──► uploading ──start──► queued ──worker──► running ──► done | failed
    multipart / sample ──────────► queued                      └─ cancel ─► cancelled
    cleanup: uploading/done ──► expired

Every rejection is an `HTTPException` whose `detail` is one plain sentence. Caps are checked
on declared sizes (init) and again on the bytes actually received (multipart, start).
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import GiB, MiB, Settings
from app.db import utcnow
from app.jobs.constants import (
    CANCELLED,
    DEFAULT_PARAMS,
    DONE,
    FAILED,
    INPUT_FILENAMES,
    INPUT_KIND_SAMPLE,
    INPUT_KIND_UPLOAD,
    KITS,
    QUEUED,
    RUNNING,
    SAMPLE_FILENAMES,
    SLOTS,
    STAGES,
    TERMINAL_STATUSES,
    UPLOADING,
)
from app.jobs.models import Job, Upload
from app.jobs.queue import JobQueue, QueueUnavailable
from app.jobs.quota import enforce_quota, quota_guard
from app.jobs.schemas import JobInit, JobStatus, ModelInfo, UploadInfo
from app.jobs.storage import JobStorage, is_uuid, new_id

log = logging.getLogger(__name__)

# regions.csv is capped by data rows, not bytes; this is only a sanity bound on the stream
# (10,000 rows are ~400 KB).
REGIONS_MAX_BYTES = 64 * MiB
KIT_FIELD_MAX_BYTES = 64

POD5_SIGNATURE = b"\x8bPOD\r\n\x1a\n"
BGZF_MAGIC = b"\x1f\x8b"

SLOT_LABELS = {
    "pod5": "pod5 file",
    "bam": "BAM file",
    "reference": "reference FASTA",
    "regions": "regions file",
}

MISSING_SLOT_DETAIL = {
    "pod5": "A pod5 file with the raw signal is required.",
    "bam": (
        "A BAM file basecalled with dorado --emit-moves is required; a pod5 alone is not "
        "enough - see Help."
    ),
    "reference": "A reference FASTA (the transcript sequences the BAM is aligned to) is required.",
    "regions": "A regions CSV (seqnames,start,end,width,strand) is required.",
}

NOT_FOUND_DETAIL = "No job with this id exists on this server."
QUEUE_DOWN_DETAIL = "The job queue is not reachable; please try again later."


@dataclass
class SignalContext:
    """Everything the job routes need; lives on `app.state.signal` when the branch is on."""

    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    storage: JobStorage
    queue: JobQueue


# --------------------------------------------------------------------------- formatting


def fmt_size(n: float) -> str:
    """Human size with binary units, labelled the way users read them (GB/MB/KB)."""
    for unit, size in (("GB", GiB), ("MB", MiB), ("KB", 1024)):
        if n >= size:
            value = n / size
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{int(n)} bytes"


def cap_bytes(slot: str, settings: Settings) -> int:
    if slot == "pod5":
        return settings.max_pod5_bytes
    if slot == "bam":
        return settings.max_bam_bytes
    if slot == "reference":
        return settings.max_reference_bytes
    return REGIONS_MAX_BYTES


def total_cap_bytes(settings: Settings) -> int:
    """Largest multipart body that could possibly be valid (files + framing overhead)."""
    return sum(cap_bytes(slot, settings) for slot in SLOTS) + 16 * MiB


def missing_slot_error(slot: str) -> HTTPException:
    return HTTPException(status_code=422, detail=MISSING_SLOT_DETAIL[slot])


def too_large_error(slot: str, size: int | None, settings: Settings) -> HTTPException:
    limit = cap_bytes(slot, settings)
    label = SLOT_LABELS[slot]
    if slot == "regions":
        return HTTPException(
            status_code=422,
            detail=(
                f"The regions file is larger than {fmt_size(limit)}; a regions.csv with at "
                f"most {settings.max_regions} rows is expected."
            ),
        )
    size_text = f"is {fmt_size(size)}" if size is not None else f"exceeds {fmt_size(limit)}"
    return HTTPException(
        status_code=422,
        detail=f"The {label} {size_text}; this server accepts at most {fmt_size(limit)}.",
    )


def check_size(slot: str, size: int, settings: Settings) -> None:
    if size <= 0:
        raise HTTPException(status_code=422, detail=f"The {SLOT_LABELS[slot]} is empty.")
    if size > cap_bytes(slot, settings):
        raise too_large_error(slot, size, settings)


def check_kit(kit: str) -> str:
    if kit not in KITS:
        raise HTTPException(
            status_code=422, detail=f"kit must be one of {', '.join(KITS)} (got {kit[:16]!r})."
        )
    return kit


# --------------------------------------------------------------------- input validation


def validate_pod5_file(path) -> None:
    with open(path, "rb") as fh:
        head = fh.read(len(POD5_SIGNATURE))
    if head != POD5_SIGNATURE:
        raise HTTPException(
            status_code=422,
            detail="The pod5 file does not look like a POD5 file (bad file signature).",
        )


def validate_bam_file(path) -> None:
    with open(path, "rb") as fh:
        head = fh.read(2)
    if head != BGZF_MAGIC:
        raise HTTPException(
            status_code=422,
            detail=(
                "The BAM file does not look like a BAM file (expected BGZF-compressed data); "
                "a SAM text file is not accepted."
            ),
        )


def validate_reference_file(path) -> None:
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b">"):
        raise HTTPException(
            status_code=422,
            detail="The reference file does not look like FASTA (the first line must start with '>').",
        )


REGIONS_COLUMNS = ("seqnames", "start", "end", "width", "strand")

# Contig names pandas would not keep as strings (worker/rmodhub_worker/prepare.py
# `find_unsafe_contig_names`, section 11 of docs/signal-branch.md). Every DirectRM stage
# reads regions.csv - and later the per-k-mer feature CSV, whose `seqnames` column is an
# arbitrary subset of these names - with `pandas.read_csv` and default dtype inference, so
# a name that parses as a number ("1", "-3", "1e5", ".5", "inf"), a boolean ("True") or one
# of pandas' NA strings ("NA", "nan", "null", "None", "<NA>", "#NA", ...) stops being a
# string and the job dies in `preparing`. The worker asks its own pandas; the API must not
# import pandas or worker code, so this is a hand-written *superset* of that rule: it
# rejects every spelling pandas' C parser converts and a few case variants it would keep
# ("Null", "NAN", "1e"), never a real contig name ("chr1", "tx_A", "ENST0001", "NA12878").
_NUMBER_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d*)?")
_INFINITY_RE = re.compile(r"[+-]?(inf|infinity)", re.IGNORECASE)
_BOOL_RE = re.compile(r"(true|false)", re.IGNORECASE)
_PANDAS_NA_STRINGS = (
    "-1.#IND", "1.#QNAN", "1.#IND", "-1.#QNAN", "#N/A N/A", "#N/A", "N/A", "n/a", "NA",
    "<NA>", "#NA", "NULL", "null", "NaN", "-NaN", "nan", "-nan", "None",
)  # pandas.io.parsers STR_NA_VALUES (minus the empty string, reported separately)
_NA_LOWER = frozenset(v.lower() for v in _PANDAS_NA_STRINGS)
UNSAFE_CONTIG_DETAIL = (
    "Region {index}: the contig name '{name}' is read as {kind} rather than a name by DirectRM's "
    "CSV reader; rename the contig in the reference FASTA, the BAM and the regions file "
    "(for example by prefixing it with 'chr')."
)


def unsafe_contig_kind(name: str) -> str | None:
    """What DirectRM's CSV reader turns `name` into instead of a string, or None if it is safe.

    Same three verdicts (and wording) as the worker: "a missing value", "a boolean",
    "a number".
    """
    if name.lower() in _NA_LOWER:
        return "a missing value"
    if _BOOL_RE.fullmatch(name):
        return "a boolean"
    if _NUMBER_RE.fullmatch(name) or _INFINITY_RE.fullmatch(name):
        return "a number"
    return None


def validate_regions_file(path, max_rows: int) -> int:
    """Check the header and every row of regions.csv; return the number of data rows."""
    try:
        return _validate_regions_rows(path, max_rows)
    except csv.Error as exc:
        # `csv.reader` itself raises (not a ValueError): a field over the 128 KB field
        # limit or a quote left open across the rest of the file.
        raise HTTPException(
            status_code=422, detail=f"The regions file could not be parsed as CSV ({exc})."
        ) from None


def _validate_regions_rows(path, max_rows: int) -> int:
    columns = ",".join(REGIONS_COLUMNS)
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise HTTPException(
                status_code=422,
                detail=f"The regions file is empty; expected a CSV with the columns {columns}.",
            )
        names = [h.strip().lstrip("\ufeff") for h in header]
        if not set(REGIONS_COLUMNS).issubset(names):
            got = ",".join(names[:8])
            raise HTTPException(
                status_code=422,
                detail=f"The regions file must be a CSV with the columns {columns} (got: {got}).",
            )
        idx = {name: names.index(name) for name in REGIONS_COLUMNS}
        rows = 0
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            rows += 1
            if rows > max_rows:
                continue  # keep counting so the message can state the real size
            if len(row) < len(names):
                raise HTTPException(
                    status_code=422,
                    detail=f"Line {line_no} of the regions file has too few columns.",
                )
            # Same checks, wording and region numbering as the worker's `load_regions`, so
            # a regions file the worker would refuse in `preparing` is refused at upload.
            name = row[idx["seqnames"]].strip()
            if not name:
                raise HTTPException(
                    status_code=422, detail=f"Region {rows} has an empty seqnames value."
                )
            if "/" in name:  # inference.py writes <outdir>/<type>/<seqname>.csv
                raise HTTPException(
                    status_code=422,
                    detail=f"Region {rows}: the contig name '{name}' may not contain '/'.",
                )
            kind = unsafe_contig_kind(name)
            if kind is not None:
                raise HTTPException(
                    status_code=422,
                    detail=UNSAFE_CONTIG_DETAIL.format(index=rows, name=name, kind=kind),
                )
            try:
                start = int(row[idx["start"]])
                end = int(row[idx["end"]])
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Line {line_no} of the regions file has a non-integer start or end.",
                ) from None
            if start < 1 or end < start:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Line {line_no} of the regions file has an invalid interval "
                        "(1-based inclusive coordinates; start must be >= 1 and end >= start)."
                    ),
                )
            strand = row[idx["strand"]].strip()
            if strand not in ("+", "-"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Line {line_no} of the regions file has strand {strand!r}; expected + or -.",
                )
    if rows == 0:
        raise HTTPException(status_code=422, detail="The regions file has no data rows.")
    if rows > max_rows:
        raise HTTPException(
            status_code=422,
            detail=f"The regions file has {rows} data rows; this server accepts at most {max_rows}.",
        )
    return rows


class InvalidInput(HTTPException):
    """A rejection from `validate_inputs`, tagged with the slot that failed."""

    def __init__(self, slot: str, exc: HTTPException) -> None:
        super().__init__(status_code=exc.status_code, detail=exc.detail, headers=exc.headers)
        self.slot = slot


def validate_inputs(paths: dict, settings: Settings) -> dict[str, int]:
    """Size caps + cheap format checks on the four input files; returns their sizes."""
    sizes: dict[str, int] = {}
    for slot in SLOTS:
        path = paths.get(slot)
        if path is None or not path.is_file():
            raise InvalidInput(slot, missing_slot_error(slot))
        size = path.stat().st_size
        try:
            check_size(slot, size, settings)
        except HTTPException as exc:
            raise InvalidInput(slot, exc) from None
        sizes[slot] = size
    for slot, check in (
        ("pod5", validate_pod5_file),
        ("bam", validate_bam_file),
        ("reference", validate_reference_file),
        ("regions", lambda p: validate_regions_file(p, settings.max_regions)),
    ):
        try:
            check(paths[slot])
        except HTTPException as exc:
            raise InvalidInput(slot, exc) from None
    return sizes


# --------------------------------------------------------------------------- job rows


def get_job(session: Session, job_id: str) -> Job | None:
    if not is_uuid(job_id):
        return None
    return session.get(Job, job_id.lower())


def require_job(session: Session, job_id: str) -> Job:
    job = get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return job


def stamp_expiry(job: Job, settings: Settings, now: datetime | None = None) -> bool:
    """Set `expires_at = finished_at + results_retention_days` on a finished job without one.

    The worker writes only `finished_at` (contract section 4), so the API derives the expiry
    the first time it sees a `done` / `failed` row lacking it: on a status poll and in every
    cleanup pass (so jobs nobody polls still expire). Returns True when the row was changed.
    """
    if job.status not in (DONE, FAILED) or job.expires_at is not None:
        return False
    finished = job.finished_at or now or utcnow()
    job.expires_at = finished + timedelta(days=settings.results_retention_days)
    return True


def to_status(job: Job) -> JobStatus:
    uploads = None
    if job.status == UPLOADING:
        uploads = {
            u.slot: UploadInfo(
                url=f"/api/uploads/{u.id}", length=u.length, offset=u.offset, complete=u.complete
            )
            for u in job.uploads
        }
    stage = job.stage if job.stage in STAGES else None
    if job.stage is not None and stage is None:
        log.warning("job %s has an unknown stage %r", job.id, job.stage)
    return JobStatus(
        job_id=job.id,
        status=job.status,
        stage=stage,
        progress=job.progress,
        eta_s=job.eta_s,
        kit=job.kit,
        input_kind=job.input_kind,
        input_bytes=dict(job.input_bytes or {}),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
        inputs_deleted_at=job.inputs_deleted_at,
        cancel_requested=job.cancel_requested_at is not None,
        error=job.error,
        n_sites=job.n_sites,
        n_reads=job.n_reads,
        n_transcripts=job.n_transcripts,
        model=ModelInfo(name=job.model_name, version=job.model_version),
        uploads=uploads,
    )


def new_job(
    session: Session,
    *,
    kit: str,
    input_kind: str,
    client_key: str,
    status: str,
    stage: str | None,
    input_bytes: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Job:
    job = Job(
        id=new_id(),
        status=status,
        stage=stage,
        kit=kit,
        input_kind=input_kind,
        input_bytes=dict(input_bytes or {}),
        params=dict(DEFAULT_PARAMS),
        client_key=client_key,
        created_at=now or utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def discard_job(ctx: SignalContext, session: Session, job: Job) -> None:
    """Remove a job that never became valid (multipart validation failed)."""
    try:
        ctx.storage.remove_job_dir(job.id)
    finally:
        session.delete(job)
        session.commit()


def enqueue_job(ctx: SignalContext, session: Session, job: Job) -> JobStatus:
    """Mark the job queued, commit, then hand it to the worker queue."""
    job.status = QUEUED
    job.stage = None
    job.progress = None
    session.commit()
    try:
        ctx.queue.enqueue(job.id)
    except QueueUnavailable as exc:
        now = utcnow()
        job.status = FAILED
        job.error = str(exc)
        job.finished_at = now
        job.expires_at = now  # nothing to keep; the cleanup loop removes the directory
        session.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    session.refresh(job)
    return to_status(job)


# ----------------------------------------------------------------------- transitions


def init_job(ctx: SignalContext, session: Session, client_key: str, body: JobInit) -> JobStatus:
    settings = ctx.settings
    for slot in SLOTS:
        if slot not in body.files:
            raise missing_slot_error(slot)
    unknown = sorted(set(body.files) - set(SLOTS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown file slot(s): {', '.join(unknown)}; expected {', '.join(SLOTS)}."),
        )
    for slot in SLOTS:
        check_size(slot, body.files[slot].size, settings)

    now = utcnow()
    with quota_guard(session, client_key):
        enforce_quota(session, client_key, settings)
        job = new_job(
            session,
            kit=body.kit,
            input_kind=INPUT_KIND_UPLOAD,
            client_key=client_key,
            status=UPLOADING,
            stage="uploading",
            input_bytes={slot: body.files[slot].size for slot in SLOTS},
            now=now,
        )
        ctx.storage.create_job_dir(job.id)
        expires = now + timedelta(hours=settings.upload_ttl_h)
        for slot in SLOTS:
            decl = body.files[slot]
            upload = Upload(
                id=new_id(),
                job_id=job.id,
                slot=slot,
                filename=decl.name[:255],
                length=decl.size,
                offset=0,
                complete=False,
                created_at=now,
                expires_at=expires,
            )
            session.add(upload)
            ctx.storage.create_tus_file(upload.id, tus_meta(upload, job.id, now))
        session.commit()
    session.refresh(job)
    return to_status(job)


def tus_meta(upload: Upload, job_id: str, now: datetime) -> dict:
    """The sidecar `tus/<upload_id>.json` (docs/signal-branch.md section 3)."""
    return {
        "job_id": job_id,
        "slot": upload.slot,
        "filename": upload.filename,
        "length": upload.length,
        "offset": upload.offset,
        "created_at": now.isoformat(),
    }


def _reset_upload(ctx: SignalContext, upload: Upload, now: datetime) -> None:
    """Bring an upload whose bytes are not (all) on disk back to a resumable state.

    The row said `complete` but the tus file is missing or shorter than the declared
    length (a crash between `os.replace` calls in `start`, a lost write, a cleanup race).
    Whatever is really on disk becomes the offset; the client re-sends the rest.
    """
    on_disk = ctx.storage.tus_size(upload.id)
    upload.offset = min(on_disk, upload.length)
    upload.complete = False
    ctx.storage.create_tus_file(upload.id, tus_meta(upload, upload.job_id, now))
    ctx.storage.truncate_tus(upload.id, upload.offset)


def start_job(ctx: SignalContext, session: Session, job_id: str) -> JobStatus:
    job = require_job(session, job_id)
    if job.status != UPLOADING:
        if job.status in (QUEUED, RUNNING):
            detail = f"The job is already {job.status}."
        else:
            detail = f"The job is {job.status} and cannot be started."
        raise HTTPException(status_code=409, detail=detail)
    by_slot = {u.slot: u for u in job.uploads}
    incomplete = [slot for slot in SLOTS if slot not in by_slot or not by_slot[slot].complete]
    if incomplete:
        raise HTTPException(
            status_code=409,
            detail=f"Uploads incomplete for: {', '.join(incomplete)}.",
        )
    # A `complete` row is not proof that the bytes are there: accept the tus file or a file
    # an earlier `start` already moved into input/ (crash between the four renames), and
    # reopen the upload when neither holds the declared number of bytes.
    paths: dict[str, Path] = {}
    to_move: list[str] = []
    lost: list[str] = []
    for slot in SLOTS:
        upload = by_slot[slot]
        tus = ctx.storage.tus_path(upload.id)
        moved = ctx.storage.input_path(job.id, slot)
        if tus.is_file() and tus.stat().st_size == upload.length:
            paths[slot] = tus
            to_move.append(slot)
        elif moved.is_file() and moved.stat().st_size == upload.length:
            paths[slot] = moved
        else:
            lost.append(slot)
    if lost:
        now = utcnow()
        for slot in lost:
            _reset_upload(ctx, by_slot[slot], now)
        session.commit()
        log.warning("job %s: upload(s) %s not on disk; reopened for resume", job.id, lost)
        raise HTTPException(
            status_code=409,
            detail=f"Uploads incomplete for: {', '.join(lost)}.",
        )
    try:
        sizes = validate_inputs(paths, ctx.settings)  # 422; the job stays `uploading`
    except InvalidInput as exc:
        if exc.slot in to_move:
            # Reopen just this upload (offset 0) so the client re-sends the corrected file
            # instead of cancelling the job and uploading the other three again.
            ctx.storage.truncate_tus(by_slot[exc.slot].id, 0)
            _reset_upload(ctx, by_slot[exc.slot], utcnow())
            session.commit()
        raise
    for slot in to_move:
        ctx.storage.move_tus_into_input(by_slot[slot].id, job.id, slot)
    job.input_bytes = sizes
    return enqueue_job(ctx, session, job)


def finalize_multipart_job(
    ctx: SignalContext, session: Session, job: Job, kit: str, received: dict[str, int]
) -> JobStatus:
    """Validate the files a multipart request streamed into input/ and queue the job."""
    for slot in SLOTS:
        if slot not in received:
            raise missing_slot_error(slot)
    job.kit = check_kit(kit)
    paths = {slot: ctx.storage.input_path(job.id, slot) for slot in SLOTS}
    job.input_bytes = validate_inputs(paths, ctx.settings)
    return enqueue_job(ctx, session, job)


def sample_paths(settings: Settings) -> dict[str, object]:
    return {slot: settings.sample_dir / name for slot, name in SAMPLE_FILENAMES.items()}


def sample_available(settings: Settings) -> bool:
    return all(p.is_file() for p in sample_paths(settings).values())


def create_sample_job(ctx: SignalContext, session: Session, client_key: str) -> JobStatus:
    settings = ctx.settings
    paths = sample_paths(settings)
    if not all(p.is_file() for p in paths.values()):
        raise HTTPException(
            status_code=404, detail="The sample data set is not installed on this server."
        )
    with quota_guard(session, client_key):
        enforce_quota(session, client_key, settings)
        job = new_job(
            session,
            kit="RNA004",
            input_kind=INPUT_KIND_SAMPLE,
            client_key=client_key,
            status=UPLOADING,
            stage="preparing",
        )
        session.commit()  # the slot is taken; the copy below runs outside the guard
    try:
        ctx.storage.create_job_dir(job.id)
        sizes: dict[str, int] = {}
        for slot, src in paths.items():
            dst = ctx.storage.copy_into_input(src, job.id, INPUT_FILENAMES[slot])
            if slot in SLOTS:
                sizes[slot] = dst.stat().st_size
    except Exception:
        discard_job(ctx, session, job)
        raise
    job.input_bytes = sizes
    return enqueue_job(ctx, session, job)


def cancel_job(ctx: SignalContext, session: Session, job_id: str) -> JobStatus:
    job = require_job(session, job_id)
    if job.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"The job is already {job.status} and cannot be cancelled."
        )
    now = utcnow()
    was_running = job.status == RUNNING
    job.cancel_requested_at = now
    job.status = CANCELLED
    job.finished_at = now
    if was_running:
        # The worker's SIGTERM handler removes the directory; the cleanup loop is the backstop.
        job.expires_at = now + timedelta(hours=1)
    else:
        for upload in list(job.uploads):
            ctx.storage.remove_tus(upload.id)
        job.uploads.clear()
        ctx.storage.remove_job_dir(job.id)
        job.inputs_deleted_at = now
        job.results_deleted_at = now
        job.expires_at = now
    session.commit()
    try:
        ctx.queue.revoke(job.id, terminate=was_running)
    except QueueUnavailable as exc:
        # The status is already `cancelled`; the worker checks it before and between stages.
        log.warning("revoke of %s failed: %s", job.id, exc)
    session.refresh(job)
    return to_status(job)


def list_uploads_for(session: Session, upload_id: str) -> Upload | None:
    if not is_uuid(upload_id):
        return None
    return session.execute(
        select(Upload).where(Upload.id == upload_id.lower())
    ).scalar_one_or_none()
