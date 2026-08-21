"""Webo Healthtech — Document Extraction service.

The upstream processing vendor is an implementation detail: every string that can
reach the browser is written in the product's own voice.
"""

from __future__ import annotations

import asyncio
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

from docx_export import build_docx

load_dotenv()

log = logging.getLogger("extraction")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_BYTES = 50 * 1024 * 1024
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
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
        "recommended": True,
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


def _expand_for(tier: str) -> list[str]:
    expand = ["text_full", "text", "usage"]
    if tier != "fast":
        expand.extend(["markdown_full", "markdown"])
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/session")
async def session() -> dict[str, Any]:
    """Everything the page needs on load: readiness, modes and the credit balance."""
    return {
        "ready": _api_key_configured(),
        "modes": MODES,
        "credits": await _credits(),
        "max_file_mb": MAX_BYTES // (1024 * 1024),
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
    meta = _jobs.get(job_id, {"job_id": job_id, "mode": DEFAULT_MODE, "filename": None})
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
