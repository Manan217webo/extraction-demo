"""Shape a visit's EDC CRFs into a form definition the mapping stage understands.

`GetVisitCRFData` returns a flat list of names and values per CRF.  Everything
that makes a CRF renderable — which fields are a table, what the rows are, what
type a field is, which options it allows — is missing.  Two things recover it:

*   **The repetition itself.**  A table flattened to a name list shows up as the
    same names cycling round: `Result, Clinical evaluation, CS/NCS` five times
    over is one column set and five rows, not fifteen questions.  A new row
    starts wherever the name that opened the last row comes round again, which
    tolerates a row carrying an extra cell the others do not (body temperature
    has a unit; the other parameters do not).

*   **The committed CRF definitions.**  Where `cronos_forms/` describes the same
    CRF, it supplies the types, the options and — most valuable — the row label
    column the EDC has no slot for at all.

What the EDC cannot tell us is which *parameter* each row is: the rows carry no
label, so row order is the only thing tying a value to a row.  Every field
therefore keeps the index it arrived at, and rows stay pinned to their printed
position rather than being renumbered, so a value can never be written back
against the wrong parameter.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Optional

log = logging.getLogger("extraction.visit_forms")

# Below this the CRF is treated as unrecognised and rendered from the EDC alone;
# a wrong definition would put the wrong options on a field.
MIN_SECTION_SCORE = 0.55

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _norm(label: str) -> str:
    return _NON_ALNUM.sub(" ", (label or "").lower()).strip()


def _slug(label: str, fallback: str) -> str:
    out = _NON_ALNUM.sub("_", (label or "").lower()).strip("_").upper()
    return (out or fallback)[:40]


def _similar(a: str, b: str) -> float:
    """How alike two field labels are, once punctuation and case are gone."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # "Body temperature Unit" is the EDC's name for the definition's "Unit", and
    # "If abnormal, specify clinical significance (CS/NCS)" for its shorter twin.
    left, right = set(a.split()), set(b.split())
    if left <= right or right <= left:
        return 0.85
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- tables


def infer_rows(names: list[str]) -> tuple[int, list[list[int]], list[str], bool]:
    """Split a flat field list into leading fields and repeating rows.

    Returns the number of fields before the table starts, the field indices
    making up each row, the shared column names in printed order, and whether
    each row opens with its own name.

    Two shapes turn up. In the first the row is bare — `Result, Clinical
    evaluation, CS/NCS` five times over — and nothing says which parameter each
    row is. In the second every row is introduced by its own uniquely named
    field, `Pulse rate (beats/min)` then `Result` and the rest. Those leading
    names are the rows' identities, not five extra columns, and treating them as
    columns produces a table as wide as the study.
    """
    norm = [_norm(name) for name in names]
    counts = Counter(norm)
    recurring = {name for name in norm if counts[name] >= 2 and name}
    if not recurring:
        return len(names), [], [], False

    # The column that opens each bare row: of the names recurring most often, the
    # one that appears first.
    most = max(counts[name] for name in recurring)
    key = min((name for name in recurring if counts[name] == most), key=norm.index)

    starts = [i for i, name in enumerate(norm) if name == key]
    if len(starts) < 2:
        return len(names), [], [], False

    labelled = _label_offset(norm, recurring, starts)
    if labelled:
        starts = [start - labelled for start in starts]

    rows = [
        list(range(start, starts[position + 1] if position + 1 < len(starts) else len(names)))
        for position, start in enumerate(starts)
    ]
    shared = [row[labelled:] for row in rows]
    return starts[0], rows, _columns(norm, names, shared), bool(labelled)


def _label_offset(norm: list[str], recurring: set[str], starts: list[int]) -> int:
    """How many fields before each row are that row's own name.

    Only counted when every row is introduced the same way and there is room
    before the first one; a run that appears on some rows and not others is a
    ragged table, not a label column.
    """
    if len(starts) < 2:
        return 0

    def trailing(stop: int, floor: int) -> int:
        """Unrepeated names immediately before `stop`, back as far as `floor`."""
        count = 0
        while stop - count - 1 >= floor and norm[stop - count - 1] not in recurring:
            count += 1
        return count

    runs = [trailing(starts[i], starts[i - 1]) for i in range(1, len(starts))]
    if not runs or min(runs) < 1 or len(set(runs)) != 1:
        return 0
    # The first row needs the same room, and something has to be left over for
    # the fields that sit above the table.
    return runs[0] if trailing(starts[0], 0) > runs[0] else 0


