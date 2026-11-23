"""Files holding several visits: finding the visits, correcting the split,
confirming it, and reading one visit from a larger file end to end."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_access import actions, signed_in, world  # noqa: E402,F401

import splitter  # noqa: E402
from splitter import PageRead  # noqa: E402


# --------------------------------------------------------------------------- PDFs


def crf_pdf(pages: list[str | None], screening: str = "03013") -> bytes:
    """A typed CRF: each entry is a page's visit text, "" for a cover page with no
    header, None for a blank sheet."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    for visit in pages:
        if visit is None:
            pdf.showPage()
            continue
        if visit == "":
            pdf.setFont("Helvetica-Bold", 16)
            pdf.drawString(72, height - 120, "CASE REPORT FORM")
            pdf.drawString(72, height - 150, "Protocol No. ICR/26/005 Version No. 1.0")
            pdf.drawString(72, height - 400, "Confidential study document for site use")
            pdf.showPage()
            continue
        pdf.setFont("Helvetica", 10)
        pdf.drawString(60, height - 40, "India Clinical Research")
        pdf.drawString(60, height - 56, f"Protocol No. ICR/26/005    Screening No.: {screening}")
        pdf.drawString(60, height - 72, f"Patient Initials: [__][__][__]    {visit}")
        pdf.drawString(60, height - 88, "Visit Date [__][__][__]")
        pdf.drawString(60, height - 300, "VITAL SIGNS   Pulse rate [__][__][__] beats/min")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def scan_pdf(n: int) -> bytes:
    """n pages with ink but no text, like a scan."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    for i in range(n):
        pdf.setFillGray(0.2)
        pdf.rect(60, 700, 400, 40 + i, fill=1, stroke=0)
        pdf.rect(60, 300, 300, 200, fill=1, stroke=0)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


V1 = "Visit 1 Screening Period: Week -2 to Week-1"
V2 = "Visit 2 Enrolment/ Baseline Visit (Week 0/ Day 1)"
V3 = "Visit 3 On Treatment Visits: Week 2 (Day 15 ±2)"
ALL = "Visit All Applicable"


def read(page, label, **kw):
    return PageRead(page=page, visit_label=label, protocol_no=kw.get("protocol", "ICR/26/005"),
                    screening_no=kw.get("screening", "03013"), has_header=label is not None,
                    confidence=kw.get("confidence", 0.95), source=kw.get("source", "ai"),
                    visit_date=kw.get("date"))


# --------------------------------------------------------------------------- reading headers


def test_text_header_is_read_without_a_model():
    text = ("India Clinical Research\r\nProtocol No. ICR/26/005 Screening No.: [__][__][__]\r\n"
            "Patient Initials: [__][__][__] Visit 2 Enrolment/ Baseline Visit (Week 0/ Day 1)\r\n"
            "Visit Date [__][__][__]")
    r = splitter.read_text_header(19, text)
    assert r.protocol_no == "ICR/26/005"
    assert r.screening_no is None  # empty boxes are not a number
    assert r.visit_label == V2
    named = splitter.read_text_header(67, "Protocol No. ICR/26/005\nPatient Initials: [__] Visit Unscheduled Visit")
    assert named.visit_label == "Unscheduled Visit"
    filled = splitter.read_text_header(5, "Protocol No. ICR/24/001 Screening No. [0][3][0][1][3]\nVisit 1 Screening Visit")
    assert filled.screening_no == "03013"


def test_schedule_table_is_not_a_visit_header():
    r = splitter.read_text_header(3, "Protocol No. ICR/26/005\nVisit 1 2 3 4 5 6 7\nDay -14 to -1 1 15")
    assert r.visit_label is None


def test_visit_keys_and_edc_names():
    assert splitter.visit_key(V1) == splitter.visit_key("Visit 1 Screening and Washout visit") == "visit 1"
    assert splitter.visit_key("Unscheduled Visit") == splitter.visit_key("Visit Unscheduled Visit")
    assert splitter.visit_key("All Applicable") is None and splitter.is_not_a_visit(ALL)
    assert splitter.edc_names(V3) == [V3, "Visit 3"]
    assert splitter.edc_names("Visit 3") == ["Visit 3"]


# --------------------------------------------------------------------------- grouping


def test_whole_crf_groups_into_visits_and_sets_aside_the_rest():
    pages = ["", "", V1, V1, V2, V2, V2, V3, "Visit Unscheduled Visit", ALL, ALL]
    reads = [splitter.read_text_header(i + 1, "" if not v else
                                       f"Protocol No. ICR/26/005 Screening No.: 03013\n{v}")
             for i, v in enumerate(pages)]
    for r in reads:
        if not r.visit_label and not r.protocol_no:
            r.source = "text"
    plan = splitter.plan(reads)
    assert [(s["label"], s["pages"]) for s in plan["segments"]] == [
        (V1, [3, 4]), (V2, [5, 6, 7]), (V3, [8]), ("Unscheduled Visit", [9])]
    assert [(i["page"], i["reason"]) for i in plan["ignored"]] == [
        (1, "no_visit_header"), (2, "no_visit_header"), (10, "not_a_visit"), (11, "not_a_visit")]
    assert plan["protocol"]["value"] == "ICR/26/005" and plan["screening"]["value"] == "03013"
    assert plan["issues"] == []


def test_a_lone_misread_page_is_outvoted_by_its_neighbours():
    plan = splitter.plan([read(1, V1), read(2, V1), read(3, "Visit 7 Safety Follow-up"),
                          read(4, V1), read(5, V2)])
    assert [s["pages"] for s in plan["segments"]] == [[1, 2, 3, 4], [5]]
    assert plan["segments"][0]["flags"] == [{"code": "misread_pages", "pages": [3]}]


def test_a_page_without_a_readable_header_joins_the_visit_before_it():
    plan = splitter.plan([read(1, None), read(2, V1), read(3, None), read(4, V2),
                          PageRead(page=5, source="blank")])
    assert [s["pages"] for s in plan["segments"]] == [[2, 3], [4]]
    assert plan["segments"][0]["flags"] == [{"code": "inferred_pages", "pages": [3]}]
    assert [(i["page"], i["reason"]) for i in plan["ignored"]] == [(1, "no_visit_header"),
                                                                   (5, "blank_page")]


def test_pages_out_of_order_still_group_by_visit():
    plan = splitter.plan([read(1, V1), read(2, V2), read(3, V1), read(4, V2)])
    assert [s["pages"] for s in plan["segments"]] == [[1, 3], [2, 4]]
    assert {"code": "not_contiguous", "pages": [2]} in plan["segments"][0]["flags"]


def test_screening_number_is_voted_and_the_minority_is_listed():
    reads = [read(i, V1) for i in range(1, 6)] + [read(6, V1, screening="03018")]
    plan = splitter.plan(reads)
    assert plan["screening"]["value"] == "03013"
    assert plan["screening"]["agreement"] == pytest.approx(5 / 6, abs=0.001)
    assert plan["screening"]["disagreeing"] == [{"page": 6, "value": "03018"}]
    assert {"code": "screening_disagree", "pages": [{"page": 6, "value": "03018"}]} in plan["issues"]


def test_one_visit_with_two_dates_is_flagged():
    plan = splitter.plan([read(1, "Unscheduled Visit", date="02 Mar 2026"),
                          read(2, "Unscheduled Visit", date="14 Apr 2026")])
    assert plan["segments"][0]["flags"][0]["code"] == "multiple_dates"


def test_reviewer_edits_move_rename_add_and_untick():
    plan = splitter.with_fingerprints(splitter.plan(
        [read(1, V1), read(2, V1), read(3, V2), read(4, V2), read(5, ALL)]))
    s1, s2 = (s["id"] for s in plan["segments"])
    edited = splitter.apply_edits(plan, {
        "assign": {"2": s2, "5": "n1", "4": "ignore"},
        "new": [{"id": "n1", "label": "Unscheduled Visit"}],
        "labels": {s2: "Visit 2 Baseline"},
        "selected": {s1: False},
        "screening_no": "03014",
    })
    by_label = {s["label"]: s for s in edited["segments"]}
    assert by_label[V1]["pages"] == [1] and by_label[V1]["selected"] is False
    assert by_label["Visit 2 Baseline"]["pages"] == [2, 3]
    assert by_label["Visit 2 Baseline"]["edc_names"] == ["Visit 2 Baseline", "Visit 2"]
    assert by_label["Unscheduled Visit"]["pages"] == [5]
    assert [i["page"] for i in edited["ignored"]] == [4]
    assert edited["screening"]["value"] == "03014"
    # A visit left with no pages disappears rather than lingering empty.
    emptied = splitter.apply_edits(plan, {"assign": {"1": s2, "2": s2}})
    assert [s["id"] for s in emptied["segments"]] == [s2]


def test_added_visit_ids_survive_cleaning():
    import splits

    cleaned = splits.clean_edits({"new": [{"id": "nnab1", "label": " Visit 9 "}, {"id": "x2", "label": "Y"}],
                                  "assign": {"1": "nnab1", "99": "s1", "x": "s1"},
                                  "selected": {"nnab1": 1}}, plan_pages=5)
    assert cleaned["new"] == [{"id": "nnab1", "label": "Visit 9"}, {"id": "nx2", "label": "Y"}]
    assert cleaned["assign"] == {"1": "nnab1"}  # pages that don't exist are dropped
    plan = splitter.plan([read(1, V1), read(2, V1)])
    assert [s["label"] for s in splitter.apply_edits(plan, cleaned)["segments"]] == ["Visit 9", V1]


def test_what_happens_next():
    import splits

    one = splitter.plan([read(1, V1), read(2, V1)])
    assert splits.decide(one, 30) == "single"
    assert splits.single_pages(one) is None
    two = splitter.plan([read(1, V1), read(2, V2)])
    assert splits.decide(two, 30) == "review"
    unread = splitter.plan([read(1, None), read(2, None)])
    assert splits.decide(unread, 30) == "whole"
    assert splits.decide(unread, 1) == "review"  # too long to read blind as one visit
    only_logs = splitter.plan([read(1, ALL), read(2, ALL)])
    assert splits.decide(only_logs, 30) == "review"
    one["segments"][0]["prior"] = {"document_id": "x"}
    assert splits.decide(one, 30) == "review"  # read before: a person decides
    covered = splitter.plan([PageRead(page=1, source="text"), read(2, V1)])
    assert splits.single_pages(covered) == [2]


def test_scanned_pages_are_read_from_header_strips_in_batches(monkeypatch):
    import render

    monkeypatch.setattr(splitter, "BATCH_SIZE", 2)
    calls = []

    async def strips(batch):
        calls.append([p for p, _ in batch])
        labels = {1: V1, 2: V1, 3: V2}
        return {p: {"has_header": True, "protocol_no": "ICR/24/001", "screening_no": "0 3 0 1 3",
                    "visit": labels[p], "confidence": 0.9} for p, _ in batch} | \
            {-1: {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "model": "mini"}}}

    async def go():
        survey = await render.survey(scan_pdf(3), 50)
        assert all(p.band and p.band.startswith("data:image/jpeg") for p in survey.pages)
        spent = []
        reads = await splitter.read_pages(survey, strips, spent)
        return reads, spent

    reads, spent = asyncio.run(go())
    assert calls == [[1, 2], [3]]
    assert len(spent) == 2
    assert [r.screening_no for r in reads] == ["03013"] * 3
    plan = splitter.plan(reads)
    assert [s["pages"] for s in plan["segments"]] == [[1, 2], [3]]


# --------------------------------------------------------------------------- page routing


def test_each_crf_is_read_from_its_own_pages():
    import visit_forms

    sections = [{"section_id": "VS", "name": "VITAL SIGNS"},
                {"section_id": "PE", "name": "PHYSICAL EXAMINATION"},
                {"section_id": "LAB", "name": "LABORATORY INVESTIGATIONS"},
                {"section_id": "XX", "name": "Something not printed"}]
    titles = [{"title": "Vital Signs", "page": 1}, {"title": "Physical Examination", "page": 1},
              {"title": "Laboratory Investigations", "page": 3}]
    routes = visit_forms.section_pages(sections, titles, [1, 2, 3, 4])
    assert routes == {"VS": [1, 2, 3], "PE": [1, 2, 3], "LAB": [3, 4], "XX": [1, 2, 3, 4]}


def test_long_crfs_are_read_in_chunks_and_merged():
    import fields

    merged = fields._merge_chunks([
        {"values": [{"section_id": "A", "group_id": None, "instance": 1, "field_id": "f1",
                     "value": None, "confidence": 0.9},
                    {"section_id": "A", "group_id": None, "instance": 1, "field_id": "f2",
                     "value": "7", "confidence": 0.4}]},
        {"values": [{"section_id": "A", "group_id": None, "instance": 1, "field_id": "f1",
                     "value": "80", "confidence": 0.8},
                    {"section_id": "A", "group_id": None, "instance": 1, "field_id": "f2",
                     "value": "1", "confidence": 0.9}]},
    ])
    assert [(r["field_id"], r["value"]) for r in merged] == [("f1", "80"), ("f2", "1")]


def test_no_page_is_dropped_past_the_image_limit(monkeypatch):
    import fields

    monkeypatch.setattr(fields, "MAX_PAGE_IMAGES", 2)
    pages = {str(n): "data:image/jpeg;base64,AAAA" for n in range(1, 6)}
    assert len(fields.page_images(pages)) == 2
    assert len(fields.page_images(pages, cap=False)) == 5
    assert [len(c) for c in fields._chunks(fields.page_images(pages, cap=False))] == [2, 2, 1]


# --------------------------------------------------------------------------- EDC visit names


def test_the_edc_is_asked_for_the_printed_name_then_the_short_one(tmp_path, monkeypatch):
    import edc

    fixture = json.loads((ROOT / "edc_fixtures" / "visits.json").read_text())
    path = tmp_path / "visits.json"
    path.write_text(json.dumps(fixture))
    monkeypatch.setenv("EDC_FIXTURES", str(path))
    visit, name = asyncio.run(edc.find_visit("ICR/24/001", "03013",
                                             ["Visit 5 Treatment Visit: Day 113", "Visit 5"]))
    assert name == "Visit 5" and visit["visitName"] == "Visit 5"
    with pytest.raises(edc.EdcUnavailable, match="any of these names"):
        asyncio.run(edc.find_visit("ICR/24/001", "03013", ["Visit 9 Late", "Visit 9"]))


# --------------------------------------------------------------------------- end to end


def _ai_forbidden(monkeypatch):
    import fields

    async def nope(*_a, **_k):
        raise AssertionError("a typed PDF must not cost a model call")

    monkeypatch.setattr(fields, "configured", lambda: True)
    monkeypatch.setattr(fields, "extract_page_headers", nope)


def test_upload_of_a_whole_crf_waits_for_the_split_and_confirming_queues_each_visit(
        world, monkeypatch):
    import blobstore
    import db
    import render
    from sqlalchemy import select

    _ai_forbidden(monkeypatch)
    pdf = crf_pdf(["", V1, V1, V2, V2, V2, V3, ALL, None], screening="[__][__]")
    c = signed_in("jsmith")
    up = c.post("/api/documents", files={"file": ("CRF.pdf", pdf, "application/pdf")})
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["status"] == "SPLIT" and body["split"]["visits"] == 3
    doc = body["split"]["document_id"]

    view = c.get(f"/api/docs/{doc}/split").json()
    assert [(v["label"], v["pages"]) for v in view["visits"]] == [
        (V1, [2, 3]), (V2, [4, 5, 6]), (V3, [7])]
    assert [i["page"] for i in view["ignored"]] == [1, 8, 9]
    assert view["document"]["status"] == "split_review"
    assert c.get(f"/api/docs/{doc}/review").status_code == 409  # a file of visits isn't reviewed itself
    assert c.get(f"/api/docs/{doc}/pages/2/thumb").headers["content-type"] == "image/jpeg"
    assert render.cached_thumbnail(blobstore.sha256_of(pdf), 2) is not None  # made during the scan

    # The blank form has no screening number: confirming is refused until it is entered.
    refused = c.post(f"/api/docs/{doc}/split/confirm", json={})
    assert refused.status_code == 422 and "screening number" in refused.json()["detail"]

    v3 = view["visits"][2]["id"]
    saved = c.put(f"/api/docs/{doc}/split",
                  json={"edits": {"screening_no": "03013", "selected": {v3: False}}}).json()
    assert saved["screening"]["value"] == "03013"
    assert [v["selected"] for v in saved["visits"]] == [True, True, False]

    done = c.post(f"/api/docs/{doc}/split/confirm", json={})
    assert done.status_code == 200, done.text
    result = done.json()["result"]
    assert [v["label"] for v in result["created"]] == [V1, V2]
    assert result["skipped"] == [V3]

    async def children():
        async with db.session() as s:
            kids = (await s.execute(select(db.Document).where(db.Document.parent_id == doc)
                                    .order_by(db.Document.created_at))).scalars().all()
            out = []
            for kid in kids:
                run = (await s.execute(select(db.Extraction).where(
                    db.Extraction.document_id == kid.id))).scalar()
                version = (await s.execute(select(db.DocumentVersion).where(
                    db.DocumentVersion.document_id == kid.id))).scalar()
                out.append((kid, run, await blobstore.get(version.sha256)))
            return out

    kids = asyncio.run(children())
    assert [(k.visit_label, k.source_pages, k.screening_no, k.protocol_no, r.status)
            for k, r, _ in kids] == [(V1, [2, 3], "03013", "ICR/26/005", "queued"),
                                     (V2, [4, 5, 6], "03013", "ICR/26/005", "queued")]
    assert [render.page_count(data) for _, _, data in kids] == [2, 3]
    seed = kids[1][1].snapshot["meta"]
    assert [r["value"] for r in seed["header_rows"]] == ["ICR/26/005", "03013", V2]
    assert seed["edc_names"] == [V2, "Visit 2"]

    # What was sent starts unticked, so pressing the button again reads nothing twice.
    after = c.get(f"/api/docs/{doc}/split").json()
    assert [v["selected"] for v in after["visits"]] == [False, False, False]
    assert c.post(f"/api/docs/{doc}/split/confirm", json={}).status_code == 422

    # Reading a visit again needs a reason; one already queued is left alone.
    v1, v2 = view["visits"][0]["id"], view["visits"][1]["id"]
    again = c.post(f"/api/docs/{doc}/split/confirm",
                   json={"edits": {"screening_no": "03013",
                                   "selected": {v1: True, v2: True, v3: True}}})
    assert again.status_code == 200, again.text
    assert again.json()["result"]["busy"] == [V1, V2]
    assert [v["label"] for v in again.json()["result"]["created"]] == [V3]

    inbox = {d["id"]: d for d in c.get("/api/docs").json()["documents"]}
    assert inbox[doc]["extraction"]["status"] == "ready" and not inbox[doc]["extraction"]["error"]
    assert inbox[doc]["kind"] == "parent" and inbox[doc]["children"] == {"processing": 3}
    assert len(actions("split.confirmed")) == 2 and len(actions("split.visit_created")) == 3
    verified = c.get("/api/audit/verify")
    assert verified.status_code == 200 and verified.json()["ok"] is True, verified.text


def test_a_visit_cut_from_a_larger_file_is_read_with_its_header_already_known(
        world, monkeypatch, tmp_path):
    import db
    import fields
    import pipeline
    from sqlalchemy import select

    _ai_forbidden(monkeypatch)
    fixture = json.loads((ROOT / "edc_fixtures" / "visits.json").read_text())
    fixture["visits"][0].update(protocolNo="ICR/26/005", screeningNo="03013", visitName="Visit 2")
    (tmp_path / "edc.json").write_text(json.dumps(fixture))
    monkeypatch.setenv("EDC_FIXTURES", str(tmp_path / "edc.json"))

    seen = {}

    async def extract_header(*_a, **_k):
        raise AssertionError("the header was read during the split")

    async def extract_form(document, definition, header, pages):
        seen["pages"] = sorted(int(p) for p in pages)
        seen["header"] = header
        return {"values": [], "images": len(pages), "usage": {"prompt_tokens": 1},
                "form_titles": None, "routes": None}

    monkeypatch.setattr(fields, "extract_header", extract_header)
    monkeypatch.setattr(fields, "extract_form", extract_form)

    c = signed_in("jsmith")
    pdf = crf_pdf(["", V1, V2, V2], screening="03013")
    doc = c.post("/api/documents", files={"file": ("CRF.pdf", pdf, "application/pdf")}).json()
    doc = doc["split"]["document_id"]
    v1 = c.get(f"/api/docs/{doc}/split").json()["visits"][0]["id"]
    assert c.post(f"/api/docs/{doc}/split/confirm",
                  json={"edits": {"selected": {v1: False}}}).status_code == 200

    async def go():
        found = await pipeline._claim()
        assert found is not None
        await pipeline.process(*found)
        async with db.session() as s:
            kid = (await s.execute(select(db.Document).where(db.Document.parent_id == doc))).scalar()
            run = (await s.execute(select(db.Extraction).where(
                db.Extraction.document_id == kid.id))).scalar()
            return kid, run

    kid, run = asyncio.run(go())
    assert run.status == "ready", run.error
    assert seen["pages"] == [1, 2]  # the child's own two pages, nothing else
    assert seen["header"]["visit_name"] == V2
    # The EDC knew the visit by its short name; the record says so.
    assert kid.visit_name == "Visit 2" and kid.visit_label == V2
    assert kid.status == "ready"


def test_a_single_visit_file_is_read_as_before_without_a_split_screen(world, monkeypatch):
    _ai_forbidden(monkeypatch)
    c = signed_in("jsmith")
    pdf = crf_pdf(["", V2, V2], screening="03013")
    up = c.post("/api/documents", files={"file": ("visit2.pdf", pdf, "application/pdf")})
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["status"] == "COMPLETED"
    header = c.post(f"/api/documents/{body['job_id']}/header").json()["header"]
    values = {f["field_id"]: f["value"] for g in header["groups"] for f in g["fields"]}
    assert values == {"protocol_no": "ICR/26/005", "screening_no": "03013", "visit_name": V2}
    doc = c.get(f"/api/docs/{body['document_id']}").json()["document"]
    assert doc["pages"] == [2, 3] and doc["kind"] is None  # the cover page is not read


def test_a_visit_already_processed_for_the_patient_starts_unticked(world, monkeypatch):
    _ai_forbidden(monkeypatch)
    c = signed_in("jsmith")
    first = c.post("/api/documents", files={"file": ("a.pdf", crf_pdf([V1, V2]), "application/pdf")})
    doc = first.json()["split"]["document_id"]
    assert c.post(f"/api/docs/{doc}/split/confirm", json={}).status_code == 200
    # The same patient's Visit 1 again, scanned into another file with a new visit.
    second = c.post("/api/documents", files={"file": ("b.pdf", crf_pdf([V1, V3, V3]), "application/pdf")})
    other = second.json()["split"]["document_id"]
    view = c.get(f"/api/docs/{other}/split").json()
    assert [(v["label"], v["selected"], bool(v["prior"])) for v in view["visits"]] == [
        (V1, False, True), (V3, True, False)]
    done = c.post(f"/api/docs/{other}/split/confirm", json={})
    assert [v["label"] for v in done.json()["result"]["created"]] == [V3]
    # Ticking it on purpose is allowed, with a reason.
    v1 = view["visits"][0]["id"]
    refused = c.post(f"/api/docs/{other}/split/confirm", json={"edits": {"selected": {v1: True}}})
    assert refused.status_code == 422 and "processed before" in refused.json()["detail"]
    ok = c.post(f"/api/docs/{other}/split/confirm",
                json={"edits": {"selected": {v1: True}}, "reason": "The pages were rescanned"})
    assert ok.status_code == 200 and [v["label"] for v in ok.json()["result"]["created"]] == [V1]


def test_unreadable_and_protected_files_are_refused_with_a_reason(world, monkeypatch):
    _ai_forbidden(monkeypatch)
    c = signed_in("jsmith")
    bad = c.post("/api/documents", files={"file": ("x.pdf", b"%PDF-1.4 nonsense", "application/pdf")})
    assert bad.status_code == 400 and "PDF" in bad.json()["detail"]
