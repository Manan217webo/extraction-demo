"""HTTP routes for sources, batches, documents, drafts, submission, audit and users.

The contract is docs/API.md. Every write records its audit event in the same
transaction as the change.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hmac
import io
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

import access
import audit
import auth
import blobstore
import cronos_auth
import db
import documents
import esource
import locks
import sources

log = logging.getLogger("extraction.api")
router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- helpers


def client_ip(request: Request) -> Optional[str]:
    """The caller's address as uvicorn resolved it. X-Forwarded-For is honoured
    only from proxies listed in FORWARDED_ALLOW_IPS, so a client can't put an
    address of its choosing into the audit trail."""
    return request.client.host if request.client else None


async def current(request: Request) -> db.User:
    username = getattr(request.state, "user", None)
    if not username:
        raise HTTPException(401, "Please sign in to continue.")
    user = await auth.get_user(username)
    if user is None or not user.active:
        raise HTTPException(401, "Please sign in to continue.")
    return user


async def writer(request: Request) -> db.User:
    user = await current(request)
    if user.role not in ("reviewer", "admin"):
        raise HTTPException(403, "Your role can view records but not change them.")
    return user


async def admin(request: Request) -> db.User:
    user = await current(request)
    if user.role != "admin":
        raise HTTPException(403, "Only an administrator can do that.")
    return user


def _session_token(request: Request) -> Optional[str]:
    return request.cookies.get(auth.COOKIE_NAME)


async def cronos_token(request: Request) -> str:
    """The signed-in person's live Cronos access token (refreshed if needed)."""
    import store
    return await cronos_auth.access_token(store.get_redis(), _session_token(request),
                                          auth.max_seconds())


async def require_duty(request: Request, user: db.User, action: str,
                       document: db.Document, *, fresh: bool = False
                       ) -> Optional[cronos_auth.Right]:
    """Refuse unless Cronos says this person holds the duty for `action` on this
    document's study right now — in their own right or by delegation. None when
    duties aren't enforced. Fails closed: if Cronos can't answer, nothing is saved."""
    if not cronos_auth.enforce_duties():
        return None
    duty = cronos_auth.duty_for(action)
    if not duty:
        return None
    if not auth.is_cronos(user):
        raise HTTPException(403, "Local accounts can't review or sign while Cronos duties are "
                                 "enforced. Sign in with your Cronos account.")
    right = await cronos_auth.effective_right(await cronos_token(request), user.cronos_user_id,
                                              duty, document.protocol_no, fresh=fresh)
    if not right.holds:
        await audit.record_now("duty.denied", username=user.username, document=document,
                               client_ip=client_ip(request), field=duty, new_value=action,
                               details=right.audit())
        study = f" for study {document.protocol_no}" if document.protocol_no else ""
        raise HTTPException(403, f"Cronos doesn't show the {duty} duty for you{study} today, "
                                 "either assigned or delegated. Ask your study lead.")
    return right


def _duty_details(right: Optional[cronos_auth.Right]) -> dict[str, Any]:
    return right.audit() if right else {}


