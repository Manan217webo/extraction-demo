"""PDF export — the mapped CRF as a document a monitor can file.

Two parts: the form itself, laid out section by section with every mapped value
and how it was obtained, and an appendix carrying the exact JSON that would be
posted to Cronos, so the paper and the payload can never disagree.
"""

from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Preformatted, Spacer, Table, TableStyle,
)

import mapping

BRAND_LOGO = Path(__file__).resolve().parent / "static" / "brand" / "whpl-logo.png"

INK = colors.HexColor("#0b0717")
MUTED = colors.HexColor("#655f73")
LINE = colors.HexColor("#ddd8e8")
SOFT = colors.HexColor("#f7f5fb")
PURPLE = colors.HexColor("#7652f5")
RED = colors.HexColor("#e45d72")
AMBER = colors.HexColor("#c98413")
GREEN = colors.HexColor("#1f8f6a")

PAGE = A4
MARGIN = 15 * mm

ISSUE_LABELS = {
    "low_confidence": "uncertain reading",
    "not_located_on_page": "not located on page",
    "not_a_number": "not numeric",
    "unrecognised_date": "date format unclear",
    "unrecognised_time": "time format unclear",
    "not_an_allowed_option": "outside allowed options",
    "option_matched_loosely": "option matched loosely",
    "below_expected_range": "below expected range",
    "above_expected_range": "above expected range",
}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["BodyText"]
    def style(name: str, **kwargs: Any) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base, alignment=TA_LEFT, **kwargs)

    return {
        "title": style("t", fontName="Helvetica-Bold", fontSize=17, leading=21,
                       textColor=INK, spaceAfter=2),
        "subtitle": style("st", fontSize=9.5, leading=13, textColor=MUTED, spaceAfter=10),
        "eyebrow": style("ey", fontName="Helvetica-Bold", fontSize=7.5, leading=10,
                         textColor=PURPLE, spaceAfter=3),
        "section": style("sec", fontName="Helvetica-Bold", fontSize=12, leading=15,
                         textColor=INK, spaceBefore=12, spaceAfter=2),
        "note": style("no", fontSize=8, leading=11, textColor=MUTED, spaceAfter=6),
        "cell": style("ce", fontSize=8, leading=10.5, textColor=INK),
        "cellhead": style("ch", fontName="Helvetica-Bold", fontSize=7.5, leading=10,
                          textColor=MUTED),
        "value": style("va", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=INK),
        "flag": style("fl", fontSize=7, leading=9, textColor=AMBER),
        "empty": style("em", fontSize=8, leading=10.5, textColor=colors.HexColor("#9c96ab")),
        "mono": ParagraphStyle("mo", fontName="Courier", fontSize=6.6, leading=8.2,
                               textColor=INK),
    }


def _escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _issues(field: dict[str, Any]) -> str:
    return ", ".join(ISSUE_LABELS.get(code, code.replace("_", " "))
                     for code in field.get("issues") or [])


def _value_cell(field: dict[str, Any], s: dict[str, ParagraphStyle]) -> list[Any]:
    if field.get("value") is None:
        return [Paragraph("Not recorded", s["empty"])]
    text = _escape(field["value"])
    if field.get("unit"):
        text += f' <font color="#655f73" size="7">{_escape(field["unit"])}</font>'
    out: list[Any] = [Paragraph(text, s["value"])]
    note = _issues(field)
    if note:
        out.append(Paragraph(f"<b>!</b> {_escape(note)}", s["flag"]))
    return out


def _source_cell(field: dict[str, Any], s: dict[str, ParagraphStyle]) -> Paragraph:
    source = field.get("source") or {}
    if field.get("value") is None:
        return Paragraph("—", s["empty"])
    if not source.get("anchored"):
        return Paragraph("not located", s["empty"])
    confidence = field.get("confidence")
    suffix = f" · {round(confidence * 100)}%" if confidence is not None else ""
    return Paragraph(f'p{source.get("page")}{suffix}', s["cell"])


def _table(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), SOFT))
    table.setStyle(TableStyle(commands))
    return table


# --------------------------------------------------------------------------- blocks


