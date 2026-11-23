"""Find the visits in a source PDF: which pages belong to which visit.

A file can hold one visit (the scans we have always processed) or a whole case
report form — seven visits, an unscheduled visit and a handful of pages that
belong to no visit at all. Every page prints the same header block, so the
structure is read from the headers alone:

    survey  every page: header text if the PDF has any, else the header strip
    read    text pages by pattern (free); scanned pages in batched model calls
            on the header strips only (small images, small model)
    vote    one protocol and one screening number for the whole file, by
            majority over every page that shows them
    group   pages by visit; misread and header-less pages resolved from their
            neighbours; pages that are not part of any visit set aside

Nothing here reads a form's fields, and nothing here talks to the database:
`plan()` is a pure function of the page reads, so every rule is unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger("extraction.splitter")

# Version of the reading rules. A stored plan from older rules is read again.
RULES_VERSION = 1

# Model calls: header strips per call, and calls in flight at once.
BATCH_SIZE = max(1, int(os.getenv("SPLIT_BATCH_SIZE") or 12))
CONCURRENCY = max(1, int(os.getenv("SPLIT_CONCURRENCY") or 4))
# A page with less ink than this, and no text, is a blank sheet (a duplex back).
BLANK_INK = 0.004

# Visit text that names no visit: forms kept for the whole study, not per visit.
_NOT_A_VISIT = re.compile(
    r"^(all\s+applicable|as\s+applicable|applicable\s+to\s+all|all\s+visits?|any\s+visit|"
    r"not\s+applicable|n/?a|common(\s+forms?)?|logs?|study\s+level|throughout(\s+the\s+study)?)\b",
    re.I)

_SPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def squash(text: Optional[str]) -> str:
    return _SPACE.sub(" ", text or "").strip()


# --------------------------------------------------------------------------- page reads


@dataclass
class PageRead:
    page: int
    # What the header says, as written. None when absent or unreadable.
    protocol_no: Optional[str] = None
    screening_no: Optional[str] = None
    visit_label: Optional[str] = None
    visit_date: Optional[str] = None
    # Whether the page prints the header block at all.
    has_header: bool = False
    confidence: float = 0.0
    # text | ai | blank | unread
    source: str = "unread"
    fingerprint: str = ""
    # Where each value sits on the page, 0..1 fractions {x, y, w, h}.
    rects: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageRead":
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def visit_key(label: Optional[str]) -> Optional[str]:
    """What makes two pages the same visit. 'Visit 1 Screening Period…' and
    'Visit 1 Screening and Washout visit…' are both visit 1."""
    text = squash(label)
    if not text:
        return None
    match = re.match(r"^visit\s*(?:no\.?\s*)?(\d{1,3})\b", text, re.I)
    if match:
        return f"visit {int(match.group(1))}"
    if _NOT_A_VISIT.match(text):
        return None
    words = _NON_ALNUM.sub(" ", text.lower()).strip()
    # "Visit Unscheduled Visit" style labels keep only their meaning.
    words = re.sub(r"^visit\s+", "", words)
    return words or None


def is_not_a_visit(label: Optional[str]) -> bool:
    text = squash(label)
    if not text:
        return False
    text = re.sub(r"^visit\s*[:\-]?\s*", "", text, flags=re.I)
    return bool(_NOT_A_VISIT.match(text))


def edc_names(label: Optional[str]) -> list[str]:
    """The visit names to try in the EDC, in order: as written first, then the
    short form the EDC has always been asked with ('Visit 3')."""
    text = squash(label)
    out: list[str] = []
    if text:
        out.append(text)
    match = re.match(r"^visit\s*(?:no\.?\s*)?(\d{1,3})\b", text, re.I)
    if match:
        out.append(f"Visit {int(match.group(1))}")
    elif text:
        bare = re.sub(r"^visit\s*[:\-]?\s*", "", text, flags=re.I)
        if bare and bare != text:
            out.append(bare)
    seen, unique = set(), []
    for name in out:
        if name.casefold() not in seen:
            seen.add(name.casefold())
            unique.append(name)
    return unique


def screening_digits(value: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def protocol_norm(value: Optional[str]) -> Optional[str]:
    text = re.sub(r"\s+", "", (value or "")).upper()
    return text or None


# --------------------------------------------------------------------------- text layer

_PROTOCOL = re.compile(
    r"Protocol\s*(?:No|Number|#)\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9/\-_.]*\d[A-Za-z0-9/\-_.]*)",
    re.I)
_SCREENING = re.compile(
    r"(?:Screening|Subject|Patient)\s*(?:No|Number|ID|#)\.?\s*[:\-]?\s*((?:[\[\]|_\s]*\d){1,12})",
    re.I)
# "Visit 2    Enrolment/ Baseline Visit (Week 0/ Day 1)". A digit after the number
# is the schedule-of-assessments table ("Visit 1 2 3 4"), not a header.
_VISIT_NUMBERED = re.compile(r"\bVisit\s*(?:No\.?\s*)?(\d{1,3})(?![\d/])(?!\s+\d)([^\r\n]*)", re.I)
# "Visit       Unscheduled Visit", "Visit      All Applicable".
_VISIT_NAMED = re.compile(r"\bVisit\s*[:\-]?\s+(?!Date\b|No\b|Name\b)([A-Za-z][^\r\n\[\]]{2,80})", re.I)
_DATE_HINT = re.compile(r"Visit\s*Date\s*[:\-]?\s*([0-9]{1,2}[\s/\-.][A-Za-z0-9]{2,9}[\s/\-.][0-9]{2,4})", re.I)


def read_text_header(page: int, text: str) -> PageRead:
    """The header block from the page's own text. Free, exact, and only for
    PDFs that carry text (typed or exported forms, not scans)."""
    read = PageRead(page=page, source="text")
    if not text:
        return read
    lines = [squash(line) for line in text.replace("\r", "\n").split("\n") if squash(line)]
    joined = "\n".join(lines)
    match = _PROTOCOL.search(joined)
    if match:
        read.protocol_no = match.group(1).strip(" .")
    match = _SCREENING.search(joined)
    if match:
        read.screening_no = screening_digits(match.group(1))
    for line in lines:
        numbered = _VISIT_NUMBERED.search(line)
        if numbered:
            rest = squash(numbered.group(2))
            read.visit_label = squash(f"Visit {numbered.group(1)} {rest}")
            break
        named = _VISIT_NAMED.search(line)
        if named:
            read.visit_label = squash(named.group(1))
            break
    match = _DATE_HINT.search(joined)
    if match:
        read.visit_date = squash(match.group(1))
    read.has_header = bool(read.visit_label or (read.protocol_no and "visit" in joined.lower()))
    # Printed text is exact; a header we could only half find is not.
    read.confidence = 1.0 if read.visit_label and read.protocol_no else 0.7 if read.visit_label else 0.0
    return read


# --------------------------------------------------------------------------- the plan


@dataclass
class Vote:
    value: Optional[str] = None
    agreement: float = 0.0
    pages_with_value: int = 0
    disagreeing: list[dict[str, Any]] = field(default_factory=list)


def _vote(reads: list[PageRead], attr: str, norm) -> Vote:
    seen = [(r.page, getattr(r, attr)) for r in reads if norm(getattr(r, attr))]
    if not seen:
        return Vote()
    counts = Counter(norm(v) for _, v in seen)
    winner, top = counts.most_common(1)[0]
    # Show it as written on the pages that agree.
    shown = Counter(v for _, v in seen if norm(v) == winner).most_common(1)[0][0]
    return Vote(value=shown, agreement=round(top / len(seen), 3), pages_with_value=len(seen),
                disagreeing=[{"page": p, "value": v} for p, v in seen if norm(v) != winner])


def _date_norm(value: Optional[str]) -> Optional[str]:
    text = _NON_ALNUM.sub("", (value or "").lower())
    return text if len(text) >= 5 else None


def plan(reads: list[PageRead]) -> dict[str, Any]:
    """Group pages into visits. Pure: the same reads always give the same plan."""
    reads = sorted(reads, key=lambda r: r.page)
    by_page = {r.page: r for r in reads}
    protocol = _vote(reads, "protocol_no", protocol_norm)
    screening = _vote(reads, "screening_no", screening_digits)

    # 1. What each page is, on its own.
    key: dict[int, Optional[str]] = {}
    kind: dict[int, str] = {}
    for r in reads:
        if r.source == "blank":
            kind[r.page] = "blank"
        elif r.visit_label and is_not_a_visit(r.visit_label):
            kind[r.page] = "not_visit"
        elif visit_key(r.visit_label):
            kind[r.page] = "visit"
            key[r.page] = visit_key(r.visit_label)
        else:
            kind[r.page] = "unknown"

    flags: dict[int, list[dict[str, Any]]] = {r.page: [] for r in reads}
    counts = Counter(k for k in key.values() if k)

    # 2. A lone page that names another visit, between two pages of the same
    #    visit, is a misread: the pages around it outvote it.
    pages = [r.page for r in reads]
    for i, page in enumerate(pages):
        if kind[page] != "visit" or i == 0 or i == len(pages) - 1:
            continue
        before, after = pages[i - 1], pages[i + 1]
        if kind[before] == kind[after] == "visit" and key[before] == key[after] \
                and key[page] != key[before] and counts[key[page]] == 1:
            flags[page].append({"code": "misread", "read_as": by_page[page].visit_label})
            key[page] = key[before]

    # 3. A page whose header couldn't be read belongs with the visit before it.
    #    Before any visit it belongs to none.
    last_key: Optional[str] = None
    for page in pages:
        if kind[page] == "visit":
            last_key = key[page]
        elif kind[page] == "unknown":
            if last_key:
                key[page] = last_key
                kind[page] = "visit"
                flags[page].append({"code": "inferred"})
        elif kind[page] == "not_visit":
            last_key = None
        # A blank sheet neither starts nor ends a visit.

    # 4. Segments, in the order the visits first appear.
    order: list[str] = []
    members: dict[str, list[int]] = {}
    for page in pages:
        k = key.get(page) if kind[page] == "visit" else None
        if not k:
            continue
        if k not in members:
            order.append(k)
            members[k] = []
        members[k].append(page)

    segments = []
    for index, k in enumerate(order):
        mine = members[k]
        labels = [by_page[p].visit_label for p in mine
                  if by_page[p].visit_label and visit_key(by_page[p].visit_label) == k]
        label = Counter(labels).most_common(1)[0][0] if labels else k.title()
        # The first label printed for the visit is how people know it.
        first = next((l for l in labels), label)
        seg_flags: list[dict[str, Any]] = []
        inferred = [p for p in mine if any(f["code"] == "inferred" for f in flags[p])]
        misread = [p for p in mine if any(f["code"] == "misread" for f in flags[p])]
        if inferred:
            seg_flags.append({"code": "inferred_pages", "pages": inferred})
        if misread:
            seg_flags.append({"code": "misread_pages", "pages": misread})
        between = [p for p in range(mine[0], mine[-1] + 1)
                   if p not in mine and kind.get(p) != "blank"]
        if between:
            seg_flags.append({"code": "not_contiguous", "pages": between})
        dates = {_date_norm(by_page[p].visit_date) for p in mine} - {None}
        if len(dates) > 1:
            seg_flags.append({"code": "multiple_dates",
                              "dates": sorted({squash(by_page[p].visit_date) for p in mine
                                               if _date_norm(by_page[p].visit_date)})})
        read_conf = [by_page[p].confidence for p in mine if by_page[p].visit_label]
        confidence = min(
            sum(read_conf) / len(read_conf) if read_conf else 0.0,
            protocol.agreement if protocol.value else 0.0,
            screening.agreement if screening.value else 0.0,
        )
        segments.append({
            "id": f"s{index + 1}",
            "key": k,
            "label": first,
            "labels_seen": sorted(set(labels)),
            "pages": mine,
            "edc_names": edc_names(first),
            "confidence": round(confidence, 3),
            "flags": seg_flags,
            "fingerprint": "",
        })

    ignored = []
    for page in pages:
        if kind[page] == "visit" and key.get(page):
            continue
        reason = {"blank": "blank_page", "not_visit": "not_a_visit"}.get(kind[page], "no_visit_header")
        ignored.append({"page": page, "reason": reason, "label": by_page[page].visit_label})

    issues = []
    if not protocol.value:
        issues.append({"code": "no_protocol"})
    elif protocol.agreement < 1:
        issues.append({"code": "protocol_disagree", "pages": protocol.disagreeing})
    if not screening.value:
        issues.append({"code": "no_screening"})
    elif screening.agreement < 1:
        issues.append({"code": "screening_disagree", "pages": screening.disagreeing})
    if not segments:
        issues.append({"code": "no_visits"})

    return {
        "rules": RULES_VERSION,
        "page_count": len(reads),
        "protocol": asdict(protocol),
        "screening": asdict(screening),
        "segments": segments,
        "ignored": ignored,
        "issues": issues,
        "pages": [r.as_dict() for r in reads],
    }


def with_fingerprints(result: dict[str, Any]) -> dict[str, Any]:
    """Each segment's fingerprint: its pages' fingerprints in order. The same
    sheets scanned into another file give the same visit fingerprint."""
    import hashlib

    prints = {p["page"]: p.get("fingerprint") or "" for p in result.get("pages") or []}
    for segment in result.get("segments") or []:
        joined = "|".join(prints.get(p, "") for p in segment["pages"])
        segment["fingerprint"] = hashlib.sha256(joined.encode()).hexdigest()[:40]
    return result


# --------------------------------------------------------------------------- editing


def apply_edits(proposed: dict[str, Any], edits: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The plan as the reviewer has corrected it.

    edits = {
      "assign":   {page: segment_id | "ignore"},   pages moved by hand
      "labels":   {segment_id: label},             a visit renamed
      "new":      [{"id": "n1", "label": "..."}],  visits added by hand
      "selected": {segment_id: bool},              what will be read
      "protocol_no": "...", "screening_no": "...", header corrected
    }
    """
    edits = edits or {}
    assign = {int(k): v for k, v in (edits.get("assign") or {}).items()}
    labels = edits.get("labels") or {}
    selected = edits.get("selected") or {}
    segments = {s["id"]: {**s, "pages": list(s["pages"]), "flags": list(s["flags"])}
                for s in proposed.get("segments") or []}
    for item in edits.get("new") or []:
        sid = str(item.get("id") or "")
        if sid and sid not in segments:
            label = squash(item.get("label")) or "New visit"
            segments[sid] = {"id": sid, "key": visit_key(label) or sid, "label": label,
                             "labels_seen": [], "pages": [], "edc_names": edc_names(label),
                             "confidence": 1.0, "flags": [], "fingerprint": "", "added": True}
    ignored = {i["page"]: dict(i) for i in proposed.get("ignored") or []}

    for page, target in assign.items():
        for segment in segments.values():
            if page in segment["pages"]:
                segment["pages"].remove(page)
        ignored.pop(page, None)
        if target == "ignore" or target not in segments:
            ignored[page] = {"page": page, "reason": "ignored_by_reviewer", "label": None}
        else:
            segments[target]["pages"].append(page)

    out = []
    for segment in segments.values():
        segment["pages"] = sorted(set(segment["pages"]))
        if segment["id"] in labels and squash(labels[segment["id"]]):
            segment["label"] = squash(labels[segment["id"]])
            segment["edc_names"] = edc_names(segment["label"])
            segment["renamed"] = True
        moved = [p for p in segment["pages"] if p in assign]
        if moved:
            segment["flags"] = [f for f in segment["flags"]
                                if f["code"] not in ("inferred_pages", "misread_pages")]
            segment["edited"] = True
        if not segment["pages"]:
            continue
        segment["selected"] = bool(selected.get(segment["id"], segment.get("selected", True)))
        out.append(segment)
    out.sort(key=lambda s: s["pages"][0])
    result = {**proposed, "segments": out,
              "ignored": sorted(ignored.values(), key=lambda i: i["page"])}
    for name in ("protocol_no", "screening_no"):
        value = squash(edits.get(name))
        if value:
            vote = dict(result["protocol" if name == "protocol_no" else "screening"])
            vote.update(value=value, corrected=True)
            result["protocol" if name == "protocol_no" else "screening"] = vote
    return with_fingerprints(result)


