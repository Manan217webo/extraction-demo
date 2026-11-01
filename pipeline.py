"""Server-side batch processing: the same stages a reviewer drives in the browser.

    render → header → visit → read → ready

A run is an `Extraction` row. Workers claim queued runs with a lease, so several
uvicorn workers (or a separate `worker.py` process) share the queue without
running anything twice; a run whose worker died is picked up again when its
lease lapses. Each stage commits its progress, so the batch screen shows where
every file is. AI output is never sent anywhere from here: a finished run waits
in the inbox for a person to review, sign and submit it.

The stages reuse the request handlers' own code in app.py (imported lazily, as
app.py starts this worker), so a batch reads a document exactly the way an
interactive review does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import socket
import time
from typing import Any, Optional

from sqlalchemy import or_, select, update

import audit
import blobstore
import db
import documents

log = logging.getLogger("extraction.pipeline")

LEASE_SECONDS = 300
MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS") or 3)
CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY") or 2))
POLL_SECONDS = 2.0
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class NeedsInput(Exception):
    """The run can't continue without a person (header unreadable, visit unknown)."""


class Permanent(Exception):
    """Retrying won't help (not a PDF, too many pages)."""


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- claiming


async def _claim() -> Optional[str]:
    now = db.utcnow()
    async with db.session() as session:
        query = (select(db.Extraction.id)
                 .where(db.Extraction.status.in_(("queued", "running")),
                        or_(db.Extraction.lease_until.is_(None) & (db.Extraction.status == "queued"),
                            db.Extraction.lease_until < now))
                 .order_by(db.Extraction.created_at).limit(1))
        if not db.is_sqlite():
            query = query.with_for_update(skip_locked=True)
        found = (await session.execute(query)).scalar()
        if found is None:
            return None
        result = await session.execute(
            update(db.Extraction)
            .where(db.Extraction.id == found,
                   or_(db.Extraction.lease_until.is_(None), db.Extraction.lease_until < now))
            .values(lease_until=now + dt.timedelta(seconds=LEASE_SECONDS), status="running",
                    started_at=now, attempts=db.Extraction.attempts + 1, error=None))
        await session.commit()
        return found if result.rowcount == 1 else None


async def _stage(extraction_id: str, stage: str) -> None:
    async with db.session() as session:
        extraction = await session.get(db.Extraction, extraction_id)
        if extraction is None:
            raise Cancelled()
        if extraction.batch_id:
            batch = await session.get(db.Batch, extraction.batch_id)
            if batch is not None and batch.cancelled:
                raise Cancelled()
        if extraction.status == "cancelled":
            raise Cancelled()
        extraction.stage = stage
        extraction.lease_until = db.utcnow() + dt.timedelta(seconds=LEASE_SECONDS)
        await session.commit()


async def _finish(extraction_id: str, status: str, *, error: Optional[str] = None,
                  snapshot: Optional[dict[str, Any]] = None, usage: Optional[dict] = None,
                  models: Optional[dict] = None, header: Optional[dict] = None,
                  retry: bool = False) -> None:
    async with db.session() as session:
        extraction = await session.get(db.Extraction, extraction_id)
        if extraction is None:
            return
        document = await session.get(db.Document, extraction.document_id)
        if retry and extraction.attempts < MAX_ATTEMPTS:
            extraction.status = "queued"
            # Back off: the next claim waits until the lease lapses.
            extraction.lease_until = db.utcnow() + dt.timedelta(seconds=30 * 2 ** extraction.attempts)
            extraction.error = error
            await session.commit()
            log.warning("extraction %s will be retried: %s", extraction_id, error)
            return
        extraction.status = status
        extraction.error = error
        extraction.lease_until = None
        extraction.finished_at = db.utcnow()
        if status == "ready":
            extraction.stage = "done"
        if snapshot is not None:
            extraction.snapshot = snapshot
        if usage is not None:
            extraction.usage = usage
        if models is not None:
            extraction.models = models
        if document is not None:
            if header:
                document.protocol_no = header.get("protocol_no") or document.protocol_no
                document.screening_no = header.get("screening_no") or document.screening_no
                document.visit_name = header.get("visit_name") or document.visit_name
            if status == "ready" and snapshot and snapshot.get("payload"):
                stats = documents.summarise_payload(snapshot["payload"])
                document.flags, document.conflicts = stats["flags"], stats["conflicts"]
                document.confidence = stats["confidence"]
            document.status = {"ready": "ready", "failed": "failed", "cancelled": "failed",
                               "needs_input": "needs_input"}.get(status, document.status)
            if status == "cancelled" and await documents.latest_extraction(
                    session, document.id, documents.DONE_STATUSES):
                document.status = "ready"
        action = {"ready": "extraction.completed", "cancelled": "extraction.cancelled"}.get(
            status, "extraction.failed")
        await audit.record(session, action, username=extraction.created_by, document=document,
                           new_value=status, details={"extraction_id": extraction.id,
                                                      "error": error, "models": models,
                                                      "attempts": extraction.attempts})
        if status == "ready" and snapshot and snapshot.get("payload"):
            await record_ai_values(session, document, extraction, snapshot["payload"], models)
        await session.commit()


