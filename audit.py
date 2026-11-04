"""The audit trail: append-only, hash-chained, written with the change it records.

Every event is written inside the same database transaction as the change it
describes, so a change without its audit row cannot be committed. Each row
carries the hash of the row before it:

    hash = SHA-256( prev_hash || canonical JSON of the row's recorded fields )

so altering, inserting or removing any past row breaks every hash after it,
which `verify()` reports. The table itself refuses UPDATE and DELETE (db.init).

Each event also gets an outbox row so it can be delivered to the EDC
(edc_audit.py). Delivery state lives in the outbox, never on the event.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import db

GENESIS = "0" * 64

# What happened. Kept as plain strings so the export reads without a legend.
ACTIONS = {
    "auth.login", "auth.login_failed", "auth.logout", "auth.locked", "auth.session_expired",
    "user.created", "user.updated", "user.disabled", "user.password_changed",
    "source.browsed", "file.ingested", "file.duplicate_detected", "file.reprocess_declined",
    "document.version_added", "document.assigned", "document.viewed", "document.relinked",
    "batch.created", "batch.cancelled",
    "extraction.queued", "extraction.started", "extraction.completed", "extraction.failed",
    "extraction.cancelled", "extraction.retried",
    "ai.value_proposed", "ai.conflict_resolved",
    "field.changed", "field.alternate_accepted", "field.verified",
    "draft.saved", "draft.discarded",
    "signature.applied", "signature.failed",
    "edc.submit_attempted", "edc.submitted", "edc.submit_unchanged", "edc.submit_failed",
    "audit.viewed", "audit.exported", "audit.verified",
    "duty.denied",
    "config.changed",
}

# The fields the hash covers, in order. Adding one changes every future hash
# but not the past ones: `hash_version` in details says which list was used.
_HASHED = ("id", "ts", "action", "username", "full_name", "role", "document_id",
           "protocol_no", "screening_no", "visit_name", "field", "old_value", "new_value",
           "reason", "details", "client_ip", "app_version")



def app_version() -> str:
    explicit = (os.getenv("APP_VERSION") or "").strip()
    if explicit:
        return explicit
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parent, timeout=2,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


APP_VERSION = app_version()


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(row: dict[str, Any]) -> str:
    body = {key: (_iso(row[key]) if key == "ts" else row.get(key)) for key in _HASHED}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hmac_key() -> Optional[bytes]:
    raw = (os.getenv("AUDIT_HMAC_KEY") or "").strip()
    return raw.encode("utf-8") if raw else None


def chain_hash(prev_hash: str, row: dict[str, Any]) -> str:
    """SHA-256 of the previous hash and this row; keyed (HMAC) when AUDIT_HMAC_KEY
    is set, so someone with database access alone cannot recompute the chain."""
    message = (prev_hash + canonical(row)).encode("utf-8")
    key = _hmac_key()
    if key:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


async def lock_chain(session: AsyncSession) -> None:
    """Make this transaction the only one extending the chain, until it ends.

    PostgreSQL: a transaction-scoped advisory lock, shared by every worker
    process and released by COMMIT/ROLLBACK — never by Python code, so it can't
    be let go while the transaction still has unwritten events. A lock wait
    longer than AUDIT_LOCK_TIMEOUT fails the request instead of hanging it.

    SQLite (development, one process): an in-process lock held until the
    session commits, rolls back or closes (see db.py).
    """
    if session.info.get("audit_locked"):
        return
    if db.is_sqlite():
        lock = db.sqlite_chain_lock()
        await lock.acquire()
        session.info["audit_locked"] = lock
        return
    timeout = int(os.getenv("AUDIT_LOCK_TIMEOUT_MS") or 20000)
    await session.execute(text(f"SET LOCAL lock_timeout = {timeout}"))
    await session.execute(text("SELECT pg_advisory_xact_lock(7402190)"))
    session.info["audit_locked"] = True


async def _last_hash(session: AsyncSession) -> str:
    await lock_chain(session)
    last = (await session.execute(
        select(db.AuditEvent.hash).order_by(db.AuditEvent.seq.desc()).limit(1)
    )).scalar()
    return last or GENESIS


async def record(session: AsyncSession, action: str, *, username: str,
                 full_name: Optional[str] = None, role: Optional[str] = None,
                 document: Optional[db.Document] = None, document_id: Optional[str] = None,
                 protocol_no: Optional[str] = None, screening_no: Optional[str] = None,
                 visit_name: Optional[str] = None, field: Optional[str] = None,
                 old_value: Any = None, new_value: Any = None, reason: Optional[str] = None,
                 details: Optional[dict[str, Any]] = None,
                 client_ip: Optional[str] = None) -> db.AuditEvent:
    """Add one event to the session. It commits with the caller's transaction."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    if document is not None:
        document_id = document_id or document.id
        protocol_no = protocol_no or document.protocol_no
        screening_no = screening_no or document.screening_no
        visit_name = visit_name or document.visit_name
    if full_name is None or role is None:
        user = await session.get(db.User, username) if username else None
        full_name = full_name or (user.full_name if user else None)
        role = role or (user.role if user else None)

    row = {
        "id": db.new_id(), "ts": db.utcnow(), "action": action, "username": username or "system",
        "full_name": full_name, "role": role, "document_id": document_id,
        "protocol_no": protocol_no, "screening_no": screening_no, "visit_name": visit_name,
        "field": field, "old_value": _text(old_value), "new_value": _text(new_value),
        "reason": reason, "details": {**(details or {}), "hash_version": 1},
        "client_ip": client_ip, "app_version": APP_VERSION,
    }
    prev = await _last_hash(session)
    event = db.AuditEvent(**row, prev_hash=prev, hash=chain_hash(prev, row))
    session.add(event)
    await session.flush()
    session.add(db.AuditOutbox(event_id=event.id, seq=event.seq,
                               status="pending" if delivery_enabled() else "held"))
    return event


