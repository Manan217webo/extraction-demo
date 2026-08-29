"""Webo Healthtech — Document Extraction service.

The upstream processing vendor is an implementation detail: every string that can
reach the browser is written in the product's own voice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import llama_cloud
from llama_cloud import AsyncLlamaCloud

import anchors
import cronos
import edc
import fields
import mapping
import vision
import visit_forms
from docx_export import build_docx
from pdf_export import build_crf_pdf

load_dotenv()

# Without this the diagnostics below sit at INFO and never reach the console,
# which is exactly when they are wanted. LOG_LEVEL=DEBUG for more.
logging.basicConfig(
    level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    format="%(levelname)s:     %(name)s: %(message)s",
)
log = logging.getLogger("extraction")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_BYTES = 50 * 1024 * 1024
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
HEADER_PAGE_LIMIT = max(1, int(os.getenv("HEADER_PAGE_LIMIT") or 3))
API_BASE = (os.getenv("LLAMA_CLOUD_BASE_URL") or "https://api.cloud.llamaindex.ai").rstrip("/")

GENERIC_FAILURE = "We couldn't process this document. Please try again."

# Extraction modes, described the way a reviewer thinks about them rather than in
# vendor terminology. `id` is the only value that crosses the vendor boundary.
MODES: list[dict[str, Any]] = [
    {
        "id": "quick",
        "name": "Quick Text",
        "tagline": "Words only",
        "description": "Pulls the plain text straight off the page. Tables and layout are not reconstructed.",
        "best_for": "Clean, typed documents where you only need the wording",
        "credits_per_page": 1,
        "speed": "Seconds",
        "accuracy": 1,
    },
    {
        "id": "standard",
        "name": "Standard",
        "tagline": "Everyday documents",
        "description": "Keeps headings, paragraphs and straightforward tables intact. A balanced default for digital PDFs.",
        "best_for": "Protocols, reports and other typed, text-based files",
        "credits_per_page": 3,
        "speed": "Under a minute",
        "accuracy": 2,
    },
    {
        "id": "high",
        "name": "High Accuracy",
        "tagline": "Typed and printed pages",
        "description": "Reads each page carefully, rebuilding complex tables, forms and printed scans. Handwriting can be misread.",
        "best_for": "Typed protocols, reports, lab printouts and clean scans",
        "credits_per_page": 10,
        "speed": "One to two minutes",
        "accuracy": 3,
        "recommended": True,
    },
    {
        "id": "maximum",
        "name": "Maximum Accuracy",
        "tagline": "Handwritten source documents",
        "description": "The most thorough read available. Needed for handwriting and tick boxes — it reads character-box fields correctly where lighter modes drop or misread digits.",
        "best_for": "Completed CRFs, handwritten notes and poor-quality scans",
        "credits_per_page": 45,
        "speed": "Several minutes",
        "accuracy": 4,
    },
]

MODE_IDS = {mode["id"] for mode in MODES}
CREDITS_PER_PAGE = {mode["id"]: mode["credits_per_page"] for mode in MODES}

# The processing vendor's own tier names never leave this module.
TIER_FOR_MODE = {
    "quick": "fast",
    "standard": "cost_effective",
    "high": "agentic",
    "maximum": "agentic_plus",
}
DEFAULT_MODE = "high"

app = FastAPI(title="Webo Healthtech Document Extraction")

_jobs: dict[str, dict[str, Any]] = {}
_client: Optional[AsyncLlamaCloud] = None
_org_id: Optional[str] = None
_credits_cache: dict[str, Any] = {"at": 0.0, "value": None}
_credits_lock = asyncio.Lock()
_header_inflight: dict[str, asyncio.Task] = {}


# --------------------------------------------------------------------------- config


def _api_key() -> str:
    return (os.getenv("LLAMA_CLOUD_API_KEY") or "").strip()


def _api_key_configured() -> bool:
    key = _api_key()
    return bool(key) and not key.startswith("llx-...") and key.lower() not in {
        "your_api_key",
        "your-api-key",
        "changeme",
    }


def _require_configured() -> None:
    if not _api_key_configured():
        raise HTTPException(
            status_code=503,
            detail="The extraction service isn't configured yet. Please contact your administrator.",
        )


def get_client() -> AsyncLlamaCloud:
    global _client
    _require_configured()
    if _client is None:
        _client = AsyncLlamaCloud(api_key=_api_key())
    return _client


# --------------------------------------------------------------------------- errors


def _friendly(exc: Exception, fallback: str = GENERIC_FAILURE) -> HTTPException:
    """Map an upstream failure to a message that is safe to show a reviewer."""
    log.exception("upstream failure", exc_info=exc)
    if isinstance(exc, llama_cloud.AuthenticationError):
        return HTTPException(
            status_code=502,
            detail="The extraction service rejected our credentials. Please contact your administrator.",
        )
    if isinstance(exc, llama_cloud.RateLimitError):
        return HTTPException(
            status_code=429,
            detail="The service is busy right now. Please wait a moment and try again.",
        )
    if isinstance(exc, llama_cloud.APIStatusError) and exc.status_code == 404:
        return HTTPException(status_code=404, detail="That extraction could not be found.")
    return HTTPException(status_code=502, detail=fallback)


# --------------------------------------------------------------------------- credits


async def _api_get(path: str) -> Any:
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(
            f"{API_BASE}{path}", headers={"Authorization": f"Bearer {_api_key()}"}
        )
        response.raise_for_status()
        return response.json()


async def _organization_id() -> Optional[str]:
    global _org_id
    if _org_id:
        return _org_id
    configured = (os.getenv("EXTRACTION_ORG_ID") or "").strip()
    if configured:
        _org_id = configured
        return _org_id
    orgs = await _api_get("/api/v1/organizations")
    if isinstance(orgs, list) and orgs:
        _org_id = orgs[0].get("id")
    return _org_id


async def _load_credits() -> dict[str, Any]:
    """Read the real credit balance for the account behind this deployment."""
    org_id = await _organization_id()
    if not org_id:
        return {"available": False}

    data = await _api_get(f"/api/v1/organizations/{org_id}/usage")
    usage = data.get("usage") or {}
    plan = data.get("plan") or {}
    grants = usage.get("active_free_credits_usage") or []

    remaining = sum(g.get("remaining_balance") or 0 for g in grants)
    total = sum(g.get("starting_balance") or 0 for g in grants)

    if not grants:
        # Paid plans bill per use rather than drawing down a grant.
        recurring = plan.get("recurring_credits") or []
        total = sum(r.get("credit_amount") or 0 for r in recurring)
        if not total:
            return {"available": False}
        remaining = total

    period = plan.get("current_billing_period") or {}
    return {
        "available": True,
        "remaining": round(remaining),
        "total": round(total),
        "used": round(max(total - remaining, 0)),
        "renews_on": period.get("end_date") or (grants[0].get("expires_at") if grants else None),
    }


async def _credits(force: bool = False) -> dict[str, Any]:
    """Cached credit lookup so polling never hammers the billing endpoint."""
    if not _api_key_configured():
        return {"available": False}
    async with _credits_lock:
        fresh = time.monotonic() - _credits_cache["at"] < 15
        if _credits_cache["value"] is not None and fresh and not force:
            return _credits_cache["value"]
        try:
            value = await _load_credits()
        except Exception as exc:  # a balance we can't read must not break extraction
            log.warning("credit lookup failed: %s", exc)
            value = _credits_cache["value"] or {"available": False}
        _credits_cache.update(at=time.monotonic(), value=value)
        return value


# --------------------------------------------------------------------------- parsing


def _count_pages(data: bytes) -> Optional[int]:
    """Best-effort page count read straight from the PDF, for the cost estimate."""
    try:
        counts = [
            int(match)
            for match in re.findall(rb"/Type\s*/Pages\b[^>]{0,400}?/Count\s+(\d+)", data, re.S)
        ]
        if counts:
            return max(counts)
        found = len(re.findall(rb"/Type\s*/Page(?![s/\w])", data))
        return found or None
    except Exception:
        return None


def _expand_for(tier: str, with_items: bool = False) -> list[str]:
    expand = ["text_full", "text", "usage"]
    if tier != "fast":
        expand.extend(["markdown_full", "markdown"])
        # `items` carries the per-page layout with bounding boxes, which is what
        # lets a mapped value be drawn back onto the original page. It is far
        # larger than the text and is needed exactly once, so it is never part of
        # a status poll — only of the single fetch the mapping stage makes.
        if with_items:
            expand.append("items")
    return expand


def _job_status(result: Any) -> str:
    job = getattr(result, "job", None) or result
    return str(getattr(job, "status", "") or "").upper()


def _job_error(result: Any) -> Optional[str]:
    job = getattr(result, "job", None) or result
    return getattr(job, "error_message", None) or getattr(job, "error", None)


# The reader escapes emphasis markers it did not intend as literal text, so a bold
# form label arrives as "\\*\\*Protocol No.\\*\\*" and would render with visible asterisks.
_OVER_ESCAPED = re.compile(r"\\([*_])")


def _tidy(markdown: Optional[str]) -> str:
    return _OVER_ESCAPED.sub(r"\1", markdown or "")


def _pages_from(container: Any, field: str) -> list[dict[str, Any]]:
    if not container:
        return []
    out = []
    for index, page in enumerate(getattr(container, "pages", None) or []):
        content = _tidy(getattr(page, field, None))
        if not content and getattr(page, "error", None):
            content = f"_This page could not be read._"
        out.append(
            {
                "page_number": getattr(page, "page_number", None) or index + 1,
                "content": content,
            }
        )
    return out


def _layout_pages(result: Any) -> list[dict[str, Any]]:
    """Plain-dict view of the parser's layout items, bounding boxes included."""
    container = getattr(result, "items", None)
    out: list[dict[str, Any]] = []
    for page in getattr(container, "pages", None) or []:
        dump = getattr(page, "model_dump", None)
        try:
            out.append(dump(mode="json") if dump else dict(page))
        except Exception as exc:  # a page we cannot read simply cannot be anchored
            log.warning("layout page skipped: %s", exc)
    _dump_layout(out)
    return out