# --------------------------------------------------------------------------- reading


async def read_pages(survey, read_strips, usage: Optional[list] = None) -> list[PageRead]:
    """Every page's header: from its text when it has some, else from its strip.

    `read_strips(batch)` takes [(page, data_url)] and returns {page: dict} — the
    model call, injected so tests run without one. Its token usage, under key -1,
    is appended to `usage`.
    """
    reads: dict[int, PageRead] = {}
    pending: list[tuple[int, str]] = []
    for item in survey.pages:
        if item.header_text and sum(ch.isalpha() for ch in item.header_text) >= 12:
            read = read_text_header(item.page, item.header_text)
            if read.visit_label or read.protocol_no:
                read.fingerprint = item.fingerprint
                reads[item.page] = read
                continue
        if not item.has_text and item.ink < BLANK_INK:
            reads[item.page] = PageRead(page=item.page, source="blank",
                                        fingerprint=item.fingerprint)
            continue
        if item.band:
            pending.append((item.page, item.band))
        else:
            # Text, but no header in it: a cover, an instructions page, a table.
            reads[item.page] = PageRead(page=item.page, source="text",
                                        fingerprint=item.fingerprint)

    if pending:
        semaphore = asyncio.Semaphore(CONCURRENCY)
        batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

        async def one(batch):
            async with semaphore:
                return await read_strips(batch)

        results = await asyncio.gather(*(one(b) for b in batches))
        prints = {item.page: item.fingerprint for item in survey.pages}
        for batch, found in zip(batches, results):
            spent = (found or {}).get(-1)
            if spent and usage is not None:
                usage.append(spent.get("usage") or {})
            for page, _ in batch:
                row = (found or {}).get(page) or {}
                read = PageRead(
                    page=page, source="ai", fingerprint=prints.get(page, ""),
                    protocol_no=squash(row.get("protocol_no")) or None,
                    screening_no=screening_digits(row.get("screening_no")),
                    visit_label=squash(row.get("visit")) or None,
                    visit_date=squash(row.get("visit_date")) or None,
                    has_header=bool(row.get("has_header")),
                    confidence=float(row.get("confidence") or 0.0),
                    rects=row.get("rects") or {},
                )
                reads[page] = read
    return [reads[p] for p in sorted(reads)]