def _header_block(payload: dict[str, Any], s: dict[str, ParagraphStyle],
                  width: float) -> list[Any]:
    flow: list[Any] = []
    for group in (payload.get("header") or {}).get("groups") or []:
        rows = [[Paragraph("Field", s["cellhead"]), Paragraph("Value", s["cellhead"]),
                 Paragraph("Source", s["cellhead"])]]
        for field in group.get("fields") or []:
            rows.append([
                Paragraph(_escape(field["label"]), s["cell"]),
                _value_cell(field, s),
                _source_cell(field, s),
            ])
        flow.append(KeepTogether([
            Paragraph(_escape(group["name"]), s["section"]),
            _table(rows, [width * 0.34, width * 0.46, width * 0.20]),
        ]))
    return flow


def _plain_fields(fields: list[dict[str, Any]], s: dict[str, ParagraphStyle],
                  width: float) -> Optional[Table]:
    if not fields:
        return None
    rows = [[Paragraph("Field", s["cellhead"]), Paragraph("Value", s["cellhead"]),
             Paragraph("Source", s["cellhead"])]]
    for field in fields:
        rows.append([
            Paragraph(_escape(field["label"]), s["cell"]),
            _value_cell(field, s),
            _source_cell(field, s),
        ])
    return _table(rows, [width * 0.34, width * 0.46, width * 0.20])


def _group_block(group: dict[str, Any], s: dict[str, ParagraphStyle],
                 width: float) -> list[Any]:
    instances = group.get("instances") or []
    label = _escape(f'{group["label"]} ({len(instances)} recorded)')
    if not instances:
        return [Paragraph(label, s["note"]),
                Paragraph("No rows recorded on the source document.", s["empty"])]

    columns = [f["field_id"] for f in instances[0]["fields"]]
    labels = {f["field_id"]: f["label"] for f in instances[0]["fields"]}

    # A wide repeating group reads better transposed than squeezed into one row.
    if len(columns) <= 6:
        rows = [[Paragraph("#", s["cellhead"])] +
                [Paragraph(_escape(labels[c]), s["cellhead"]) for c in columns]]
        for instance in instances:
            values = {f["field_id"]: f for f in instance["fields"]}
            rows.append([Paragraph(str(instance["instance"]), s["cell"])] +
                        [_value_cell(values[c], s) for c in columns])
        first = width * 0.08
        rest = (width - first) / len(columns)
        return [Paragraph(label, s["note"]), _table(rows, [first] + [rest] * len(columns))]

    flow: list[Any] = [Paragraph(label, s["note"])]
    for instance in instances:
        rows = [[Paragraph(f'{group["row_label"]} {instance["instance"]}', s["cellhead"]),
                 Paragraph("Value", s["cellhead"]), Paragraph("Source", s["cellhead"])]]
        for field in instance["fields"]:
            rows.append([Paragraph(_escape(field["label"]), s["cell"]),
                         _value_cell(field, s), _source_cell(field, s)])
        flow.append(KeepTogether(
            [_table(rows, [width * 0.34, width * 0.46, width * 0.20]), Spacer(1, 5)]
        ))
    return flow


def _form_block(payload: dict[str, Any], s: dict[str, ParagraphStyle],
                width: float) -> list[Any]:
    flow: list[Any] = []
    for section in (payload.get("form") or {}).get("sections") or []:
        flow.append(Paragraph(_escape(section["name"]), s["section"]))
        if section.get("description"):
            flow.append(Paragraph(_escape(section["description"]), s["note"]))
        plain = _plain_fields(section.get("fields") or [], s, width)
        if plain is not None:
            flow.extend([plain, Spacer(1, 7)])
        for group in section.get("groups") or []:
            flow.extend(_group_block(group, s, width))
            flow.append(Spacer(1, 5))
    return flow


# A pretty-printed provenance block costs five lines to say what fits on one.
_LEAF_OBJECT = re.compile(r'\{\s*\n(\s*"[^"{}]*?"\s*:\s*(?:"[^"]*"|-?[\d.]+|true|false|null)\s*,?\s*\n)+\s*\}')


def _compact_leaves(text: str) -> str:
    """Collapse objects with no nested objects onto a single line."""
    def collapse(match: "re.Match[str]") -> str:
        parts = [line.strip() for line in match.group(0).strip("{} \n").splitlines()]
        return "{ " + " ".join(part for part in parts if part) + " }"

    previous = None
    while previous != text:
        previous, text = text, _LEAF_OBJECT.sub(collapse, text)
    return text


