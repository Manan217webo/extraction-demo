"""Common-sense checks on a built CRF — the things a reviewer would spot at a glance.

The EDC gives a number field no range and a row no meaning beyond its name, so a
reading of 47.9 for a body temperature, or a "No" to "was the examination
performed?" above a filled-in examination, would otherwise pass as clean. These
checks read the row and field names the way a person would and flag what cannot
be right. They never change a value: they add an issue and cap the confidence,
so the reviewer looks.

Run on every build and every revalidate, so a reviewer's own typo is caught too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import mapping

# A flagged value is shown no surer than this, however sure the read was.
CHECK_CONFIDENCE = 0.3

# Limits a living patient can be measured at — deliberately wide. A value outside
# them is a misread digit or a value in the wrong row, never a clinical finding.
_VITALS: list[tuple[re.Pattern, float, float]] = [
    (re.compile(r"\b(pulse|heart\s*rate)\b", re.I), 25, 250),
    (re.compile(r"\brespirat", re.I), 4, 70),
    (re.compile(r"\bsystolic\b", re.I), 50, 280),
    (re.compile(r"\bdiastolic\b", re.I), 20, 180),
    (re.compile(r"\b(spo2|sp02|oxygen\s+saturation|saturation)\b", re.I), 50, 100),
]
_TEMPERATURE = re.compile(r"\btemp", re.I)
_TEMP_C = (30.0, 45.0)
_TEMP_F = (86.0, 113.0)

_PERFORMED = re.compile(r"\b(performed|done|assessed|collected|conducted|taken|dispensed)\b\??",
                        re.I)
_IF_ABNORMAL = re.compile(r"^\s*if\s+abnormal", re.I)
_EVALUATION = re.compile(r"\b(clinical\s+evaluation|condition|status|interpretation|result)\b",
                         re.I)
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

# Clinical trial sources are recent; anything older is a misread year.
_OLDEST = datetime(2000, 1, 1)


def _number(value: Any) -> Optional[float]:
    match = _NUMBER.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _filled(field: dict[str, Any]) -> bool:
    return field.get("value") not in (None, "") and not field.get("row_name")


def _flag(field: dict[str, Any], code: str) -> None:
    issues = field.setdefault("issues", [])
    if code not in issues:
        issues.append(code)
    if field.get("confidence") is not None:
        field["confidence"] = min(field["confidence"], CHECK_CONFIDENCE)
        if "low_confidence" not in issues:
            issues.append("low_confidence")


def _rows(section: dict[str, Any]):
    """Each table row as (row name, {field_id: field})."""
    for group in section.get("groups") or []:
        names = group.get("row_names") or []
        for instance in group.get("instances") or []:
            fields = {f["field_id"]: f for f in instance.get("fields") or []}
            number = instance.get("source_instance") or instance.get("instance") or 0
            name = names[number - 1] if 0 < number <= len(names) else ""
            if not name:
                name = next((str(f.get("value") or "") for f in fields.values()
                             if f.get("row_name")), "")
            yield name, fields


def _section_fields(section: dict[str, Any]) -> list[dict[str, Any]]:
    out = list(section.get("fields") or [])
    for group in section.get("groups") or []:
        for instance in group.get("instances") or []:
            out.extend(instance.get("fields") or [])
    return out


# --------------------------------------------------------------------------- rows


def _check_vitals(name: str, fields: dict[str, dict[str, Any]]) -> None:
    numbers = [f for f in fields.values()
               if f.get("type") == "number" and _filled(f)]
    for field in numbers:
        value = _number(field["value"])
        if value is None:
            continue
        label = f"{name} {field.get('label') or ''}"
        if _TEMPERATURE.search(label):
            unit = next((str(f.get("value") or "") for f in fields.values()
                         if "unit" in str(f.get("field_id", "")).lower() and _filled(f)), "")
            unit = unit.upper()
            in_c = _TEMP_C[0] <= value <= _TEMP_C[1]
            in_f = _TEMP_F[0] <= value <= _TEMP_F[1]
            if not in_c and not in_f:
                _flag(field, "implausible_value")
            elif ("C" in unit and not in_c) or ("F" in unit and not in_f):
                _flag(field, "unit_mismatch")
            continue
        for pattern, low, high in _VITALS:
            if pattern.search(label):
                if not low <= value <= high:
                    _flag(field, "implausible_value")
                break


def _check_follow_up(fields: dict[str, dict[str, Any]]) -> None:
    """"If abnormal, specify …" is answered exactly when the row is abnormal."""
    evaluation = next((f for f in fields.values()
                       if _EVALUATION.search(str(f.get("label") or ""))
                       and set(map(str.lower, f.get("options") or [])) >= {"normal", "abnormal"}),
                      None)
    if not evaluation:
        return
    state = str(evaluation.get("value") or "").strip().lower()
    for field in fields.values():
        if not _IF_ABNORMAL.search(str(field.get("label") or "")):
            continue
        if state == "abnormal" and not _filled(field) and field.get("type") in ("select", "radio"):
            _flag(field, "missing_when_abnormal")
        elif state == "normal" and _filled(field):
            _flag(field, "answered_when_normal")


def _check_blood_pressure(section: dict[str, Any]) -> None:
    systolic = diastolic = None
    for name, fields in _rows(section):
        for field in fields.values():
            if field.get("type") != "number" or not _filled(field):
                continue
            label = f"{name} {field.get('label') or ''}"
            if re.search(r"\bsystolic\b", label, re.I):
                systolic = field
            elif re.search(r"\bdiastolic\b", label, re.I):
                diastolic = field
    if systolic and diastolic:
        high, low = _number(systolic["value"]), _number(diastolic["value"])
        if high is not None and low is not None and high <= low:
            _flag(systolic, "systolic_not_above_diastolic")
            _flag(diastolic, "systolic_not_above_diastolic")


# --------------------------------------------------------------------------- sections


def _check_performed(section: dict[str, Any]) -> None:
    """A "No" to "was it performed?" above recorded results cannot both be right."""
    for gate in section.get("fields") or []:
        label = str(gate.get("label") or "")
        options = set(map(str.lower, gate.get("options") or []))
        if not (_PERFORMED.search(label) and {"yes", "no"} <= options):
            continue
        answer = str(gate.get("value") or "").strip().lower()
        # Values that could only exist if the assessment happened. A date is
        # left out: a "not done" section is often still dated.
        results = [f for f in _section_fields(section)
                   if f is not gate and _filled(f) and f.get("type") != "date"]
        if answer == "no" and results:
            _flag(gate, "contradicts_section")
        elif answer == "yes" and not results:
            _flag(gate, "nothing_recorded")


def _check_dates(fields: list[dict[str, Any]], today: datetime) -> None:
    for field in fields:
        if field.get("type") != "date" or not _filled(field):
            continue
        when = mapping.parse_date(field["value"])
        if when is None:
            continue
        if when > today + timedelta(days=1):
            _flag(field, "date_in_future")
        elif when < _OLDEST:
            _flag(field, "date_too_old")


# --------------------------------------------------------------------------- entry


def apply(payload: dict[str, Any], now: Callable[[], datetime] = datetime.now) -> None:
    """Add common-sense issues to every field of a built payload, in place."""
    today = now()
    form = payload.get("form") or {}
    every: list[dict[str, Any]] = []
    for section in form.get("sections") or []:
        for name, fields in _rows(section):
            _check_vitals(name, fields)
            _check_follow_up(fields)
        _check_vitals("", {f["field_id"]: f for f in section.get("fields") or []})
        _check_blood_pressure(section)
        _check_performed(section)
        every.extend(_section_fields(section))
    for group in (payload.get("header") or {}).get("groups") or []:
        every.extend(group.get("fields") or [])
    _check_dates(every, today)
