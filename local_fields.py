"""Read header and CRF values from the parsed page text.

The parser has already turned the page into markdown. Protocol, screening
number, visit, and the labelled tables can be taken from that text, so the
field-extraction service being up or down does not change this step.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any, Optional

import cronos

log = logging.getLogger("extraction.local_fields")

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_PAGE = re.compile(r'<page\s+number="(\d+)">\s*(.*?)\s*</page>', re.S | re.I)
_PROTOCOL = re.compile(
    r"Protocol\s*No\.?\s*[:|]?\s*([A-Za-z0-9]+(?:/[A-Za-z0-9]+)+)", re.I
)
_SCREENING = re.compile(
    r"Screening\s*No\.?\s*[:|]?\s*((?:\d[\s.]*){3,16})", re.I
)
_VISIT = re.compile(r"\bVisit\s+(\d+)\b", re.I)
_TICK = re.compile(r"☑|☒|✓|✔|\[(?:x|X|yes)\]", re.I)
_EMPTY_BOX = re.compile(r"☐|\[(?:\s*|no)\]", re.I)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_TAG = re.compile(r"<[^>]+>")
_BOX = re.compile(r"\[([^\]]*)\]")


def extract_header(document: str) -> dict[str, Any]:
    """The three identifying fields, read from the parsed header block."""
    rows: list[dict[str, Any]] = []
    for page, text in _pages(document):
        plain = _plain(text)
        if not any(rows) or "protocol_no" not in {r["field_id"] for r in rows}:
            found = _PROTOCOL.search(plain)
            if found:
                rows.append(_hit("protocol_no", found.group(1), found.group(0),
                                  "Protocol No.", page))
        if "screening_no" not in {r["field_id"] for r in rows}:
            found = _SCREENING.search(plain)
            if found:
                digits = re.sub(r"\D", "", found.group(1))
                if digits:
                    rows.append(_hit("screening_no", digits, found.group(0),
                                      "Screening No.", page))
        if "visit_name" not in {r["field_id"] for r in rows}:
            found = _VISIT.search(plain)
            if found:
                rows.append(_hit("visit_name", str(int(found.group(1))),
                                  found.group(0), "Visit", page))
    allowed = set(cronos.HEADER_FIELD_IDS)
    rows = [row for row in rows if row["field_id"] in allowed]
    log.info("local header: %s",
             ", ".join(f"{r['field_id']}={r['value']}" for r in rows) or "nothing")
    return {"values": rows, "usage": _usage()}


def extract_form(document: str, form: dict[str, Any],
                 header: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One row per value that is actually printed, addressed like the model output."""
    del header  # the page already carries the header; it is not needed to read tables
    pages = _pages(document)
    tables = [
        {**table, "page": page}
        for page, text in pages
        for table in _tables(text)
    ]
    rows: list[dict[str, Any]] = []
    used: set[int] = set()
    for section in form.get("sections") or []:
        table = _section_table(section, tables, used)
        if table is None:
            log.info("local section %s: no table", section.get("name"))
            continue
        used.add(id(table))
        found = _read_section(section, table)
        log.info("local section %s: %s value(s) on page %s",
                 section.get("name"), len(found), table["page"])
        rows.extend(found)
    return {"values": rows, "usage": _usage(), "repaired": 0, "dropped": 0}


# --------------------------------------------------------------------------- pages and tables


def _pages(document: str) -> list[tuple[int, str]]:
    found = [(int(number), body) for number, body in _PAGE.findall(document or "")]
    return found or [(1, document or "")]


def _plain(text: str) -> str:
    text = _TAG.sub(" ", text or "")
    return re.sub(r"[*_`]+", "", text)