def _source_error(exc: sources.SourceError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def _document(session, document_id: str, user: Optional[db.User] = None) -> db.Document:
    """The document, if it exists and — given a user — is in their study/site scope.
    Out of scope reads as not found, so ids can't be probed."""
    document = await session.get(db.Document, document_id)
    if document is None:
        raise HTTPException(404, "That document could not be found.")
    if user is not None and not access.scope_for(user).sees_document(document):
        raise HTTPException(404, "That document could not be found.")
    return document


def _source(user: db.User, source_id: str) -> sources.Connector:
    """A configured source this person may see; others read as not configured."""
    conn = sources.get(source_id)
    if not access.scope_for(user).sees_source(conn):
        raise sources.SourceError("That file source isn't configured.", 404)
    return conn


async def _last_submission_at(session, document: db.Document) -> Optional[dt.datetime]:
    return (await session.execute(
        select(func.max(db.Submission.submitted_at)).where(
            db.Submission.document_id == document.id,
            db.Submission.status.in_(("sent", "unchanged"))))).scalar()


async def _reopen_required(session, document: db.Document) -> bool:
    """Submitted, and not opened for changes again (with remarks) since."""
    last = await _last_submission_at(session, document)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    reopened = document.reopened_at
    if reopened is not None and reopened.tzinfo is None:
        reopened = reopened.replace(tzinfo=dt.timezone.utc)
    return reopened is None or reopened < last


ARCHIVED = "This record is archived. Restore it before changing it."
REOPEN_FIRST = ("This record was already submitted to the EDC. Choose “Process again” and "
                "enter remarks before changing it.")


def _locked_out(held: dict[str, Any]) -> HTTPException:
    return HTTPException(423, {
        "message": f"{held.get('full_name') or held['username']} is working on this record. "
                   "It opens read-only until they close it.",
        "lock": {"username": held["username"], "full_name": held.get("full_name"),
                 "since": held.get("since")}})


async def _guard_write(session, document: db.Document, user: db.User, *,
                       submitted_ok: bool = False) -> None:
    """Refuse a change to an archived record, to a submitted one nobody reopened,
    or to one someone else has open. Takes (or renews) this person's lock."""
    if document.archived_at is not None:
        raise HTTPException(409, ARCHIVED)
    if not submitted_ok and await _reopen_required(session, document):
        raise HTTPException(409, REOPEN_FIRST)
    ok, held = await locks.acquire(document.id, user.username, user.full_name)
    if not ok:
        raise _locked_out(held)


# --------------------------------------------------------------------------- meta


@router.get("/meta")
async def meta(request: Request) -> dict[str, Any]:
    user = await current(request)
    return {"reasons": esource.REASONS, "meanings": esource.MEANINGS,
            "esign_required": esource.esign_required(), "user": auth.user_dict(user)}


# --------------------------------------------------------------------------- sources


@router.get("/sources")
async def list_sources(request: Request) -> dict[str, Any]:
    user = await current(request)
    scope = access.scope_for(user)
    return {"sources": [sources.summary(c) for c in sources.load().values()
                        if scope.sees_source(c)]}


async def _status_by_path(source_id: str, paths: list[str]) -> dict[str, tuple[str, str, Any]]:
    """path -> (document_id, status, latest version) for documents known on this source."""
    if not paths:
        return {}
    out: dict[str, tuple[str, str, Any]] = {}
    async with db.session() as session:
        rows = (await session.execute(
            select(db.Document).where(db.Document.source_id == source_id,
                                      db.Document.path.in_(paths)))).scalars().all()
        for document in rows:
            version = await documents.latest_version(session, document.id)
            out[document.path] = (document.id, document.status, version)
    return out


@router.get("/sources/{source_id}/browse")
async def browse(source_id: str, request: Request, path: str = "") -> dict[str, Any]:
    user = await current(request)
    try:
        conn = _source(user, source_id)
        rel = sources.clean_rel(path)
        entries, truncated = await conn.list_dir(rel)
    except sources.SourceError as exc:
        raise _source_error(exc) from exc
    known = await _status_by_path(source_id, [e.path for e in entries if e.type == "pdf"])
    out = []
    for entry in entries:
        item = {"name": entry.name, "path": entry.path, "type": entry.type, "size": entry.size,
                "modified": entry.modified, "status": None, "document_id": None}
        if entry.type == "pdf":
            found = known.get(entry.path)
            if found is None:
                item["status"] = "new"
            else:
                doc_id, status, version = found
                item["document_id"] = doc_id
                if status in ("processing",):
                    item["status"] = "processing"
                elif status == "failed":
                    item["status"] = "failed"
                elif version is not None and entry.size is not None and \
                        _size_of(version) not in (None, entry.size):
                    item["status"] = "changed"
                else:
                    item["status"] = "processed"
        out.append(item)
    parts = rel.split("/") if rel else []
    crumbs = [{"name": conn.name, "path": ""}] + [
        {"name": p, "path": "/".join(parts[: i + 1])} for i, p in enumerate(parts)]
    return {"source": {"id": conn.id, "name": conn.name}, "path": rel,
            "parent": "/".join(parts[:-1]) if parts else None, "breadcrumbs": crumbs,
            "entries": out, "truncated": truncated}


_sizes: dict[str, Optional[int]] = {}


def _size_of(version: db.DocumentVersion) -> Optional[int]:
    """The stored size of a version, cached; used to spot a changed file without reading it."""
    if version.sha256 not in _sizes:
        try:
            _sizes[version.sha256] = blobstore._path(version.sha256).stat().st_size
        except OSError:
            _sizes[version.sha256] = None
    return _sizes[version.sha256]


# --------------------------------------------------------------------------- batches


class PreviewRequest(BaseModel):
    source_id: str
    paths: list[str]


@router.post("/batches/preview")
async def preview(body: PreviewRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    try:
        conn = _source(user, body.source_id)
        found: dict[str, sources.Entry] = {}
        for raw in body.paths or [""]:
            for entry in await conn.walk_pdfs(sources.clean_rel(raw)):
                found[entry.path] = entry
                if len(found) > sources.MAX_SCAN_FILES:
                    raise sources.SourceError(
                        f"That selection has more than {sources.MAX_SCAN_FILES} PDFs. "
                        "Pick fewer folders.")
    except sources.SourceError as exc:
        raise _source_error(exc) from exc

    sem = asyncio.Semaphore(8)

    async def digest(entry: sources.Entry) -> tuple[sources.Entry, Optional[str], Optional[str]]:
        async with sem:
            try:
                data = await conn.read(entry.path)
                return entry, blobstore.sha256_of(data), None
            except sources.SourceError as exc:
                return entry, None, str(exc)

    hashed = await asyncio.gather(*(digest(e) for e in sorted(found.values(), key=lambda e: e.path)))
    files = []
    counts = {"new": 0, "processed": 0, "changed": 0, "processing": 0, "unreadable": 0}
    async with db.session() as session:
        for entry, sha, error in hashed:
            if sha is None:
                counts["unreadable"] += 1
                files.append({"path": entry.path, "name": entry.name, "size": entry.size,
                              "sha256": None, "status": "unreadable", "error": error,
                              "prior": None})
                continue
            status, prior = await documents.classify(session, conn.id, entry.path, sha)
            if prior and prior.get("document_id"):
                # The same file read at a site this person can't see: say it was
                # processed, but not where, when or by whom.
                other = await session.get(db.Document, prior["document_id"])
                if other is not None and not access.scope_for(user).sees_document(other):
                    prior = {"document_id": None, "processed_at": None, "processed_by": None,
                             "submitted": prior.get("submitted")}
            counts[status] += 1
            files.append({"path": entry.path, "name": entry.name, "size": entry.size,
                          "modified": entry.modified, "sha256": sha, "status": status,
                          "prior": prior})
    return {"files": files, "counts": counts}


class BatchFile(BaseModel):
    path: str
    sha256: Optional[str] = None
    reprocess: bool = False


class BatchRequest(BaseModel):
    source_id: str
    files: list[BatchFile]
    reason: Optional[str] = None
    label: Optional[str] = None


@router.post("/batches")
async def create_batch(body: BatchRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    ip = client_ip(request)
    if not body.files:
        raise HTTPException(400, "Pick at least one file to scan.")
    reason = (body.reason or "").strip()
    if any(f.reprocess for f in body.files) and not reason:
        raise HTTPException(422, "Give a reason for processing these files again.")
    try:
        conn = _source(user, body.source_id)
    except sources.SourceError as exc:
        raise _source_error(exc) from exc

    # Read and store every file first, outside any transaction: a slow share
    # must not hold the database (or the audit chain) while it answers.
    stored: list[tuple[str, tuple[str, str, int]]] = []
    for item in body.files:
        try:
            rel = sources.clean_rel(item.path)
            data = await conn.read(rel)
        except sources.SourceError as exc:
            raise _source_error(exc) from exc
        sha, path = await blobstore.put(data)
        stored.append((rel, (sha, path, len(data))))
        del data
    reprocess = {sources.clean_rel(f.path): f.reprocess for f in body.files}

    async with db.session() as session:
        await audit.lock_chain(session)  # also serialises batch numbers
        number = int((await session.execute(select(func.max(db.Batch.number)))).scalar() or 0) + 1
        folders = sorted({rel.rsplit("/", 1)[0] if "/" in rel else "" for rel, _ in stored})
        label = body.label or (f"{conn.name} · {folders[0] or 'top folder'}" if len(folders) == 1
                               else f"{conn.name} · {len(folders)} folders")
        batch = db.Batch(number=number, created_by=user.username, source_id=conn.id,
                         label=label, total=0)
        session.add(batch)
        await session.flush()
        await audit.record(session, "batch.created", username=user.username, client_ip=ip,
                           field=label, reason=reason or None,
                           details={"batch_id": batch.id, "number": number,
                                    "files": len(stored)})
        queued = skipped = 0
        for rel, blob in stored:
            sha = blob[0]
            status, prior = await documents.classify(session, conn.id, rel, sha)
            if status == "processing":
                skipped += 1
                continue
            if status == "processed" and not reprocess.get(rel):
                skipped += 1
                await audit.record(session, "file.reprocess_declined", username=user.username,
                                   client_ip=ip, field=rel,
                                   details={"batch_id": batch.id, "sha256": sha, "prior": prior})
                continue
            if status == "processed" and prior and prior.get("document_id"):
                await audit.record(session, "file.duplicate_detected", username=user.username,
                                   client_ip=ip, field=rel, reason=reason,
                                   details={"batch_id": batch.id, "sha256": sha, "prior": prior,
                                            "decision": "reprocess"})
            ingested = await documents.ingest(
                session, source_id=conn.id, path=rel, filename=rel.rsplit("/", 1)[-1],
                stored=blob, username=user.username, client_ip=ip,
                reason=reason if status in ("changed", "processed") else None)
            await documents.new_extraction(session, ingested.document, username=user.username,
                                           batch_id=batch.id, client_ip=ip,
                                           reason=reason if status != "new" else None)
            queued += 1
        batch.total, batch.skipped = queued, skipped
        await session.commit()
        return {"batch": await _batch_dict(session, batch)}


async def _batch_dict(session, batch: db.Batch) -> dict[str, Any]:
    rows = (await session.execute(
        select(db.Extraction.status, func.count()).where(db.Extraction.batch_id == batch.id)
        .group_by(db.Extraction.status))).all()
    counts = {"queued": 0, "running": 0, "ready": 0, "failed": 0, "cancelled": 0,
              "needs_input": 0}
    for status, n in rows:
        counts[status] = counts.get(status, 0) + int(n)
    done = counts["queued"] + counts["running"] == 0
    return {"id": batch.id, "number": batch.number, "label": batch.label,
            "created_at": documents.iso(batch.created_at), "created_by": batch.created_by,
            "source_id": batch.source_id, "total": batch.total, "skipped": batch.skipped,
            "counts": counts, "done": done, "cancelled": batch.cancelled}


@router.get("/batches")
async def list_batches(request: Request, limit: int = 50) -> dict[str, Any]:
    user = await current(request)
    scope = access.scope_for(user)
    query = select(db.Batch)
    if not scope.everything:
        query = query.where(db.Batch.source_id.in_(sorted(scope.source_ids())))
    async with db.session() as session:
        rows = (await session.execute(
            query.order_by(db.Batch.created_at.desc()).limit(min(limit, 200))
        )).scalars().all()
        return {"batches": [await _batch_dict(session, b) for b in rows]}


def _item(extraction: db.Extraction, document: db.Document) -> dict[str, Any]:
    return {"extraction_id": extraction.id, "document_id": document.id,
            "filename": document.filename, "path": document.path, "status": extraction.status,
            "stage": extraction.stage, "error": extraction.error, "flags": document.flags,
            "conflicts": document.conflicts, "confidence": document.confidence,
            "attempts": extraction.attempts,
            "started_at": documents.iso(extraction.started_at),
            "finished_at": documents.iso(extraction.finished_at)}


def _sees_batch(user: db.User, batch: db.Batch) -> bool:
    scope = access.scope_for(user)
    return scope.everything or batch.source_id in scope.source_ids()


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, request: Request) -> dict[str, Any]:
    user = await current(request)
    async with db.session() as session:
        batch = await session.get(db.Batch, batch_id)
        if batch is None or not _sees_batch(user, batch):
            raise HTTPException(404, "That batch could not be found.")
        rows = (await session.execute(
            select(db.Extraction, db.Document).join(db.Document,
                                                    db.Document.id == db.Extraction.document_id)
            .where(db.Extraction.batch_id == batch_id).order_by(db.Document.path))).all()
        return {"batch": await _batch_dict(session, batch),
                "items": [_item(e, d) for e, d in rows]}


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str, request: Request) -> dict[str, Any]:
    user = await writer(request)
    async with db.session() as session:
        batch = await session.get(db.Batch, batch_id)
        if batch is None or not _sees_batch(user, batch):
            raise HTTPException(404, "That batch could not be found.")
        if batch.created_by != user.username and user.role != "admin":
            raise HTTPException(403, "Only the person who started a batch, or an admin, can cancel it.")
        batch.cancelled = True
        queued = (await session.execute(
            select(db.Extraction).where(db.Extraction.batch_id == batch_id,
                                        db.Extraction.status == "queued"))).scalars().all()
        for extraction in queued:
            extraction.status = "cancelled"
            extraction.finished_at = db.utcnow()
            document = await session.get(db.Document, extraction.document_id)
            if document is not None and document.status == "processing":
                previous = await documents.latest_extraction(session, document.id,
                                                             documents.DONE_STATUSES)
                document.status = "ready" if previous else "failed"
        await audit.record(session, "batch.cancelled", username=user.username,
                           client_ip=client_ip(request), field=batch.label,
                           details={"batch_id": batch.id, "cancelled_queued": len(queued)})
        await session.commit()
        return {"batch": await _batch_dict(session, batch)}


@router.post("/extractions/{extraction_id}/retry")
async def retry_extraction(extraction_id: str, request: Request) -> dict[str, Any]:
    user = await writer(request)
    async with db.session() as session:
        extraction = await session.get(db.Extraction, extraction_id)
        if extraction is None:
            raise HTTPException(404, "That run could not be found.")
        if extraction.status not in ("failed", "cancelled", "needs_input") \
                and not (extraction.status == "running" and not documents.is_active(extraction)):
            raise HTTPException(409, "Only a failed or stopped run can be retried.")
        document = await _document(session, extraction.document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, ARCHIVED)
        extraction.status, extraction.stage, extraction.error = "queued", None, None
        extraction.lease_until, extraction.attempts, extraction.finished_at = None, 0, None
        extraction.claimed_by = None
        document.status = "processing"
        await audit.record(session, "extraction.retried", username=user.username,
                           document=document, client_ip=client_ip(request),
                           details={"extraction_id": extraction.id})
        await session.commit()
        return {"item": _item(extraction, document)}


# --------------------------------------------------------------------------- documents


@router.get("/docs")
async def list_docs(request: Request, status: Optional[str] = None, q: Optional[str] = None,
                    protocol: Optional[str] = None, subject: Optional[str] = None,
                    assignee: Optional[str] = None, flagged: Optional[bool] = None,
                    archived: bool = False,
                    limit: int = 50, offset: int = 0) -> dict[str, Any]:
    user = await current(request)
    filters = []
    clause = access.scope_for(user).document_clause()
    if clause is not None:
        filters.append(clause)
    if protocol:
        filters.append(db.Document.protocol_no == protocol)
    if subject:
        filters.append(db.Document.screening_no == subject)
    if assignee == "none":
        filters.append(db.Document.assignee.is_(None))
    elif assignee:
        filters.append(db.Document.assignee == assignee)
    if flagged:
        filters.append(db.Document.flags > 0)
    if q:
        like = f"%{q.strip()}%"
        filters.append(or_(db.Document.filename.ilike(like), db.Document.path.ilike(like),
                           db.Document.screening_no.ilike(like),
                           db.Document.protocol_no.ilike(like),
                           db.Document.visit_name.ilike(like)))
    # Archived records leave the work list; ?archived=true lists only them.
    live = filters + [db.Document.archived_at.is_(None)]
    async with db.session() as session:
        archived_count = (await session.execute(
            select(func.count()).select_from(db.Document)
            .where(*filters, db.Document.archived_at.is_not(None)))).scalar() or 0
        filters = filters + [db.Document.archived_at.is_not(None) if archived
                             else db.Document.archived_at.is_(None)]
        counts_rows = (await session.execute(
            select(db.Document.status, func.count()).where(*live)
            .group_by(db.Document.status))).all()
        counts = {"processing": 0, "ready": 0, "in_review": 0, "submitted": 0, "failed": 0,
                  "needs_input": 0, "split_review": 0, "split": 0}
        for st, n in counts_rows:
            counts[st] = counts.get(st, 0) + int(n)
        counts["archived"] = int(archived_count)
        query = select(db.Document).where(*filters)
        if status:
            query = query.where(db.Document.status.in_(status.split(",")))
        total = (await session.execute(
            select(func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await session.execute(
            query.order_by(db.Document.updated_at.desc())
            .limit(max(1, min(limit, 200))).offset(max(0, offset)))).scalars().all()
        out = []
        for document in rows:
            extraction = await documents.latest_extraction(session, document.id)
            draft = await esource.latest_draft(session, document.id)
            out.append(documents.doc_dict(document, extraction,
                                          draft.revision if draft else None))
        # A file of several visits shows how far its visits have got.
        parents = [d["id"] for d in out if d.get("kind") == "parent"]
        if parents:
            rows = (await session.execute(
                select(db.Document.parent_id, db.Document.status, func.count())
                .where(db.Document.parent_id.in_(parents))
                .group_by(db.Document.parent_id, db.Document.status))).all()
            tally: dict[str, dict[str, int]] = {}
            for parent_id, st, n in rows:
                tally.setdefault(parent_id, {})[st] = int(n)
            for item in out:
                if item.get("kind") == "parent":
                    item["children"] = tally.get(item["id"], {})
    try:
        held = await locks.holders([d["id"] for d in out])
    except Exception as exc:  # the list still loads without lock badges
        log.warning("record locks unavailable for the inbox: %s", exc)
        held = {}
    for item in out:
        item["lock"] = locks.public(held.get(item["id"]), user.username)
    return {"documents": out, "total": total, "counts": counts}


@router.get("/docs/{document_id}")
async def get_doc(document_id: str, request: Request) -> dict[str, Any]:
    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        extraction = await documents.latest_extraction(session, document.id)
        draft = await esource.latest_draft(session, document.id)
        return {"document": documents.doc_dict(document, extraction,
                                               draft.revision if draft else None)}


@router.get("/docs/{document_id}/review")
async def open_review(document_id: str, request: Request) -> dict[str, Any]:
    import app as web
    import fields

    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.kind == "parent":
            # A file of several visits isn't reviewed itself; its visits are.
            raise HTTPException(409, {"message": "This file holds several visits. Open it to "
                                                 "choose and review its visits.",
                                      "split": document.id})
        extraction = await esource.current_extraction(session, document.id)
        if extraction is None:
            raise HTTPException(409, "This document hasn't been read yet.")
        # A batch run is still being read on the server; an upload's run is
        # driven from the browser, so it can be opened (and finished) at any step.
        if extraction.batch_id and extraction.status in ("queued", "running"):
            raise HTTPException(409, "This document is still being read. It will be ready shortly.")
        draft = await esource.latest_draft(session, document.id, extraction.id)
        submission = (await session.execute(
            select(db.Submission).where(db.Submission.document_id == document.id,
                                        db.Submission.status.in_(("sent", "unchanged")))
            .order_by(db.Submission.submitted_at.desc()).limit(1))).scalar()
        signature = (await session.get(db.Signature, submission.signature_id)
                     if submission and submission.signature_id else None)
        snapshot = extraction.snapshot or {}
        reopen_required = await _reopen_required(session, document)
        await audit.record(session, "document.viewed", username=user.username, document=document,
                           client_ip=client_ip(request),
                           details={"extraction_id": extraction.id})
        await session.commit()
        doc = documents.doc_dict(document, extraction, draft.revision if draft else None)
        job_id = extraction.job_id

    meta = await web._owned_job(job_id, user.username)
    result = web._vision_status(meta)
    # Writers take the record's lock when they could change it; whoever opens it
    # second gets it read-only and is told who has it.
    lock = None
    if user.role in ("reviewer", "admin"):
        if doc.get("archived") is None and not reopen_required:
            ok, held = await locks.acquire(document_id, user.username, user.full_name)
            lock = held
        else:
            lock = await locks.holder(document_id)
    primary, secondary = fields.read_models(True)
    payload = draft.payload if draft is not None else snapshot.get("payload")
    return {
        "document": doc, "job_id": job_id, "extraction_id": extraction.id,
        "readonly": user.role == "monitor" or doc.get("archived") is not None,
        "reopen_required": reopen_required and user.role in ("reviewer", "admin"),
        "lock": locks.public(lock, user.username),
        "result": result, "header": snapshot.get("header"), "visit": snapshot.get("visit"),
        "payload": payload,
        "draft": ({"revision": draft.revision, "saved_at": documents.iso(draft.saved_at),
                   "saved_by": draft.saved_by} if draft else None),
        "submitted": ({"submitted_at": documents.iso(submission.submitted_at),
                       "submitted_by": submission.submitted_by, "status": submission.status,
                       "draft_revision": submission.draft_revision,
                       "signature": esource.signature_dict(signature)}
                      if submission else None),
        "reader": {"model": primary, "second_read": secondary,
                   "preferred": fields.preferred_read()},
    }


@router.get("/docs/{document_id}/file")
async def doc_file(document_id: str, request: Request, version: Optional[int] = None) -> Response:
    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        query = select(db.DocumentVersion).where(db.DocumentVersion.document_id == document.id)
        if version:
            query = query.where(db.DocumentVersion.version == version)
        row = (await session.execute(
            query.order_by(db.DocumentVersion.version.desc()).limit(1))).scalar()
        if row is None:
            raise HTTPException(404, "That version could not be found.")
    try:
        data = await blobstore.get(row.sha256)
    except blobstore.BlobCorrupted as exc:
        raise HTTPException(500, "The stored original failed its integrity check.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "The stored original is missing.") from exc
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in document.filename)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}"',
                             "X-Content-SHA256": row.sha256,
                             "Cache-Control": "private, max-age=3600"})