def delivery_enabled() -> bool:
    return bool((os.getenv("EDC_AUDIT_URL") or "").strip())


async def record_now(action: str, **kwargs: Any) -> db.AuditEvent:
    """An event that is the whole transaction (logins, views, exports)."""
    async with db.session() as session:
        event = await record(session, action, **kwargs)
        await session.commit()
        return event


def as_dict(event: db.AuditEvent, outbox: Optional[db.AuditOutbox] = None) -> dict[str, Any]:
    out = {key: getattr(event, key) for key in _HASHED}
    out["ts"] = _iso(event.ts)
    out.update(seq=event.seq, prev_hash=event.prev_hash, hash=event.hash)
    if outbox is not None:
        out["delivery"] = {"status": outbox.status, "attempts": outbox.attempts,
                           "last_error": outbox.last_error,
                           "sent_at": _iso(outbox.sent_at) if outbox.sent_at else None}
    return out


async def verify(session: AsyncSession, batch: int = 2000) -> dict[str, Any]:
    """Walk the whole chain. Returns the first broken row, if any."""
    prev = GENESIS
    checked = 0
    after = 0
    while True:
        rows = (await session.execute(
            select(db.AuditEvent).where(db.AuditEvent.seq > after)
            .order_by(db.AuditEvent.seq).limit(batch)
        )).scalars().all()
        if not rows:
            break
        for event in rows:
            row = {key: getattr(event, key) for key in _HASHED}
            if event.prev_hash != prev or chain_hash(prev, row) != event.hash:
                return {"ok": False, "checked": checked, "broken_at": event.seq,
                        "broken_id": event.id}
            prev = event.hash
            checked += 1
            after = event.seq
    return {"ok": True, "checked": checked, "head": prev, "verified_at": _iso(db.utcnow())}


async def count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(db.AuditEvent.seq)))).scalar() or 0)
