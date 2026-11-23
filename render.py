"""Render an uploaded PDF to page images on the server.

With the vision reader there is no parsing service: the model reads each page
from its image. The images are rendered once at upload, the way the browser's
pdf.js renders them (2x, JPEG), and kept with the job so the header read, the
form read and the box locator all look at the same pictures.

The structure scan (splitter.py) needs much less than a full page: the printed
header strip of every page, a fingerprint per page to recognise a visit that was
scanned before, and any text the PDF already carries. `survey` produces all
three in one pass, so a 300-page file is opened once and never rendered at full
size until a visit is actually read.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

import pypdfium2 as pdfium

SCALE = 2.0
QUALITY = 82

# The header strip: the top part of the page, where every CRF prints protocol,
# screening number and visit. Rendered just wide enough for handwritten digits.
BAND_FRACTION = float(os.getenv("SPLIT_BAND_FRACTION") or 0.24)
BAND_WIDTH_PX = int(os.getenv("SPLIT_BAND_WIDTH_PX") or 1000)
BAND_QUALITY = 70
# Thumbnails for the split screen, and the fingerprint grid.
THUMB_SCALE = 0.32
THUMB_WIDTH_PX = 200
FINGERPRINT_PX = 48

# pdfium is not thread-safe; uploads render one at a time per worker.
_lock = threading.Lock()


class RenderError(ValueError):
    """The upload is not a PDF we can open, or has too many pages."""


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        text = str(exc).lower()
        if "password" in text or "security" in text:
            raise RenderError("That PDF is password-protected. Save an unprotected copy "
                              "and upload that instead.") from exc
        raise RenderError("That file couldn't be opened as a PDF. It may be damaged; "
                          "try saving or scanning it again.") from exc


def _jpeg(image, quality: int) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _render(data: bytes, max_pages: int, only: Optional[list[int]] = None) -> dict[str, str]:
    with _lock:
        pdf = _open(data)
        try:
            count = len(pdf)
            if count == 0:
                raise RenderError("That PDF has no pages.")
            wanted = [p for p in (only or range(1, count + 1)) if 1 <= p <= count]
            if len(wanted) > max_pages:
                raise RenderError(f"PDFs must be {max_pages} pages or fewer.")
            out: dict[str, str] = {}
            for number in wanted:
                page = pdf[number - 1]
                try:
                    image = page.render(scale=SCALE).to_pil().convert("RGB")
                finally:
                    page.close()
                out[str(number)] = _jpeg(image, QUALITY)
            return out
        finally:
            pdf.close()


async def page_images(data: bytes, max_pages: int,
                      only: Optional[list[int]] = None) -> dict[str, str]:
    """Page number -> JPEG data URL, rendered off the event loop.

    `only` renders just those pages (numbered from 1), keyed by their own numbers.
    """
    return await asyncio.to_thread(_render, data, max_pages, only)


# --------------------------------------------------------------------------- survey


@dataclass
class PageSurvey:
    page: int
    width: float
    height: float
    # Text the PDF carries in the header strip ("" for a scan).
    header_text: str = ""
    # Whole-page text, for telling a blank page from a page with content.
    has_text: bool = False
    # The header strip as a JPEG data URL; only rendered for pages without text.
    band: Optional[str] = None
    fingerprint: str = ""
    # How much of the page is not white, 0..1. A blank sheet is near 0.
    ink: float = 0.0
    rotation: int = 0
    # A small JPEG of the page for the split screen, made from the same render.
    thumb: Optional[bytes] = None


@dataclass
class Survey:
    pages: list[PageSurvey] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.pages)


def _fingerprint(image) -> tuple[str, float]:
    """A digest of the page's look, stable across re-saves of the same scan,
    and how much ink the page carries."""
    small = image.convert("L").resize((FINGERPRINT_PX, int(FINGERPRINT_PX * 1.3)))
    raw = small.tobytes()
    # Quantise so a re-encode's tiny pixel noise doesn't change the digest.
    quantised = bytes(b >> 4 for b in raw)
    dark = sum(1 for b in raw if b < 200) / max(len(raw), 1)
    return hashlib.sha256(quantised).hexdigest()[:32], dark


def _survey(data: bytes, max_pages: int, bands: bool) -> Survey:
    with _lock:
        pdf = _open(data)
        try:
            count = len(pdf)
            if count == 0:
                raise RenderError("That PDF has no pages.")
            if count > max_pages:
                raise RenderError(f"This PDF has {count} pages. Files can have at most "
                                  f"{max_pages}; split it into smaller files and upload those.")
            out = Survey()
            for index in range(count):
                page = pdf[index]
                try:
                    width, height = page.get_size()
                    rotation = page.get_rotation()
                    textpage = page.get_textpage()
                    try:
                        whole = textpage.get_text_range() or ""
                        header = textpage.get_text_bounded(
                            0, height * (1 - BAND_FRACTION * 1.25), width, height) or ""
                    finally:
                        textpage.close()
                    # One render serves the band, the fingerprint and the ink check.
                    scale = BAND_WIDTH_PX / max(width, 1)
                    image = page.render(scale=scale).to_pil().convert("RGB")
                finally:
                    page.close()
                fingerprint, ink = _fingerprint(image)
                small = image.resize((THUMB_WIDTH_PX, max(1, int(image.height * THUMB_WIDTH_PX / image.width))))
                buffer = io.BytesIO()
                small.save(buffer, format="JPEG", quality=72, optimize=True)
                item = PageSurvey(page=index + 1, width=width, height=height,
                                  header_text=header.strip(), has_text=bool(whole.strip()),
                                  fingerprint=fingerprint, ink=ink, rotation=rotation,
                                  thumb=buffer.getvalue())
                if bands and not _usable_text(item.header_text):
                    band = image.crop((0, 0, image.width, int(image.height * BAND_FRACTION)))
                    item.band = _jpeg(band.convert("L"), BAND_QUALITY)
                out.pages.append(item)
            return out
        finally:
            pdf.close()


def _usable_text(text: str) -> bool:
    """Enough real words in the header strip to read it without a model."""
    return sum(1 for ch in text if ch.isalpha()) >= 12


async def survey(data: bytes, max_pages: int, bands: bool = True) -> Survey:
    """Header text, header strip (scans only), fingerprint and ink for every page."""
    return await asyncio.to_thread(_survey, data, max_pages, bands)


def page_count(data: bytes) -> int:
    with _lock:
        pdf = _open(data)
        try:
            return len(pdf)
        finally:
            pdf.close()


# --------------------------------------------------------------------------- slices


def _slice(data: bytes, pages: list[int]) -> bytes:
    with _lock:
        source = _open(data)
        try:
            count = len(source)
            wanted = [p - 1 for p in pages if 1 <= p <= count]
            if not wanted:
                raise RenderError("None of those pages are in the file.")
            target = pdfium.PdfDocument.new()
            try:
                target.import_pages(source, wanted)
                buffer = io.BytesIO()
                target.save(buffer)
                return buffer.getvalue()
            finally:
                target.close()
        finally:
            source.close()


async def slice_pdf(data: bytes, pages: list[int]) -> bytes:
    """A new PDF holding just these pages (numbered from 1), in the order given."""
    return await asyncio.to_thread(_slice, data, pages)


def _thumbnail(data: bytes, number: int, scale: float) -> bytes:
    with _lock:
        pdf = _open(data)
        try:
            if not 1 <= number <= len(pdf):
                raise RenderError("That page isn't in the file.")
            page = pdf[number - 1]
            try:
                image = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=72, optimize=True)
            return buffer.getvalue()
        finally:
            pdf.close()


# --------------------------------------------------------------------------- thumbnail cache


def _thumb_path(sha: str, number: int):
    import blobstore
    return blobstore.data_dir() / "thumbs" / sha[:2] / sha / f"{number}.jpg"


def save_thumbnails(sha: str, survey: "Survey") -> None:
    """Keep the survey's thumbnails, so the split screen never re-renders the file."""
    for item in survey.pages:
        if not item.thumb:
            continue
        path = _thumb_path(sha, item.page)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(item.thumb)
                tmp.replace(path)
        except OSError:
            return  # a thumbnail is a convenience; it is rendered on demand instead


def cached_thumbnail(sha: str, number: int) -> Optional[bytes]:
    try:
        return _thumb_path(sha, number).read_bytes()
    except OSError:
        return None


async def thumbnail(data: bytes, number: int, scale: float = THUMB_SCALE) -> bytes:
    """One page as a small JPEG, for the split screen."""
    scale = max(0.1, min(float(scale), 2.0))
    return await asyncio.to_thread(_thumbnail, data, number, scale)