@router.get("/docs/{document_id}/pages/{number}/thumb")
async def page_thumb(document_id: str, number: int, request: Request,
                     scale: float = 0.32) -> Response:
    """One page of the document's current file, small, for the split screen."""
    import render

    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        version = await documents.latest_version(session, document.id)
        if version is None:
            raise HTTPException(404, "That file could not be found.")
    cached = render.cached_thumbnail(version.sha256, number) if scale == 0.32 else None
    try:
        if cached is not None:
            image = cached
        else:
            data = await blobstore.get(version.sha256)
            image = await render.thumbnail(data, number, scale)
    except render.RenderError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (FileNotFoundError, blobstore.BlobCorrupted) as exc:
        raise HTTPException(404, "The stored original is missing or damaged.") from exc
    return Response(image, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400",
                             "ETag": f'"{version.sha256[:16]}-{number}-{scale}"'})


# --------------------------------------------------------------------------- splits


class SplitEdits(BaseModel):
    edits: dict[str, Any] = {}


class SplitConfirm(BaseModel):
    edits: Optional[dict[str, Any]] = None
    reason: Optional[str] = None


async def _split_of(session, document: db.Document):
    import splits

    split = await splits.get_split(session, document.id, document.current_version)
    if split is None:
        raise HTTPException(404, "This file hasn't been checked for visits yet.")
    return split