def _dump_layout(pages: list[dict[str, Any]]) -> None:
    """Write the raw layout to disk when DEBUG_LAYOUT_DIR is set.

    Highlight placement can only be reasoned about against what the parser
    actually returns — the box shapes, whether they carry character ranges, how a
    table is split. Keeping a copy means that work does not cost a re-parse.
    """
    directory = (os.getenv("DEBUG_LAYOUT_DIR") or "").strip()
    if not directory or not pages:
        return
    try:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / "layout.json"
        path.write_text(json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("layout written to %s", path)
    except Exception as exc:
        log.warning("could not write layout dump: %s", exc)


def _serialize(result: Any, meta: dict[str, Any]) -> dict[str, Any]:
    job = getattr(result, "job", None)
    usage = getattr(job, "usage", None) if job is not None else None
    markdown_full = _tidy(getattr(result, "markdown_full", None))
    text_full = getattr(result, "text_full", None) or ""
    markdown_pages = _pages_from(getattr(result, "markdown", None), "markdown")
    text_pages = _pages_from(getattr(result, "text", None), "text")

    if not markdown_full and markdown_pages:
        markdown_full = "\n\n".join(p["content"] for p in markdown_pages if p["content"])
    if not text_full and text_pages:
        text_full = "\n\n".join(p["content"] for p in text_pages if p["content"])

    status = _job_status(result) or "UNKNOWN"
    error = _job_error(result)
    return {
        "job_id": meta.get("job_id") or getattr(job, "id", None),
        "status": status,
        "filename": meta.get("filename"),
        "mode": meta.get("mode"),
        "error": GENERIC_FAILURE if error else None,
        "page_count": len(markdown_pages) or len(text_pages),
        "credits_used": getattr(usage, "credits", None) if usage is not None else None,
        "markdown": markdown_full,
        "text": text_full,
        "pages": markdown_pages or text_pages,
    }


# --------------------------------------------------------------------------- routes


_ASSET_REF = re.compile(r'(src|href)="(/static/[^"?]+)"')


def _asset_stamp() -> str:
    """A token that changes whenever any local asset does.

    Without it a browser holding an older bundle shows an older flow, and there
    is nothing on the page to say the code it is running is not the code on
    disk — a stale build is indistinguishable from a bug.
    """
    newest = 0.0
    for path in STATIC_DIR.rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return f"{int(newest)}"


@app.get("/")
async def index() -> Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    stamp = _asset_stamp()
    html = _ASSET_REF.sub(rf'\1="\2?v={stamp}"', html)
    # The same stamp on `window`, so a review saved by an older build is not
    # restored as if it were current.
    html = html.replace("<head>", f"<head><script>window.APP_BUILD={stamp!r};</script>", 1)
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/api/session")
async def session() -> dict[str, Any]:
    """Everything the page needs on load: readiness, modes and the credit balance."""
    connector = cronos.get_connector()
    return {
        "ready": _api_key_configured(),
        "modes": MODES,
        "credits": await _credits(),
        "max_file_mb": MAX_BYTES // (1024 * 1024),
        "mapping": {
            "ready": fields.configured(),
            "model": fields.model_name() if fields.configured() else None,
            "cronos": {"connector": connector.name,
                       "live": getattr(connector, "live", False)},
        },
    }


@app.get("/api/credits")
async def credits(refresh: bool = False) -> dict[str, Any]:
    return await _credits(force=refresh)


@app.post("/api/documents")
async def create_extraction(
    file: UploadFile = File(...),
    mode: str = Form(DEFAULT_MODE),
) -> dict[str, Any]:
    mode = (mode or DEFAULT_MODE).strip().lower()
    if mode not in MODE_IDS:
        raise HTTPException(status_code=400, detail="Please choose an extraction mode.")
    tier = TIER_FOR_MODE[mode]

    filename = file.filename or "document.pdf"
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="That file appears to be empty.")
    if len(contents) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDFs must be {MAX_BYTES // (1024 * 1024)} MB or smaller.",
        )

    client = get_client()
    try:
        job = await client.parsing.create(
            upload_file=(filename, contents, file.content_type or "application/pdf"),
            tier=tier,
            version="latest",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _friendly(exc, "We couldn't start the extraction. Please try again.") from exc

    pages = _count_pages(contents)
    _jobs[job.id] = {"job_id": job.id, "filename": filename, "mode": mode,
                     "tier": tier, "pages": pages}
    return {
        "job_id": job.id,
        "filename": filename,
        "mode": mode,
        "page_count": pages,
        "status": getattr(job, "status", "PENDING") or "PENDING",
    }


@app.get("/api/documents/{job_id}")
async def extraction_status(job_id: str) -> dict[str, Any]:
    meta = _jobs.setdefault(
        job_id, {"job_id": job_id, "mode": DEFAULT_MODE, "filename": None}
    )
    tier = meta.get("tier") or TIER_FOR_MODE[DEFAULT_MODE]
    client = get_client()

    try:
        result = await client.parsing.get(job_id=job_id, expand=_expand_for(tier))
    except llama_cloud.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="That extraction could not be found.") from exc
    except llama_cloud.APIStatusError as exc:
        if tier == "fast":
            raise _friendly(exc) from exc
        try:  # some modes return no markdown; fall back to text only
            result = await client.parsing.get(job_id=job_id, expand=["text_full", "text", "usage"])
        except Exception as inner:
            raise _friendly(inner) from inner
    except Exception as exc:
        raise _friendly(exc) from exc

    payload = _serialize(result, meta)
    if payload["status"] != "COMPLETED":
        payload.update(markdown="", text="", pages=[], page_count=0)
        if meta.get("pages"):
            payload["page_count"] = meta["pages"]
    return payload


