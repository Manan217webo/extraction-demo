"""Splitting a file into its visits, on the record.

splitter.py works out which pages belong to which visit. This module keeps that
on the record and acts on it:

    scan      survey the file, read every page's header, store the plan (once
              per file version; a retry or a second look reuses it)
    decide    one visit → read the file as it is; anything else → a person
              confirms the split first, because reading costs money and a
              wrong split puts values on the wrong visit
    priors    visits in the file that were processed before (same patient,
              same visit), so nothing is read twice by accident
    confirm   each chosen visit becomes a child document — a slice of the
              file's pages — queued for the normal read → review → sign path

Every step writes its audit event in the same transaction as the change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import audit
import blobstore
import db
import documents
import render
import splitter

log = logging.getLogger("extraction.splits")

# The longest file the structure scan accepts. Reading is per visit, so this
# bounds only the cheap header scan, not any single model call.
MAX_SPLIT_PAGES = max(1, int(os.getenv("MAX_SPLIT_PAGES") or 400))

PROCESSED = documents.DONE_STATUSES | {"needs_input", "processing"}


class SplitError(ValueError):
    """A split that can't be confirmed as it stands; the message says why."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- scan


async def _read_strips(batch):
    import fields
    return await fields.extract_page_headers(batch, band=render.BAND_FRACTION)