def _split_error(exc) -> HTTPException:
    return HTTPException(getattr(exc, "status", 422), str(exc))


@router.get("/docs/{document_id}/split")
async def get_split(document_id: str, request: Request) -> dict[str, Any]:
    import splits

    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        split = await _split_of(session, document)
        out = await splits.view(session, document, split, user)
    held = None
    try:
        if user.role in ("reviewer", "admin") and out["document"].get("archived") is None:
            _, held = await locks.acquire(document_id, user.username, user.full_name)
        else:
            held = await locks.holder(document_id)
    except Exception as exc:  # the screen still opens without lock badges
        log.warning("record lock unavailable for the split screen: %s", exc)
    out["lock"] = locks.public(held, user.username)
    out["readonly"] = user.role == "monitor" or out["document"].get("archived") is not None
    return out


@router.put("/docs/{document_id}/split")
async def save_split(document_id: str, body: SplitEdits, request: Request) -> dict[str, Any]:
    """Save the reviewer's corrections as they make them. Nothing is read yet."""
    import splits

    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, ARCHIVED)
        ok, held = await locks.acquire(document.id, user.username, user.full_name)
        if not ok:
            raise _locked_out(held)
        split = await _split_of(session, document)
        await splits.save_edits(session, document, split, body.edits, user.username,
                                client_ip(request))
        await session.commit()
        return await splits.view(session, document, split, user)


@router.post("/docs/{document_id}/split/confirm")
async def confirm_split(document_id: str, body: SplitConfirm, request: Request) -> dict[str, Any]:
    """Create a document for every chosen visit and queue them to be read."""
    import splits

    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        ok, held = await locks.acquire(document.id, user.username, user.full_name)
        if not ok:
            raise _locked_out(held)
    try:
        result = await splits.confirm(document_id, body.edits, user.username,
                                      reason=body.reason, client_ip=client_ip(request), user=user)
    except splits.SplitError as exc:
        raise _split_error(exc) from exc
    try:
        await locks.release(document_id, user.username)
    except Exception:
        pass
    async with db.session() as session:
        document = await _document(session, document_id, user)
        split = await _split_of(session, document)
        return {"result": result, **(await splits.view(session, document, split, user))}


class AssignRequest(BaseModel):
    username: Optional[str] = None


@router.post("/docs/{document_id}/assign")
async def assign(document_id: str, body: AssignRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, ARCHIVED)
        if body.username and await session.get(db.User, body.username) is None:
            raise HTTPException(404, "That user doesn't exist.")
        old = document.assignee
        document.assignee = body.username or None
        await audit.record(session, "document.assigned", username=user.username,
                           document=document, client_ip=client_ip(request),
                           old_value=old, new_value=document.assignee)
        await session.commit()
        return {"document": documents.doc_dict(document)}


@router.get("/docs/{document_id}/history")
async def history(document_id: str, request: Request) -> dict[str, Any]:
    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        versions = (await session.execute(
            select(db.DocumentVersion).where(db.DocumentVersion.document_id == document.id)
            .order_by(db.DocumentVersion.version))).scalars().all()
        extractions = (await session.execute(
            select(db.Extraction).where(db.Extraction.document_id == document.id)
            .order_by(db.Extraction.created_at))).scalars().all()
        drafts = (await session.execute(
            select(db.Draft).where(db.Draft.document_id == document.id)
            .order_by(db.Draft.revision))).scalars().all()
        submissions = (await session.execute(
            select(db.Submission).where(db.Submission.document_id == document.id)
            .order_by(db.Submission.submitted_at))).scalars().all()
        signatures = {s.id: s for s in (await session.execute(
            select(db.Signature).where(db.Signature.document_id == document.id))).scalars()}
        return {
            "document": documents.doc_dict(document),
            "versions": [{"version": v.version, "sha256": v.sha256,
                          "created_at": documents.iso(v.created_at), "created_by": v.created_by,
                          "reason": v.reason} for v in versions],
            "extractions": [{"id": e.id, "version": e.version, "run": e.run, "status": e.status,
                             "stage": e.stage, "error": e.error,
                             "created_at": documents.iso(e.created_at),
                             "finished_at": documents.iso(e.finished_at),
                             "created_by": e.created_by, "models": e.models, "reason": e.reason,
                             "batch_id": e.batch_id} for e in extractions],
            "drafts": [{"revision": d.revision, "extraction_id": d.extraction_id,
                        "saved_at": documents.iso(d.saved_at), "saved_by": d.saved_by,
                        "autosave": d.autosave, "edits": len(d.edits or [])} for d in drafts],
            "submissions": [{"id": s.id, "submitted_at": documents.iso(s.submitted_at),
                             "submitted_by": s.submitted_by, "status": s.status,
                             "changes": s.changes, "skipped": s.skipped, "images": s.images,
                             "error": s.error, "draft_revision": s.draft_revision,
                             "signature": esource.signature_dict(signatures.get(s.signature_id))}
                            for s in submissions],
        }


