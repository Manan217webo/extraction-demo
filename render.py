"""Render an uploaded PDF to page images on the server.

With the vision reader there is no parsing service: the model reads each page
from its image. The images are rendered once at upload, the way the browser's
pdf.js renders them (2x, JPEG), and kept with the job so the header read, the
form read and the box locator all look at the same pictures.
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading

import pypdfium2 as pdfium

SCALE = 2.0
QUALITY = 82

# pdfium is not thread-safe; uploads render one at a time per worker.
_lock = threading.Lock()


class RenderError(ValueError):
    """The upload is not a PDF we can open, or has too many pages."""


def _render(data: bytes, max_pages: int) -> dict[str, str]:
    with _lock:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise RenderError("That file couldn't be opened as a PDF.") from exc
        try:
            count = len(pdf)
            if count == 0:
                raise RenderError("That PDF has no pages.")
            if count > max_pages:
                raise RenderError(f"PDFs must be {max_pages} pages or fewer.")
            out: dict[str, str] = {}
            for index in range(count):
                page = pdf[index]
                try:
                    image = page.render(scale=SCALE).to_pil().convert("RGB")
                finally:
                    page.close()
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=QUALITY)
                out[str(index + 1)] = (
                    "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
                )
            return out
        finally:
            pdf.close()


async def page_images(data: bytes, max_pages: int) -> dict[str, str]:
    """Page number -> JPEG data URL, rendered off the event loop."""
    return await asyncio.to_thread(_render, data, max_pages)
