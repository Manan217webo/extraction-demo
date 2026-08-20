"""FastAPI app: CRF extraction prototype."""

from __future__ import annotations

import threading
import traceback
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from extract import run_extraction
from schema import PAGE_SCHEMAS
from validate import validate

app = FastAPI(title="CRF Extraction Prototype")

# In-memory job store
jobs: Dict[str, Dict[str, Any]] = {}


class ExportBody(BaseModel):
    fields: Dict[str, Any]
    pages: Optional[List[Dict[str, Any]]] = None


def _new_job(filename: str) -> str:
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "filename": filename,
        "status": "queued",  # queued | converting | extracting | validating | done | error
        "current_page": 0,
        "total_pages": 3,
        "error": None,
        "pages": [],  # [{page_no, fields}]
        "images": {},  # page_no -> png bytes
        "validations": [],
        "pdf_bytes": None,
    }
    return job_id


def _run_job(job_id: str, pdf_bytes: bytes) -> None:
    job = jobs[job_id]

    def on_progress(status: str, current: int, total: int) -> None:
        job["status"] = status
        job["current_page"] = current
        if total:
            job["total_pages"] = total

    try:
        pngs, pages_data = run_extraction(pdf_bytes, on_progress=on_progress)
        job["images"] = {i + 1: png for i, png in enumerate(pngs)}
        # Pad schema pages if PDF had fewer pages (keep empty fields)
        job["pages"] = pages_data
        job["validations"] = validate(pages_data)
        job["status"] = "done"
        job["error"] = None
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        job["traceback"] = traceback.format_exc()


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "Empty file")

    job_id = _new_job(file.filename)
    jobs[job_id]["pdf_bytes"] = pdf_bytes
    # Run in background thread so polling works
    t = threading.Thread(target=_run_job, args=(job_id, pdf_bytes), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    pages_out = []
    for p in job["pages"]:
        page_no = p["page_no"]
        schema = PAGE_SCHEMAS.get(page_no, {})
        pages_out.append(
            {
                "page_no": page_no,
                "title": schema.get("title", f"Page {page_no}"),
                "image_url": f"/api/jobs/{job_id}/image/{page_no}",
                "schema_fields": schema.get("fields", []),
                "fields": p["fields"],
            }
        )

    # While extracting, expose schema for pages not yet done so UI can show progress
    return {
        "job_id": job_id,
        "filename": job["filename"],
        "status": job["status"],
        "current_page": job["current_page"],
        "total_pages": job["total_pages"],
        "error": job["error"],
        "pages": pages_out,
        "validations": job["validations"],
        "schema": {
            str(k): {
                "page_no": v["page_no"],
                "title": v["title"],
                "fields": v["fields"],
            }
            for k, v in PAGE_SCHEMAS.items()
        },
    }


@app.get("/api/jobs/{job_id}/image/{page_no}")
async def get_image(job_id: str, page_no: int):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    png = job["images"].get(page_no)
    if not png:
        raise HTTPException(404, "Image not ready")
    return Response(content=png, media_type="image/png")


@app.post("/api/jobs/{job_id}/export")
async def export_job(job_id: str, body: ExportBody):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    # Re-validate against edited fields if pages provided
    pages = body.pages
    if pages is None:
        # Rebuild pages from edited flat fields + original page structure
        pages = []
        for p in job["pages"]:
            new_fields = {}
            for k in p["fields"]:
                if k in body.fields:
                    new_fields[k] = body.fields[k]
                else:
                    new_fields[k] = p["fields"][k]
            pages.append({"page_no": p["page_no"], "fields": new_fields})
    else:
        # Normalize pages to have fields with value/confidence
        normalized = []
        for p in pages:
            fields = {}
            for k, item in p.get("fields", {}).items():
                if isinstance(item, dict) and "value" in item:
                    fields[k] = item
                else:
                    fields[k] = {"value": item, "confidence": "high"}
            normalized.append({"page_no": p["page_no"], "fields": fields})
        pages = normalized

    validations = validate(pages)

    # Flatten values for export convenience
    flat_values = {}
    for p in pages:
        for k, item in p["fields"].items():
            flat_values[k] = item.get("value") if isinstance(item, dict) else item

    return JSONResponse(
        {
            "filename": job["filename"],
            "job_id": job_id,
            "fields": flat_values,
            "pages": pages,
            "validations": validations,
        }
    )


@app.post("/api/jobs/{job_id}/rerun")
async def rerun(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    pdf_bytes = job.get("pdf_bytes")
    if not pdf_bytes:
        raise HTTPException(400, "Original PDF not available")
    job["status"] = "queued"
    job["current_page"] = 0
    job["error"] = None
    job["pages"] = []
    job["images"] = {}
    job["validations"] = []
    t = threading.Thread(target=_run_job, args=(job_id, pdf_bytes), daemon=True)
    t.start()
    return {"job_id": job_id}


# Static files last so API routes take precedence
app.mount("/", StaticFiles(directory="static", html=True), name="static")
