"""The eSource record: what the browser flow and the batch worker write into the database.

The interactive review (app.py's /api/documents/{job_id}/… steps) works on a
disposable Redis copy of a job. This module ties that working copy to its
permanent record — document, version, extraction run, drafts, signatures,
submissions — and writes the audit trail for each step.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from sqlalchemy import select

import audit
import blobstore
import db
import documents

log = logging.getLogger("extraction.esource")

REASONS = [
    {"code": "TRANSCRIPTION_ERROR", "label": "AI misread — corrected against source"},
    {"code": "ILLEGIBLE_SOURCE", "label": "Source hard to read — value confirmed"},
    {"code": "CORRECTED_PER_SOURCE", "label": "Corrected per source document"},
    {"code": "ALTERNATE_AI_READ", "label": "Accepted the other AI read"},
    {"code": "UNIT_CONVERSION", "label": "Unit or format conversion"},
    {"code": "QUERY_RESPONSE", "label": "Response to a data query"},
    {"code": "VALUE_REMOVED", "label": "Value removed — not on source"},
    {"code": "OTHER", "label": "Other (explain)"},
]
REASON_LABELS = {r["code"]: r["label"] for r in REASONS}
# Correcting what an AI proposed, before anything reached the EDC, is the
# review itself; it is recorded with this reason unless the reviewer gives one.
AI_CORRECTION = "Reviewer correction of an AI-proposed value"

MEANINGS = [
    {"code": "REVIEWED_ACCURATE",
     "label": "I confirm these values were verified against the source document"},
    {"code": "AUTHOR", "label": "I am the author of these entries"},
]
MEANING_LABELS = {m["code"]: m["label"] for m in MEANINGS}


def canonical_sha(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reason_text(edit: Optional[dict[str, Any]]) -> Optional[str]:
    if not edit:
        return None
    code = edit.get("reason_code")
    text = (edit.get("reason_text") or "").strip()
    label = REASON_LABELS.get(code or "", code)
    if code == "OTHER":
        return text or None
    if label and text:
        return f"{label} — {text}"
    return label or text or None


# --------------------------------------------------------------------------- upload + stages


async def register_upload(username: str, filename: str, contents: bytes, pages: int,
                          job_id: str, client_ip: Optional[str] = None) -> dict[str, str]:
    """A browser upload becomes a document (keyed by its content) and a running extraction."""
    sha = blobstore.sha256_of(contents)
    async with db.session() as session:
        ingested = await documents.ingest(
            session, source_id=documents.UPLOAD_SOURCE, path=sha, filename=filename,
            data=contents, username=username, pages=pages, client_ip=client_ip)
        extraction = await documents.new_extraction(
            session, ingested.document, username=username, job_id=job_id, status="running",
            client_ip=client_ip)
        await session.commit()
        return {"document_id": ingested.document.id, "extraction_id": extraction.id}


async def remember_stage(job_id: str, kind: str, value: dict[str, Any], username: str,
                         meta: Optional[dict[str, Any]]) -> None:
    """Keep what an interactive step produced with its extraction run.

    A finished run is never rewritten: reading the document again produces a
    new run beside it, so the earlier AI result stays as it was.
    """
    import pipeline

    async with db.session() as session:
        extraction = await _extraction_for_job(session, job_id)
        if extraction is None:
            return
        document = await session.get(db.Document, extraction.document_id)
        triple = value.pop("_triple", None) if kind == "visit" else None
        if triple and document is not None:
            problem = await relink(session, document, triple, username,
                                   reason="Header confirmed by the reviewer")
            if problem:
                return
        if kind == "payload" and extraction.status in documents.DONE_STATUSES:
            previous = extraction.snapshot or {}
            extraction = await documents.new_extraction(
                session, document, username=username, job_id=job_id, status="running",
                reason="Read again from the review screen")
            extraction.snapshot = {k: previous[k] for k in ("header", "visit", "meta")
                                   if k in previous}
        snapshot = dict(extraction.snapshot or {})
        snapshot[kind] = value
        if meta:
            snapshot["meta"] = pipeline.working_meta(meta)
        extraction.snapshot = snapshot
        if kind == "payload":
            from fields import preferred_read, read_models
            primary, secondary = read_models(True)
            models = {"primary": primary, "secondary": secondary, "preferred": preferred_read()}
            extraction.models = models
            extraction.usage = value.get("usage")
            extraction.status, extraction.stage = "ready", "done"
            extraction.finished_at = db.utcnow()
            if document is not None:
                stats = documents.summarise_payload(value)
                document.flags, document.conflicts = stats["flags"], stats["conflicts"]
                document.confidence = stats["confidence"]
                document.status = ("in_review" if await documents.submitted(session, document.id)
                                   else "ready")
            await audit.record(session, "extraction.completed", username=username,
                               document=document, new_value="ready",
                               details={"extraction_id": extraction.id, "models": models})
            await pipeline.record_ai_values(session, document, extraction, value, models)
        await session.commit()


TRIPLE = ("protocol_no", "screening_no", "visit_name")


async def relink_problem(session, document: db.Document, triple: dict[str, Any]) -> Optional[str]:
    """Why this document may not move to another subject/visit, or None."""
    old = {k: getattr(document, k) for k in TRIPLE}
    new = {k: (triple.get(k) or old[k]) for k in TRIPLE}
    if new == old or not all(old.values()):
        return None
    if await documents.submitted(session, document.id):
        return (f"This document was already submitted to {old['protocol_no']} / "
                f"{old['screening_no']} / {old['visit_name']}. It can't be moved to "
                f"{new['protocol_no']} / {new['screening_no']} / {new['visit_name']}. "
                "If the header was misread at submission, raise it as a data issue.")
    return None


async def relink(session, document: db.Document, triple: dict[str, Any], username: str,
                 reason: str, client_ip: Optional[str] = None) -> Optional[str]:
    """Point the document at a subject/visit, auditing any change. Returns a
    problem (and changes nothing) when the document is already submitted elsewhere."""
    problem = await relink_problem(session, document, triple)
    if problem:
        return problem
    old = {k: getattr(document, k) for k in TRIPLE}
    new = {k: (triple.get(k) or old[k]) for k in TRIPLE}
    if new == old:
        return None
    for key in TRIPLE:
        setattr(document, key, new[key])
    await audit.record(session, "document.relinked", username=username, document=document,
                       client_ip=client_ip, old_value=" / ".join(str(old[k] or "—") for k in TRIPLE),
                       new_value=" / ".join(str(new[k]) for k in TRIPLE), reason=reason)
    return None


async def check_relink(job_id: str, triple: dict[str, Any]) -> Optional[str]:
    async with db.session() as session:
        extraction = await _extraction_for_job(session, job_id)
        if extraction is None:
            return None
        document = await session.get(db.Document, extraction.document_id)
        return await relink_problem(session, document, triple)


async def _extraction_for_job(session, job_id: str) -> Optional[db.Extraction]:
    return (await session.execute(
        select(db.Extraction).where(db.Extraction.job_id == job_id)
        .order_by(db.Extraction.created_at.desc()).limit(1))).scalar()


async def reconcile_submissions() -> int:
    """Submissions left "sending" by a process that died mid-call: the EDC may or
    may not have the values. Mark them, so nobody assumes either way."""
    import datetime as _dt
    async with db.session() as session:
        stale = (await session.execute(
            select(db.Submission).where(
                db.Submission.status == "sending",
                db.Submission.submitted_at < db.utcnow() - _dt.timedelta(minutes=10)))
        ).scalars().all()
        for submission in stale:
            submission.status = "unknown"
            submission.error = ("The server stopped while sending to the EDC. Check the "
                                "visit in the EDC, then submit again if the values are missing.")
            document = await session.get(db.Document, submission.document_id)
            await audit.record(session, "edc.submit_failed", username=submission.submitted_by,
                               document=document, new_value="unknown", reason=submission.error,
                               details={"submission_id": submission.id})
        await session.commit()
        return len(stale)


async def restore_job(job_id: str) -> Optional[dict[str, Any]]:
    """Rebuild a lapsed Redis working copy from the database and the stored original."""
    import render
    import store

    async with db.session() as session:
        extraction = (await session.execute(
            select(db.Extraction).where(db.Extraction.job_id == job_id)
            .order_by(db.Extraction.created_at.desc()).limit(1))).scalar()
        if extraction is None:
            return None
        document = await session.get(db.Document, extraction.document_id)
        version = (await session.execute(
            select(db.DocumentVersion).where(db.DocumentVersion.document_id == document.id,
                                             db.DocumentVersion.version == extraction.version))
        ).scalar()
        if version is None:
            return None
        snapshot = extraction.snapshot or {}
        owner = extraction.created_by
    data = await blobstore.get(version.sha256)
    images = await render.page_images(data, 200)
    meta = {"job_id": job_id, "filename": document.filename, "mode": "vision",
            "parser": "vision", "pages": len(images), "owner": owner,
            "document_id": document.id, "extraction_id": extraction.id,
            **(snapshot.get("meta") or {})}
    await store.put_parse(job_id, {"markdown": "", "items": [], "images": images,
                                   "pages": [{"page_number": int(n), "content": ""} for n in images]})
    await store.put_job(job_id, meta)
    log.info("rebuilt working copy of %s from the database", job_id)
    return meta


# --------------------------------------------------------------------------- reviews


async def current_extraction(session, document_id: str) -> Optional[db.Extraction]:
    """The run a reviewer works on: the newest one that produced something to review,
    else the newest at all."""
    found = await documents.latest_extraction(
        session, document_id, documents.DONE_STATUSES | {"needs_input"})
    return found or await documents.latest_extraction(session, document_id)


async def latest_draft(session, document_id: str,
                       extraction_id: Optional[str] = None) -> Optional[db.Draft]:
    query = select(db.Draft).where(db.Draft.document_id == document_id)
    if extraction_id:
        query = query.where(db.Draft.extraction_id == extraction_id)
    return (await session.execute(query.order_by(db.Draft.revision.desc()).limit(1))).scalar()


async def base_payload(session, document_id: str, extraction: db.Extraction) -> dict[str, Any]:
    draft = await latest_draft(session, document_id, extraction.id)
    if draft is not None:
        return draft.payload
    return (extraction.snapshot or {}).get("payload") or {}


async def record_changes(session, document: db.Document, before: dict[str, Any],
                         after: dict[str, Any], edits: list[dict[str, Any]], *, username: str,
                         client_ip: Optional[str], after_submission: bool,
                         context: str) -> list[dict[str, Any]]:
    """One audit event per field whose value differs between two versions of the form.

    The server's own diff decides what changed; the browser only supplies the
    reasons. A change after the record reached the EDC must carry a reason.
    """
    old_values = documents.field_values(before)
    new_values = documents.field_values(after)
    by_key: dict[str, dict[str, Any]] = {}
    for edit in edits or []:
        if edit.get("key"):
            by_key[edit["key"]] = edit
    changes = []
    missing = []
    for key in list(new_values) + [k for k in old_values if k not in new_values]:
        new = new_values.get(key) or {**old_values[key], "value": None}
        old = old_values.get(key, {}).get("value")
        if _norm(old) == _norm(new.get("value")):
            continue
        edit = by_key.get(key)
        reason = reason_text(edit)
        if not reason:
            if after_submission:
                missing.append(new.get("label") or key)
                continue
            reason = AI_CORRECTION
        kind = (edit or {}).get("kind") or "edit"
        changes.append({"key": key, "label": new.get("label"), "crf": new.get("crf"),
                        "old": old, "new": new.get("value"), "reason": reason, "kind": kind})
    if missing:
        raise MissingReason(missing)
    for change in changes:
        await audit.record(
            session, "field.alternate_accepted" if change["kind"] == "alternate" else "field.changed",
            username=username, document=document, client_ip=client_ip,
            field=f"{change['crf']} · {change['label']}", old_value=change["old"],
            new_value=change["new"], reason=change["reason"],
            details={"key": change["key"], "context": context})
    return changes


class MissingReason(Exception):
    def __init__(self, labels: list[str]) -> None:
        super().__init__("missing reason")
        self.labels = labels


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def signature_dict(signature: Optional[db.Signature]) -> Optional[dict[str, Any]]:
    if signature is None:
        return None
    return {"full_name": signature.full_name, "username": signature.username,
            "meaning": signature.meaning, "signed_at": documents.iso(signature.signed_at),
            "record_sha256": signature.record_sha256}
