"""Source documents: ingest, versions, duplicate detection, extraction runs.

A *document* is a file as people know it — a path on a configured source, or a
browser upload (identified by its content, since an upload has no path). Each
time the bytes at that path change, the document gains a *version*; the bytes
of every version are kept unchanged in the blob store. Each processing run of a
version is an *extraction*. Nothing here overwrites anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import audit
import blobstore
import db

UPLOAD_SOURCE = "upload"
DONE_STATUSES = {"ready", "in_review", "submitted"}
ACTIVE_EXTRACTION = {"queued", "running"}


def iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Ingested:
    document: db.Document
    version: db.DocumentVersion
    new_version: bool


async def ingest(session: AsyncSession, *, source_id: str, path: str, filename: str,
                 data: bytes, username: str, pages: Optional[int] = None,
                 size: Optional[int] = None, modified: Optional[str] = None,
                 reason: Optional[str] = None, client_ip: Optional[str] = None) -> Ingested:
    """Store the bytes and attach them to their document, adding a version if new."""
    sha, blob_path = await blobstore.put(data)
    source_file = (await session.execute(
        select(db.SourceFile).where(db.SourceFile.sha256 == sha))).scalar()
    if source_file is None:
        source_file = db.SourceFile(sha256=sha, size=len(data), pages=pages,
                                    blob_path=blob_path, first_seen_by=username)
        session.add(source_file)
        await session.flush()
        await audit.record(session, "file.ingested", username=username, client_ip=client_ip,
                           field=filename, details={"sha256": sha, "size": len(data),
                                                    "source": source_id, "path": path})

    document = (await session.execute(
        select(db.Document).where(db.Document.source_id == source_id,
                                  db.Document.path == path))).scalar()
    if document is None:
        document = db.Document(source_id=source_id, path=path, filename=filename,
                               created_by=username, status="processing", current_version=0)
        session.add(document)
        await session.flush()

    current = await latest_version(session, document.id)
    if current is not None and current.sha256 == sha:
        return Ingested(document, current, False)

    number = (current.version if current else 0) + 1
    version = db.DocumentVersion(document_id=document.id, version=number,
                                 source_file_id=source_file.id, sha256=sha,
                                 created_by=username, reason=reason)
    session.add(version)
    document.current_version = number
    document.filename = filename
    await session.flush()
    await audit.record(session, "document.version_added", username=username, document=document,
                       client_ip=client_ip, new_value=f"v{number}", reason=reason,
                       details={"sha256": sha, "previous_sha256": current.sha256 if current else None,
                                "path": path, "size": size or len(data), "modified": modified})
    return Ingested(document, version, True)


async def latest_version(session: AsyncSession, document_id: str) -> Optional[db.DocumentVersion]:
    return (await session.execute(
        select(db.DocumentVersion).where(db.DocumentVersion.document_id == document_id)
        .order_by(db.DocumentVersion.version.desc()).limit(1))).scalar()


async def latest_extraction(session: AsyncSession, document_id: str,
                            statuses: Optional[set[str]] = None) -> Optional[db.Extraction]:
    query = select(db.Extraction).where(db.Extraction.document_id == document_id)
    if statuses:
        query = query.where(db.Extraction.status.in_(statuses))
    return (await session.execute(
        query.order_by(db.Extraction.created_at.desc()).limit(1))).scalar()


async def new_extraction(session: AsyncSession, document: db.Document, *, username: str,
                         job_id: Optional[str] = None, batch_id: Optional[str] = None,
                         status: str = "queued", reason: Optional[str] = None,
                         client_ip: Optional[str] = None) -> db.Extraction:
    runs = (await session.execute(
        select(func.count(db.Extraction.id)).where(
            db.Extraction.document_id == document.id,
            db.Extraction.version == document.current_version))).scalar() or 0
    extraction = db.Extraction(document_id=document.id, version=document.current_version,
                               run=runs + 1, job_id=job_id or db.new_id(), batch_id=batch_id,
                               status=status, created_by=username, reason=reason,
                               stage="render" if status == "running" else None,
                               started_at=db.utcnow() if status == "running" else None)
    session.add(extraction)
    document.status = "processing"
    await session.flush()
    await audit.record(session, "extraction.queued" if status == "queued" else "extraction.started",
                       username=username, document=document, client_ip=client_ip, reason=reason,
                       details={"extraction_id": extraction.id, "version": document.current_version,
                                "run": extraction.run, "batch_id": batch_id})
    return extraction


async def submitted(session: AsyncSession, document_id: str) -> bool:
    return (await session.execute(
        select(db.Submission.id).where(db.Submission.document_id == document_id,
                                       db.Submission.status.in_(("sent", "unchanged")))
        .limit(1))).scalar() is not None


async def prior_for(session: AsyncSession, document: db.Document) -> dict[str, Any]:
    done = await latest_extraction(session, document.id, DONE_STATUSES)
    return {"document_id": document.id,
            "processed_at": iso(done.finished_at or done.created_at) if done else None,
            "processed_by": done.created_by if done else None,
            "submitted": await submitted(session, document.id)}


async def classify(session: AsyncSession, source_id: str, path: str,
                   sha256: str) -> tuple[str, Optional[dict[str, Any]]]:
    """How a file on a source relates to what was processed before.

    new         nothing like it has been processed
    processed   these exact bytes were processed already (here or at another path)
    changed     this path was processed, but the file has changed since
    processing  a run for it is queued or running now
    """
    document = (await session.execute(
        select(db.Document).where(db.Document.source_id == source_id,
                                  db.Document.path == path))).scalar()
    if document is not None:
        active = await latest_extraction(session, document.id, ACTIVE_EXTRACTION)
        if active is not None:
            return "processing", await prior_for(session, document)
        version = await latest_version(session, document.id)
        done = await latest_extraction(session, document.id, DONE_STATUSES)
        if version is not None and version.sha256 != sha256 and done is not None:
            return "changed", await prior_for(session, document)
        if version is not None and version.sha256 == sha256 and done is not None:
            return "processed", await prior_for(session, document)

    # The same bytes processed under another path (copied, renamed, uploaded).
    other = (await session.execute(
        select(db.Document).join(db.DocumentVersion,
                                 db.DocumentVersion.document_id == db.Document.id)
        .join(db.Extraction, db.Extraction.document_id == db.Document.id)
        .where(db.DocumentVersion.sha256 == sha256,
               db.Extraction.status.in_(DONE_STATUSES))
        .limit(1))).scalar()
    if other is not None:
        prior = await prior_for(session, other)
        prior["path"] = other.path if other.source_id != UPLOAD_SOURCE else None
        return "processed", prior
    return "new", None


def doc_dict(document: db.Document, extraction: Optional[db.Extraction] = None,
             draft_revision: Optional[int] = None) -> dict[str, Any]:
    return {
        "id": document.id, "filename": document.filename, "path": document.path,
        "source_id": document.source_id, "status": document.status,
        "protocol_no": document.protocol_no, "screening_no": document.screening_no,
        "visit_name": document.visit_name, "flags": document.flags,
        "conflicts": document.conflicts, "confidence": document.confidence,
        "assignee": document.assignee, "updated_at": iso(document.updated_at),
        "created_at": iso(document.created_at), "current_version": document.current_version,
        "extraction": ({"id": extraction.id, "status": extraction.status,
                        "stage": extraction.stage, "error": extraction.error}
                       if extraction else None),
        "draft_revision": draft_revision,
    }


def summarise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flags, conflicts and mean confidence of a reviewed form, for the worklist."""
    flags = conflicts = 0
    scores: list[float] = []
    for field in iter_form_fields((payload or {}).get("form") or {}):
        issues = field.get("issues") or []
        if issues:
            flags += 1
        if "reads_disagree" in issues:
            conflicts += 1
        if field.get("value") is not None and isinstance(field.get("confidence"), (int, float)):
            scores.append(float(field["confidence"]))
    return {"flags": flags, "conflicts": conflicts,
            "confidence": round(sum(scores) / len(scores), 3) if scores else None}


def iter_form_fields(form: dict[str, Any]):
    for section in form.get("sections") or []:
        for field in section.get("fields") or []:
            yield field
        for group in section.get("groups") or []:
            for instance in group.get("instances") or []:
                for field in instance.get("fields") or []:
                    yield field


def field_values(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """key -> {label, crf, value} for every field of a reviewed form."""
    out: dict[str, dict[str, Any]] = {}
    for section in ((payload or {}).get("form") or {}).get("sections") or []:
        crf = section.get("name") or section.get("section_id")
        for field in section.get("fields") or []:
            out[field.get("key") or f"{section['section_id']}.{field['field_id']}"] = {
                "label": field.get("label"), "crf": crf, "value": field.get("value")}
        for group in section.get("groups") or []:
            for instance in group.get("instances") or []:
                for field in instance.get("fields") or []:
                    key = field.get("key") or (f"{section['section_id']}.{group['group_id']}."
                                               f"{instance['instance']}.{field['field_id']}")
                    out[key] = {"label": f"{field.get('label')} ({group.get('row_label') or 'row'} "
                                         f"{instance['instance']})",
                                "crf": crf, "value": field.get("value")}
    return out