class WordExport(BaseModel):
    filename: str = "document"
    markdown: str = ""


@app.post("/api/exports/word")
async def export_word(payload: WordExport) -> Response:
    stem = re.sub(r"[\\/:*?\"<>|]+", "", Path(payload.filename or "document").stem).strip()
    stem = stem or "document"
    try:
        data = build_docx(payload.markdown, stem)
    except Exception as exc:
        log.exception("word export failed", exc_info=exc)
        raise HTTPException(status_code=500, detail="We couldn't build the Word file.") from exc
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{stem}.docx\"; "
                f"filename*=UTF-8''{quote(stem)}.docx"
            )
        },
    )



# ----------------------------------------------------------------- field mapping


def _document_meta(job_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "filename": meta.get("filename"),
        "page_count": meta.get("pages"),
        "mode": meta.get("mode"),
    }


def _job_meta(job_id: str) -> dict[str, Any]:
    return _jobs.setdefault(
        job_id, {"job_id": job_id, "mode": DEFAULT_MODE, "filename": None}
    )


def _parse_lock(meta: dict[str, Any]) -> asyncio.Lock:
    """One fetch per job even when the header and the layout are wanted at once."""
    lock = meta.get("lock")
    if lock is None:
        lock = meta["lock"] = asyncio.Lock()
    return lock


