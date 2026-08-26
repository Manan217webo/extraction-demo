"""Mapping — assemble extracted values into the JSON Cronos accepts.

Two things happen here.  Values arrive from the model as strings and are coerced
to the type the CRF field declares, recording an issue rather than discarding
anything when a value will not fit.  And each value is anchored to a rectangle on
the original page, so the reviewer can see in the PDF exactly what was read.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import anchors
import cronos

LOW_CONFIDENCE = 0.6

# How the parser writes tick boxes. `[no]` is an EMPTY box, not the answer "No".
_TICKED = re.compile(r"\[\s*(?:x|X|yes)\s*\]")
_UNTICKED = re.compile(r"\[\s*(?:|no)\s*\]")

DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
    "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %y",
]
TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%H%M"]

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


# --------------------------------------------------------------------------- coercion


def _coerce(value: Any, field: dict[str, Any]) -> tuple[Any, list[str]]:
    """Return (typed value, issues). A value that will not fit is kept, not dropped."""
    if value is None or value == "":
        return None, []
    text = str(value).strip()
    kind = field.get("type", "text")
    issues: list[str] = []

    if kind == "number":
        match = _NUMBER.search(text.replace(",", "."))
        if not match:
            return text, ["not_a_number"]
        number = float(match.group(0))
        number = int(number) if number.is_integer() else round(number, 4)
        low, high = field.get("min"), field.get("max")
        if low is not None and number < low:
            issues.append("below_expected_range")
        if high is not None and number > high:
            issues.append("above_expected_range")
        return number, issues

    if kind == "date":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat(), issues
            except ValueError:
                continue
        return text, ["unrecognised_date"]

    if kind == "time":
        for fmt in TIME_FORMATS:
            try:
                return datetime.strptime(text, fmt).strftime("%H:%M"), issues
            except ValueError:
                continue
        return text, ["unrecognised_time"]

    if kind in {"select", "radio"}:
        options = field.get("options") or []
        for option in options:
            if option.lower() == text.lower():
                return option, issues
        for option in options:  # "Abnormal, CS" against "Abnormal, clinically significant"
            if text.lower().startswith(option.lower()) or option.lower().startswith(text.lower()):
                return option, ["option_matched_loosely"]
        return text, ["not_an_allowed_option"]

    return text, issues


# --------------------------------------------------------------------------- field build


def _unticked_only(evidence: Optional[str]) -> bool:
    """True when the quoted evidence shows tick boxes and none of them is ticked.

    Readers reliably turn "[ ] CS [ ] NCS" into "No" — inventing an answer out of
    an empty pair of boxes. On a CRF that is a fabricated observation, so it is
    rejected here rather than trusted to the prompt.
    """
    if not evidence:
        return False
    return bool(_UNTICKED.search(evidence)) and not _TICKED.search(evidence)


def _build_field(field: dict[str, Any], row: Optional[dict[str, Any]],
                 index: Optional[anchors.PageIndex], key: str,
                 region: Optional[list] = None) -> dict[str, Any]:
    raw = (row or {}).get("value")
    if (field.get("type") in {"select", "radio"}
            and _unticked_only((row or {}).get("evidence"))):
        raw = None
    value, issues = _coerce(raw, field)
    if raw is None and (row or {}).get("value") is not None:
        issues.append("no_option_ticked")
    confidence = (row or {}).get("confidence")

    source: dict[str, Any] = {"evidence": (row or {}).get("evidence"),
                              "locator": (row or {}).get("locator"),
                              "page": (row or {}).get("page"),
                              "anchored": False, "rects": [], "match": "none"}
    if row and value is not None and index:
        found = index.anchor(row.get("evidence"), raw, row.get("page"),
                             locator=row.get("locator"), region=region)
        if found:
            source.update(
                anchored=True,
                page=found["page"],
                rects=found["rects"],
                match="exact" if found["exact"] else "partial",
                matched_text=found["matched_text"],
            )
    if value is not None and not source["anchored"]:
        issues.append("not_located_on_page")
        # The parser could not place it, but we do know which block it belongs
        # to. Naming that page beats repeating a page the model may have taken
        # from the wrong CRF, and lets the locator go and look for it.
        if region:
            source["page"] = region[0][0]
    if value is not None and confidence is not None and confidence < LOW_CONFIDENCE:
        issues.append("low_confidence")

    built = {
        "key": key,
        "field_id": field["field_id"],
        "label": field["label"],
        "type": field.get("type", "text"),
        "value": value,
        "raw_value": raw,
        "status": "extracted" if value is not None else "empty",
        "confidence": round(confidence, 3) if confidence is not None else None,
        "issues": issues,
        "source": source,
    }
    for key in ("unit", "options", "required", "min", "max", "derived"):
        if field.get(key) is not None:
            built[key] = field[key]
    return built


def _address(row: dict[str, Any]) -> tuple:
    return (row.get("section_id"), row.get("group_id"), row.get("instance", 1),
            row.get("field_id"))


# --------------------------------------------------------------------------- header


def build_header(rows: list[dict[str, Any]],
                 index: Optional[anchors.PageIndex]) -> dict[str, Any]:
    """The confirm-first block: grouped fields, each with its own evidence."""
    by_id = {row["field_id"]: row for row in rows}
    groups = []
    for group in cronos.HEADER_GROUPS:
        built = [
            _build_field(field, by_id.get(field["field_id"]), index,
                         f"header.{group['group_id']}.{field['field_id']}")
            for field in group["fields"]
        ]
        groups.append({"group_id": group["group_id"], "name": group["name"], "fields": built})
    return {"groups": groups, "summary": summarise(groups)}


def header_values(groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a confirmed header back to field_id -> value."""
    return {
        field["field_id"]: field.get("value")
        for group in groups for field in group.get("fields") or []
        if field.get("value") not in (None, "")
    }