async def read_plan(data: bytes, sha: Optional[str] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(plan, usage) for a file's bytes. Text pages cost nothing; scanned pages
    cost one small-model call per batch of header strips."""
    survey = await render.survey(data, MAX_SPLIT_PAGES)
    if sha:
        await asyncio.to_thread(render.save_thumbnails, sha, survey)
    spent: list[dict[str, Any]] = []
    reads = await splitter.read_pages(survey, _read_strips, spent)
    result = splitter.with_fingerprints(splitter.plan(reads))
    usage = {"prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in spent) or None,
             "completion_tokens": sum(u.get("completion_tokens") or 0 for u in spent) or None,
             "calls": len(spent),
             "strips": sum(1 for p in survey.pages if p.band),
             "text_pages": sum(1 for r in reads if r.source == "text"),
             "model": next((u.get("model") for u in spent if u.get("model")), None)}
    return result, usage


async def get_split(session, document_id: str, version: Optional[int] = None) -> Optional[db.Split]:
    query = select(db.Split).where(db.Split.document_id == document_id)
    if version is not None:
        query = query.where(db.Split.version == version)
    return (await session.execute(query.order_by(db.Split.version.desc()).limit(1))).scalar()


async def scan(document_id: str, version: int, sha: str, data: bytes,
               username: str) -> db.Split:
    """The stored plan for this file version, reading the pages only the first time."""
    async with db.session() as session:
        found = await get_split(session, document_id, version)
        if found is not None and (found.plan or {}).get("rules") == splitter.RULES_VERSION:
            return found
    result, usage = await read_plan(data, sha)
    async with db.session() as session:
        found = await get_split(session, document_id, version)
        document = await session.get(db.Document, document_id)
        await annotate_priors(session, result, document)
        if found is not None:
            found.plan, found.usage, found.status = result, usage, "proposed"
            found.edits = None
            split = found
        else:
            split = db.Split(document_id=document_id, version=version, sha256=sha,
                             created_by=username, plan=result, usage=usage)
            session.add(split)
        await audit.record(session, "split.scanned", username=username, document=document,
                           new_value=f"{len(result['segments'])} visit(s)",
                           details={"version": version, "pages": result["page_count"],
                                    "visits": [{"label": s["label"], "pages": s["pages"]}
                                               for s in result["segments"]],
                                    "ignored": [i["page"] for i in result["ignored"]],
                                    "issues": [i["code"] for i in result["issues"]],
                                    "usage": usage})
        try:
            await session.commit()
        except IntegrityError:
            # Another worker stored the same version's plan first: use theirs.
            await session.rollback()
            split = await get_split(session, document_id, version)
            assert split is not None
        return split


# --------------------------------------------------------------------------- priors


def _same_visit(document: db.Document, key: str) -> bool:
    return splitter.visit_key(document.visit_label or document.visit_name) == key


async def annotate_priors(session, result: dict[str, Any],
                          document: Optional[db.Document]) -> None:
    """Mark each visit in the plan that this patient's records already hold."""
    protocol = splitter.protocol_norm((result.get("protocol") or {}).get("value"))
    screening = splitter.screening_digits((result.get("screening") or {}).get("value"))
    for segment in result.get("segments") or []:
        segment.pop("prior", None)
    if not protocol or not screening:
        return
    raw = splitter.squash((result.get("screening") or {}).get("value"))
    candidates = (await session.execute(
        select(db.Document).where(
            db.Document.screening_no.in_(sorted({screening, raw})),
            db.Document.archived_at.is_(None),
            (db.Document.kind.is_(None)) | (db.Document.kind == "child")))).scalars().all()
    mine = [d for d in candidates
            if splitter.screening_digits(d.screening_no) == screening
            and splitter.protocol_norm(d.protocol_no) == protocol
            and d.status in PROCESSED
            and (document is None or d.id != document.id)]
    for segment in result.get("segments") or []:
        same = [d for d in mine if _same_visit(d, segment["key"])]
        if not same:
            continue
        same.sort(key=lambda d: (d.fingerprint == segment.get("fingerprint"), d.updated_at),
                  reverse=True)
        best = same[0]
        prior = await documents.prior_for(session, best)
        prior.update(status=best.status, filename=best.filename,
                     same_pages=bool(best.fingerprint)
                     and best.fingerprint == segment.get("fingerprint"),
                     own_child=bool(document is not None and best.parent_id == document.id))
        segment["prior"] = prior


# --------------------------------------------------------------------------- decide


def decide(result: dict[str, Any], max_single_pages: int) -> str:
    """What happens to a scanned file next.

    single   one visit and nothing to ask: read it as one document
    whole    no visit could be named, but the file is small enough to be one
             visit: read it as before (the reviewer fills in the header)
    review   anything else: several visits, a visit read before, or a file
             nobody should read blind
    """
    segments = result.get("segments") or []
    if len(segments) == 1 and not segments[0].get("prior"):
        return "single"
    if not segments:
        ignored = result.get("ignored") or []
        visit_less = [i for i in ignored if i["reason"] == "not_a_visit"]
        readable = [i for i in ignored if i["reason"] != "blank_page"]
        if visit_less and len(visit_less) == len(readable):
            return "review"  # only study-wide forms: nothing here belongs to a visit
        if 0 < len(readable) <= max_single_pages:
            return "whole"
    return "review"


def single_pages(result: dict[str, Any]) -> Optional[list[int]]:
    """The pages to read for a single-visit file, or None for all of them."""
    segments = result.get("segments") or []
    if len(segments) != 1:
        return None
    pages = segments[0]["pages"]
    return None if len(pages) == result.get("page_count") else list(pages)


# --------------------------------------------------------------------------- view


def current_plan(split: db.Split) -> dict[str, Any]:
    return splitter.apply_edits(split.plan or {}, split.edits)


def untick_priors(plan_now: dict[str, Any], edits: Optional[dict[str, Any]]) -> None:
    """A visit this patient's records already hold starts unticked, so it is never
    read twice by accident; the reviewer can still tick it (and give a reason)."""
    chosen = (edits or {}).get("selected") or {}
    for segment in plan_now.get("segments") or []:
        prior = segment.get("prior")
        if prior and not prior.get("own_child") and segment["id"] not in chosen:
            segment["selected"] = False


async def children(session, parent_id: str) -> list[db.Document]:
    return list((await session.execute(
        select(db.Document).where(db.Document.parent_id == parent_id)
        .order_by(db.Document.created_at))).scalars().all())


def _child_for(segment: dict[str, Any], kids: list[db.Document]) -> Optional[db.Document]:
    for kid in kids:
        if splitter.visit_key(kid.visit_label or kid.visit_name) == segment["key"]:
            return kid
    return None


async def view(session, parent: db.Document, split: db.Split, user=None) -> dict[str, Any]:
    """Everything the split screen shows: pages, visits, what was sent, what is left."""
    import access

    plan_now = current_plan(split)
    await annotate_priors(session, plan_now, parent)
    untick_priors(plan_now, split.edits)
    kids = await children(session, parent.id)
    scope = access.scope_for(user) if user is not None else None
    visits = []
    for segment in plan_now["segments"]:
        kid = _child_for(segment, kids)
        item = {k: segment.get(k) for k in ("id", "key", "label", "labels_seen", "pages",
                                            "edc_names", "confidence", "flags", "selected",
                                            "added", "renamed", "edited", "fingerprint")}
        prior = segment.get("prior")
        if prior and prior.get("own_child"):
            prior = None  # its own earlier read shows as "sent", below
        if prior and scope is not None and prior.get("document_id"):
            other = await session.get(db.Document, prior["document_id"])
            if other is not None and not scope.sees_document(other):
                prior = {"submitted": prior.get("submitted"), "same_pages": prior.get("same_pages"),
                         "hidden": True}
        item["prior"] = prior
        if kid is not None:
            run = await documents.latest_extraction(session, kid.id)
            item["child"] = {"document_id": kid.id, "status": kid.status,
                             "filename": kid.filename, "flags": kid.flags,
                             "conflicts": kid.conflicts, "confidence": kid.confidence,
                             "same_pages": kid.fingerprint == segment.get("fingerprint"),
                             "extraction": ({"id": run.id, "status": run.status,
                                             "stage": run.stage, "error": run.error}
                                            if run else None)}
        else:
            item["child"] = None
        visits.append(item)
    counts: dict[str, int] = {}
    for kid in kids:
        counts[kid.status] = counts.get(kid.status, 0) + 1
    return {
        "document": documents.doc_dict(parent),
        "split": {"id": split.id, "version": split.version, "status": split.status,
                  "confirmed_at": documents.iso(split.confirmed_at),
                  "confirmed_by": split.confirmed_by,
                  "edits_saved_at": documents.iso(split.edits_saved_at),
                  "usage": split.usage},
        "page_count": plan_now.get("page_count"),
        "protocol": plan_now.get("protocol"),
        "screening": plan_now.get("screening"),
        "issues": plan_now.get("issues"),
        "visits": visits,
        "ignored": plan_now.get("ignored"),
        "pages": [{"page": p["page"], "visit_label": p.get("visit_label"),
                   "source": p.get("source"), "confidence": p.get("confidence"),
                   "protocol_no": p.get("protocol_no"), "screening_no": p.get("screening_no")}
                  for p in plan_now.get("pages") or []],
        "edits": split.edits or {},
        "children": counts,
    }


# --------------------------------------------------------------------------- edit


def clean_edits(edits: dict[str, Any], plan_pages: int) -> dict[str, Any]:
    """Only what apply_edits understands, with page numbers that exist."""
    out: dict[str, Any] = {}
    assign = {}
    for page, target in (edits.get("assign") or {}).items():
        try:
            number = int(page)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= plan_pages and isinstance(target, str) and target:
            assign[str(number)] = target[:40]
    if assign:
        out["assign"] = assign
    labels = {str(k)[:40]: splitter.squash(str(v))[:200]
              for k, v in (edits.get("labels") or {}).items() if splitter.squash(str(v))}
    if labels:
        out["labels"] = labels
    new = []
    for item in (edits.get("new") or [])[:50]:
        sid = re.sub(r"[^a-z0-9]", "", str((item or {}).get("id") or "").lower())[:20]
        label = splitter.squash(str((item or {}).get("label") or ""))[:200]
        if sid and label:
            # Added visits are "n…" so they can never collide with found ones ("s1").
            new.append({"id": sid if sid.startswith("n") else f"n{sid}", "label": label})
    if new:
        out["new"] = new
    selected = {str(k)[:40]: bool(v) for k, v in (edits.get("selected") or {}).items()}
    if selected:
        out["selected"] = selected
    for name in ("protocol_no", "screening_no"):
        value = splitter.squash(str(edits.get(name) or ""))[:64]
        if value:
            out[name] = value
    return out


async def save_edits(session, parent: db.Document, split: db.Split, edits: dict[str, Any],
                     username: str, client_ip: Optional[str] = None) -> None:
    cleaned = clean_edits(edits, (split.plan or {}).get("page_count") or 0)
    if cleaned == (split.edits or {}):
        return
    split.edits = cleaned
    split.edits_saved_at = db.utcnow()
    split.edits_saved_by = username
    await audit.record(session, "split.edited", username=username, document=parent,
                       client_ip=client_ip, details={"split_id": split.id, "edits": cleaned})


# --------------------------------------------------------------------------- confirm


def check(plan_now: dict[str, Any], kids: list[db.Document], reason: str) -> list[dict[str, Any]]:
    """The visits to create now, or a SplitError saying what to fix first."""
    protocol = (plan_now.get("protocol") or {}).get("value")
    screening = (plan_now.get("screening") or {}).get("value")
    if not protocol:
        raise SplitError("Enter the protocol number before reading the visits.")
    if not screening:
        raise SplitError("Enter the screening number before reading the visits.")
    if not splitter.screening_digits(screening):
        raise SplitError("The screening number should contain digits.")
    chosen = [s for s in plan_now["segments"] if s.get("selected")]
    if not chosen:
        raise SplitError("Tick at least one visit to read.")
    keys: dict[str, str] = {}
    for segment in plan_now["segments"]:
        if not segment.get("selected"):
            continue
        if segment["key"] in keys:
            raise SplitError(f"“{keys[segment['key']]}” and “{segment['label']}” are the same "
                             "visit. Move their pages into one visit, or rename one.")
        keys[segment["key"]] = segment["label"]
    for segment in chosen:
        if not splitter.squash(segment.get("label")):
            raise SplitError("Every visit needs a name.")
        kid = _child_for(segment, kids)
        again = kid is not None or bool(segment.get("prior"))
        if again and not reason:
            raise SplitError(f"“{segment['label']}” was processed before. Give a reason for "
                             "reading it again, or untick it.")
    return chosen


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "visit"


def _child_path(parent: db.Document, segment: dict[str, Any]) -> str:
    return f"{parent.path}#visit={_slug(segment['key'])}"


def _child_name(parent: db.Document, segment: dict[str, Any]) -> str:
    """'Visit 3 — CRF_ICR-26-005…': the visit first, so a cut-off name still says
    which visit it is. The full printed visit name is kept on the record."""
    stem = parent.filename.rsplit(".", 1)[0] if "." in parent.filename else parent.filename
    names = segment.get("edc_names") or [segment["label"]]
    short = names[-1] if len(names[-1]) <= 40 else segment["label"][:40]
    return f"{short} — {stem}"[:240]


async def confirm(parent_id: str, edits: Optional[dict[str, Any]], username: str,
                  reason: Optional[str] = None, client_ip: Optional[str] = None,
                  user=None) -> dict[str, Any]:
    """Create (or re-read) a child document for every chosen visit and queue it."""
    reason = (reason or "").strip()
    # Read the file outside the transaction; slicing is CPU work.
    async with db.session() as session:
        parent = await session.get(db.Document, parent_id)
        if parent is None:
            raise SplitError("That file could not be found.", 404)
        split = await get_split(session, parent.id, parent.current_version)
        if split is None:
            raise SplitError("This file hasn't been scanned for visits yet.", 409)
        version = await documents.latest_version(session, parent.id)
        sha = version.sha256
        planned = splitter.apply_edits(split.plan or {}, clean_edits(
            edits if edits is not None else (split.edits or {}),
            (split.plan or {}).get("page_count") or 0))
    data = await blobstore.get(sha)
    # Cut the visits' PDFs now, outside the transaction (it holds the audit chain).
    pieces: dict[tuple[int, ...], bytes] = {}
    for segment in planned.get("segments") or []:
        if segment.get("selected") and segment["pages"]:
            pieces[tuple(segment["pages"])] = await render.slice_pdf(data, segment["pages"])

    async with db.session() as session:
        await audit.lock_chain(session)  # serialises confirms and batch numbers
        parent = await session.get(db.Document, parent_id)
        split = await get_split(session, parent.id, parent.current_version)
        if split is None or split.sha256 != sha:
            raise SplitError("The file changed while you were looking at it. Reload and check "
                             "the visits again.", 409)
        if parent.archived_at is not None:
            raise SplitError("This file is archived. Restore it before reading its visits.", 409)
        if edits is not None:
            await save_edits(session, parent, split, edits, username, client_ip)
        plan_now = current_plan(split)
        await annotate_priors(session, plan_now, parent)
        untick_priors(plan_now, split.edits)
        kids = await children(session, parent.id)
        # A visit already queued or being read is left alone, whatever is ticked.
        busy = []
        for segment in plan_now["segments"]:
            kid = _child_for(segment, kids)
            if kid is not None:
                run = await documents.latest_extraction(session, kid.id)
                if documents.is_active(run):
                    segment["selected"] = False
                    busy.append(segment["label"])
        chosen = check(plan_now, kids, reason)

        protocol = plan_now["protocol"]["value"]
        screening = splitter.screening_digits(plan_now["screening"]["value"])
        number = int((await session.execute(select(func.max(db.Batch.number)))).scalar() or 0) + 1
        batch = db.Batch(number=number, created_by=username, source_id=parent.source_id,
                         label=f"{parent.filename} · {len(chosen)} visit(s)", total=0)
        session.add(batch)
        await session.flush()
        await audit.record(session, "batch.created", username=username, client_ip=client_ip,
                           field=batch.label, reason=reason or None,
                           details={"batch_id": batch.id, "number": number, "files": len(chosen),
                                    "split_id": split.id, "parent_id": parent.id})
        created = []
        for segment in chosen:
            kid = _child_for(segment, kids)
            seed = splitter.header_seed(plan_now, segment)
            same = kid is not None and kid.fingerprint == segment["fingerprint"] \
                and kid.source_pages == segment["pages"]
            if same:
                document = kid
            else:
                piece = pieces.get(tuple(segment["pages"])) \
                    or await render.slice_pdf(data, segment["pages"])
                stored = await blobstore.put(piece)
                ingested = await documents.ingest(
                    session, source_id=parent.source_id, path=_child_path(parent, segment),
                    filename=_child_name(parent, segment), stored=(*stored, len(piece)),
                    pages=len(segment["pages"]), username=username, client_ip=client_ip,
                    reason=reason or None)
                document = ingested.document
            document.kind = "child"
            document.parent_id = parent.id
            document.visit_label = segment["label"]
            document.source_pages = list(segment["pages"])
            document.fingerprint = segment["fingerprint"]
            document.pages = None
            if not await documents.submitted(session, document.id):
                document.protocol_no = protocol
                document.screening_no = screening
                document.visit_name = segment["edc_names"][0] if segment["edc_names"] else segment["label"]
            extraction = await documents.new_extraction(
                session, document, username=username, batch_id=batch.id, client_ip=client_ip,
                reason=reason or None)
            extraction.snapshot = {"meta": {"header_rows": seed, "header_usage": {},
                                            "edc_names": segment["edc_names"]}}
            await audit.record(session, "split.visit_created", username=username,
                               document=document, client_ip=client_ip, reason=reason or None,
                               new_value=segment["label"],
                               details={"parent_id": parent.id, "split_id": split.id,
                                        "pages": segment["pages"], "reused_file": same,
                                        "extraction_id": extraction.id,
                                        "prior": segment.get("prior")})
            created.append({"label": segment["label"], "document_id": document.id,
                            "pages": segment["pages"]})
        skipped = [s for s in plan_now["segments"] if not s.get("selected")]
        for segment in skipped:
            await audit.record(session, "split.visit_skipped", username=username,
                               document=parent, client_ip=client_ip, field=segment["label"],
                               details={"split_id": split.id, "pages": segment["pages"],
                                        "prior": segment.get("prior"),
                                        "busy": segment["label"] in busy})
        batch.total = len(created)
        batch.skipped = len(skipped)
        # What was just sent starts unticked, so a second press never reads it
        # again by accident; ticking it again asks for a reason.
        kept = dict(split.edits or {})
        kept["selected"] = {**(kept.get("selected") or {}),
                            **{segment["id"]: False for segment in chosen}}
        split.edits = kept
        split.status = "confirmed"
        split.confirmed_at = db.utcnow()
        split.confirmed_by = username
        split.reason = reason or split.reason
        parent.kind = "parent"
        parent.status = "split"
        # The run that found the visits has done its job: it no longer waits for input.
        waiting = await documents.latest_extraction(session, parent.id, {"needs_input"})
        if waiting is not None and waiting.stage == "split":
            waiting.status, waiting.stage, waiting.error = "ready", "done", None
            waiting.finished_at = waiting.finished_at or db.utcnow()
        parent.protocol_no = parent.protocol_no or protocol
        parent.screening_no = parent.screening_no or screening
        await audit.record(session, "split.confirmed", username=username, document=parent,
                           client_ip=client_ip, reason=reason or None,
                           new_value=f"{len(created)} visit(s) queued",
                           details={"split_id": split.id, "batch_id": batch.id,
                                    "visits": created,
                                    "skipped": [s["label"] for s in skipped],
                                    "ignored": [i["page"] for i in plan_now["ignored"]]})
        await session.commit()
        return {"batch_id": batch.id, "created": created,
                "skipped": [s["label"] for s in skipped], "busy": busy}


# --------------------------------------------------------------------------- park


async def park_for_review(session, document: db.Document, split: db.Split) -> str:
    """Mark a file as waiting for its split to be confirmed; returns the message."""
    plan_now = current_plan(split)
    document.kind = "parent"
    document.status = "split_review"
    visits = len(plan_now["segments"])
    if visits == 0:
        if any(i["reason"] == "not_a_visit" for i in plan_now["ignored"]):
            return ("No visit pages were found: every page is a form that applies to all "
                    "visits. Open it to check, or assign pages to a visit by hand.")
        return ("No visit names could be read from this file's pages. Open it to assign "
                "pages to visits by hand.")
    if visits == 1:
        return ("This visit was processed before. Open it to choose whether to read it again.")
    return (f"This file holds {visits} visits. Open it to check the visits and choose "
            "which to read.")