async def _fetch(job_id: str, meta: dict[str, Any], with_items: bool) -> Any:
    tier = meta.get("tier") or TIER_FOR_MODE[DEFAULT_MODE]
    client = get_client()
    started = time.monotonic()
    try:
        result = await client.parsing.get(
            job_id=job_id, expand=_expand_for(tier, with_items=with_items)
        )
    except llama_cloud.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="That extraction could not be found.") from exc
    except Exception as exc:
        raise _friendly(exc) from exc

    if _job_status(result) != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail="That document hasn't finished extracting yet. Please try again.",
        )
    log.info("fetched %s (items=%s) in %.1fs", job_id, with_items,
             time.monotonic() - started)
    return result


async def _parse_artifacts(job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Text and page layout for a finished job, fetched once and kept.

    This is the only place the layout items are pulled, because they dwarf the
    text — roughly a third of a second per page — and are needed only to draw
    values back onto the original.
    """
    meta = _job_meta(job_id)
    async with _parse_lock(meta):
        if meta.get("parse"):
            return meta, meta["parse"]

        result = await _fetch(job_id, meta, with_items=True)
        started = time.monotonic()
        payload = _serialize(result, meta)
        meta["parse"] = {
            "markdown": payload["markdown"],
            "pages": payload["pages"],
            "items": _layout_pages(result),
        }
        log.info(
            "cached parse for %s: %s page(s), %s layout page(s), prepared in %.1fs",
            job_id, len(meta["parse"]["pages"]), len(meta["parse"]["items"]),
            time.monotonic() - started,
        )
        return meta, meta["parse"]


async def _header_pages(job_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """The first few pages of text — all the header block can be on.

    Fetched without the layout items so it comes back in about a second, which
    lets the header be read while the layout is still downloading.
    """
    if meta.get("parse"):
        return (meta["parse"].get("pages") or [])[:HEADER_PAGE_LIMIT]

    result = await _fetch(job_id, meta, with_items=False)
    pages = _pages_from(getattr(result, "markdown", None), "markdown") \
        or _pages_from(getattr(result, "text", None), "text")
    if not pages:
        whole = _tidy(getattr(result, "markdown_full", None)) \
            or getattr(result, "text_full", None) or ""
        pages = [{"page_number": 1, "content": whole}]
    return pages[:HEADER_PAGE_LIMIT]


def _header_response(job_id: str, meta: dict[str, Any], header: dict[str, Any],
                     parse: dict[str, Any], document: str, pages: list[dict[str, Any]],
                     usage: dict[str, Any]) -> dict[str, Any]:
    index = anchors.PageIndex(parse.get("items") or [])
    return {
        "document": _document_meta(job_id, meta),
        "header": header,
        "highlights": mapping.highlights(header),
        "summary": header["summary"],
        "anchoring": bool(index),
        "truncated": fields.was_truncated(document),
        "header_pages_read": len(pages),
        "usage": usage,
    }


async def _read_header(job_id: str) -> dict[str, Any]:
    """Step one: what we believe the document's header block says."""
    meta = _job_meta(job_id)
    started = time.monotonic()

    if "header_rows" in meta and meta.get("parse"):
        parse = meta["parse"]
        pages = (parse.get("pages") or [])[:HEADER_PAGE_LIMIT]
        document = fields.document_for_model(pages, "")
        header = mapping.build_header(
            meta["header_rows"], anchors.PageIndex(parse.get("items") or [])
        )
        log.info("header cache hit for %s", job_id)
        return _header_response(
            job_id, meta, header, parse, document, pages, meta.get("header_usage") or {}
        )

    # The layout download is the slow half and the model call does not need it,
    # so the two run together rather than one after the other.
    layout = asyncio.create_task(_parse_artifacts(job_id))
    try:
        pages = await _header_pages(job_id, meta)
        document = fields.document_for_model(pages, "")
        log.info("header prompt for %s: %s page(s), %s chars",
                 job_id, len(pages), len(document))

        read_started = time.monotonic()
        try:
            result = await fields.extract_header(document)
        except fields.ExtractionUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        log.info("header read for %s in %.1fs", job_id, time.monotonic() - read_started)

        meta, parse = await layout
    except BaseException:
        layout.cancel()
        raise

    header = mapping.build_header(
        result["values"], anchors.PageIndex(parse.get("items") or [])
    )
    meta["header_rows"] = result["values"]
    meta["header_usage"] = result.get("usage", {})
    log.info("header stage for %s took %.1fs", job_id, time.monotonic() - started)

    return _header_response(
        job_id, meta, header, parse, document, pages, result.get("usage", {})
    )


@app.post("/api/documents/{job_id}/header")
async def read_header(job_id: str) -> dict[str, Any]:
    """Join an in-flight read so a refresh does not stack another OpenAI wait."""
    existing = _header_inflight.get(job_id)
    if existing is not None and not existing.done():
        log.info("joining in-flight header read for %s", job_id)
        return await asyncio.shield(existing)
    task = asyncio.create_task(_read_header(job_id))
    _header_inflight[job_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _header_inflight.get(job_id) is task:
            _header_inflight.pop(job_id, None)


@app.get("/api/cronos/forms")
async def cronos_forms(protocol_no: Optional[str] = None) -> dict[str, Any]:
    connector = cronos.get_connector()
    try:
        forms = await connector.list_forms(protocol_no)
    except cronos.CronosUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="We couldn't reach Cronos. Please try again."
        ) from exc
    return {"connector": connector.name, "live": getattr(connector, "live", False),
            "protocol_no": protocol_no, "forms": forms}


@app.get("/api/cronos/forms/{form_id}")
async def cronos_form(form_id: str) -> dict[str, Any]:
    try:
        form = await cronos.get_connector().get_form(form_id)
    except cronos.CronosUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="We couldn't reach Cronos. Please try again."
        ) from exc
    if not form:
        raise HTTPException(status_code=404, detail="That form could not be found in Cronos.")
    return form