async def record_ai_values(session, document, extraction, payload: dict[str, Any],
                           models: Optional[dict]) -> None:
    """One audit event per value an AI proposed: what, from which model, how sure."""
    for section in (payload.get("form") or {}).get("sections") or []:
        fields_ = list(section.get("fields") or [])
        for group in section.get("groups") or []:
            for instance in group.get("instances") or []:
                fields_.extend(instance.get("fields") or [])
        for field in fields_:
            if field.get("value") is None:
                continue
            reading = field.get("reading") or {}
            await audit.record(
                session, "ai.value_proposed", username="system", document=document,
                field=f"{section.get('name') or section.get('section_id')} · {field.get('label')}",
                new_value=field.get("value"),
                details={"key": field.get("key"), "extraction_id": extraction.id,
                         "confidence": field.get("confidence"),
                         "agreed": reading.get("agreed"), "other_read": reading.get("second_value"),
                         "chosen_read": reading.get("chosen_read"),
                         "page": (field.get("source") or {}).get("page"),
                         "issues": field.get("issues") or [], "models": models})


# --------------------------------------------------------------------------- one run

WORKING_KEYS = ("header_rows", "header_usage", "visit_definition")


def working_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """The parts of a job's Redis meta needed to rebuild it after Redis lets it lapse."""
    return {key: meta[key] for key in WORKING_KEYS if key in meta}


