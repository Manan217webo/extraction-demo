"""Upload size limits, the Date of Birth age range, submitting without an
e-signature, and the token usage report (26-Nov-2026 eSource meeting)."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- file size


def test_file_server_refuses_files_below_the_minimum(tmp_path, monkeypatch):
    import sources
    monkeypatch.setattr(sources, "MIN_FILE_BYTES", 10 * 1024)
    root = tmp_path / "share"
    root.mkdir()
    (root / "tiny.pdf").write_bytes(b"%PDF-1.4 blank")
    (root / "real.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 20 * 1024)
    conn = sources.LocalConnector("s", "S", str(root))
    with pytest.raises(sources.SourceError, match="at least 10 KB"):
        asyncio.run(conn.read("tiny.pdf"))
    assert asyncio.run(conn.read("real.pdf")).startswith(b"%PDF")


def test_size_problem_covers_empty_small_and_large(monkeypatch):
    import sources
    monkeypatch.setattr(sources, "MIN_FILE_BYTES", 10 * 1024)
    monkeypatch.setattr(sources, "MAX_FILE_BYTES", 1024 * 1024)
    assert "empty" in sources.size_problem("a.pdf", 0)
    assert "at least 10 KB" in sources.size_problem("a.pdf", 2048)
    assert "over 1 MB" in sources.size_problem("a.pdf", 2 * 1024 * 1024)
    assert sources.size_problem("a.pdf", 50 * 1024) is None


# --------------------------------------------------------------------------- age


def _checked(label, value, field_type="date", field_id="X"):
    import checks
    field = {"field_id": field_id, "label": label, "type": field_type, "value": value,
             "confidence": 0.95}
    checks.apply({"form": {"sections": [{"fields": [field]}]}},
                 now=lambda: datetime(2026, 11, 27))
    return field


@pytest.mark.parametrize("dob,flagged", [
    ("12-Mar-1980", False),   # an ordinary adult — once wrongly flagged as "too old"
    ("27-Nov-1926", False),   # exactly 100 today
    ("28-Nov-1925", False),   # still 100; turns 101 tomorrow
    ("26-Nov-1925", True),    # 101 yesterday
    ("01-Jan-1890", True),
])
def test_date_of_birth_is_allowed_up_to_100_years(dob, flagged):
    field = _checked("Date of birth", dob, field_id="DMBRTHDT")
    assert ("age_over_limit" in field.get("issues", [])) is flagged
    assert field["confidence"] == (0.3 if flagged else 0.95)


def test_other_old_dates_are_still_flagged():
    assert "date_too_old" in _checked("Visit date", "12-Mar-1980")["issues"]


def test_a_future_date_of_birth_is_flagged():
    assert "date_in_future" in _checked("DOB", "01-Jan-2030")["issues"]


@pytest.mark.parametrize("label,value,flagged", [
    ("Age (years)", "104", True), ("Age", "45", False), ("Age", "-3", True),
    ("Page count", "500", False), ("Dosage", "500", False),
])
def test_age_written_as_a_number(label, value, flagged):
    field = _checked(label, value, field_type="number")
    assert ("age_over_limit" in field.get("issues", [])) is flagged


# --------------------------------------------------------------------------- e-signature


def test_esign_is_off_unless_switched_on(monkeypatch):
    import esource
    monkeypatch.delenv("REQUIRE_ESIGN", raising=False)
    assert esource.esign_required() is False
    monkeypatch.setenv("REQUIRE_ESIGN", "true")
    assert esource.esign_required() is True


def test_submission_without_esign_is_marked_as_such():
    import db
    import esource
    plain = db.Signature(document_id="d", username="u", full_name="U",
                         meaning=esource.SUBMITTED_LABEL, record_sha256="x")
    signed = db.Signature(document_id="d", username="u", full_name="U",
                          meaning=esource.MEANING_LABELS["REVIEWED_ACCURATE"], record_sha256="x")
    assert esource.signature_dict(plain)["esigned"] is False
    assert esource.signature_dict(signed)["esigned"] is True


def test_submit_request_needs_no_password():
    import api
    body = api.SubmitRequest(payload={})
    assert body.password is None


# --------------------------------------------------------------------------- tokens


def test_usage_summary_totals_costs_and_waste(monkeypatch):
    import usage_report
    table = {"big": {"input": 2.0, "output": 8.0}}
    row = lambda **kw: {"day": "2026-11-20", "document_id": "d1", "filename": "a.pdf",
                        "extraction_id": "e1", "pages": 4, "status": "ready",
                        "stage": "form", "model": "big", "prompt": 1_000_000,
                        "completion": 100_000, **kw}
    report = usage_report.summarise([
        row(),
        row(stage="header", prompt=100_000, completion=0),
        row(extraction_id="e2", status="failed", prompt=500_000, completion=0),
        row(document_id="d2", extraction_id="e3", model="small", prompt=10, completion=10,
            pages=2),
    ], table)
    assert report["totals"]["prompt_tokens"] == 1_600_010
    assert report["by_model"]["big"]["cost_usd"] == pytest.approx(2.0 + 0.8 + 0.2 + 1.0)
    assert report["by_model"]["small"]["cost_complete"] is False
    assert report["unpriced_models"] == ["small"]
    assert report["waste"]["failed_runs"] == 1
    assert report["waste"]["failed_cost_usd"] == pytest.approx(1.0)
    assert report["waste"]["documents_read_more_than_once"] == 1
    assert report["averages"]["documents"] == 2


def test_usage_entries_read_both_shapes():
    import usage_report
    assert usage_report.entries({"prompt_tokens": 5, "completion_tokens": 2, "model": "m"}) == \
        [("m", 5, 2)]
    assert usage_report.entries({"by_model": {"a": {"prompt_tokens": 1, "completion_tokens": 1},
                                              "b": {"prompt_tokens": 2}}}) == \
        [("a", 1, 1), ("b", 2, 0)]
    assert usage_report.entries({}) == [] and usage_report.entries(None) == []


def test_token_prices_from_env(monkeypatch):
    import usage_report
    monkeypatch.setenv("TOKEN_PRICES", '{"m": {"input": 1, "output": 2}, "bad": {}}')
    assert usage_report.prices() == {"m": {"input": 1.0, "output": 2.0}}
    monkeypatch.setenv("TOKEN_PRICES", "not json")
    assert usage_report.prices() == {}