class MapRequest(BaseModel):
    form_id: str
    header: dict[str, Any] = {}


def _apply_header_edits(header: dict[str, Any], confirmed: dict[str, Any]) -> None:
    """Overlay the reviewer's confirmed header onto what was extracted."""
    for group in header.get("groups") or []:
        for field in group.get("fields") or []:
            if field["field_id"] not in confirmed:
                continue
            value = confirmed[field["field_id"]]
            value = value.strip() if isinstance(value, str) else value
            value = None if value in ("", None) else value
            if str(value) == str(field.get("value")):
                continue
            field["value"] = value
            field["status"] = "manual" if field.get("value") is None else "edited"


@app.post("/api/documents/{job_id}/map")
async def map_to_form(job_id: str, request: MapRequest) -> dict[str, Any]:
    """Step two: read the document against the chosen Cronos form."""
    meta, parse = await _parse_artifacts(job_id)

    try:
        form = await cronos.get_connector().get_form(request.form_id)
    except cronos.CronosUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="We couldn't reach Cronos. Please try again."
        ) from exc
    if not form:
        raise HTTPException(status_code=404, detail="That form could not be found in Cronos.")

    document = fields.document_for_model(parse.get("pages") or [], parse.get("markdown") or "")
    index = anchors.PageIndex(parse.get("items") or [])
    log.info("form prompt for %s: %s page(s), %s chars",
             job_id, len(parse.get("pages") or []), len(document))

    header = mapping.build_header(meta.get("header_rows") or [], index)
    _apply_header_edits(header, request.header)
    confirmed = mapping.header_values(header["groups"])

    started = time.monotonic()
    try:
        result = await fields.extract_form(document, form, confirmed)
    except fields.ExtractionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    log.info("form read for %s in %.1fs (%s value(s), %s repaired, %s dropped)",
             job_id, time.monotonic() - started, len(result["values"]),
             result.get("repaired", 0), result.get("dropped", 0))

    payload = {
        "document": _document_meta(job_id, meta),
        "header": header,
        "form": mapping.build_form(form, result["values"], index),
    }
    mapping.revalidate(payload)
    payload["anchoring"] = bool(index)
    payload["truncated"] = fields.was_truncated(document)
    payload["dropped"] = result.get("dropped", 0)
    payload["usage"] = result.get("usage", {})
    return payload


