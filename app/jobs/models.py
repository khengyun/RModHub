"""ORM models for the `jobs` and `uploads` tables (docs/signal-branch.md section 4).

The worker writes a subset of the `jobs` columns with plain SQL, so column names here are a
contract. Types are SQLite-compatible so the test-suite can run on `sqlite+pysqlite`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow
from app.jobs.constants import MODEL_NAME, MODEL_VERSION


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(16))
    progress: Mapped[float | None] = mapped_column(Float)
    eta_s: Mapped[float | None] = mapped_column(Float)
    kit: Mapped[str] = mapped_column(String(8), nullable=False)
    input_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    input_bytes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # HMAC-SHA256 of the client address; raw IPs are never stored.
    client_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    inputs_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    results_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    error: Mapped[str | None] = mapped_column(Text)
    n_sites: Mapped[int | None] = mapped_column(Integer)
    n_reads: Mapped[int | None] = mapped_column(Integer)
    n_transcripts: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str] = mapped_column(String(32), nullable=False, default=MODEL_NAME)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False, default=MODEL_VERSION)
    worker_hostname: Mapped[str | None] = mapped_column(String(255))

    uploads: Mapped[list[Upload]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Upload.slot",
    )

    __table_args__ = (
        Index("jobs_expires_at", "expires_at"),
        Index("jobs_client_status", "client_key", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.id} {self.status}/{self.stage}>"


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    job: Mapped[Job] = relationship(back_populates="uploads")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Upload {self.id} {self.slot} {self.offset}/{self.length}>"
