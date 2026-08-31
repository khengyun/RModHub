"""SQLAlchemy 2 plumbing for the job metadata store.

The engine is created once in the application lifespan (only when the signal branch is
enabled) and kept on ``app.state``; request handlers get short-lived sessions from the
sessionmaker. Column types are chosen to work on both Postgres (production) and SQLite
(tests use ``sqlite+pysqlite:///<tmp>/jobs.db``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend.

    Postgres stores ``timestamptz``; SQLite has no timezone support and hands naive values
    back, so they are re-tagged as UTC on the way out. Naive inputs are treated as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(url: str) -> Engine:
    """Engine for ``url``. SQLite gets the settings a threadpool-served API needs."""
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        # Sync endpoints run in FastAPI's threadpool, so a pooled connection may be used by
        # another thread than the one that opened it; `timeout` makes concurrent writers
        # wait instead of failing with "database is locked".
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create the tables if they do not exist (idempotent)."""
    # Import for the side effect of registering the models on `Base.metadata`.
    from app.jobs import models  # noqa: F401

    Base.metadata.create_all(engine)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Generator used as a FastAPI dependency: one session per request, closed afterwards."""
    session = factory()
    try:
        yield session
    finally:
        session.close()