# --------------------------------------------------------------------------- EDC visit


class VisitRequest(BaseModel):
    protocol_no: str
    screening_no: str
    visit_name: str


def _visit_view(definition: dict[str, Any]) -> dict[str, Any]:
    """The definition as the browser needs it — without the write-back map."""
    return {
        "form_id": definition["form_id"],
        "form_name": definition["form_name"],
        "form_description": definition["form_description"],
        "visit": definition["visit"],
        "sections": definition["sections"],
        "crfs": [
            {"crfId": crf["crfId"], "crfName": crf["crfName"],
             "section_id": crf["section_id"], "field_count": len(crf["fields"]),
             "row_count": len(crf["rows"]), "matched": crf["matched"],
             "matched_form": crf["matched_form"], "match_score": crf["match_score"]}
            for crf in definition["edc"]["crfs"]
        ],
    }


@app.post("/api/documents/{job_id}/visit")
async def load_visit(job_id: str, request: VisitRequest) -> dict[str, Any]:
    """Step two: the CRFs the EDC holds for the confirmed visit."""
    meta = _job_meta(job_id)
    try:
        visit = await edc.get_visit(request.protocol_no, request.screening_no,
                                    request.visit_name)
    except edc.EdcUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    definition = visit_forms.build_definition(visit, cronos.local_forms())
    meta["visit_definition"] = definition
    log.info("visit %s/%s/%s: %s CRF(s)", request.protocol_no, request.screening_no,
             request.visit_name, len(definition["sections"]))
    return {"document": _document_meta(job_id, meta), "form": _visit_view(definition),
            "unsaveable": visit_forms.unsaveable(definition)}