async def _run(extraction_id: str) -> None:
    import app as web  # the request handlers' own stages
    import cronos
    import edc
    import fields
    import mapping
    import render
    import store
    import visit_forms

    async with db.session() as session:
        extraction = await session.get(db.Extraction, extraction_id)
        document = await session.get(db.Document, extraction.document_id)
        version = (await session.execute(
            select(db.DocumentVersion).where(db.DocumentVersion.document_id == document.id,
                                             db.DocumentVersion.version == extraction.version))
        ).scalar()
        job_id, owner, filename = extraction.job_id, extraction.created_by, document.filename
        sha = version.sha256
    primary, secondary = fields.read_models(True)
    models = {"primary": primary, "secondary": secondary, "preferred": fields.preferred_read()}

    await _stage(extraction_id, "render")
    data = await blobstore.get(sha)
    try:
        images = await render.page_images(data, web.MAX_PAGES)
    except render.RenderError as exc:
        raise Permanent(str(exc)) from exc
    # A retried run keeps what its earlier attempt already read (the header).
    previous = await store.get_job(job_id) or {}
    meta = {**{k: previous[k] for k in WORKING_KEYS if k in previous},
            "job_id": job_id, "filename": filename, "mode": "vision", "parser": "vision",
            "pages": len(images), "owner": owner, "document_id": document.id,
            "extraction_id": extraction_id}
    await store.put_parse(job_id, {"markdown": "", "items": [], "images": images,
                                   "pages": [{"page_number": int(n), "content": ""} for n in images]})
    await store.put_job(job_id, meta)

    await _stage(extraction_id, "header")
    header_response = await web._read_header(job_id, owner)
    confirmed = mapping.header_values(header_response["header"]["groups"])
    triple = {"protocol_no": str(confirmed.get("protocol_no") or "").strip(),
              "screening_no": str(confirmed.get("screening_no") or confirmed.get("subject_no") or "").strip(),
              "visit_name": cronos.format_visit_name(str(confirmed.get("visit_name") or ""))}
    base_snapshot = {"header": header_response, "visit": None, "payload": None,
                     "meta": working_meta(await store.get_job(job_id) or meta)}
    if not all(triple.values()):
        missing = [k.replace("_", " ") for k, v in triple.items() if not v]
        raise NeedsInput(f"The header doesn't show the {', '.join(missing)}. Open it to fill "
                         "them in.", base_snapshot, triple)

    await _stage(extraction_id, "visit")
    try:
        visit = await edc.get_visit(triple["protocol_no"], triple["screening_no"],
                                    triple["visit_name"])
    except edc.EdcUnavailable as exc:
        if "did not recognise" in str(exc):
            raise NeedsInput(str(exc), base_snapshot, triple) from exc
        raise
    definition = visit_forms.build_definition(visit, cronos.local_forms())
    meta = await store.get_job(job_id) or meta
    meta["visit_definition"] = definition
    await store.put_job(job_id, meta)
    visit_response = {"document": web._document_meta(job_id, meta),
                      "form": web._visit_view(definition),
                      "unsaveable": visit_forms.unsaveable(definition)}

    await _stage(extraction_id, "read")
    header_edits = {"protocol_no": triple["protocol_no"], "screening_no": triple["screening_no"],
                    "visit_name": confirmed.get("visit_name")}
    payload = await web._map_job(job_id, header_edits)
    snapshot = {"header": header_response, "visit": visit_response, "payload": payload,
                "meta": working_meta(await store.get_job(job_id) or meta)}
    await _finish(extraction_id, "ready", snapshot=snapshot, usage=payload.get("usage"),
                  models=models, header=triple)


async def process(extraction_id: str) -> None:
    import edc
    import fields

    started = time.monotonic()
    try:
        await _run(extraction_id)
        log.info("extraction %s ready in %.1fs", extraction_id, time.monotonic() - started)
    except Cancelled:
        await _finish(extraction_id, "cancelled", error="Cancelled.")
    except NeedsInput as exc:
        message, snapshot, triple = exc.args
        await _finish(extraction_id, "needs_input", error=message, snapshot=snapshot,
                      header={k: v for k, v in triple.items() if v})
    except Permanent as exc:
        await _finish(extraction_id, "failed", error=str(exc))
    except (edc.EdcUnavailable, fields.ExtractionUnavailable, asyncio.TimeoutError) as exc:
        await _finish(extraction_id, "failed", error=str(exc) or "Timed out.", retry=True)
    except blobstore.BlobCorrupted as exc:
        await _finish(extraction_id, "failed", error=f"The stored original failed its integrity check: {exc}")
    except Exception as exc:  # never lose a run silently
        log.exception("extraction %s crashed", extraction_id)
        detail = getattr(exc, "detail", None) or "Something went wrong while reading this file."
        await _finish(extraction_id, "failed", error=str(detail), retry=True)


# --------------------------------------------------------------------------- loop

_task: Optional[asyncio.Task] = None
_stop = asyncio.Event()


async def _loop() -> None:
    running: set[asyncio.Task] = set()
    log.info("batch worker %s started (concurrency %s)", WORKER_ID, CONCURRENCY)
    while not _stop.is_set():
        try:
            while len(running) < CONCURRENCY:
                found = await _claim()
                if not found:
                    break
                task = asyncio.create_task(process(found))
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception as exc:
            log.warning("worker claim failed: %s", exc)
        try:
            await asyncio.wait_for(_stop.wait(), POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    for task in running:
        task.cancel()


def start() -> None:
    global _task
    if os.getenv("WORKER_IN_PROCESS", "1").strip().lower() in {"0", "false", "no", "off"}:
        log.info("in-process batch worker disabled (run worker.py)")
        return
    _stop.clear()
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