def header_seed(result: dict[str, Any], segment: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three header rows the review shows, already read from the pages, so the
    per-visit header read (two models, three pages) is never paid for again."""
    if segment is None:
        return []
    pages = {p["page"]: p for p in result.get("pages") or []}
    mine = [pages[p] for p in segment["pages"] if p in pages]

    def row(field_id: str, value: Optional[str], attr: str, confidence: float) -> dict[str, Any]:
        shown = next((p for p in mine if p.get(attr) and (
            screening_digits(p.get(attr)) == screening_digits(value) if field_id == "screening_no"
            else protocol_norm(p.get(attr)) == protocol_norm(value) if field_id == "protocol_no"
            else visit_key(p.get(attr)) == visit_key(value))), None)
        return {"field_id": field_id, "value": value,
                "evidence": f"Read from the header of {len(mine)} page(s)" if value else None,
                "locator": None, "page": (shown or {}).get("page"),
                "confidence": round(confidence, 3), "corrected": False, "struck_value": None,
                "rect": ((shown or {}).get("rects") or {}).get(attr)}

    protocol = result.get("protocol") or {}
    screening = result.get("screening") or {}
    conf = segment.get("confidence") or 0.0
    return [
        row("protocol_no", protocol.get("value"), "protocol_no",
            min(conf, protocol.get("agreement") or 0) if not protocol.get("corrected") else 1.0),
        row("screening_no", screening.get("value"), "screening_no",
            min(conf, screening.get("agreement") or 0) if not screening.get("corrected") else 1.0),
        row("visit_name", segment.get("label"), "visit_label", conf),
    ]