class VisitMapRequest(BaseModel):
    header: dict[str, Any] = {}


@app.post("/api/documents/{job_id}/visit/map")
async def map_to_visit(job_id: str, request: VisitMapRequest) -> dict[str, Any]:
    """Step three: read the document against the CRFs the EDC returned."""
    meta, parse = await _parse_artifacts(job_id)
    definition = meta.get("visit_definition")
    if not definition:
        raise HTTPException(
            status_code=409,
            detail="Load the visit from the EDC before mapping the document.",
        )

    document = fields.document_for_model(parse.get("pages") or [], parse.get("markdown") or "")
    index = anchors.PageIndex(parse.get("items") or [])

    header = mapping.build_header(meta.get("header_rows") or [], index)
    _apply_header_edits(header, request.header)
    confirmed = mapping.header_values(header["groups"])

    started = time.monotonic()
    try:
        result = await fields.extract_form(document, definition, confirmed)
    except fields.ExtractionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    log.info("visit read for %s in %.1fs (%s value(s))", job_id,
             time.monotonic() - started, len(result["values"]))

    payload = {
        "document": _document_meta(job_id, meta),
        "header": header,
        "form": mapping.build_form(definition, result["values"], index),
    }
    mapping.revalidate(payload)
    payload["anchoring"] = bool(index)
    payload["truncated"] = fields.was_truncated(document)
    payload["usage"] = result.get("usage", {})
    return payload


class VisitSaveRequest(BaseModel):
    payload: dict[str, Any]
    # Field key -> {"base64Data": ..., "contentType": ...}: the crop of the page
    # the value was read from, cut client-side where the rendered PDF already is.
    crops: dict[str, dict[str, Any]] = {}
    dry_run: bool = False


@app.post("/api/documents/{job_id}/visit/save")
async def save_visit(job_id: str, request: VisitSaveRequest) -> dict[str, Any]:
    """Step four: send the reviewed values, and their source crops, to the EDC."""
    meta = _job_meta(job_id)
    definition = meta.get("visit_definition")
    if not definition:
        raise HTTPException(
            status_code=409, detail="Load the visit from the EDC before saving."
        )

    reviewed = mapping.revalidate(request.payload)
    body, warnings = visit_forms.build_save(definition, reviewed.get("form") or {},
                                            request.crops)

    refused = edc.check_images(body["crfs"])
    if refused:
        raise HTTPException(status_code=400, detail=refused)

    counts = {
        "fields": sum(len(c["fields"]) for c in body["crfs"]),
        "values": sum(1 for c in body["crfs"] for f in c["fields"] if f["value"]),
        "images": sum(len(c["images"]) for c in body["crfs"]),
    }
    log.info("visit save for %s: %s crop(s) received, %s image(s) attached, %s value(s)",
             job_id, len(request.crops), counts["images"], counts["values"])
    if request.crops and not counts["images"]:
        log.warning("visit save for %s: crop keys did not match any field: %s",
                    job_id, list(request.crops)[:20])
    if request.dry_run:
        log.info("visit save for %s: dry run, nothing sent", job_id)
        return {"sent": False, "dry_run": True, "counts": counts,
                "warnings": warnings, "payload": body}

    try:
        result = await edc.save_visit(body)
    except edc.EdcUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log.info("visit save for %s: %s value(s), %s image(s)", job_id,
             counts["values"], counts["images"])
    return {"sent": True, "counts": counts, "warnings": warnings, "result": result}


