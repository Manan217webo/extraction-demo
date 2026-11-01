"""Deliver the audit trail to the EDC.

Every audit event gets an outbox row in the same transaction as the event. This
sender works through the outbox in order, posting batches to EDC_AUDIT_URL:

    POST {EDC_AUDIT_URL}
    Authorization: Bearer {EDC_AUDIT_API_KEY or EDC_API_KEY}
    Idempotency-Key: <id of the first event>-<id of the last event>
    {"source": "webo-esource", "sourceVersion": "...", "records": [AuditRecord, ...]}

An AuditRecord follows CDISC ODM's AuditRecord vocabulary (who, what, when,
why, where in the study) and carries the hash chain, so the EDC can check that
nothing was dropped or altered on the way. Each record has its own stable
`AuditRecordID`, so a resent batch can be deduplicated by the receiver.

With no EDC_AUDIT_URL, rows wait as "held" and the audit screen says the trail
is kept locally only; setting the URL later releases them, oldest first.
Delivery is strictly in order: while the oldest record waits (back-off) or is
parked, nothing newer is sent. Failures back off exponentially (to an hour) and
are retried indefinitely; 4xx answers other than 408/429 park the batch as
"failed" until an admin retries it from the audit screen.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import random
from typing import Any, Optional

import httpx
from sqlalchemy import func, select, update

import audit
import db

log = logging.getLogger("extraction.edc_audit")
BATCH = int(os.getenv("EDC_AUDIT_BATCH") or 50)
INTERVAL = float(os.getenv("EDC_AUDIT_INTERVAL_SECONDS") or 5)


def endpoint() -> Optional[str]:
    return (os.getenv("EDC_AUDIT_URL") or "").strip() or None


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = (os.getenv("EDC_AUDIT_API_KEY") or os.getenv("EDC_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def record(event: db.AuditEvent) -> dict[str, Any]:
    details = event.details or {}
    return {
        "AuditRecordID": event.id,
        "Sequence": event.seq,
        "DateTimeStamp": audit._iso(event.ts),
        "Action": event.action,
        "UserRef": {"LoginName": event.username, "FullName": event.full_name,
                    "Role": event.role},
        "LocationRef": {"IP": event.client_ip},
        "ReasonForChange": event.reason,
        "StudyOID": event.protocol_no,
        "SubjectKey": event.screening_no,
        "StudyEventOID": event.visit_name,
        "ItemRef": event.field,
        "ItemOID": details.get("field_id"),
        "FormOID": details.get("crfId"),
        "OldValue": event.old_value,
        "NewValue": event.new_value,
        "SourceDocumentID": event.document_id,
        "Details": details,
        "SourceSystemVersion": event.app_version,
        "PrevHash": event.prev_hash,
        "Hash": event.hash,
    }


async def _release_held() -> None:
    async with db.session() as session:
        await session.execute(update(db.AuditOutbox).where(db.AuditOutbox.status == "held")
                              .values(status="pending", next_attempt_at=db.utcnow()))
        await session.commit()


async def deliver_once() -> int:
    """Send one batch. Returns how many records were delivered."""
    url = endpoint()
    if not url:
        return 0
    now = db.utcnow()
    async with db.session() as session:
        # Strictly in order: the oldest undelivered record goes first, and while
        # it is waiting out a back-off (or parked as failed) nothing newer is
        # sent, so the EDC never sees a gap in the hash chain.
        head = (await session.execute(
            select(db.AuditOutbox).where(db.AuditOutbox.status.in_(("pending", "failed")))
            .order_by(db.AuditOutbox.seq).limit(1))).scalar()
        if head is None or head.status == "failed" or head.next_attempt_at > now:
            return 0
        rows = (await session.execute(
            select(db.AuditOutbox, db.AuditEvent)
            .join(db.AuditEvent, db.AuditEvent.id == db.AuditOutbox.event_id)
            .where(db.AuditOutbox.status == "pending", db.AuditOutbox.seq >= head.seq)
            .order_by(db.AuditOutbox.seq).limit(BATCH))).all()
    if not rows:
        return 0
    body = {"source": "webo-esource", "sourceVersion": audit.APP_VERSION,
            "records": [record(event) for _, event in rows]}
    headers = {**_headers(),
               "Idempotency-Key": f"{rows[0][1].id}-{rows[-1][1].id}"}
    ids = [outbox.event_id for outbox, _ in rows]
    error: Optional[str] = None
    permanent = False
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
            response = await http.post(url, json=body, headers=headers)
        if response.status_code >= 300:
            error = f"EDC answered {response.status_code}"
            permanent = 400 <= response.status_code < 500 and response.status_code not in (408, 429)
    except httpx.HTTPError as exc:
        error = f"Couldn't reach the EDC audit endpoint: {exc.__class__.__name__}"

    async with db.session() as session:
        if error is None:
            await session.execute(update(db.AuditOutbox).where(db.AuditOutbox.event_id.in_(ids))
                                  .values(status="sent", sent_at=db.utcnow(), last_error=None,
                                          attempts=db.AuditOutbox.attempts + 1))
        else:
            attempts = max(o.attempts for o, _ in rows) + 1
            delay = min(3600, 5 * 2 ** min(attempts, 10)) * (0.8 + random.random() * 0.4)
            await session.execute(
                update(db.AuditOutbox).where(db.AuditOutbox.event_id.in_(ids))
                .values(status="failed" if permanent else "pending", last_error=error,
                        attempts=db.AuditOutbox.attempts + 1,
                        next_attempt_at=db.utcnow() + dt.timedelta(seconds=delay)))
            log.warning("audit delivery of %s record(s) failed: %s", len(ids), error)
        await session.commit()
    return 0 if error else len(ids)


async def status() -> dict[str, Any]:
    async with db.session() as session:
        rows = (await session.execute(
            select(db.AuditOutbox.status, func.count()).group_by(db.AuditOutbox.status))).all()
        oldest = (await session.execute(
            select(func.min(db.AuditOutbox.next_attempt_at))
            .where(db.AuditOutbox.status == "pending"))).scalar()
        last_error = (await session.execute(
            select(db.AuditOutbox.last_error).where(db.AuditOutbox.last_error.is_not(None))
            .order_by(db.AuditOutbox.seq.desc()).limit(1))).scalar()
    counts = {"pending": 0, "sent": 0, "failed": 0, "held": 0}
    for st, n in rows:
        counts[st] = int(n)
    url = endpoint()
    return {"mode": "live" if url else "held", "endpoint": url, "counts": counts,
            "oldest_pending": audit._iso(oldest) if oldest else None, "last_error": last_error}


async def retry_failed() -> int:
    async with db.session() as session:
        result = await session.execute(
            update(db.AuditOutbox).where(db.AuditOutbox.status == "failed")
            .values(status="pending", next_attempt_at=db.utcnow()))
        await session.commit()
        return result.rowcount or 0


_task: Optional[asyncio.Task] = None


async def _deliver_exclusively() -> int:
    """One sender at a time across every worker process, so records go out in order."""
    import store
    lock = store.get_redis().lock("lock:audit-delivery", timeout=120, blocking_timeout=0)
    if not await lock.acquire(blocking=False):
        return 0
    try:
        return await deliver_once()
    finally:
        try:
            await lock.release()
        except Exception:
            pass


async def _loop() -> None:
    released = False
    while True:
        try:
            if endpoint() and not released:
                await _release_held()
                released = True
            sent = await _deliver_exclusively()
            if sent:
                continue  # drain without waiting
        except Exception as exc:
            log.warning("audit delivery loop: %s", exc)
        await asyncio.sleep(INTERVAL)


def start() -> None:
    global _task
    if os.getenv("WORKER_IN_PROCESS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    if _task is not None:
        _task.cancel()