def _appendix(payload: dict[str, Any], s: dict[str, ParagraphStyle]) -> list[Any]:
    data = _compact_leaves(
        json.dumps(mapping.for_export(payload, slim=True), indent=2, ensure_ascii=False)
    )
    wrapped: list[str] = []
    for line in data.splitlines():
        wrapped.extend(textwrap.wrap(line, width=112, subsequent_indent="    ",
                                     drop_whitespace=False) or [""])
    return [
        PageBreak(),
        Paragraph("Appendix", s["eyebrow"]),
        Paragraph("Cronos submission payload", s["title"]),
        Paragraph(
            "The submission payload for this document. Each field carries its value, "
            "the reader\u2019s confidence and the page it was read from; the page "
            "coordinates behind the red boxes are kept in the downloadable JSON only.",
            s["subtitle"],
        ),
        Preformatted("\n".join(wrapped), s["mono"]),
    ]


# --------------------------------------------------------------------------- document


def _chrome(payload: dict[str, Any]):
    """Header and footer painted on every page."""
    document = payload.get("document") or {}
    form = payload.get("form") or {}
    header_values = {
        field["field_id"]: field.get("value")
        for group in (payload.get("header") or {}).get("groups") or []
        for field in group.get("fields") or []
    }
    identity = " · ".join(
        str(value) for value in (
            header_values.get("protocol_no"),
            f'Subject {header_values["subject_no"]}' if header_values.get("subject_no") else None,
            header_values.get("visit_name"),
        ) if value
    ) or (document.get("filename") or "")

    def draw(canvas, doc):
        canvas.saveState()
        top = PAGE[1] - MARGIN + 6 * mm
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, top, (form.get("form_name") or "Case Report Form").upper())
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(PAGE[0] - MARGIN, top, identity[:90])
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, top - 2.5 * mm, PAGE[0] - MARGIN, top - 2.5 * mm)

        bottom = MARGIN - 6 * mm
        canvas.line(MARGIN, bottom + 4 * mm, PAGE[0] - MARGIN, bottom + 4 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, bottom,
                          "Machine-extracted from a source document — verify against the "
                          "original before use.")
        canvas.drawRightString(PAGE[0] - MARGIN, bottom, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def _cover(payload: dict[str, Any], s: dict[str, ParagraphStyle], width: float) -> list[Any]:
    document = payload.get("document") or {}
    form = payload.get("form") or {}
    summary = payload.get("summary") or {}

    flow: list[Any] = []
    if BRAND_LOGO.exists():
        logo = Image(str(BRAND_LOGO), width=30 * mm, height=30 * mm * 142 / 414)
        logo.hAlign = "LEFT"
        flow.extend([logo, Spacer(1, 8)])

    flow.append(Paragraph("Case report form", s["eyebrow"]))
    flow.append(Paragraph(_escape(form.get("form_name") or "Mapped document"), s["title"]))
    flow.append(Paragraph(
        _escape(f'{form.get("form_id")} · version {form.get("form_version")} · '
                f'{form.get("visit")} visit'),
        s["subtitle"],
    ))

    generated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    meta = [
        ("Source document", document.get("filename") or "—"),
        ("Pages", document.get("page_count") or "—"),
        ("Reading mode", (document.get("mode") or "—").title()),
        ("Generated", generated),
        ("Fields mapped", f'{summary.get("filled", 0)} of {summary.get("fields", 0)}'),
        ("Located on page", f'{summary.get("anchored", 0)} of {summary.get("filled", 0)}'),
        ("Needing review", summary.get("flagged", 0)),
    ]
    rows = [[Paragraph(_escape(k), s["cellhead"]), Paragraph(_escape(v), s["cell"])]
            for k, v in meta]
    table = Table(rows, colWidths=[width * 0.28, width * 0.72], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.extend([table, Spacer(1, 4)])
    flow.append(Paragraph(
        "An exclamation mark flags a value that needs a reviewer's eye. "
        "\"Source\" gives the page the "
        "value was read from and the reader's confidence in it; \"not located\" means the "
        "value could not be tied to a position on the page.",
        s["note"],
    ))
    return flow


def build_crf_pdf(payload: dict[str, Any]) -> bytes:
    s = _styles()
    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=PAGE,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN + 4 * mm, bottomMargin=MARGIN + 2 * mm,
        title=f'{(payload.get("form") or {}).get("form_name") or "Case Report Form"}',
        author="Webo Healthtech",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([
        PageTemplate(id="page", frames=[frame], onPage=_chrome(payload))
    ])

    flow = _cover(payload, s, doc.width)
    flow.append(Spacer(1, 4))
    flow.extend(_header_block(payload, s, doc.width))
    flow.extend(_form_block(payload, s, doc.width))
    flow.extend(_appendix(payload, s))

    doc.build(flow)
    return buffer.getvalue()