class LocateRequest(BaseModel):
    # Page number -> a data URL of that page rendered as an image. The browser
    # renders them, because the PDF never reaches the server after the parse.
    pages: dict[str, str] = {}
    targets: list[dict[str, Any]] = []


@app.post("/api/documents/{job_id}/locate")
async def locate_fields(job_id: str, request: LocateRequest) -> dict[str, Any]:
    """Place values on the page (OpenAI vision or Tesseract)."""
    if not vision.configured():
        raise HTTPException(
            status_code=503,
            detail="Box refinement isn't configured yet. Please contact your administrator.",
        )
    started = time.monotonic()
    # Keep the exact request when asked, so a placement can be replayed and
    # its raw model boxes inspected without another trip through the browser.
    dump = (os.getenv("DEBUG_LAYOUT_DIR") or "").strip()
    if dump:
        try:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / "locate_pages.json").write_text(json.dumps(request.pages))
            (Path(dump) / "locate_targets.json").write_text(json.dumps(request.targets))
        except Exception as exc:
            log.warning("could not dump locate request: %s", exc)
    found = await vision.locate(request.pages, request.targets)
    log.info("%s locate for %s: %s/%s in %.1fs", vision.model_name(), job_id,
             len(found), len(request.targets), time.monotonic() - started)
    return {"model": vision.model_name(), "located": found,
            "requested": len(request.targets)}


class CrfExport(BaseModel):
    payload: dict[str, Any]
    filename: str = "case-report-form"


def _export_stem(name: str, payload: dict[str, Any]) -> str:
    header = {
        field["field_id"]: field.get("value")
        for group in (payload.get("header") or {}).get("groups") or []
        for field in group.get("fields") or []
    }
    parts = [header.get("protocol_no"), header.get("subject_no"), header.get("visit_name")]
    stem = " ".join(str(part) for part in parts if part) or Path(name or "crf").stem
    stem = re.sub(r"[\\/:*?\"<>|]+", "", stem).strip()
    return stem or "case-report-form"


@app.post("/api/exports/crf-pdf")
async def export_crf_pdf(request: CrfExport) -> Response:
    payload = mapping.revalidate(request.payload)
    stem = _export_stem(request.filename, payload)
    try:
        data = build_crf_pdf(payload)
    except Exception as exc:
        log.exception("crf pdf export failed", exc_info=exc)
        raise HTTPException(status_code=500, detail="We couldn't build the PDF.") from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{stem}.pdf"; '
                f"filename*=UTF-8''{quote(stem)}.pdf"
            )
        },
    )


@app.post("/api/exports/crf-json")
async def export_crf_json(request: CrfExport) -> Response:
    payload = mapping.revalidate(request.payload)
    stem = _export_stem(request.filename, payload)
    data = json.dumps(mapping.for_export(payload), indent=2, ensure_ascii=False)
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{stem}.json"; '
                f"filename*=UTF-8''{quote(stem)}.json"
            )
        },
    )


class Submission(BaseModel):
    form_id: str
    payload: dict[str, Any]


@app.post("/api/cronos/submissions")
async def submit_to_cronos(request: Submission) -> dict[str, Any]:
    payload = mapping.revalidate(request.payload)
    try:
        return await cronos.get_connector().submit(
            request.form_id, mapping.for_export(payload)
        )
    except cronos.CronosUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="Cronos didn't accept the submission. Please try again."
        ) from exc


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    """Keep the browser off a cached build of the app outside production.

    The served file and the one on disk drifting apart is indistinguishable from
    a bug in the app itself — an old bundle shows an old flow and nothing says
    why. Railway serves behind a CDN where caching is wanted, so the header only
    goes on locally.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/") and not os.getenv("RAILWAY_ENVIRONMENT"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT"))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=not on_railway,
    )