def _columns(norm: list[str], names: list[str], rows: list[list[int]]) -> list[str]:
    """Column names, ordered so a row with an extra cell slots it in the right place."""
    order: list[str] = []
    display: dict[str, str] = {}
    for row in rows:
        cursor = 0
        for index in row:
            name = norm[index]
            display.setdefault(name, names[index])
            if name in order:
                cursor = order.index(name) + 1
            else:
                order.insert(cursor, name)
                cursor += 1
    return [display[name] for name in order]


# --------------------------------------------------------------------------- matching


def _section_labels(section: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = [(_norm(f["label"]), f) for f in section.get("fields") or []]
    out += [(_norm(f["label"]), f) for f in (section.get("group") or {}).get("fields") or []]
    return out


def _score(crf_names: list[str], crf_name: str, section: dict[str, Any]) -> float:
    """How well a committed section describes this CRF."""
    labels = _section_labels(section)
    if not labels:
        return 0.0
    hits = [
        max(_similar(_norm(name), label) for label, _ in labels)
        for name in crf_names
    ]
    overlap = sum(hits) / len(hits)
    named = _similar(_norm(crf_name), _norm(section.get("name") or ""))
    return (0.8 * overlap) + (0.2 * named)


def match_section(crf: dict[str, Any],
                  forms: list[dict[str, Any]]) -> Optional[tuple[dict[str, Any], dict[str, Any], float]]:
    """The committed form and section that best describe this CRF, if any does."""
    names = [f.get("fieldName") or "" for f in crf.get("fields") or []]
    best: Optional[tuple[dict[str, Any], dict[str, Any], float]] = None
    for form in forms:
        for section in form.get("sections") or []:
            score = _score(names, crf.get("crfName") or "", section)
            if best is None or score > best[2]:
                best = (form, section, score)
    if best and best[2] >= MIN_SECTION_SCORE:
        return best
    return None


def _best_definition(label: str,
                     candidates: list[tuple[str, dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """The committed field description closest to an EDC field name."""
    if not candidates:
        return None
    norm = _norm(label)
    scored = [(( _similar(norm, key)), field) for key, field in candidates]
    score, field = max(scored, key=lambda pair: pair[0])
    return field if score >= 0.7 else None


# ------------------------------------------------------------------- structured

# Newer deployments describe the CRF properly: every field carries a unique
# `field_id`, its `field_row`/`field_col` in the printed grid, the control it is
# drawn with, and the options it allows. None of that has to be guessed at, and
# `field_id` is what finally makes two fields both called "Result" tellable
# apart. The inference above stays for deployments still returning the flat list.

CONTROL_TYPES = {
    "select": "select",
    "radio": "radio",
    "checkbox": "select",
    "date": "date",
    "number": "number",
    "text": "text",
    "textarea": "textarea",
    "label": "label",
}


def is_structured(crf: dict[str, Any]) -> bool:
    """True when the CRF carries its own layout rather than needing inference."""
    return any(
        field.get("field_id") is not None and field.get("field_col") is not None
        for field in crf.get("fields") or []
    )


def _attributes(field: dict[str, Any]) -> dict[str, Any]:
    raw = field.get("vAttributes")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("unreadable vAttributes on field %s", field.get("field_id"))
        return {}


def _csv_options(attributes: dict[str, Any]) -> Optional[list[str]]:
    """The options a control offers, from its CSV datasource.

    The list is written with a leading comma — ",Normal,Abnormal" — so the empty
    first entry is the unset state and is not an option a reviewer can pick.
    """
    raw = attributes.get("data-csv")
    if not isinstance(raw, str) or not raw.strip():
        return None
    options = [part.strip() for part in raw.split(",") if part.strip()]
    return options or None


def _structured_field(field: dict[str, Any], field_id: str) -> dict[str, Any]:
    attributes = _attributes(field)
    control = str(field.get("control_type") or "text").lower()
    built: dict[str, Any] = {
        "field_id": field_id,
        "label": field.get("field_name") or field.get("fieldName") or field_id,
        "type": CONTROL_TYPES.get(control, "text"),
        "edc_index": None,
    }
    options = _csv_options(attributes)
    if options:
        built["options"] = options
    if attributes.get("max"):
        built["max_length"] = attributes["max"]
    return built


def build_structured_crf(crf: dict[str, Any]) -> dict[str, Any]:
    """One CRF as a section definition, read straight off the EDC's own layout."""
    fields = crf.get("fields") or []
    section_id = f"C{crf.get('crfId')}"
    address: dict[str, int] = {}

    # A table and its cells share a field_seq; the cells alone carry a parent id.
    cells: dict[Any, list[dict[str, Any]]] = {}
    for field in fields:
        if field.get("nParentSectionDtlID"):
            cells.setdefault(field.get("field_seq"), []).append(field)

    # An address indexes the field list, the same as the inferred path, so one
    # save routine serves both. The EDC's own field_id travels beside it.
    position = {id(field): index for index, field in enumerate(fields)}

    built_fields: list[dict[str, Any]] = []
    group: Optional[dict[str, Any]] = None
    row_labels: list[str] = []
    taken: set[str] = set()

    for field in fields:
        if field.get("nParentSectionDtlID"):
            continue
        control = str(field.get("control_type") or "").lower()
        if control == "table":
            group, row_labels, mapped = _structured_group(
                field, cells.get(field.get("field_seq")) or [], section_id, position
            )
            address.update(mapped)
            continue
        field_id = _unique(_slug(field.get("field_name") or "", f"F{field.get('field_id')}"), taken)
        taken.add(field_id)
        built_fields.append(_structured_field(field, field_id))
        address[f"{section_id}||1|{field_id}"] = position[id(field)]

    return {
        "section": {
            "section_id": section_id,
            "name": crf.get("crfName") or f"CRF {crf.get('crfId')}",
            "description": None,
            "fields": built_fields,
            "group": group,
        },
        "edc": {
            "crfId": crf.get("crfId"),
            "crfName": crf.get("crfName"),
            "crf_seq": crf.get("crf_seq"),
            "section_id": section_id,
            "structured": True,
            "fields": [
                {"fieldName": f.get("field_name") or f.get("fieldName") or "",
                 "field_id": f.get("field_id"), "index": i,
                 "value": f.get("value") or "",
                 "imageId": f.get("imageId"),
                 # The field exactly as it arrived. The save hands each one back
                 # unchanged apart from its value, so the request mirrors the
                 # response — which is what the endpoint took before the layout
                 # was added, and there is no reason for that to have changed.
                 "record": f}
                for i, f in enumerate(fields)
            ],
            "rows": [[]] * len(row_labels),
            "address": address,
            "matched": True,
            "matched_form": "edc-layout",
            "match_score": 1.0,
        },
    }


def _structured_group(container: dict[str, Any], cells: list[dict[str, Any]],
                      section_id: str,
                      position: dict[int, int]) -> tuple[dict[str, Any], list[str], dict[str, int]]:
    """The table a container field declares, and where each cell writes back."""
    address: dict[str, int] = {}
    rows = sorted({int(c.get("table_row_number") or c.get("field_row") or 1) for c in cells})
    columns = sorted({int(c.get("field_col") or 1) for c in cells})
    by_cell = {
        (int(c.get("table_row_number") or c.get("field_row") or 1), int(c.get("field_col") or 1)): c
        for c in cells
    }

    group_id = f"{section_id}TBL"
    definitions: list[dict[str, Any]] = []
    ids: dict[int, str] = {}
    taken: set[str] = set()
    for column in columns:
        sample = next((by_cell[(row, column)] for row in rows if (row, column) in by_cell), None)
        if sample is None:
            continue
        label_column = str(sample.get("control_type") or "").lower() == "label"
        # Column one names each row, so its heading comes from the table itself
        # rather than from whichever parameter happens to sit in the first row.
        heading = container.get("field_name") if label_column else sample.get("field_name")
        field_id = _unique(_slug(heading or f"COL{column}", f"COL{column}"), taken)
        taken.add(field_id)
        built = _structured_field(sample, field_id)
        built["label"] = heading or built["label"]
        if label_column:
            built["type"] = "text"
            # It names the row rather than holding data a reviewer entered, so it
            # is not boxed on the page in its own right — the row's values carry
            # it into their own boxes instead.
            built["row_name"] = True
        definitions.append(built)
        ids[column] = field_id

    labels: list[str] = []
    for number, row in enumerate(rows, start=1):
        first = by_cell.get((row, columns[0])) if columns else None
        labels.append((first or {}).get("field_name") or f"Row {row}")
        for column in columns:
            cell = by_cell.get((row, column))
            if cell is None or column not in ids:
                continue
            address[f"{section_id}|{group_id}|{number}|{ids[column]}"] = position[id(cell)]

    attributes = _attributes(container)
    group = {
        "group_id": group_id,
        "label": container.get("field_name") or "Rows",
        "row_label": container.get("field_name") or "Row",
        "max_instances": int(attributes.get("rows") or len(rows) or 1),
        "positional_rows": True,
        "row_names": labels,
        "fields": definitions,
    }
    return group, labels, address


# --------------------------------------------------------------------------- building


def _describe(label: str, field_id: str, definition: Optional[dict[str, Any]],
              edc_index: Optional[int]) -> dict[str, Any]:
    """One field for the definition, taking type and options from the match."""
    out: dict[str, Any] = {
        "field_id": field_id,
        "label": label,
        "type": (definition or {}).get("type", "text"),
    }
    for key in ("options", "unit", "required", "description"):
        if definition and definition.get(key) is not None:
            out[key] = definition[key]
    # A field with no index is ours, not the EDC's — the row label it has no slot
    # for. It is shown and read, but never written back.
    out["edc_index"] = edc_index
    return out


def _row_label_field(section: Optional[dict[str, Any]],
                     used: set[str]) -> Optional[dict[str, Any]]:
    """The committed group's row-label column, when the EDC has no equivalent.

    The EDC's rows are bare — no parameter name, no system examined. Carrying the
    definition's label column gives the reviewer a row heading and gives the model
    something to tie each row to.
    """
    group = (section or {}).get("group") or {}
    for field in group.get("fields") or []:
        if _norm(field["label"]) in used:
            continue
        # The label column is the group's own row heading, by name.
        if _norm(field["label"]) == _norm(group.get("row_label") or ""):
            return field
    return None


def build_crf(crf: dict[str, Any], forms: list[dict[str, Any]]) -> dict[str, Any]:
    """One CRF as a section definition, plus the map back to EDC field indices."""
    if is_structured(crf):
        return build_structured_crf(crf)

    fields = crf.get("fields") or []
    names = [f.get("fieldName") or "" for f in fields]
    lead, rows, columns, labelled = infer_rows(names)

    matched = match_section(crf, forms)
    form, section, score = matched if matched else (None, None, 0.0)
    if matched:
        log.info("CRF %r matched %s/%s (%.2f)", crf.get("crfName"),
                 form.get("form_id"), section.get("section_id"), score)
    else:
        log.info("CRF %r has no committed definition — rendering from the EDC alone",
                 crf.get("crfName"))

    plain = [(_norm(f["label"]), f) for f in (section or {}).get("fields") or []]
    grouped = [(_norm(f["label"]), f) for f in ((section or {}).get("group") or {}).get("fields") or []]

    section_id = f"C{crf.get('crfId')}"
    address: dict[str, int] = {}

    built_fields = []
    for index in range(lead):
        definition = _best_definition(names[index], plain + grouped)
        field_id = (definition or {}).get("field_id") or _slug(names[index], f"F{index}")
        field_id = _unique(field_id, {f["field_id"] for f in built_fields})
        built_fields.append(_describe(names[index], field_id, definition, index))
        address[f"{section_id}||1|{field_id}"] = index

    group = None
    row_labels: list[str] = []
    if rows:
        used = {_norm(name) for name in columns}
        label_field = _row_label_field(section, used)
        group_fields = []
        naming: Optional[dict[str, Any]] = None
        if labelled:
            # Each row names itself, so the column is the EDC's own and carries a
            # value per row rather than being something we invent.
            heading = (label_field or {}).get("label") or names[lead - 1] or "Row"
            field_id = (label_field or {}).get("field_id") or _slug(heading, "ROWLABEL")
            naming = _describe(heading, field_id, label_field, None)
            naming["row_name"] = True
            group_fields.append(naming)
            row_labels = [names[row[0]] for row in rows]
        elif label_field:
            borrowed = _describe(label_field["label"], label_field["field_id"],
                                 label_field, None)
            borrowed["row_name"] = True
            group_fields.append(borrowed)
        by_column: dict[str, dict[str, Any]] = {}
        for column in columns:
            definition = _best_definition(column, grouped + plain)
            field_id = (definition or {}).get("field_id") or _slug(column, f"C{len(group_fields)}")
            field_id = _unique(field_id, {f["field_id"] for f in group_fields})
            built = _describe(column, field_id, definition, None)
            group_fields.append(built)
            by_column[_norm(column)] = built

        group_id = ((section or {}).get("group") or {}).get("group_id") or f"{section_id}ROW"
        row_label = ((section or {}).get("group") or {}).get("row_label") or "Row"
        group = {
            "group_id": group_id,
            "label": ((section or {}).get("group") or {}).get("label") or "Rows",
            "row_label": row_label,
            "max_instances": len(rows),
            # The EDC's rows are positional and unlabelled, so instance numbers
            # must stay pinned to the printed row rather than being compacted.
            "positional_rows": True,
            # The name each row goes by, where the EDC gives one. Shown as the
            # row heading so a reviewer sees "Pulse rate", not "Parameter 1".
            "row_names": row_labels,
            "fields": group_fields,
        }

        for number, row in enumerate(rows, start=1):
            body = row
            if naming is not None:
                address[f"{section_id}|{group_id}|{number}|{naming['field_id']}"] = row[0]
                body = row[1:]
            for index in body:
                field = by_column.get(_norm(names[index]))
                if not field:
                    continue
                address[f"{section_id}|{group_id}|{number}|{field['field_id']}"] = index

    return {
        "section": {
            "section_id": section_id,
            "name": crf.get("crfName") or f"CRF {crf.get('crfId')}",
            "description": (section or {}).get("description"),
            "fields": built_fields,
            "group": group,
        },
        "edc": {
            "crfId": crf.get("crfId"),
            "crfName": crf.get("crfName"),
            "section_id": section_id,
            "fields": [
            {"fieldName": name, "index": i,
             "value": (fields[i] or {}).get("value") or ""}
            for i, name in enumerate(names)
        ],
            "rows": rows,
            "address": address,
            "matched": bool(matched),
            "matched_form": (form or {}).get("form_id"),
            "match_score": round(score, 3),
        },
    }


def _unique(field_id: str, taken: set[str]) -> str:
    if field_id not in taken:
        return field_id
    for suffix in range(2, 100):
        candidate = f"{field_id}_{suffix}"
        if candidate not in taken:
            return candidate
    return field_id


def build_definition(visit: dict[str, Any],
                     forms: list[dict[str, Any]]) -> dict[str, Any]:
    """A whole visit as one form definition — one section per CRF."""
    ordered = sorted(visit.get("crfs") or [],
                     key=lambda crf: (crf.get("crf_seq") or 0, crf.get("crfId") or 0))
    built = [build_crf(crf, forms) for crf in ordered]
    return {
        "form_id": f"EDC-{visit.get('protocolNo')}-{visit.get('screeningNo')}-{visit.get('visitName')}",
        "form_name": f"{visit.get('visitName')} — {visit.get('protocolNo')}",
        "form_version": "live",
        "form_description": (
            f"Read live from the Cronos EDC for subject {visit.get('screeningNo')}."
        ),
        "visit": visit.get("visitName"),
        "sections": [item["section"] for item in built],
        "edc": {
            "protocolNo": visit.get("protocolNo"),
            "screeningNo": visit.get("screeningNo"),
            "visitName": visit.get("visitName"),
            "crfs": [item["edc"] for item in built],
        },
    }


# --------------------------------------------------------------------------- saving


def _save_field(field: dict[str, Any], value: str) -> dict[str, Any]:
    """One field as SaveVisitCRFData wants it.

    A structured deployment identifies a field by `field_id` and reads its
    `field_name`, `table_row_number` and `control_type` alongside the value. A
    deployment still returning bare names has only `fieldName` to go on.
    """
    record = field.get("record") or {}
    out: dict[str, Any] = {}
    if field.get("field_id") is not None:
        out["field_id"] = field["field_id"]
        out["field_name"] = record.get("field_name") or field["fieldName"]
        out["value"] = value
        if record.get("table_row_number") is not None:
            out["table_row_number"] = record["table_row_number"]
        if record.get("control_type"):
            out["control_type"] = record["control_type"]
    else:
        out["fieldName"] = field["fieldName"]
        out["value"] = value
    return out

# A value carrying one of these was flagged by `mapping._coerce` as something the
# field cannot represent. It stays on the review for a person to resolve; it does
# not go into the EDC.
BLOCKING_ISSUES = {"not_a_number", "not_an_allowed_option", "unrecognised_date",
                   "unrecognised_time"}


def repeated_names(crf: dict[str, Any]) -> dict[str, list[int]]:
    """Field names this CRF uses more than once, and the slots that share them.

    This only matters where the EDC has nothing else to go on. Measured against
    the deployment, `SaveVisitCRFData` resolved a field by its name alone —
    position was ignored, and so were index hints — so five fields called
    "Result" collapsed into one and the last value written won.

    A CRF that carries `field_id` per field is not exposed to that: every value
    is addressed by an id nothing else shares, and nothing is reported here.
    """
    if any(field.get("field_id") is not None for field in crf.get("fields") or []):
        return {}
    slots: dict[str, list[int]] = {}
    for field in crf.get("fields") or []:
        slots.setdefault(field["fieldName"], []).append(field["index"])
    return {name: indices for name, indices in slots.items() if len(indices) > 1}


def unsaveable(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Per CRF, the fields the EDC has no way to tell apart."""
    out = []
    for crf in definition["edc"]["crfs"]:
        repeated = repeated_names(crf)
        if not repeated:
            continue
        out.append({
            "crfName": crf["crfName"],
            "crfId": crf["crfId"],
            "names": [{"fieldName": name, "slots": len(indices)}
                      for name, indices in repeated.items()],
            "fields_affected": sum(len(indices) for indices in repeated.values()),
        })
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def build_save(definition: dict[str, Any], form: dict[str, Any],
               crops: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The SaveVisitCRFData body for a reviewed form, and anything worth flagging.

    Every field the EDC gave us is sent back, in the order it arrived, carrying
    the value it already held unless the review replaced it.  Sending the whole
    list is what makes the payload unambiguous: five fields are all called
    "Result", so position is the only thing that distinguishes them — and sending
    only the filled ones would leave the EDC to guess which row each belonged to.
    Fields nobody touched keep their existing value rather than being blanked.
    """
    edc = definition["edc"]
    by_section = {crf["section_id"]: crf for crf in edc["crfs"]}
    warnings: list[str] = []
    crfs: list[dict[str, Any]] = []
    used_crops: set[str] = set()

    for section in form.get("sections") or []:
        meta = by_section.get(section["section_id"])
        if not meta:
            continue
        address = meta["address"]
        values: dict[int, str] = {}
        images: list[dict[str, Any]] = []
        images_by_index: dict[int, dict[str, Any]] = {}

        def place(field: dict[str, Any], key: str, addr: str, row: Optional[int]) -> None:
            index = address.get(addr)
            if index is None:
                # The row-label column is ours, not the EDC's; it has nowhere to go.
                if field.get("value") is not None and field.get("edc_index") is not None:
                    warnings.append(f"“{field['label']}” has no field in the EDC and was not sent.")
                return
            # A value the field cannot hold — "abc" in a number, "Not done" where
            # the options are Normal/Abnormal — is kept on screen for the reviewer
            # to fix, but it is not written into a clinical record as it stands.
            blocking = set(field.get("issues") or []) & BLOCKING_ISSUES
            if blocking and field.get("value") is not None:
                warnings.append(
                    f"“{field['label']}” reads {field['value']!r}, which this field cannot "
                    f"hold ({', '.join(sorted(blocking))}). It was left unchanged in the EDC."
                )
                return
            if field.get("value") is not None:
                values[index] = _as_text(field["value"])
            # The browser keys crops by field.key. The reconstructed address is
            # the fallback when a payload was built without keys.
            crop_key = field.get("key") or key
            crop = crops.get(crop_key) or (crops.get(key) if key != crop_key else None)
            if crop and crop.get("base64Data"):
                used_crops.add(crop_key)
                if key != crop_key:
                    used_crops.add(key)
                slot = meta["fields"][index]
                blob = {
                    "fileName": f"{str(crop_key).replace('.', '_')}.png",
                    "contentType": crop.get("contentType") or "image/png",
                    "base64Data": crop["base64Data"],
                }
                images_by_index[index] = blob
                images.append({
                    **({"field_id": slot["field_id"]} if slot.get("field_id") is not None else {}),
                    "field_name": slot["fieldName"],
                    **blob,
                })

        for field in section.get("fields") or []:
            key = f"{section['section_id']}.{field['field_id']}"
            place(field, key, f"{section['section_id']}||1|{field['field_id']}", None)

        for group in section.get("groups") or []:
            for instance in group.get("instances") or []:
                # The printed row, not the display row: unfilled rows are dropped
                # before display, and the EDC stores rows by position.
                row = instance.get("source_instance") or instance["instance"]
                for field in instance.get("fields") or []:
                    key = (f"{section['section_id']}.{group['group_id']}."
                           f"{instance['instance']}.{field['field_id']}")
                    place(field, key,
                          f"{section['section_id']}|{group['group_id']}|{row}|{field['field_id']}",
                          row)

        for name, indices in repeated_names(meta).items():
            written = [index for index in indices if values.get(index)]
            if len(written) > 1:
                warnings.append(
                    f"“{name}” names {len(indices)} separate fields on {meta['crfName']}, "
                    f"and the EDC saves by name alone — only one of the {len(written)} "
                    f"values read for it can be stored. Check this CRF in the EDC."
                )

        crf: dict[str, Any] = {
            "crfName": meta["crfName"],
            "crfId": meta["crfId"],
            # Exactly what SaveVisitCRFData documents per field: its id, its
            # name, its value, the row it sits in and the control it is drawn
            # with. The rest of the GET record — grid coordinates, vAttributes,
            # section sequence — is layout, not data, and is not echoed back;
            # nor is the source crop, which travels in `images` alone. A
            # deployment still on bare names gets `fieldName` and `value`.
            "fields": [_save_field(field, values.get(field["index"], field.get("value") or ""))
                       for field in meta["fields"]],
            "images": images,
        }
        if meta.get("crf_seq") is not None:
            crf["crf_seq"] = meta["crf_seq"]
        crfs.append(crf)

    leftover = [key for key in crops if key not in used_crops and (crops[key] or {}).get("base64Data")]
    if leftover:
        sample = ", ".join(f"“{key}”" for key in leftover[:6])
        more = f" and {len(leftover) - 6} more" if len(leftover) > 6 else ""
        warnings.append(
            f"{len(leftover)} source image{'' if len(leftover) == 1 else 's'} "
            f"did not match an EDC field ({sample}{more}) and were not sent."
        )

    payload = {
        "protocolNo": edc["protocolNo"],
        "screeningNo": edc["screeningNo"],
        "visitName": edc["visitName"],
        "crfs": crfs,
    }
    return payload, warnings
