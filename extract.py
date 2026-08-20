"""PDF → images and per-page Gemini VLM extraction."""

from __future__ import annotations

import io
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pdf2image import convert_from_bytes

from schema import PAGE_SCHEMAS, SYSTEM_PROMPT, json_schema_for_page

load_dotenv()

MODEL_NAME = "gemini-2.5-flash"


def pdf_to_pngs(pdf_bytes: bytes, dpi: int = 300) -> List[bytes]:
    """Convert PDF pages to PNG bytes at the given DPI."""
    images = convert_from_bytes(pdf_bytes, dpi=dpi)
    out: List[bytes] = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


def _client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env")
    return genai.Client(api_key=api_key)


def extract_page(page_no: int, png_bytes: bytes) -> dict:
    """
    Call Gemini once for a single page.
    Returns dict of field_key -> {value, confidence}.
    """
    client = _client()
    schema = json_schema_for_page(page_no)
    page_meta = PAGE_SCHEMAS[page_no]
    field_list = ", ".join(f["key"] for f in page_meta["fields"])

    user_text = (
        f"Extract all fields from this CRF page (page {page_no}). "
        f"Fields: {field_list}."
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=SYSTEM_PROMPT),
                    types.Part.from_text(text=user_text),
                    types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )

    text = response.text or "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    # Normalize: ensure every expected key exists
    result = {}
    for f in page_meta["fields"]:
        key = f["key"]
        raw = data.get(key)
        if isinstance(raw, dict) and "value" in raw:
            conf = raw.get("confidence", "low")
            if conf not in ("high", "medium", "low"):
                conf = "low"
            result[key] = {"value": raw.get("value"), "confidence": conf}
        else:
            result[key] = {"value": None, "confidence": "low"}
    return result


def run_extraction(
    pdf_bytes: bytes,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[List[bytes], List[dict]]:
    """
    Convert PDF and extract each page separately.
    on_progress(status, current_page, total_pages)
    Returns (png_list, pages_data) where pages_data is list of
    {page_no, fields}.
    """
    if on_progress:
        on_progress("converting", 0, 0)

    pngs = pdf_to_pngs(pdf_bytes, dpi=300)
    total = len(pngs)
    if total == 0:
        raise ValueError("PDF has no pages")

    pages_data: List[dict] = []
    # Process only first 3 pages (or fewer if PDF is shorter); schema is for 3 pages
    for i, png in enumerate(pngs[:3]):
        page_no = i + 1
        if on_progress:
            on_progress("extracting", page_no, min(total, 3))
        # If PDF has fewer than 3 pages, still use page_no schema if available
        schema_page = page_no if page_no in PAGE_SCHEMAS else max(PAGE_SCHEMAS.keys())
        fields = extract_page(schema_page, png)
        pages_data.append({"page_no": page_no, "fields": fields})

    if on_progress:
        on_progress("validating", min(total, 3), min(total, 3))

    return pngs[:3], pages_data