@router.get("/docs/{document_id}/diff")
async def diff(document_id: str, request: Request, a: int = 0, b: Optional[int] = None) -> dict[str, Any]:
    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        extraction = await esource.current_extraction(session, document.id)

        async def payload_for(rev: Optional[int]) -> dict[str, Any]:
            if not rev:
                return ((extraction.snapshot or {}).get("payload") or {}) if extraction else {}
            draft = (await session.execute(
                select(db.Draft).where(db.Draft.document_id == document.id,
                                       db.Draft.revision == rev))).scalar()
            if draft is None:
                raise HTTPException(404, f"Revision {rev} doesn't exist.")
            return draft.payload

        if b is None:
            latest = await esource.latest_draft(session, document.id)
            b = latest.revision if latest else 0
        left = documents.field_values(await payload_for(a))
        right = documents.field_values(await payload_for(b))
    changes = []
    for key in sorted(set(left) | set(right)):
        va, vb = (left.get(key) or {}).get("value"), (right.get(key) or {}).get("value")
        if esource._norm(va) != esource._norm(vb):
            meta_ = right.get(key) or left.get(key) or {}
            changes.append({"key": key, "label": meta_.get("label"), "crf": meta_.get("crf"),
                            "a": va, "b": vb})
    return {"a": a, "b": b, "changes": changes}


class ReasonRequest(BaseModel):
    reason: str


@router.post("/docs/{document_id}/reprocess")
async def reprocess(document_id: str, body: ReasonRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "Give a reason for processing this document again.")
    async with db.session() as session:
        document = await _document(session, document_id, user)
        await _guard_write(session, document, user, submitted_ok=True)
        if documents.is_active(await documents.latest_extraction(
                session, document.id, documents.ACTIVE_EXTRACTION)):
            raise HTTPException(409, "This document is already being read.")
        extraction = await documents.new_extraction(session, document, username=user.username,
                                                    reason=reason, client_ip=client_ip(request))
        # Reading a submitted record again is processing it again, with this reason.
        if await _last_submission_at(session, document) is not None:
            document.reopened_at, document.reopened_by = db.utcnow(), user.username
        await session.commit()
        return {"extraction_id": extraction.id}


@router.post("/docs/{document_id}/lock")
async def take_lock(document_id: str, request: Request) -> dict[str, Any]:
    """Take or renew this person's lock on a record they have open for changes."""
    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, ARCHIVED)
        if await _reopen_required(session, document):
            raise HTTPException(409, REOPEN_FIRST)
    ok, held = await locks.acquire(document_id, user.username, user.full_name)
    return {"acquired": ok, "lock": locks.public(held, user.username)}


@router.delete("/docs/{document_id}/lock")
async def drop_lock(document_id: str, request: Request, force: bool = False) -> dict[str, Any]:
    """Give the lock back on close. An administrator may release someone else's."""
    user = await current(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if not force:
            released = await locks.release(document_id, user.username)
            return {"released": released is not None}
        if user.role != "admin":
            raise HTTPException(403, "Only an administrator can release someone else's lock.")
        released = await locks.release(document_id)
        if released is not None:
            await audit.record(session, "document.lock_released", username=user.username,
                               document=document, client_ip=client_ip(request),
                               old_value=released["username"],
                               reason="Released by an administrator",
                               details={"since": released.get("since")})
            await session.commit()
        return {"released": released is not None}


@router.post("/docs/{document_id}/archive")
async def archive(document_id: str, body: ReasonRequest, request: Request) -> dict[str, Any]:
    """Take a record off the work list — e.g. a copy pushed to the wrong site. Nothing
    is deleted; it stays searchable under Archived and opens read-only."""
    user = await writer(request)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "Give a reason for archiving this record.")
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, "This record is already archived.")
        ok, held = await locks.acquire(document.id, user.username, user.full_name)
        if not ok:
            raise _locked_out(held)
        if documents.is_active(await documents.latest_extraction(
                session, document.id, documents.ACTIVE_EXTRACTION)):
            raise HTTPException(409, "This record is still being read. Archive it when it's done.")
        document.archived_at, document.archived_by = db.utcnow(), user.username
        document.archive_reason = reason
        await audit.record(session, "document.archived", username=user.username,
                           document=document, client_ip=client_ip(request), reason=reason,
                           new_value="archived", old_value=document.status)
        await session.commit()
        out = documents.doc_dict(document)
    await locks.release(document_id, user.username)
    return {"document": out}


@router.post("/docs/{document_id}/restore")
async def restore(document_id: str, body: ReasonRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "Give a reason for restoring this record.")
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is None:
            raise HTTPException(409, "This record isn't archived.")
        old = f"archived by {document.archived_by}: {document.archive_reason}"
        document.archived_at = document.archived_by = document.archive_reason = None
        await audit.record(session, "document.restored", username=user.username,
                           document=document, client_ip=client_ip(request), reason=reason,
                           old_value=old, new_value=document.status)
        await session.commit()
        return {"document": documents.doc_dict(document)}


@router.post("/docs/{document_id}/reopen")
async def reopen(document_id: str, body: ReasonRequest, request: Request) -> dict[str, Any]:
    """Process an already submitted record again. Remarks are mandatory; until this is
    done, a submitted record opens read-only."""
    user = await writer(request)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "Enter remarks for processing this record again.")
    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.archived_at is not None:
            raise HTTPException(409, ARCHIVED)
        last = await _last_submission_at(session, document)
        if last is None:
            raise HTTPException(409, "This record hasn't been submitted, so it is already open "
                                     "for changes.")
        ok, held = await locks.acquire(document.id, user.username, user.full_name)
        if not ok:
            raise _locked_out(held)
        document.reopened_at, document.reopened_by = db.utcnow(), user.username
        await audit.record(session, "document.reopened", username=user.username,
                           document=document, client_ip=client_ip(request), reason=reason,
                           details={"last_submitted_at": documents.iso(last)})
        await session.commit()
        return {"document": documents.doc_dict(document),
                "lock": locks.public(held, user.username)}


class DraftRequest(BaseModel):
    payload: dict[str, Any]
    base_revision: Optional[int] = None
    edits: list[dict[str, Any]] = []
    autosave: bool = False