class _GridParser(HTMLParser):
    """HTML tables expanded so colspan and rowspan line up with the body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: Optional[list[list[tuple[str, int, int]]]] = None
        self._cells: Optional[list[tuple[str, int, int]]] = None
        self._buf: Optional[list[str]] = None
        self._span = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._cells = []
        elif tag in {"td", "th"} and self._cells is not None:
            self._buf = []
            self._span = (_span(attr.get("colspan")), _span(attr.get("rowspan")))
        elif tag == "br" and self._buf is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._buf is not None and self._cells is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._cells.append((text, self._span[0], self._span[1]))
            self._buf = None
        elif tag == "tr" and self._cells is not None and self._rows is not None:
            self._rows.append(self._cells)
            self._cells = None
        elif tag == "table" and self._rows is not None:
            grid = _expand(self._rows)
            if grid:
                self.tables.append(grid)
            self._rows = None

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def _span(value: Optional[str]) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


def _expand(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    occupied: dict[tuple[int, int], str] = {}
    grid: list[list[str]] = []
    for r, cells in enumerate(rows):
        row: list[str] = []
        c = 0
        for text, colspan, rowspan in cells:
            while (r, c) in occupied:
                row.append(occupied[(r, c)])
                c += 1
            for dc in range(colspan):
                for dr in range(rowspan):
                    occupied[(r + dr, c + dc)] = text
                row.append(text)
            c += colspan
        while (r, c) in occupied:
            row.append(occupied[(r, c)])
            c += 1
        if any(cell.strip() for cell in row):
            grid.append(row)
    return grid


def _pipe_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [re.sub(r"\*+", "", cell).strip()
                     for cell in stripped.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) or cell == ""
                             for cell in cells):
                continue
            current.append(cells)
            continue
        if len(current) >= 2:
            tables.append(current)
        current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


def _tables(text: str) -> list[dict[str, Any]]:
    parser = _GridParser()
    try:
        parser.feed(text or "")
    except Exception as exc:
        log.warning("html table parse failed: %s", exc)
    found = [{"grid": grid, "plain": _plain(text)} for grid in parser.tables]
    if not found:
        found = [{"grid": grid, "plain": _plain(text)} for grid in _pipe_tables(text)]
    return found


def _unique(row: list[str]) -> list[str]:
    seen: list[str] = []
    for cell in row:
        text = (cell or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _is_header(row: list[str]) -> bool:
    texts = _unique(row)
    if len(texts) < 2:
        return False
    blob = " ".join(texts)
    if _TICK.search(blob) or _EMPTY_BOX.search(blob) or re.search(r"\[\d", blob):
        return False
    return True


def _split_table(grid: list[list[str]]) -> tuple[str, list[str], list[list[str]]]:
    """Preamble text, the column headers, and the data rows under them."""
    data_at = None
    for index, row in enumerate(grid):
        if _is_header(row) or len(_unique(row)) < 2:
            continue
        data_at = index
        break
    if data_at is None:
        return " ".join(" ".join(_unique(row)) for row in grid), [], []
    header_at = data_at - 1
    start = header_at
    while start > 0 and _is_header(grid[start - 1]):
        start -= 1
    if header_at < 0 or not _is_header(grid[header_at]):
        return " ".join(" ".join(_unique(row)) for row in grid[:data_at]), [], grid[data_at:]
    preamble = " ".join(" ".join(_unique(row)) for row in grid[:start])
    return preamble, grid[header_at], grid[data_at:]


def _section_table(section: dict[str, Any], tables: list[dict[str, Any]],
                   used: set[int]) -> Optional[dict[str, Any]]:
    name = _norm(section.get("name") or "")
    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for table in tables:
        if id(table) in used:
            continue
        blob = _norm(" ".join(" ".join(row) for row in table["grid"]))
        score = 0.0
        if name and name in blob:
            score = 1.0
        elif name:
            score = _overlap(name, blob)
        if score > best_score:
            best, best_score = table, score
    return best if best_score >= 0.6 else None


# --------------------------------------------------------------------------- reading a section


def _read_section(section: dict[str, Any], table: dict[str, Any]) -> list[dict[str, Any]]:
    preamble, headers, data = _split_table(table["grid"])
    page = table["page"]
    section_id = section["section_id"]
    rows: list[dict[str, Any]] = []
    plain = section.get("fields") or []
    rows.extend(_read_plain(section_id, plain, preamble, page))

    group = section.get("group") or None
    if group and data:
        rows.extend(_read_group(section_id, group, headers, data, page))
    return rows


def _read_plain(section_id: str, fields: list[dict[str, Any]],
                preamble: str, page: int) -> list[dict[str, Any]]:
    if not preamble.strip() or not fields:
        return []
    marks = []
    for field in fields:
        found = _find_label(preamble, field.get("label") or "")
        if found:
            marks.append((found.start(), found.end(), field))
    marks.sort(key=lambda item: item[0])
    rows = []
    for index, (start, end, field) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else len(preamble)
        snippet = preamble[start:stop]
        value, evidence, confidence = _value_from_text(field, snippet)
        if value is None:
            continue
        rows.append(_record(section_id, None, 1, field, value, evidence,
                            field.get("label") or "", page, confidence))
    return rows


def _read_group(section_id: str, group: dict[str, Any], headers: list[str],
                data: list[list[str]], page: int) -> list[dict[str, Any]]:
    fields = group.get("fields") or []
    naming = next((field for field in fields if field.get("row_name")), None)
    names = [_norm(name) for name in (group.get("row_names") or [])]
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for offset, cells in enumerate(data):
        if not cells or not (cells[0] or "").strip():
            continue
        label = cells[0].strip().lstrip("*").strip()
        instance = _instance(label, names, offset, seen)
        if instance is None:
            continue
        seen.add(instance)
        if naming is not None:
            rows.append(_record(section_id, group["group_id"], instance, naming,
                                label, label, label, page, 0.9))
        body_headers = headers[1:] if len(headers) > 1 else headers
        body_cells = cells[1:] if len(cells) > 1 else cells
        for field in fields:
            if field is naming:
                continue
            value, evidence, confidence = _value_from_row(field, body_cells, body_headers, cells)
            if value is None:
                continue
            rows.append(_record(section_id, group["group_id"], instance, field,
                                value, evidence, label, page, confidence))
    return rows


def _instance(label: str, names: list[str], offset: int, seen: set[int]) -> Optional[int]:
    if not names:
        instance = offset + 1
        return None if instance in seen else instance
    wanted = _norm(label)
    best_at = None
    best = 0.0
    for index, name in enumerate(names):
        score = _label_score(wanted, name)
        if score > best:
            best, best_at = score, index
    if best_at is None or best < 0.72:
        return None
    instance = best_at + 1
    return None if instance in seen else instance


def _value_from_row(field: dict[str, Any], cells: list[str], headers: list[str],
                    whole: list[str]) -> tuple[Optional[str], str, float]:
    options = [str(option) for option in (field.get("options") or [])]
    option_at = []
    if options:
        for index, header in enumerate(headers):
            if any(_label_score(_norm(header), _norm(option)) >= 0.85 for option in options):
                option_at.append(index)
    if option_at:
        bits = []
        for index in option_at:
            cell = cells[index] if index < len(cells) else ""
            header = headers[index] if index < len(headers) else ""
            if _tick_only(cell):
                bits.append(("☑ " if _checked(cell) else "☐ ") + header)
            else:
                bits.append(cell)
        text = " ".join(bits)
        value, confidence = _chosen_option(text, options)
        return value, text or " ".join(whole), confidence

    best_at, best = None, 0.0
    for index, header in enumerate(headers):
        score = _label_score(_norm(field.get("label") or ""), _norm(header))
        if score > best:
            best, best_at = score, index
    if best_at is not None and best >= 0.62 and best_at < len(cells):
        return _value_from_text(field, cells[best_at])

    if _is_unit(field):
        text = " ".join(whole)
        unit = _read_unit(text, options)
        if unit:
            return unit, text, 0.8
    if options:
        text = " ".join(whole)
        value, confidence = _chosen_option(text, options)
        if value:
            return value, text, confidence
    return None, "", 0.0


def _value_from_text(field: dict[str, Any], text: str) -> tuple[Optional[str], str, float]:
    options = [str(option) for option in (field.get("options") or [])]
    if options:
        value, confidence = _chosen_option(text, options)
        return value, text.strip(), confidence
    if (field.get("type") or "") == "date" or "date" in _norm(field.get("label") or ""):
        value = _read_date(text)
        return value, text.strip(), 0.85 if value else 0.0
    if _is_unit(field):
        unit = _read_unit(text, options)
        return unit, text.strip(), 0.8 if unit else 0.0
    if _is_result(field) or (field.get("type") or "") == "number":
        value = _read_measure(text)
        return value, text.strip(), 0.85 if value else 0.0
    cleaned = _clean_text(text, field.get("label") or "")
    if _blank(cleaned):
        return None, text.strip(), 0.0
    return cleaned, text.strip(), 0.75


def _chosen_option(text: str, options: list[str]) -> tuple[Optional[str], float]:
    hits: list[str] = []
    folded = text or ""
    for option in options:
        pattern = re.compile(r"(?:☑|☒|✓|✔|\[(?:x|X|yes)\])\s*" + re.escape(option), re.I)
        if pattern.search(folded):
            hits.append(option)
            continue
        # A column that is only the option name, already prefixed when the cell was a bare tick.
        if re.search(r"(?:☑|☒|✓|✔)\s*" + re.escape(option) + r"\b", folded, re.I):
            hits.append(option)
    if len(hits) == 1:
        return hits[0], 0.9
    if len(hits) > 1:
        return hits[0], 0.5
    return None, 0.0


def _read_measure(text: str) -> Optional[str]:
    """The number written in character boxes, or the spaced digits beside a crossed-out box."""
    boxes = _BOX.findall(text or "")
    if boxes:
        kept: list[str] = []
        trustworthy = True
        for box in boxes:
            token = box.strip()
            if token in {"", "_", "-", "."}:
                continue
            if re.fullmatch(r"\d", token):
                kept.append(token)
            else:
                trustworthy = False
        if trustworthy and len(kept) >= 2:
            collapsed = _BOX.sub(lambda match: re.sub(r"\D", "", match.group(1)), text)
            head = collapsed.split("(")[0]
            found = re.search(r"\d+(?:\.\d+)?", head)
            if found:
                return found.group(0)
    spaced = re.search(r"(?:\d\s+){1,}\d(?:\s*\.\s*\d+)?", text or "")
    if spaced:
        return re.sub(r"\s+", "", spaced.group(0))
    found = re.search(r"\d+(?:\.\d+)?", text or "")
    return found.group(0) if found else None


def _read_date(text: str) -> Optional[str]:
    measure = _read_measure(text)
    digits = re.sub(r"\D", "", measure or "")
    if len(digits) >= 6:
        day, month, year = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{day:02d} {_MONTHS[month - 1]} {2000 + year}"
    named = re.search(
        r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b", text or ""
    )
    if named:
        return f"{named.group(1)} {named.group(2)} {named.group(3)}"
    return None


def _read_unit(text: str, options: list[str]) -> Optional[str]:
    if options:
        chosen, _confidence = _chosen_option(text, options)
        # A listed unit that is not ticked is blank. Do not substitute the
        # result's own unit, such as mmHg, into a °C/°F field.
        return chosen
    found = re.search(
        r"(?:☑|☒|✓|✔|\[(?:x|X|yes)\])\s*(°\s*[CFcf])\b", text or ""
    )
    if found:
        unit = re.sub(r"\s+", "", found.group(1))
        return "°" + unit[-1].upper()
    paren = re.search(r"\(([^)]+)\)", text or "")
    if paren and re.search(r"[A-Za-z]", paren.group(1)):
        return paren.group(1).strip()
    trailing = re.search(r"\d\s*([A-Za-z°/]{1,12})\s*$", (text or "").strip())
    if trailing:
        return trailing.group(1)
    return None


def _clean_text(text: str, label: str) -> str:
    body = text or ""
    found = _find_label(body, label)
    if found:
        body = body[found.end():]
    body = re.sub(r"\s+", " ", body).strip(" :|-—")
    return body


def _tick_only(cell: str) -> bool:
    if not (cell or "").strip():
        return False
    leftover = _TICK.sub("", cell)
    leftover = _EMPTY_BOX.sub("", leftover)
    leftover = re.sub(r"[\[\]xX\s]", "", leftover)
    return leftover == ""


def _checked(cell: str) -> bool:
    return bool(_TICK.search(cell or ""))


def _blank(text: Optional[str]) -> bool:
    return _norm(text or "") in {"", "na", "n a", "none", "null", "not recorded"}


def _is_unit(field: dict[str, Any]) -> bool:
    return "unit" in _norm(field.get("label") or "")


def _is_result(field: dict[str, Any]) -> bool:
    return _norm(field.get("label") or "") in {"result", "value", "reading"}


def _find_label(text: str, label: str) -> Optional[re.Match[str]]:
    words = _norm(label).split()
    if not words:
        return None
    pattern = r"\b" + r"\W+".join(re.escape(word) for word in words)
    return re.search(pattern, text or "", re.I)


def _norm(text: str) -> str:
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def _label_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        shorter, longer = sorted((left, right), key=len)
        return 0.72 + 0.28 * (len(shorter) / max(len(longer), 1))
    shared = len(set(left.split()) & set(right.split()))
    return shared / max(len(left.split()), len(right.split()))


def _overlap(name: str, blob: str) -> float:
    words = [word for word in name.split() if len(word) > 2]
    if not words:
        return 0.0
    return sum(word in blob for word in words) / len(words)


def _hit(field_id: str, value: str, evidence: str, locator: str, page: int) -> dict[str, Any]:
    return {
        "field_id": field_id,
        "value": value,
        "evidence": evidence.strip(),
        "locator": locator,
        "page": page,
        "confidence": 0.9,
    }


def _record(section_id: str, group_id: Optional[str], instance: int,
            field: dict[str, Any], value: str, evidence: str, locator: str,
            page: int, confidence: float) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "group_id": group_id,
        "instance": instance,
        "field_id": field["field_id"],
        "value": value,
        "evidence": (evidence or value).strip(),
        "locator": locator,
        "page": page,
        "confidence": confidence,
    }


def _usage() -> dict[str, Any]:
    return {"prompt_tokens": None, "completion_tokens": None, "model": "local"}
