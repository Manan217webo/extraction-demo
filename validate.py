"""Post-merge validation rules for CRF extraction."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set


SPECIAL = {"UNCLEAR", "MULTIPLE"}


def _unwrap(fields: dict, key: str) -> Any:
    """Get raw value from field dict or plain value."""
    if key not in fields:
        return None
    item = fields[key]
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    return item


def _is_missing(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, str) and v in SPECIAL)


def _num(v: Any) -> Optional[float]:
    if _is_missing(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v: Any) -> Optional[datetime]:
    if _is_missing(v):
        return None
    if not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v[:10], fmt)
        except ValueError:
            continue
    return None


def _issue(rule_id: str, severity: str, message: str, affected: List[str]) -> dict:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "affected_fields": affected,
    }


def merge_fields(pages: List[dict]) -> dict:
    """
    Merge page field dicts. Header fields may appear on multiple pages —
    keep page-specific keys with page suffix for cross-page checks, and
    also keep a flat map of last-wins for single keys.
    """
    merged: Dict[str, Any] = {}
    by_page: Dict[int, dict] = {}
    for p in pages:
        page_no = p["page_no"]
        fields = p["fields"]
        by_page[page_no] = fields
        for k, v in fields.items():
            merged[k] = v
            merged[f"__p{page_no}_{k}"] = v
    merged["__by_page"] = by_page
    return merged


def validate(pages: List[dict]) -> List[dict]:
    """Run all 11 validation rules. Returns list of issues."""
    issues: List[dict] = []
    by_page = {p["page_no"]: p["fields"] for p in pages}
    flat: Dict[str, Any] = {}
    for p in pages:
        flat.update(p["fields"])

    def v(key: str, page: Optional[int] = None) -> Any:
        if page is not None:
            return _unwrap(by_page.get(page, {}), key)
        return _unwrap(flat, key)

    # 1. tablet balance
    td = _num(v("tablets_dispensed"))
    tc = _num(v("tablets_consumed"))
    tr = _num(v("tablets_returned"))
    tl = _num(v("tablets_lost"))
    if all(x is not None for x in (td, tc, tr, tl)):
        if abs((td - tc - tr - tl)) > 1e-6:
            issues.append(
                _issue(
                    "tablet_balance",
                    "error",
                    f"Tablet balance failed: dispensed ({td}) − consumed ({tc}) "
                    f"− returned ({tr}) − lost ({tl}) ≠ {td - tc - tr - tl}, expected 0",
                    [
                        "tablets_dispensed",
                        "tablets_consumed",
                        "tablets_returned",
                        "tablets_lost",
                    ],
                )
            )

    # 2. compliance percent
    cp = _num(v("compliance_percent"))
    if td is not None and tc is not None and td != 0 and cp is not None:
        expected = (tc / td) * 100
        if abs(cp - expected) > 1:
            issues.append(
                _issue(
                    "compliance_percent",
                    "error",
                    f"Compliance % ({cp}) does not match consumed/dispensed "
                    f"({expected:.1f}% ±1)",
                    ["compliance_percent", "tablets_consumed", "tablets_dispensed"],
                )
            )

    # 3. visit_date identical across pages
    visit_dates = []
    for pn in sorted(by_page.keys()):
        if "visit_date" in by_page[pn]:
            visit_dates.append((pn, v("visit_date", pn)))
    readable = [(pn, d) for pn, d in visit_dates if not _is_missing(d)]
    if len(readable) >= 2:
        values = {d for _, d in readable}
        if len(values) > 1:
            issues.append(
                _issue(
                    "visit_date_mismatch",
                    "error",
                    f"visit_date differs across pages: "
                    + ", ".join(f"p{pn}={d}" for pn, d in readable),
                    ["visit_date"],
                )
            )

    # 4. screening_no and patient_initials identical
    for key, rule_id in (
        ("screening_no", "screening_no_mismatch"),
        ("patient_initials", "patient_initials_mismatch"),
    ):
        pairs = []
        for pn in sorted(by_page.keys()):
            if key in by_page[pn]:
                pairs.append((pn, v(key, pn)))
        readable = [(pn, d) for pn, d in pairs if not _is_missing(d)]
        if len(readable) >= 2:
            values = {str(d).strip().upper() for _, d in readable}
            if len(values) > 1:
                issues.append(
                    _issue(
                        rule_id,
                        "error",
                        f"{key} differs across pages: "
                        + ", ".join(f"p{pn}={d}" for pn, d in readable),
                        [key],
                    )
                )

    # 5. activity dates should equal visit_date (warning)
    visit = v("visit_date")
    if not _is_missing(visit):
        for key in ("vitals_date", "physical_exam_date", "vas_date", "pgic_date"):
            d = v(key)
            if not _is_missing(d) and d != visit:
                issues.append(
                    _issue(
                        f"{key}_vs_visit",
                        "warning",
                        f"{key} ({d}) does not match visit_date ({visit})",
                        [key, "visit_date"],
                    )
                )

    # 6. vas_score 0–100
    vas = _num(v("vas_score"))
    if vas is not None and not (0 <= vas <= 100):
        issues.append(
            _issue(
                "vas_score_range",
                "error",
                f"vas_score ({vas}) must be between 0 and 100",
                ["vas_score"],
            )
        )

    # 7. vitals plausibility
    checks = [
        ("pulse_rate_value", 30, 200, "Pulse rate"),
        ("respiratory_rate_value", 5, 60, "Respiratory rate"),
        ("systolic_bp_value", 60, 250, "Systolic BP"),
        ("diastolic_bp_value", 30, 150, "Diastolic BP"),
    ]
    for key, lo, hi, label in checks:
        n = _num(v(key))
        if n is not None and not (lo <= n <= hi):
            issues.append(
                _issue(
                    f"plausibility_{key}",
                    "warning",
                    f"{label} ({n}) outside plausible range {lo}–{hi}",
                    [key],
                )
            )

    temp = _num(v("body_temperature_value"))
    unit = v("body_temperature_unit")
    if temp is not None:
        if unit == "F" and not (90 <= temp <= 110):
            issues.append(
                _issue(
                    "plausibility_temp",
                    "warning",
                    f"Body temperature ({temp}°F) outside plausible range 90–110°F",
                    ["body_temperature_value", "body_temperature_unit"],
                )
            )
        elif unit == "C" and not (32 <= temp <= 43):
            issues.append(
                _issue(
                    "plausibility_temp",
                    "warning",
                    f"Body temperature ({temp}°C) outside plausible range 32–43°C",
                    ["body_temperature_value", "body_temperature_unit"],
                )
            )
        elif unit not in ("C", "F") and not (
            (90 <= temp <= 110) or (32 <= temp <= 43)
        ):
            issues.append(
                _issue(
                    "plausibility_temp",
                    "warning",
                    f"Body temperature ({temp}) outside typical C/F ranges",
                    ["body_temperature_value"],
                )
            )

    # 8. Abnormal ⇒ significance required
    # vitals evaluations
    vital_bases = [
        "pulse_rate",
        "respiratory_rate",
        "systolic_bp",
        "diastolic_bp",
        "body_temperature",
    ]
    for base in vital_bases:
        ev = v(f"{base}_evaluation")
        sig = v(f"{base}_significance")
        if ev == "Abnormal" and _is_missing(sig):
            issues.append(
                _issue(
                    f"abnormal_sig_{base}",
                    "error",
                    f"{base}: Abnormal evaluation requires significance (CS/NCS)",
                    [f"{base}_evaluation", f"{base}_significance"],
                )
            )

    exam_bases = [
        "general_appearance",
        "heent",
        "dermatological",
        "neurological",
        "respiratory",
        "cardiovascular",
        "gastrointestinal",
        "musculoskeletal",
        "genitourinary",
        "lymph_nodes",
        "other",
    ]
    for base in exam_bases:
        cond = v(f"{base}_condition")
        sig = v(f"{base}_significance")
        if cond == "Abnormal" and _is_missing(sig):
            issues.append(
                _issue(
                    f"abnormal_sig_{base}",
                    "error",
                    f"{base}: Abnormal condition requires significance (CS/NCS)",
                    [f"{base}_condition", f"{base}_significance"],
                )
            )

    # 9. pregnancy N/A ⇒ reason required
    if v("pregnancy_test_done") == "Not Applicable" and _is_missing(
        v("pregnancy_reason")
    ):
        issues.append(
            _issue(
                "pregnancy_reason_required",
                "error",
                "pregnancy_test_done is Not Applicable but pregnancy_reason is missing",
                ["pregnancy_test_done", "pregnancy_reason"],
            )
        )

    # 10. UNCLEAR / MULTIPLE / null → warning (once per field key)
    seen_keys = set()  # type: Set[str]
    for p in pages:
        for key, item in p["fields"].items():
            if key in seen_keys:
                continue
            seen_keys.add(key)
            val = _unwrap({key: item}, key)
            if val is None:
                issues.append(
                    _issue(
                        f"empty_{key}",
                        "warning",
                        f"{key} is empty (null)",
                        [key],
                    )
                )
            elif isinstance(val, str) and val in SPECIAL:
                issues.append(
                    _issue(
                        f"special_{key}",
                        "warning",
                        f"{key} is {val}",
                        [key],
                    )
                )

    # 11. next_visit_date after visit_date
    nv = _date(v("next_visit_date"))
    vd = _date(v("visit_date"))
    if nv is not None and vd is not None and nv <= vd:
        issues.append(
            _issue(
                "next_visit_after_visit",
                "warning",
                f"next_visit_date ({v('next_visit_date')}) should be after "
                f"visit_date ({v('visit_date')})",
                ["next_visit_date", "visit_date"],
            )
        )

    return issues
