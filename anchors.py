"""Anchoring — tie an extracted value back to a rectangle on the original page.

The field-extraction pass reads markdown, so it returns values and the wording it
saw, but no coordinates.  The parser's layout items *do* carry coordinates: every
item has a list of bounding boxes, most of which record the character range of the
item text they cover.  Locating the quoted evidence inside an item therefore gives
a tight rectangle rather than a box around the whole paragraph.

Rectangles come back normalised to 0..1 of the page, so the browser can draw them
over a page rendered at any zoom.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Optional

# Below this share of the evidence matched, the box would sit somewhere arbitrary
# and is worse than admitting we could not place it.
MIN_FUZZY_RATIO = 0.62
MIN_FUZZY_CHARS = 4

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_SEPARATOR_ROW = re.compile(r"^[\s|:+-]+$")


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """The whole line containing [start, end) — a table row, in practice."""
    return text.rfind("\n", 0, start) + 1, (
        text.find("\n", end) if text.find("\n", end) >= 0 else len(text)
    )


def _normalise(text: str) -> tuple[str, list[int]]:
    """Lowercase and collapse punctuation, keeping a map back to original offsets."""
    out: list[str] = []
    index: list[int] = []
    previous_space = True
    for position, char in enumerate(text.lower()):
        if _NON_ALNUM.match(char):
            if not previous_space:
                out.append(" ")
                index.append(position)
                previous_space = True
            continue
        out.append(char)
        index.append(position)
        previous_space = False
    return "".join(out), index


def _item_text(item: dict[str, Any]) -> str:
    for key in ("value", "md", "csv", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extent(boxes: list[dict[str, Any]], width: float,
            height: float) -> Optional[dict[str, float]]:
    """The region an item really occupies: the union of its boxes.

    A merged form-table is given one box covering only part of itself plus loose
    boxes for the rest, so the first box alone badly understates the block and any
    row worked out from it lands far too high. Page-sized wrappers are ignored,
    since they describe the sheet rather than the item.
    """
    usable = [
        box for box in boxes
        if all(isinstance(box.get(k), (int, float)) for k in ("x", "y", "w", "h"))
        and box["w"] > 0 and box["h"] > 0
        and not (box["h"] >= 0.75 * height and box["w"] >= 0.75 * width)
    ]
    if not usable:
        return None
    x0 = min(b["x"] for b in usable)
    y0 = min(b["y"] for b in usable)
    x1 = max(b["x"] + b["w"] for b in usable)
    y1 = max(b["y"] + b["h"] for b in usable)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _boxes_are_normalised(pages: list[dict[str, Any]]) -> bool:
    """Guard against a parser build that already returns 0..1 coordinates."""
    for page in pages:
        for item in page.get("items") or []:
            for box in item.get("bbox") or []:
                if max(box.get("x", 0), box.get("y", 0),
                       box.get("w", 0), box.get("h", 0)) > 1.5:
                    return False
    return True


class PageIndex:
    """Searchable view of one parse job's layout items."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._normalised_boxes = _boxes_are_normalised(pages) if pages else False
        self._entries: list[dict[str, Any]] = []
        for page in pages or []:
            if page.get("success") is False:
                continue
            number = page.get("page_number")
            width = float(page.get("page_width") or 0) or 1.0
            height = float(page.get("page_height") or 0) or 1.0
            for item in page.get("items") or []:
                text = _item_text(item)
                if not text.strip():
                    continue
                norm, index = _normalise(text)
                if not norm.strip():
                    continue
                boxes = item.get("bbox") or []
                self._entries.append({
                    "page": number,
                    "width": width,
                    "height": height,
                    "raw": text,
                    "norm": norm,
                    "map": index,
                    "bbox": boxes,
                    "type": item.get("type"),
                    "extent": _extent(boxes, width, height),
                })

    def __bool__(self) -> bool:
        return bool(self._entries)

    @property
    def page_sizes(self) -> dict[int, dict[str, float]]:
        sizes: dict[int, dict[str, float]] = {}
        for entry in self._entries:
            sizes.setdefault(entry["page"], {"width": entry["width"], "height": entry["height"]})
        return sizes

    # ------------------------------------------------------------------ matching

    def _locate(self, entry: dict[str, Any], needle: str) -> Optional[tuple[int, int, float]]:
        """Return (start, end, score) in *original* offsets, or None."""
        hay = entry["norm"]
        position = hay.find(needle)
        if position >= 0:
            return self._to_original(entry, position, position + len(needle), 1.0)

        if len(needle) < MIN_FUZZY_CHARS:
            return None
        matcher = SequenceMatcher(None, hay, needle, autojunk=False)
        block = matcher.find_longest_match(0, len(hay), 0, len(needle))
        if block.size < MIN_FUZZY_CHARS or block.size / len(needle) < MIN_FUZZY_RATIO:
            return None
        return self._to_original(entry, block.a, block.a + block.size, block.size / len(needle))

    def _narrow_to_value(self, entry: dict[str, Any], start: int, end: int,
                         value: Any) -> tuple[int, int]:
        """Shrink an evidence match down to the value inside it.

        The model quotes enough context to locate a value — often a whole table
        row — so the quote alone would put the box on the row's first cell. The
        value is searched for inside the quoted span, which is what the box should
        actually sit on.

        A `locator` match is only the printed label, so the value is never inside
        it; the search widens to the rest of the line, where the value is sitting
        in the next cell along.
        """
        if value is None:
            return start, end
        needle, _ = _normalise(str(value))
        needle = needle.strip()
        if not needle:
            return start, end

        # These forms are full of character boxes — "[0][3][0][1][3]" for a
        # screening number, "[1][6]/[0][2]" for a date. The brackets normalise to
        # spaces, so the value's own characters have to be spread out to match.
        spread = " ".join(needle.replace(" ", ""))
        candidates = [needle] if spread == needle else [needle, spread]

        for origin, stop in ((start, end), _line_span(entry["raw"], start, end)):
            norm, mapping = _normalise(entry["raw"][origin:stop])
            for candidate in candidates:
                position = norm.find(candidate)
                if position < 0 or not mapping:
                    continue
                first = mapping[position]
                last = mapping[min(position + len(candidate), len(mapping)) - 1] + 1
                return origin + first, origin + last
        return start, end

    @staticmethod
    def _to_original(entry: dict[str, Any], start: int, end: int,
                     score: float) -> tuple[int, int, float]:
        mapping = entry["map"]
        end = min(end, len(mapping))
        start = min(start, max(end - 1, 0))
        first = mapping[start] if mapping else 0
        last = mapping[end - 1] + 1 if end > 0 and mapping else first
        return first, last, score

    def _rects(self, entry: dict[str, Any], start: int, end: int) -> list[dict[str, float]]:
        """Boxes covering [start, end), narrowed to the line or cell where possible."""
        boxes = [box for box in entry["bbox"]
                 if self._usable(box) and not self._is_page_sized(entry, box)]
        ranged = [
            box for box in boxes
            if box.get("start_index") is not None and box.get("end_index") is not None
        ]

        if ranged:
            hits = [box for box in ranged
                    if box["start_index"] < end and box["end_index"] > start]
            # Boxes carry ranges but none covers the match: a genuine miss, not a
            # reason to fall back to boxing the whole item.
            return [rect for box in hits
                    for rect in self._narrow(entry, box, box["start_index"],
                                             box["end_index"], start, end)]

        # No ranges at all — each box is assumed to cover the item's whole text.
        return [rect for box in boxes
                for rect in self._narrow(entry, box, 0, len(entry["raw"]), start, end)]

    def _narrow(self, entry: dict[str, Any], box: dict[str, Any], box_start: int,
                box_end: int, start: int, end: int) -> list[dict[str, float]]:
        """Shrink a box that spans many lines down to the line actually matched.

        The parser returns a single box for a whole table and for a multi-line
        paragraph, so without this every value in a table would be drawn over the
        entire table. Lines are evenly spaced in practice, so the row a value came
        from can be interpolated reliably.

        Columns are subdivided from the pipe positions in the markdown. Widths are
        taken from the widest row rather than the matched one, so every row shares
        one grid even where the parser has not padded the table to align. This is a
        proxy for the printed column widths, not a reading of them, so the box is
        still reported as approximate — but a box over the right cell beats one
        stretched across the whole row, which located nothing at all.
        """
        text = entry["raw"][box_start:box_end]
        lines = text.split("\n")
        is_table = entry.get("type") == "table"

        # A single-line box is already as tight as the parser can make it.
        if len(lines) <= 1:
            return [self._scale(entry, box)]

        spans: list[tuple[int, int]] = []
        cursor = 0
        for line in lines:
            spans.append((cursor, cursor + len(line)))
            cursor += len(line) + 1

        offset = max(start - box_start, 0)
        line_index = next(
            (i for i, (_, stop) in enumerate(spans) if stop > offset), len(lines) - 1
        )

        # A pipe table's rule row occupies no height on the page.
        rows = [
            i for i, line in enumerate(lines)
            if line.strip() and not _SEPARATOR_ROW.match(line)
        ] if is_table else list(range(len(lines)))
        if line_index not in rows:
            return [self._scale(entry, box)]

        position = rows.index(line_index)
        left, right = self._columns(lines, rows, line_index, offset - spans[line_index][0])
        # The row is worked out against the item's full extent; the box that
        # happened to match may cover only part of it.
        frame = entry.get("extent") or box
        top, bottom = self._bands(entry, frame, rows, spans, box_start)[position]
        scaled = self._scale(entry, {
            "x": frame["x"] + frame["w"] * left,
            "y": top,
            "w": frame["w"] * (right - left),
            "h": bottom - top,
        })
        scaled["approximate"] = True
        return [scaled]

    def _bands(self, entry: dict[str, Any], frame: dict[str, float],
               rows: list[int], spans: list[tuple[int, int]],
               box_start: int) -> list[tuple[float, float]]:
        """Top and bottom of every row, pinned wherever the parser says.

        A form table comes back as one box, so rows have to be placed inside it.
        Spacing them evenly assumes every row is the same height, which is wrong
        on exactly the tables that matter — a header where one row wraps to two
        printed lines and the next is a single short line.

        The parser does emit a few inner boxes carrying character ranges. Each of
        those pins the row its range falls in to a real measurement; the rows in
        between are then spread across whatever space is left. With no inner
        boxes this degrades to the even spacing it replaces.
        """
        count = len(rows)
        top, bottom = frame["y"], frame["y"] + frame["h"]
        even = [(top + (bottom - top) * i / count,
                 top + (bottom - top) * (i + 1) / count) for i in range(count)]

        known: dict[int, tuple[float, float]] = {}
        for candidate in entry["bbox"]:
            start = candidate.get("start_index")
            end = candidate.get("end_index")
            if start is None or end is None or not self._usable(candidate):
                continue
            if self._is_page_sized(entry, candidate):
                continue
            # The box wrapping the whole item pins nothing — it is the frame.
            if end - start >= len(entry["raw"]) - 1:
                continue
            for position, line in enumerate(rows):
                first, last = spans[line][0] + box_start, spans[line][1] + box_start
                if start < last and end > first:
                    low, high = known.get(
                        position, (candidate["y"], candidate["y"] + candidate["h"]))
                    known[position] = (min(low, candidate["y"]),
                                       max(high, candidate["y"] + candidate["h"]))
        if not known:
            return even

        edges: list[Optional[float]] = [None] * (count + 1)
        edges[0], edges[count] = top, bottom
        for position, (low, high) in known.items():
            edges[position] = low if edges[position] is None else min(edges[position], low)
            edges[position + 1] = high if edges[position + 1] is None else max(
                edges[position + 1], high)

        index = 1
        while index <= count:
            if edges[index] is not None:
                index += 1
                continue
            nxt = index
            while edges[nxt] is None:
                nxt += 1
            low, high = edges[index - 1], edges[nxt]
            step = (high - low) / (nxt - index + 1)
            for k in range(index, nxt):
                edges[k] = low + step * (k - index + 1)
            index = nxt

        bands = [(edges[i], edges[i + 1]) for i in range(count)]
        # A measurement that disagrees with the reading order is a misread box,
        # not a row: fall back rather than draw something incoherent.
        if any(b <= a for a, b in bands):
            return even
        return bands

    @staticmethod
    def _columns(lines: list[str], rows: list[int], line_index: int,
                 offset: int) -> tuple[float, float]:
        """Left and right edge of the matched cell, as a fraction of the row.

        Returns the full width whenever the shape cannot be trusted — a line that
        is not a pipe row, or a table whose rows disagree on how many cells they
        have, where guessing a column would place the box arbitrarily.
        """
        grid = [[p for p, char in enumerate(lines[i]) if char == "|"] for i in rows]
        grid = [bars for bars in grid if len(bars) >= 2]
        if not grid:
            return 0.0, 1.0

        # The width the parser reports for a table can fall a character short of
        # its text, which costs the last row its closing pipe. The column count
        # is therefore what most rows agree on, not what all of them do, and a
        # row that disagrees is left out of the measurements rather than
        # disabling columns for the whole table.
        counts = Counter(len(bars) - 1 for bars in grid)
        count = counts.most_common(1)[0][0]
        grid = [bars for bars in grid if len(bars) - 1 == count]
        if count < 1 or len(grid) < 2:
            return 0.0, 1.0

        bars = [p for p, char in enumerate(lines[line_index]) if char == "|"]
        cell = next((k for k in range(min(count, len(bars) - 1))
                     if bars[k] < offset <= bars[k + 1]), None)
        if cell is None:
            return 0.0, 1.0

        widths = [max(bars[k + 1] - bars[k] for bars in grid) for k in range(count)]
        total = sum(widths) or 1
        before = sum(widths[:cell])
        return before / total, (before + widths[cell]) / total

    @staticmethod
    def _usable(box: dict[str, Any]) -> bool:
        return all(isinstance(box.get(k), (int, float)) for k in ("x", "y", "w", "h")) \
            and box["w"] > 0 and box["h"] > 0

    def _is_page_sized(self, entry: dict[str, Any], box: dict[str, Any]) -> bool:
        """A wrapper around the whole sheet locates nothing and must not be drawn."""
        return (box["h"] >= 0.75 * entry["height"]
                and box["w"] >= 0.75 * entry["width"])

    def _scale(self, entry: dict[str, Any], box: dict[str, Any]) -> dict[str, float]:
        """Page coordinates to a 0..1 fraction, so any zoom draws the same box."""
        width = 1.0 if self._normalised_boxes else entry["width"]
        height = 1.0 if self._normalised_boxes else entry["height"]
        x = max(box["x"] / width, 0.0)
        y = max(box["y"] / height, 0.0)
        return {
            "x": round(x, 5),
            "y": round(y, 5),
            "w": round(min(box["w"] / width, 1.0 - x), 5),
            "h": round(min(box["h"] / height, 1.0 - y), 5),
        }

    def anchor(self, evidence: Optional[str], value: Any = None,
               page_hint: Optional[int] = None,
               locator: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Best rectangle for a value, preferring the printed label beside it.

        On a tick-box form the reader cannot quote a tick, so its `evidence` turns
        into prose like "Lymph Nodes row shows Normal checked", which matches
        nothing verbatim and fuzzy-matches onto the wrong row. The `locator` — the
        row or field label as printed — is real text on the page and is tried first.
        """
        spoken = None if value is None else str(value)
        candidates = [text for text in (locator, evidence, spoken) if text]
        if not candidates or not self._entries:
            return None

        best: Optional[dict[str, Any]] = None
        for rank, text in enumerate(candidates):
            needle, _ = _normalise(text)
            needle = needle.strip()
            if not needle:
                continue
            for entry in self._entries:
                found = self._locate(entry, needle)
                if not found:
                    continue
                start, end, score = found
                # A locator names the row; widen from that cell across the value
                # so the box covers "Pulse rate" and 080 together, not one or the other.
                loc_rects: list[dict[str, float]] = []
                if text != spoken:
                    if text == locator:
                        loc_rects = self._rects(entry, start, end)
                    start, end = self._narrow_to_value(entry, start, end, value)
                rects = self._rects(entry, start, end)
                if loc_rects:
                    merged = merge_rects(loc_rects + rects)
                    if merged:
                        rects = [merged]
                if not rects:
                    continue
                # Prefer the quoted evidence over the bare value, an exact hit over a
                # partial one, and the page the extractor said it read. Between two
                # equally good matches the tighter box locates more.
                area = sum(rect["w"] * rect["h"] for rect in rects)
                weight = (score - (0.15 * rank)
                          + (0.1 if entry["page"] == page_hint else 0.0)
                          - (0.05 * min(area, 1.0)))
                if best is None or weight > best["_weight"]:
                    best = {
                        "_weight": weight,
                        "page": entry["page"],
                        "rects": rects,
                        "score": round(score, 3),
                        "exact": score >= 1.0,
                        "matched_text": entry["raw"][start:end],
                    }
            if best and best["exact"]:
                break

        if best:
            best.pop("_weight", None)
        return best


def merge_rects(rects: list[dict[str, float]]) -> Optional[dict[str, float]]:
    """Single enclosing rectangle — used where only one box can be drawn."""
    if not rects:
        return None
    x0 = min(r["x"] for r in rects)
    y0 = min(r["y"] for r in rects)
    x1 = max(r["x"] + r["w"] for r in rects)
    y1 = max(r["y"] + r["h"] for r in rects)
    return {"x": round(x0, 5), "y": round(y0, 5),
            "w": round(x1 - x0, 5), "h": round(y1 - y0, 5)}