# --------------------------------------------------------------------------- form


def build_form(form: dict[str, Any], rows: list[dict[str, Any]],
               index: Optional[anchors.PageIndex]) -> dict[str, Any]:
    """Nested Cronos JSON — sections, plain fields, and repeating group instances."""
    by_address = {_address(row): row for row in rows}
    # Each CRF is a titled block on the sheet. Confining a section's search to
    # its own block stops a value being lifted from the CRF below it, where the
    # same question is often asked again.
    regions = (
        index.section_regions([s.get("name") or "" for s in form.get("sections") or []])
        if index else {}
    )

    sections = []
    for section in form.get("sections") or []:
        section_id = section["section_id"]
        region = regions.get(section.get("name") or "")
        built_fields = [
            _build_field(field, by_address.get((section_id, None, 1, field["field_id"])), index,
                         f"{section_id}.{field['field_id']}", region)
            for field in section.get("fields") or []
        ]

        built_groups = []
        group = section.get("group")
        if group:
            group_id = group["group_id"]
            instances = sorted({
                row["instance"] for row in rows
                if row.get("section_id") == section_id and row.get("group_id") == group_id
            })
            instances = instances[: group.get("max_instances") or len(instances)]
            # Instances are renumbered from 1 after dropping any the model opened
            # but never filled, so the row a reviewer sees and the key a red box
            # is filed under always agree.
            kept = [
                instance for instance in instances
                if any(by_address.get((section_id, group_id, instance, field["field_id"]),
                                      {}).get("value") is not None
                       for field in group.get("fields") or [])
            ]
            built_instances = []
            for number, instance in enumerate(kept, start=1):
                built_instances.append({
                    "instance": number,
                    # The number the model reported, before unfilled rows were
                    # dropped. A positional store writes rows back by this, so
                    # renumbering for display must not lose it.
                    "source_instance": instance,
                    "fields": [
                        _build_field(
                            field,
                            by_address.get((section_id, group_id, instance, field["field_id"])),
                            index,
                            f"{section_id}.{group_id}.{number}.{field['field_id']}",
                            region,
                        )
                        for field in group.get("fields") or []
                    ],
                })
            built_groups.append({
                "group_id": group_id,
                "label": group["label"],
                "row_label": group["row_label"],
                "row_names": group.get("row_names") or [],
                "max_instances": group.get("max_instances"),
                "field_definitions": group.get("fields") or [],
                "instances": built_instances,
            })

        sections.append({
            "section_id": section_id,
            "name": section["name"],
            "description": section.get("description"),
            "fields": built_fields,
            "groups": built_groups,
        })

    return {
        "form_id": form.get("form_id"),
        "form_name": form.get("form_name"),
        "form_version": form.get("form_version"),
        "form_description": form.get("form_description"),
        "visit": form.get("visit"),
        "sections": sections,
    }


# --------------------------------------------------------------------------- views


def iter_built(container: dict[str, Any]):
    """Yield (path, field) for every built field in a header or form payload."""
    for group in container.get("groups") or []:  # header shape
        if "fields" in group and "instances" not in group:
            for field in group["fields"]:
                yield {"group": group.get("name"), "instance": None}, field
    for section in container.get("sections") or []:
        for field in section.get("fields") or []:
            yield {"group": section["name"], "instance": None}, field
        for group in section.get("groups") or []:
            for instance in group.get("instances") or []:
                for field in instance["fields"]:
                    yield ({"group": f"{section['name']} — {group['label']}",
                            "instance": instance["instance"]}, field)


