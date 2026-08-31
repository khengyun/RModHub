"""Files on the shared upload volume (docs/signal-branch.md section 3).

    <upload_dir>/tus/<upload_id>         bytes received so far
    <upload_dir>/tus/<upload_id>.json    upload metadata
    <upload_dir>/jobs/<job_id>/input/    input.pod5, input_sorted.bam(.bai), reference.fa, regions.csv
    <upload_dir>/jobs/<job_id>/work/     worker scratch
    <upload_dir>/jobs/<job_id>/results.sqlite

Every id is validated as a canonical UUID before it is joined to a path, so a client can
never escape the layout. Writers stream to disk with a hard byte cap and publish files
atomically (`.part` + `os.replace`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Self

from app.jobs.constants import INPUT_FILENAMES

log = logging.getLogger(__name__)

PART_SUFFIX = ".part"
COPY_CHUNK = 1024 * 1024


def new_id() -> str:
    return str(uuid.uuid4())


def is_uuid(value: str) -> bool:
    """True for a canonical, lower/upper-case, hyphenated UUID string (36 chars)."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def validate_uuid(value: str, what: str = "id") -> str:
    if not is_uuid(value):
        raise ValueError(f"{what} is not a UUID: {value!r}")
    return value.lower()


class FileTooLarge(Exception):
    """A stream exceeded its byte cap. `received` counts the bytes seen before giving up."""

    def __init__(self, limit: int, received: int) -> None:
        super().__init__(f"stream exceeded {limit} bytes")
        self.limit = limit
        self.received = received


def dir_size(path: Path) -> int:
    """Total size in bytes of the regular files below `path` (0 if it does not exist)."""
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _rmtree(path: Path) -> int:
    """Remove a directory tree (or file) and return the bytes it occupied."""
    freed = dir_size(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    return freed


class CappedWriter:
    """Write chunks to `path` (via a `.part` sibling unless appending) with a byte cap.

    Use as a context manager: on success `commit()` renames the part file into place; on
    error the part file is removed. `FileTooLarge` is raised as soon as the cap is exceeded
    so the caller can stop reading the request.
    """

    def __init__(self, path: Path, limit: int, *, append: bool = False) -> None:
        self.path = path
        self.limit = limit
        self.append = append
        self.received = 0
        self._tmp = path if append else path.with_name(path.name + PART_SUFFIX)
        self._fh = open(self._tmp, "ab" if append else "wb")  # noqa: SIM115
        self._committed = False

    def write(self, data: bytes | memoryview) -> None:
        n = len(data)
        if self.received + n > self.limit:
            self.received += n
            raise FileTooLarge(self.limit, self.received)
        self._fh.write(data)
        self.received += n

    def commit(self) -> int:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        if not self.append:
            os.replace(self._tmp, self.path)
        self._committed = True
        return self.received

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            if not self.append:
                try:
                    self._tmp.unlink()
                except FileNotFoundError:
                    pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._committed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.abort()


async def write_stream(
    stream: AsyncIterator[bytes], path: Path, limit: int, *, append: bool = False
) -> int:
    """Stream an ASGI request body to `path`; returns the number of bytes written."""
    with CappedWriter(path, limit, append=append) as writer:
        async for chunk in stream:
            if chunk:
                writer.write(chunk)
        return writer.commit()


class JobStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.tus_dir = self.root / "tus"
        self.jobs_dir = self.root / "jobs"

    def ensure_layout(self) -> None:
        self.tus_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------ job dirs
    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / validate_uuid(job_id, "job_id")

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def work_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "work"

    def results_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "results.sqlite"

    def input_path(self, job_id: str, slot: str) -> Path:
        return self.input_dir(job_id) / INPUT_FILENAMES[slot]

    def create_job_dir(self, job_id: str) -> Path:
        d = self.job_dir(job_id)
        (d / "input").mkdir(parents=True, exist_ok=True)
        (d / "work").mkdir(parents=True, exist_ok=True)
        return d

    def remove_job_dir(self, job_id: str) -> int:
        return _rmtree(self.job_dir(job_id))

    def remove_input_dir(self, job_id: str) -> int:
        return _rmtree(self.input_dir(job_id))

    def list_job_dirs(self) -> list[tuple[str, Path]]:
        if not self.jobs_dir.is_dir():
            return []
        out = []
        for p in self.jobs_dir.iterdir():
            if p.is_dir() and is_uuid(p.name):
                out.append((p.name.lower(), p))
        return out

    # ---------------------------------------------------------------------- tus files
    def tus_path(self, upload_id: str) -> Path:
        return self.tus_dir / validate_uuid(upload_id, "upload_id")

    def tus_meta_path(self, upload_id: str) -> Path:
        return self.tus_dir / (validate_uuid(upload_id, "upload_id") + ".json")

    def create_tus_file(self, upload_id: str, meta: dict) -> Path:
        path = self.tus_path(upload_id)
        path.touch(exist_ok=True)
        self.write_tus_meta(upload_id, meta)
        return path

    def write_tus_meta(self, upload_id: str, meta: dict) -> None:
        target = self.tus_meta_path(upload_id)
        tmp = target.with_name(target.name + PART_SUFFIX)
        tmp.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)

    def read_tus_meta(self, upload_id: str) -> dict | None:
        try:
            return json.loads(self.tus_meta_path(upload_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def tus_size(self, upload_id: str) -> int:
        try:
            return self.tus_path(upload_id).stat().st_size
        except FileNotFoundError:
            return 0

    def truncate_tus(self, upload_id: str, size: int) -> None:
        """Cut a tus file back to `size` bytes; never extends it.

        Bytes past the committed offset come from a PATCH whose commit did not happen and
        are safe to drop. A file *shorter* than the offset is a lost write: `ftruncate`
        would silently pad the gap with NUL bytes and the upload would complete with a
        hole, so the caller must reconcile the row to the on-disk size instead.
        """
        path = self.tus_path(upload_id)
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            return
        if current > size:
            with open(path, "r+b") as fh:
                fh.truncate(size)

    def remove_tus(self, upload_id: str) -> int:
        freed = 0
        for p in (self.tus_path(upload_id), self.tus_meta_path(upload_id)):
            try:
                freed += p.stat().st_size
                p.unlink()
            except FileNotFoundError:
                pass
        return freed

    def list_tus_files(self) -> list[tuple[str, Path]]:
        """(upload_id, data file) for every upload on disk (ids from the data files)."""
        if not self.tus_dir.is_dir():
            return []
        out = []
        for p in self.tus_dir.iterdir():
            if p.is_file() and is_uuid(p.name):
                out.append((p.name.lower(), p))
        return out

    def move_tus_into_input(self, upload_id: str, job_id: str, slot: str) -> Path:
        """Rename a complete tus file into the job's input/ (same filesystem, atomic)."""
        src = self.tus_path(upload_id)
        dst = self.input_path(job_id, slot)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            # Different filesystems (unusual: both live under upload_dir). Copy then delete.
            shutil.move(str(src), str(dst))
        try:
            self.tus_meta_path(upload_id).unlink()
        except FileNotFoundError:
            pass
        return dst

    # ----------------------------------------------------------------------- sample copy
    def copy_into_input(self, src: Path, job_id: str, filename: str) -> Path:
        """Copy a file into input/<filename> atomically (used by the sample job)."""
        dst = self.input_dir(job_id) / filename
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + PART_SUFFIX)
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        return dst