@router.post("/docs/{document_id}/draft")
async def save_draft(document_id: str, body: DraftRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        await _guard_write(session, document, user)
        right = await require_duty(request, user, "review", document)
        if document.kind == "parent":
            # A file of several visits isn't reviewed itself; its visits are.
            raise HTTPException(409, {"message": "This file holds several visits. Open it to "
                                                 "choose and review its visits.",
                                      "split": document.id})
        extraction = await esource.current_extraction(session, document.id)
        if extraction is None:
            raise HTTPException(409, "This document hasn't been read yet.")
        latest = await esource.latest_draft(session, document.id)
        latest_rev = latest.revision if latest else 0
        if body.base_revision is not None and body.base_revision != latest_rev:
            raise HTTPException(409, {"message": "Someone saved a newer draft of this document.",
                                      "revision": latest_rev,
                                      "saved_by": latest.saved_by if latest else None,
                                      "saved_at": documents.iso(latest.saved_at) if latest else None})
        sha = esource.canonical_sha(body.payload.get("form") or {})
        if latest is not None and latest.payload_sha256 == sha and latest.extraction_id == extraction.id:
            return {"revision": latest.revision, "saved_at": documents.iso(latest.saved_at),
                    "unchanged": True}
        before = await esource.base_payload(session, document.id, extraction)
        try:
            changes = await esource.record_changes(
                session, document, before, body.payload, body.edits, username=user.username,
                client_ip=client_ip(request),
                after_submission=await documents.submitted(session, document.id),
                context="draft")
        except esource.MissingReason as exc:
            raise HTTPException(422, "These values changed after the record went to the EDC "
                                     "and need a reason: " + ", ".join(exc.labels[:8])) from exc
        draft = db.Draft(document_id=document.id, extraction_id=extraction.id,
                         revision=latest_rev + 1, payload=body.payload, payload_sha256=sha,
                         edits=changes, saved_by=user.username, autosave=body.autosave)
        session.add(draft)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(409, {"message": "Someone saved a newer draft of this document.",
                                      "revision": latest_rev + 1}) from exc
        stats = documents.summarise_payload(body.payload)
        document.flags, document.conflicts = stats["flags"], stats["conflicts"]
        document.confidence = stats["confidence"]
        if document.status in ("ready", "needs_input"):
            document.status = "in_review"
        await session.flush()
        await audit.record(session, "draft.saved", username=user.username, document=document,
                           client_ip=client_ip(request), new_value=f"revision {draft.revision}",
                           details={"revision": draft.revision, "autosave": body.autosave,
                                    "changes": len(changes), "payload_sha256": sha,
                                    **_duty_details(right)})
        await session.commit()
        return {"revision": draft.revision, "saved_at": documents.iso(draft.saved_at),
                "changes": len(changes)}


class PreflightRequest(BaseModel):
    payload: dict[str, Any]


async def _fresh_save(document: db.Document, extraction: db.Extraction, payload: dict[str, Any],
                      crops: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """The save body built against what the EDC holds right now, not when the review opened."""
    import cronos
    import edc
    import mapping
    import visit_forms

    reviewed = mapping.revalidate(payload)
    triple = (document.protocol_no, document.screening_no, document.visit_name)
    if not all(triple):
        raise HTTPException(409, "This document isn't linked to an EDC visit yet. Confirm the "
                                 "header and load the visit first.")
    try:
        visit = await edc.get_visit(*triple)
    except edc.EdcUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    definition = visit_forms.build_definition(visit, cronos.local_forms())
    body, warnings = visit_forms.build_save(definition, reviewed.get("form") or {}, crops)
    delta = body.pop("_delta", {"changes": [], "skipped": []})
    refused = edc.check_images(body["crfs"])
    if refused:
        raise HTTPException(400, refused)
    return body, warnings, delta


def _blocked(payload: dict[str, Any]) -> list[dict[str, Any]]:
    import visit_forms
    out = []
    for field in documents.iter_form_fields((payload or {}).get("form") or {}):
        blocking = set(field.get("issues") or []) & visit_forms.BLOCKING_ISSUES
        if blocking and field.get("value") is not None:
            out.append({"key": field.get("key"), "label": field.get("label"),
                        "reason": ", ".join(sorted(blocking)).replace("_", " ")})
    return out


@router.post("/docs/{document_id}/preflight")
async def preflight(document_id: str, body: PreflightRequest, request: Request) -> dict[str, Any]:
    user = await writer(request)
    async with db.session() as session:
        document = await _document(session, document_id, user)
        await _guard_write(session, document, user)
        await require_duty(request, user, "submit", document)
        extraction = await esource.current_extraction(session, document.id)
    save, warnings, delta = await _fresh_save(document, extraction, body.payload, {})
    open_flags = [f.get("label") for f in documents.iter_form_fields(body.payload.get("form") or {})
                  if f.get("value") is not None and f.get("status") not in ("edited", "manual")
                  and set(f.get("issues") or []) & {"reads_disagree", "one_read_only", "low_confidence"}]
    missing = esource.missing_form_warnings(body.payload)
    warnings = missing + warnings
    if open_flags:
        warnings = [f"{len(open_flags)} value(s) the AI was unsure of haven't been changed "
                    f"— make sure you checked them against the page: "
                    f"{', '.join(str(x) for x in open_flags[:6])}"
                    f"{'…' if len(open_flags) > 6 else ''}."] + warnings
    return {"changes": delta["changes"], "skipped": delta["skipped"], "warnings": warnings,
            "blocked": _blocked(body.payload), "diff_sha": esource.canonical_sha(delta["changes"]),
            "visit": {"protocol_no": document.protocol_no, "screening_no": document.screening_no,
                      "visit_name": document.visit_name}}


class SubmitRequest(BaseModel):
    payload: dict[str, Any]
    crops: dict[str, dict[str, Any]] = {}
    password: Optional[str] = None  # only when REQUIRE_ESIGN is on
    meaning: str = "REVIEWED_ACCURATE"
    diff_sha: Optional[str] = None
    edits: list[dict[str, Any]] = []


@router.post("/docs/{document_id}/submit")
async def submit(document_id: str, body: SubmitRequest, request: Request) -> dict[str, Any]:
    import edc

    user = await writer(request)
    ip = client_ip(request)
    esign = esource.esign_required()
    if esign and body.meaning not in esource.MEANING_LABELS:
        raise HTTPException(422, "Choose what your signature means.")
    if esign and not body.password:
        raise HTTPException(422, "Enter your password to sign.")
    async with db.session() as session:
        document = await _document(session, document_id, user)
        await _guard_write(session, document, user)
    # Checked fresh (no cache) before submitting, so a revoked delegation
    # stops the submit rather than being noticed afterwards.
    right = await require_duty(request, user, "submit", document, fresh=True)
    if esign:
        # Re-authentication at the moment of signing (21 CFR 11.200). A wrong
        # password counts towards the lockout but does not end the session.
        try:
            ok, grant = await auth.confirm_signature(user, body.password, client_ip=ip)
        except auth.Locked as exc:
            raise HTTPException(423, f"Your account is locked until "
                                     f"{exc.until.strftime('%H:%M UTC')} after too many "
                                     f"attempts.") from exc
        if not ok:
            raise HTTPException(403, "That password isn't right.")
        if grant is not None:
            # Signing re-proved the password to Cronos: keep the fresh tokens it issued.
            import store
            await cronos_auth.save_grant(store.get_redis(), _session_token(request), grant,
                                         auth.max_seconds())

    async with db.session() as session:
        document = await _document(session, document_id, user)
        if document.kind == "parent":
            # A file of several visits isn't reviewed itself; its visits are.
            raise HTTPException(409, {"message": "This file holds several visits. Open it to "
                                                 "choose and review its visits.",
                                      "split": document.id})
        extraction = await esource.current_extraction(session, document.id)
        if extraction is None:
            raise HTTPException(409, "This document hasn't been read yet.")
    save, warnings, delta = await _fresh_save(document, extraction, body.payload, body.crops)
    diff_sha = esource.canonical_sha(delta["changes"])
    if body.diff_sha and body.diff_sha != diff_sha:
        raise HTTPException(409, "The EDC changed since you checked. Review the changes again.")

    async with db.session() as session:
        lock = select(db.Document).where(db.Document.id == document_id)
        if not db.is_sqlite():
            lock = lock.with_for_update()  # one submit per document at a time
        document = (await session.execute(lock)).scalar()
        if document is None:
            raise HTTPException(404, "That document could not be found.")
        in_flight = (await session.execute(
            select(db.Submission.id).where(
                db.Submission.document_id == document.id, db.Submission.status == "sending",
                db.Submission.submitted_at > db.utcnow() - dt.timedelta(minutes=10)))).scalar()
        if in_flight:
            raise HTTPException(409, "A submission of this document is already in progress.")
        version = await documents.latest_version(session, document.id)
        before = await esource.base_payload(session, document.id, extraction)
        try:
            await esource.record_changes(
                session, document, before, body.payload, body.edits, username=user.username,
                client_ip=ip, after_submission=await documents.submitted(session, document.id),
                context="submit")
        except esource.MissingReason as exc:
            raise HTTPException(422, "These values changed after the record went to the EDC "
                                     "and need a reason: " + ", ".join(exc.labels[:8])) from exc
        # Every submit is preceded by a draft revision holding exactly what was signed.
        latest = await esource.latest_draft(session, document.id)
        sha = esource.canonical_sha(body.payload.get("form") or {})
        if latest is None or latest.payload_sha256 != sha:
            latest = db.Draft(document_id=document.id, extraction_id=extraction.id,
                              revision=(latest.revision if latest else 0) + 1,
                              payload=body.payload, payload_sha256=sha, edits=[],
                              saved_by=user.username, autosave=False)
            session.add(latest)
            await session.flush()
        record = {"document_id": document.id, "source_sha256": version.sha256 if version else None,
                  "visit": [document.protocol_no, document.screening_no, document.visit_name],
                  "draft_revision": latest.revision, "form_sha256": sha,
                  "changes": delta["changes"],
                  "duty": _duty_details(right),
                  "images": sorted(blobstore.sha256_of((i.get("base64Data") or "").encode())
                                   for c in save["crfs"] for i in c.get("images") or [])}
        # Without an e-signature this row is still who verified and submitted,
        # bound to a hash of the record — just not a Part 11 signature.
        signature = db.Signature(document_id=document.id, username=user.username,
                                 full_name=user.full_name,
                                 meaning=(esource.MEANING_LABELS[body.meaning] if esign
                                          else esource.SUBMITTED_LABEL),
                                 record_sha256=esource.canonical_sha(record))
        session.add(signature)
        await session.flush()
        await audit.record(session, "signature.applied" if esign else "submission.confirmed",
                           username=user.username,
                           document=document, client_ip=ip, new_value=signature.meaning,
                           details={"signature_id": signature.id,
                                    "record_sha256": signature.record_sha256,
                                    "draft_revision": latest.revision,
                                    **_duty_details(right)})
        # Written before the EDC is called: if this process dies mid-call, the
        # attempt is on record (and reconciled at start-up) rather than lost.
        pending = db.Submission(document_id=document.id, extraction_id=extraction.id,
                                draft_revision=latest.revision, signature_id=signature.id,
                                submitted_by=user.username,
                                status="sending" if save["crfs"] else "unchanged",
                                changes=delta["changes"], skipped=len(delta["skipped"]))
        session.add(pending)
        await session.flush()
        await audit.record(session, "edc.submit_attempted", username=user.username,
                           document=document, client_ip=ip,
                           details={"changes": len(delta["changes"]),
                                    "skipped": len(delta["skipped"]),
                                    "submission_id": pending.id,
                                    **_duty_details(right)})
        await session.commit()
        signature_id, draft_rev, submission_id = signature.id, latest.revision, pending.id

    counts = {"fields": sum(len(c["fields"]) for c in save["crfs"]),
              "values": len(delta["changes"]), "unchanged": len(delta["skipped"]),
              "images": sum(len(c["images"]) for c in save["crfs"])}
    status, response, error = "unchanged", None, None
    verification: Optional[dict[str, Any]] = None
    if save["crfs"]:
        try:
            response = await edc.save_visit(save)
            status = "sent"
        except (edc.EdcUnavailable, cronos_auth.CronosAuthError) as exc:
            status, error = "failed", str(exc)
        if status == "sent":
            verification = await _verify_in_edc(document, delta["changes"])

    async with db.session() as session:
        document = await _document(session, document_id, user)
        submission = await session.get(db.Submission, submission_id)
        submission.status, submission.error = status, error
        submission.images = counts["images"]
        submission.edc_response = {"response": response, "verification": verification}
        if status != "failed":
            document.status = "submitted"
        action = {"sent": "edc.submitted", "unchanged": "edc.submit_unchanged"}.get(
            status, "edc.submit_failed")
        await audit.record(session, action, username=user.username, document=document,
                           client_ip=ip, new_value=status, reason=error,
                           details={"counts": counts, "changes": delta["changes"],
                                    "signature_id": signature_id,
                                    "verification": verification,
                                    **_duty_details(right)})
        for change in delta["changes"] if status == "sent" else []:
            await audit.record(session, "field.changed", username=user.username,
                               document=document, client_ip=ip,
                               field=f"EDC · {change['crfName']} · {change['fieldName']}",
                               old_value=change["old"], new_value=change["new"],
                               reason="Submitted to the EDC from the verified source",
                               details={"context": "edc", "field_id": change.get("field_id"),
                                        "crfId": change.get("crfId")})
        await session.commit()
        signed = await session.get(db.Signature, signature_id)
    if status == "failed":
        raise HTTPException(502, f"{error} Your signature and draft are saved; "
                                 "submit again when the EDC is back.")
    if verification and verification.get("mismatches"):
        warnings = warnings + [
            f"The EDC shows a different value for {len(verification['mismatches'])} field(s) "
            "after the save. Check them in the EDC."]
    return {"status": status, "counts": counts, "delta": delta, "warnings": warnings,
            "verification": verification, "signature": esource.signature_dict(signed)}


async def _verify_in_edc(document: db.Document, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the visit back and confirm each sent value is what the EDC now holds."""
    import edc
    import visit_forms
    try:
        visit = await edc.get_visit(document.protocol_no, document.screening_no,
                                    document.visit_name)
    except (edc.EdcUnavailable, cronos_auth.CronosAuthError) as exc:
        return {"checked": False, "error": str(exc)}
    held: dict[tuple, Any] = {}
    for crf in visit.get("crfs") or []:
        for field in crf.get("fields") or []:
            held[(crf.get("crfId"), field.get("field_id"))] = field.get("value")
    mismatches = []
    for change in changes:
        if change.get("field_id") is None:
            continue
        now = held.get((change["crfId"], change["field_id"]))
        if not visit_forms.same_value(str(change["new"]), str(now or "")):
            mismatches.append({"crfName": change["crfName"], "fieldName": change["fieldName"],
                               "sent": change["new"], "edc": now})
    return {"checked": True, "matched": len(changes) - len(mismatches),
            "mismatches": mismatches}


# --------------------------------------------------------------------------- audit


def _audit_filters(user: Optional[str], document_id: Optional[str], subject: Optional[str],
                   action: Optional[str], q: Optional[str], start: Optional[str],
                   end: Optional[str]) -> list[Any]:
    filters = []
    if user:
        filters.append(db.AuditEvent.username == user)
    if document_id:
        filters.append(db.AuditEvent.document_id == document_id)
    if subject:
        filters.append(db.AuditEvent.screening_no == subject)
    if action:
        actions = action.split(",")
        filters.append(or_(*[db.AuditEvent.action.like(a.rstrip("*") + "%") if a.endswith("*")
                             else db.AuditEvent.action == a for a in actions]))
    if q:
        like = f"%{q}%"
        filters.append(or_(db.AuditEvent.field.ilike(like), db.AuditEvent.reason.ilike(like),
                           db.AuditEvent.old_value.ilike(like),
                           db.AuditEvent.new_value.ilike(like)))
    for raw, op in ((start, ">="), (end, "<")):
        if not raw:
            continue
        try:
            when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(400, "Dates must look like 2026-11-01.") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if op == "<" and len(raw) == 10:
            when += dt.timedelta(days=1)  # an end date includes that whole day
        filters.append(db.AuditEvent.ts >= when if op == ">=" else db.AuditEvent.ts < when)
    return filters


async def _audit_scope(user: db.User, filters: list[Any]) -> list[Any]:
    # Reviewers see the trail of documents; account events stay with admins/monitors.
    if user.role == "reviewer":
        filters.append(or_(db.AuditEvent.document_id.is_not(None),
                           db.AuditEvent.username == user.username))
    # Study/site scope: only the trail of records this person may see, plus their own
    # events that belong to no record.
    scope = access.scope_for(user)
    if not scope.everything:
        filters.append(or_(db.AuditEvent.document_id.in_(scope.visible_document_ids()),
                           and_(db.AuditEvent.document_id.is_(None),
                                db.AuditEvent.username == user.username)))
    return filters


@router.get("/audit")
async def audit_list(request: Request, user: Optional[str] = None,
                     document_id: Optional[str] = None, subject: Optional[str] = None,
                     action: Optional[str] = None, q: Optional[str] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     before: Optional[int] = None, limit: int = 100) -> dict[str, Any]:
    me = await current(request)
    params = request.query_params
    start = start or params.get("from")
    end = end or params.get("to")
    filters = await _audit_scope(me, _audit_filters(user, document_id, subject, action, q,
                                                    start, end))
    if before:
        filters.append(db.AuditEvent.seq < before)
    limit = max(1, min(limit, 500))
    async with db.session() as session:
        rows = (await session.execute(
            select(db.AuditEvent, db.AuditOutbox)
            .outerjoin(db.AuditOutbox, db.AuditOutbox.event_id == db.AuditEvent.id)
            .where(*filters).order_by(db.AuditEvent.seq.desc()).limit(limit))).all()
    events = [audit.as_dict(e, o) for e, o in rows]
    return {"events": events,
            "next_before": events[-1]["seq"] if len(events) == limit else None}


@router.get("/audit/actions")
async def audit_actions(request: Request) -> dict[str, Any]:
    await current(request)
    return {"actions": sorted(audit.ACTIONS)}


_EXPORT_COLUMNS = ["seq", "ts", "action", "username", "full_name", "role", "protocol_no",
                   "screening_no", "visit_name", "document_id", "field", "old_value", "new_value",
                   "reason", "client_ip", "app_version", "details", "prev_hash", "hash",
                   "edc_delivery"]


@router.get("/audit/export")
async def audit_export(request: Request, format: str = "csv", user: Optional[str] = None,
                       document_id: Optional[str] = None, subject: Optional[str] = None,
                       action: Optional[str] = None, q: Optional[str] = None) -> Response:
    me = await current(request)
    if me.role == "reviewer" and not document_id:
        raise HTTPException(403, "Exporting the full trail needs a monitor or admin account.")
    params = request.query_params
    filters = await _audit_scope(me, _audit_filters(user, document_id, subject, action, q,
                                                    params.get("from"), params.get("to")))
    async with db.session() as session:
        rows = (await session.execute(
            select(db.AuditEvent, db.AuditOutbox)
            .outerjoin(db.AuditOutbox, db.AuditOutbox.event_id == db.AuditEvent.id)
            .where(*filters).order_by(db.AuditEvent.seq))).all()
        exported = await audit.record(
            session, "audit.exported", username=me.username, client_ip=client_ip(request),
            new_value=format, details={"rows": len(rows), "filters": dict(params)})
        await session.commit()
        stamp = f"Exported by {me.full_name} ({me.username}) at {audit._iso(exported.ts)}"
    events = [audit.as_dict(e, o) for e, o in rows]
    name = f"audit-trail-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    if format == "json":
        body = json.dumps({"exported_by": me.username, "exported_at": audit._iso(exported.ts),
                           "app_version": audit.APP_VERSION, "events": events},
                          ensure_ascii=False, indent=2, default=str)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    buffer = io.StringIO()
    writer_ = csv.writer(buffer)
    writer_.writerow([f"# {stamp}"])
    writer_.writerow(_EXPORT_COLUMNS)
    for event in events:
        row = dict(event)
        row["details"] = json.dumps(event.get("details") or {}, ensure_ascii=False, sort_keys=True)
        row["edc_delivery"] = (event.get("delivery") or {}).get("status")
        writer_.writerow([_csv_safe(row.get(c)) for c in _EXPORT_COLUMNS])
    return Response("﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


def _csv_safe(value: Any) -> Any:
    """Keep a spreadsheet from running a value as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


@router.get("/audit/verify")
async def audit_verify(request: Request) -> dict[str, Any]:
    me = await current(request)
    async with db.session() as session:
        result = await audit.verify(session)
        await audit.record(session, "audit.verified", username=me.username,
                           client_ip=client_ip(request), new_value="ok" if result["ok"] else "broken",
                           details=result)
        await session.commit()
    return {"broken_at": None, **result}


@router.get("/audit/delivery")
async def audit_delivery(request: Request) -> dict[str, Any]:
    import edc_audit
    await current(request)
    return await edc_audit.status()


@router.post("/audit/delivery/retry")
async def audit_delivery_retry(request: Request) -> dict[str, Any]:
    import edc_audit
    await admin(request)
    return {"requeued": await edc_audit.retry_failed()}


# --------------------------------------------------------------------------- EDC pull


@router.get("/edc/audit")
async def edc_audit_pull(request: Request, after: int = 0, limit: int = 500) -> dict[str, Any]:
    """The audit trail for the EDC to fetch, oldest first, with an API key instead of a
    sign-in. The EDC keeps `next_after` and asks again from there; the records are the
    same AuditRecords the push sends. Off (404) until EDC_PULL_API_KEY is set."""
    import edc_audit
    keys = edc_audit.pull_keys()
    if not keys:
        raise HTTPException(404, "Not Found")
    scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
    given = given.strip()
    if scheme.lower() != "bearer" or not given or not any(
            hmac.compare_digest(given.encode(), key.encode()) for key in keys):
        log.warning("EDC audit pull refused from %s: bad or missing key", client_ip(request))
        raise HTTPException(401, "A valid API key is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    page = await edc_audit.pull(after=after, limit=limit)
    log.info("EDC pulled %d audit records after seq %d from %s",
             len(page["records"]), after, client_ip(request))
    return page


# --------------------------------------------------------------------------- admin


class NewUser(BaseModel):
    username: str
    full_name: str
    role: str = "reviewer"
    password: str


class UserPatch(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    unlock: Optional[bool] = None
    password: Optional[str] = None


@router.get("/admin/usage")
async def usage(request: Request, days: int = 30) -> dict[str, Any]:
    """AI token use and its cost over the last `days` days (see usage_report.py)."""
    import usage_report

    await admin(request)
    days = max(1, min(days, 366))
    until = db.utcnow()
    return await usage_report.build(until - dt.timedelta(days=days), until)


@router.get("/admin/users")
async def list_users(request: Request) -> dict[str, Any]:
    await admin(request)
    async with db.session() as session:
        rows = (await session.execute(select(db.User).order_by(db.User.username))).scalars().all()
        return {"users": [auth.user_dict(u) for u in rows]}


@router.post("/admin/users")
async def create_user(body: NewUser, request: Request) -> dict[str, Any]:
    me = await admin(request)
    username = body.username.strip()
    if not username or any(c.isspace() for c in username):
        raise HTTPException(422, "Usernames are a single word.")
    if body.role not in auth.ROLES:
        raise HTTPException(422, "Unknown role.")
    if not body.full_name.strip():
        raise HTTPException(422, "Give the person's full name — it appears on their signatures.")
    problem = auth.password_problem(body.password, username)
    if problem:
        raise HTTPException(422, problem)
    async with db.session() as session:
        if await session.get(db.User, username) is not None:
            raise HTTPException(409, "That username is taken. Usernames are never reused.")
        user = db.User(username=username, full_name=body.full_name.strip(), role=body.role,
                       password_hash=auth.hash_password(body.password),
                       password_changed_at=db.utcnow())
        session.add(user)
        await session.flush()
        await audit.record(session, "user.created", username=me.username,
                           client_ip=client_ip(request), field=username, new_value=body.role)
        await session.commit()
        return {"user": auth.user_dict(user)}


@router.patch("/admin/users/{username}")
async def update_user(username: str, body: UserPatch, request: Request) -> dict[str, Any]:
    me = await admin(request)
    async with db.session() as session:
        user = await session.get(db.User, username)
        if user is None:
            raise HTTPException(404, "That user doesn't exist.")
        changes: dict[str, Any] = {}
        if body.full_name is not None and body.full_name.strip() != user.full_name:
            changes["full_name"] = (user.full_name, body.full_name.strip())
            user.full_name = body.full_name.strip()
        if body.role is not None and body.role != user.role:
            if body.role not in auth.ROLES:
                raise HTTPException(422, "Unknown role.")
            if user.username == me.username:
                raise HTTPException(409, "You can't change your own role.")
            changes["role"] = (user.role, body.role)
            user.role = body.role
        if body.active is not None and body.active != user.active:
            if user.username == me.username:
                raise HTTPException(409, "You can't disable your own account.")
            changes["active"] = (user.active, body.active)
            user.active = body.active
        if body.unlock:
            if auth.is_cronos(user):
                # The lockout that matters is in Cronos; unlock it there with the
                # admin's own Cronos session.
                if not auth.is_cronos(me):
                    raise HTTPException(409, "Unlock Cronos accounts while signed in with a "
                                             "Cronos admin account, or in Cronos itself.")
                await cronos_auth.unlock(await cronos_token(request), user.username)
                changes["cronos_lock"] = ("locked", "unlocked")
            changes["locked_until"] = (user.locked_until, None)
            user.locked_until, user.failed_logins = None, 0
        if body.password:
            if auth.is_cronos(user):
                raise HTTPException(409, "This person signs in through Cronos. Change their "
                                         "password in Cronos.")
            problem = auth.password_problem(body.password, username)
            if problem:
                raise HTTPException(422, problem)
            user.password_hash = auth.hash_password(body.password)
            user.password_changed_at = db.utcnow()
            changes["password"] = ("•••", "reset")
        for key, (old, new) in changes.items():
            action = ("user.disabled" if key == "active" and new is False
                      else "user.password_changed" if key == "password" else "user.updated")
            await audit.record(session, action, username=me.username,
                               client_ip=client_ip(request), field=f"{username}.{key}",
                               old_value=old, new_value=new)
        await session.commit()
        return {"user": auth.user_dict(user)}
