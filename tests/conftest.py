"""Shared test setup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def tiny_pdfs_allowed(monkeypatch):
    """The suite builds PDFs of a few hundred bytes. Real scans are far larger, so
    the production minimum (MIN_UPLOAD_KB) is lowered here; tests of the minimum
    itself set it back."""
    import sources
    monkeypatch.setattr(sources, "MIN_FILE_BYTES", 1)