def highlights(*containers: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of red boxes for the page viewer, keyed back to their field."""
    out = []
    for container in containers:
        for path, field in iter_built(container):
            source = field.get("source") or {}
            # Values the parser could not place are carried with no rectangle:
            # the viewer draws nothing for them, and the locator is given the
            # chance to find what the layout could not.
            if not source.get("anchored") and (
                field.get("value") in (None, "") or not source.get("page")
            ):
                continue
            out.append({
                "key": field["key"],
                "field_id": field["field_id"],
                "label": field["label"],
                "group": path["group"],
                "instance": path["instance"],
                "value": field["value"],
                "raw": field.get("raw_value"),
                "locator": source.get("locator"),
                "evidence": source.get("evidence"),
                "confidence": field.get("confidence"),
                "match": source.get("match"),
                "page": source.get("page"),
                "rects": source.get("rects") or [],
                "issues": field.get("issues") or [],
            })
    return out


def summarise(*containers: Any) -> dict[str, Any]:
    total = filled = anchored = low = flagged = 0
    for container in containers:
        items = (container if isinstance(container, list)
                 else list(iter_built(container)))
        for entry in items:
            field = entry[1] if isinstance(entry, tuple) else entry
            if isinstance(field, dict) and "fields" in field:  # a header group
                for inner in field["fields"]:
                    total += 1
                    filled += inner["value"] is not None
                    anchored += bool((inner.get("source") or {}).get("anchored"))
                    low += "low_confidence" in (inner.get("issues") or [])
                    flagged += bool(inner.get("issues"))
                continue
            total += 1
            filled += field["value"] is not None
            anchored += bool((field.get("source") or {}).get("anchored"))
            low += "low_confidence" in (field.get("issues") or [])
            flagged += bool(field.get("issues"))
    return {"fields": total, "filled": filled, "empty": total - filled,
            "anchored": anchored, "low_confidence": low, "flagged": flagged}


# --------------------------------------------------------------------------- export

_UI_ONLY_FIELD_KEYS = ("options", "min", "max", "required", "derived")


def for_export(payload: dict[str, Any], slim: bool = False) -> dict[str, Any]:
    """The payload without the definition metadata the browser needed to draw inputs.

    `slim` additionally drops the page geometry, which is what the viewer draws red
    boxes from but is only noise once the payload is printed.
    """
    import copy

    clean = copy.deepcopy(payload)
    if slim:
        clean.pop("highlights", None)

    def strip(field: dict[str, Any]) -> None:
        for key in _UI_ONLY_FIELD_KEYS:
            field.pop(key, None)
        if not slim:
            return
        source = field.get("source") or {}
        source.pop("rects", None)
        source.pop("matched_text", None)
        if field.get("raw_value") == field.get("value"):
            field.pop("raw_value", None)
        if not field.get("issues"):
            field.pop("issues", None)
        if field.get("value") is None:
            field.pop("source", None)
            field.pop("confidence", None)

    for group in (clean.get("header") or {}).get("groups") or []:
        for field in group.get("fields") or []:
            strip(field)

    for section in (clean.get("form") or {}).get("sections") or []:
        for field in section.get("fields") or []:
            strip(field)
        for group in section.get("groups") or []:
            group.pop("field_definitions", None)
            for instance in group.get("instances") or []:
                for field in instance.get("fields") or []:
                    strip(field)
    return clean


# --------------------------------------------------------------------------- revalidate

_EDIT_STATUSES = {"edited", "manual"}


def revalidate(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-coerce and re-check every value in a payload returned by the browser.

    The reviewer can change anything on the mapping screen, so what arrives back is
    not necessarily what we produced. Types, ranges and allowed options are checked
    again here rather than trusting the client, and the summary is recounted. An
    edited value keeps the evidence it was originally read from, but is no longer
    claimed to sit at that spot on the page.
    """
    for container in (payload.get("header") or {}, payload.get("form") or {}):
        for _, field in iter_built(container):
            value, issues = _coerce(field.get("value"), field)
            field["value"] = value
            edited = field.get("status") in _EDIT_STATUSES

            source = field.get("source") or {}
            if edited:
                source["anchored"] = False
                source["rects"] = []
                source["match"] = "edited"
                field["confidence"] = None
            field["source"] = source

            if value is None:
                field["status"] = "manual" if edited else "empty"
                field["issues"] = issues
                continue

            field["status"] = field.get("status") if edited else "extracted"
            if not source.get("anchored") and not edited:
                issues.append("not_located_on_page")
            confidence = field.get("confidence")
            if confidence is not None and confidence < LOW_CONFIDENCE:
                issues.append("low_confidence")
            if field.get("required") and value in (None, ""):
                issues.append("required_field_empty")
            field["issues"] = issues

    payload["highlights"] = highlights(payload.get("header") or {}, payload.get("form") or {})
    payload["summary"] = summarise(
        (payload.get("header") or {}).get("groups") or [], payload.get("form") or {}
    )
    return payload
