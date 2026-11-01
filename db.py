"""The system of record: documents, versions, drafts, submissions, audit trail.

Redis keeps what is disposable (sessions, locks, the per-job working cache).
Everything a clinical inspector could ask about lives here, in a relational
database: Postgres in production (DATABASE_URL=postgresql+asyncpg://…), SQLite
for development and tests (the default, a file beside the app).

Tables are created on start-up with `create_all`, and `schema_version` records
which revision of this module created them. There are no destructive
migrations: a column is only ever added.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, TypeDecorator, UniqueConstraint, event, select, text)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

SCHEMA_VERSION = 1


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def database_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if raw:
        # Accept the URL most hosts hand out and pick the async driver.
        if raw.startswith("postgres://"):
            raw = "postgresql+asyncpg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql://"):
            raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
        return raw
    path = Path(__file__).resolve().parent / "data" / "esource.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TZDateTime(TypeDecorator):
    """Always timezone-aware UTC. Stored as UTC on every backend (SQLite keeps no zone)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        value = value.astimezone(dt.timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- tables


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)


class SourceFile(Base):
    """One distinct file content, by SHA-256. The original bytes are immutable."""

    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    pages: Mapped[Optional[int]] = mapped_column(Integer)
    blob_path: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    first_seen_by: Mapped[str] = mapped_column(String(128))


class Document(Base):
    """A source document as people know it: a path on a source (or an upload).

    A document has versions; each version points at one SourceFile. When the
    file at a path changes, a new version is added rather than replacing it.
    """

    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(64), index=True)  # "upload" for browser uploads
    path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(128))
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    # draft | processing | ready | in_review | submitted | failed
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    protocol_no: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    screening_no: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    visit_name: Mapped[Optional[str]] = mapped_column(String(128))
    assignee: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    flags: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow,
                                                    index=True)
    __table_args__ = (UniqueConstraint("source_id", "path", name="uq_document_source_path"),)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    source_file_id: Mapped[str] = mapped_column(ForeignKey("source_files.id"))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_doc_version"),)


class Extraction(Base):
    """One processing run of one document version: render, header, EDC visit, two reads."""

    __tablename__ = "extractions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    run: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str] = mapped_column(String(64), index=True)  # the Redis working job
    batch_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # queued | running | ready | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[Optional[str]] = mapped_column(String(24))
    error: Mapped[Optional[str]] = mapped_column(Text)
    models: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    usage: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # The review snapshot the browser opens: header, visit, payload.
    snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime(), index=True)
    # The worker that holds the lease. Every progress write is conditional on
    # it, so a run re-claimed after its lease lapsed can't be finished twice.
    claimed_by: Mapped[Optional[str]] = mapped_column(String(64))


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    number: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class Draft(Base):
    """A reviewer's saved-but-not-submitted review. Every save is a new revision."""

    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    edits: Mapped[list[Any]] = mapped_column(JSON, default=list)
    saved_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    saved_by: Mapped[str] = mapped_column(String(128))
    autosave: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("document_id", "revision", name="uq_draft_revision"),)


class Signature(Base):
    __tablename__ = "signatures"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    username: Mapped[str] = mapped_column(String(128))
    full_name: Mapped[str] = mapped_column(Text)
    meaning: Mapped[str] = mapped_column(Text)
    signed_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    record_sha256: Mapped[str] = mapped_column(String(64))


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"))
    draft_revision: Mapped[Optional[int]] = mapped_column(Integer)
    signature_id: Mapped[Optional[str]] = mapped_column(ForeignKey("signatures.id"))
    submitted_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    submitted_by: Mapped[str] = mapped_column(String(128))
    # sent | unchanged | failed
    status: Mapped[str] = mapped_column(String(16))
    changes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    edc_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)


class AuditEvent(Base):
    """Append-only, hash-chained. Never updated, never deleted (see audit.py)."""

    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id)
    ts: Mapped[dt.datetime] = mapped_column(TZDateTime(), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    full_name: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[Optional[str]] = mapped_column(String(32))
    document_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    protocol_no: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    screening_no: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    visit_name: Mapped[Optional[str]] = mapped_column(String(128))
    field: Mapped[Optional[str]] = mapped_column(Text)
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    app_version: Mapped[Optional[str]] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditOutbox(Base):
    """One row per audit event to deliver to the EDC. Delivery state is mutable;
    the event it points at is not."""

    __tablename__ = "audit_outbox"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())


class User(Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    full_name: Mapped[str] = mapped_column(Text)
    # reviewer | monitor | admin
    role: Mapped[str] = mapped_column(String(16), default="reviewer")
    password_hash: Mapped[str] = mapped_column(Text)
    password_changed_at: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime(), default=utcnow)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(TZDateTime())


Index("ix_extraction_claim", Extraction.status, Extraction.created_at)


# --------------------------------------------------------------------------- engine

_engine: Optional[AsyncEngine] = None
_sessions: Optional[async_sessionmaker[AsyncSession]] = None


def engine() -> AsyncEngine:
    global _engine, _sessions
    if _engine is None:
        url = database_url()
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"timeout": 30}}
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine.sync_engine, "connect")
            def _pragmas(conn, _record):  # pragma: no cover - driver hook
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def is_sqlite() -> bool:
    return database_url().startswith("sqlite")


_sqlite_lock: Optional[asyncio.Lock] = None


def sqlite_chain_lock() -> asyncio.Lock:
    global _sqlite_lock
    if _sqlite_lock is None:
        _sqlite_lock = asyncio.Lock()
    return _sqlite_lock


def _release_chain(sync_session, *_args) -> None:
    held = sync_session.info.pop("audit_locked", None)
    if isinstance(held, asyncio.Lock) and held.locked():
        held.release()


# The audit chain lock lives exactly as long as the transaction that took it.
event.listen(Session, "after_commit", _release_chain)
event.listen(Session, "after_rollback", _release_chain)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    engine()
    assert _sessions is not None
    async with _sessions() as s:
        try:
            yield s
        finally:
            _release_chain(s.sync_session)


def _add_missing_columns(sync_conn) -> list[str]:
    """Schema changes here only ever add nullable columns; add any a table lacks."""
    from sqlalchemy import inspect
    inspector = inspect(sync_conn)
    added = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            kind = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {kind}'))
            added.append(f"{table.name}.{column.name}")
    return added


async def init() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        if not is_sqlite():
            # The audit trail is append-only at the database too, not just in code.
            await conn.execute(text("""
                CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$ LANGUAGE plpgsql;
            """))
            await conn.execute(text("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events"))
            await conn.execute(text("""
                CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
                FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
            """))
        else:
            for op in ("UPDATE", "DELETE"):
                await conn.execute(text(f"""
                    CREATE TRIGGER IF NOT EXISTS audit_events_no_{op.lower()}
                    BEFORE {op} ON audit_events
                    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
                """))
    async with session() as s:
        if await s.get(SchemaVersion, SCHEMA_VERSION) is None:
            s.add(SchemaVersion(version=SCHEMA_VERSION))
            await s.commit()


async def close() -> None:
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessions = None


async def ping() -> bool:
    try:
        async with session() as s:
            await s.execute(select(1))
        return True
    except Exception:
        return False
