"""Table (repeating-row) data entry, from the EDC layout through to the save body.

Covers the scenarios raised in the 26-Nov-2026 eSource meeting: every cell goes
to its own EDC field, partly filled and edited tables send only what changed,
cleared cells are sent as blanks, and a row the EDC has no cells for is never
dropped without the reviewer being told.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mapping  # noqa: E402
import visit_forms  # noqa: E402


def _cell(fid, name, row, col, control="text", value="", seq=4):
    return {"field_id": fid, "field_name": name, "fieldName": name, "control_type": control,
            "field_col": col, "field_row": row, "table_row_number": row, "field_seq": seq,
            "nParentSectionDtlID": 1, "value": value}


def _meds_crf(rows=3, declared=None, values=None):
    """A free-entry table (S No., Medication, Dose) with `rows` rows of cells."""
    values = values or {}
    fields = [{"field_id": 1, "field_name": "Concomitant medications", "control_type": "table",
               "field_col": 1, "field_row": 1, "field_seq": 4,
               "vAttributes": json.dumps({"control_name": "Table",
                                          "rows": str(declared or rows), "columns": "3"})}]
    fid = 100
    for row in range(1, rows + 1):
        for col, name in enumerate(("S No.", "Medication", "Dose"), start=1):
            fid += 1
            fields.append(_cell(fid, name, row, col, value=values.get((row, name), "")))
    return {"crfId": 50, "crfName": "CONMEDS", "crf_seq": 1, "fields": fields}


def _visit(*crfs):
    return {"protocolNo": "P1", "screeningNo": "S1", "visitName": "Visit 2", "crfs": list(crfs)}


def _read(section_id, group_id, instance, field_id, value):
    return {"section_id": section_id, "group_id": group_id, "instance": instance,
            "field_id": field_id, "value": value, "confidence": 0.95, "page": 1,
            "rect": [0.1, 0.1, 0.2, 0.2]}


def _ids(definition):
    section = definition["sections"][0]
    group = section["group"]
    by_label = {f["label"]: f["field_id"] for f in group["fields"]}
    return section["section_id"], group["group_id"], by_label


def _save(definition, form):
    body, warnings = visit_forms.build_save(definition, form, {})
    body.pop("_delta", None)
    sent = {}
    for crf in body["crfs"]:
        for field in crf["fields"]:
            sent[field["field_id"]] = field["value"]
    return sent, warnings


def _cell_id(crf, row, name):
    return next(f["field_id"] for f in crf["fields"]
                if f.get("table_row_number") == row and f["field_name"] == name)


# --------------------------------------------------------------------------- layout


def test_every_row_with_cells_is_offered_and_no_more():
    crf = _meds_crf(rows=2, declared=5)
    group = visit_forms.build_structured_crf(crf)["section"]["group"]
    assert group["max_instances"] == 2
    assert group["declared_rows"] == 5


def test_rows_the_edc_returns_beyond_its_declared_count_are_not_hidden():
    crf = _meds_crf(rows=4, declared=2)
    group = visit_forms.build_structured_crf(crf)["section"]["group"]
    assert group["max_instances"] == 4


def test_fixture_vital_signs_table_names_its_rows():
    visits = json.loads((ROOT / "edc_fixtures" / "visits.json").read_text())["visits"]
    vitals = next(c for v in visits for c in v["crfs"] if c["crfName"] == "VITAL SIGNS")
    group = visit_forms.build_structured_crf(vitals)["section"]["group"]
    assert group["max_instances"] == 5
    assert group["row_names"][0] == "Pulse rate (beats/min)"


# --------------------------------------------------------------------------- entry → save


def test_each_cell_goes_to_its_own_edc_field():
    crf = _meds_crf(rows=3)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    reads = [
        _read(sid, gid, 1, ids["S No."], "1"), _read(sid, gid, 1, ids["Medication"], "Paracetamol"),
        _read(sid, gid, 1, ids["Dose"], "500 mg"),
        _read(sid, gid, 2, ids["S No."], "2"), _read(sid, gid, 2, ids["Medication"], "Ibuprofen"),
        _read(sid, gid, 2, ids["Dose"], "200 mg"),
    ]
    form = mapping.build_form(definition, reads, None)
    sent, warnings = _save(definition, form)
    assert sent == {
        _cell_id(crf, 1, "S No."): "1", _cell_id(crf, 1, "Medication"): "Paracetamol",
        _cell_id(crf, 1, "Dose"): "500 mg",
        _cell_id(crf, 2, "S No."): "2", _cell_id(crf, 2, "Medication"): "Ibuprofen",
        _cell_id(crf, 2, "Dose"): "200 mg",
    }
    assert warnings == []


def test_a_row_filled_lower_down_keeps_its_printed_position():
    crf = _meds_crf(rows=3)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    form = mapping.build_form(definition, [_read(sid, gid, 3, ids["Medication"], "Aspirin")], None)
    assert len(form["sections"][0]["groups"][0]["instances"]) == 3  # empty rows still shown
    sent, _ = _save(definition, form)
    assert sent == {_cell_id(crf, 3, "Medication"): "Aspirin"}


def test_only_changed_cells_are_sent_when_the_edc_already_has_the_table():
    crf = _meds_crf(rows=2, values={(1, "Medication"): "Paracetamol", (1, "Dose"): "500 mg"})
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    reads = [_read(sid, gid, 1, ids["Medication"], "Paracetamol"),
             _read(sid, gid, 1, ids["Dose"], "650 mg")]
    sent, _ = _save(definition, mapping.build_form(definition, reads, None))
    assert sent == {_cell_id(crf, 1, "Dose"): "650 mg"}


def test_a_reviewer_typing_into_an_empty_row_is_sent():
    crf = _meds_crf(rows=2)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    form = mapping.build_form(definition, [], None)
    cell = next(f for f in form["sections"][0]["groups"][0]["instances"][1]["fields"]
                if f["field_id"] == ids["Medication"])
    cell.update(value="Metformin", status="manual")
    sent, _ = _save(definition, form)
    assert sent == {_cell_id(crf, 2, "Medication"): "Metformin"}


def test_a_cleared_cell_is_sent_as_blank():
    crf = _meds_crf(rows=1, values={(1, "Dose"): "500 mg"})
    definition = visit_forms.build_definition(_visit(crf), [])
    _, _, ids = _ids(definition)
    form = mapping.build_form(definition, [], None)
    cell = next(f for f in form["sections"][0]["groups"][0]["instances"][0]["fields"]
                if f["field_id"] == ids["Dose"])
    cell.update(value=None, status="edited")
    sent, _ = _save(definition, form)
    assert sent == {_cell_id(crf, 1, "Dose"): ""}


def test_label_column_is_never_sent():
    visits = json.loads((ROOT / "edc_fixtures" / "visits.json").read_text())["visits"]
    vitals = copy.deepcopy(next(c for v in visits for c in v["crfs"] if c["crfName"] == "VITAL SIGNS"))
    definition = visit_forms.build_definition(_visit(vitals), [])
    form = mapping.build_form(definition, [], None)
    for instance in form["sections"][0]["groups"][0]["instances"]:
        for field in instance["fields"]:
            if field.get("type") == "label":
                field.update(value="Changed", status="edited")
    sent, _ = _save(definition, form)
    assert sent == {}


# --------------------------------------------------------------------------- overflow


def test_rows_read_beyond_the_edc_table_are_reported():
    crf = _meds_crf(rows=2)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    reads = [_read(sid, gid, n, ids["Medication"], f"Drug {n}") for n in (1, 2, 3, 4)]
    form = mapping.build_form(definition, reads, None)
    assert len(form["sections"][0]["groups"][0]["instances"]) == 2
    assert form["table_overflow"] == [{"section": "CONMEDS", "table": "Concomitant medications",
                                       "rows_available": 2, "rows_read": 4}]


def test_no_overflow_notice_when_everything_fits():
    crf = _meds_crf(rows=2)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    form = mapping.build_form(definition, [_read(sid, gid, 2, ids["Dose"], "5 mg")], None)
    assert "table_overflow" not in form


def test_a_value_in_a_row_without_edc_cells_warns_instead_of_vanishing():
    crf = _meds_crf(rows=1)
    definition = visit_forms.build_definition(_visit(crf), [])
    sid, gid, ids = _ids(definition)
    form = mapping.build_form(definition, [], None)
    group = form["sections"][0]["groups"][0]
    extra = copy.deepcopy(group["instances"][0])
    extra.update(instance=2, source_instance=2)
    for field in extra["fields"]:
        if field["field_id"] == ids["Medication"]:
            field.update(value="Atorvastatin", status="manual")
    group["instances"].append(extra)
    sent, warnings = _save(definition, form)
    assert sent == {}
    assert any("row 2" in w and "Medication" in w for w in warnings)
