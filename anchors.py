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
from difflib import SequenceMatcher
from typing import Any, Optional

# Below this share of the evidence matched, the box would sit somewhere arbitrary
# and is worse than admitting we could not place it.
MIN_FUZZY_RATIO = 0.62
MIN_FUZZY_CHARS = 4

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


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
                self._entries.append({
                    "page": number,
                    "width": width,
                    "height": height,
                    "raw": text,
                    "norm": norm,
                    "map": index,
                    "bbox": item.get("bbox") or [],
                    "type": item.get("type"),
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
        """Boxes covering [start, end) — by character range where the parser gives one."""
        boxes = entry["bbox"]
        chosen = [
            box for box in boxes
            if box.get("start_index") is not None and box.get("end_index") is not None
            and box["start_index"] < end and box["end_index"] > start
        ]
        if not chosen:
            indexed = any(box.get("start_index") is not None for box in boxes)
            # An item whose boxes carry no ranges can still be located as a whole;
            # one that has ranges but none overlapping is a genuine miss.
            chosen = [] if indexed else list(boxes)
        return [self._scale(entry, box) for box in chosen if self._usable(box)]

    def _usable(self, box: dict[str, Any]) -> bool:
        return all(isinstance(box.get(k), (int, float)) for k in ("x", "y", "w", "h")) \
            and box["w"] > 0 and box["h"] > 0

    def _scale(self, entry: dict[str, Any], box: dict[str, Any]) -> dict[str, float]:
        width = 1.0 if self._normalised_boxes else entry["width"]
        height = 1.0 if self._normalised_boxes else entry["height"]
        return {
            "x": round(max(box["x"] / width, 0.0), 5),
            "y": round(max(box["y"] / height, 0.0), 5),
            "w": round(min(box["w"] / width, 1.0), 5),
            "h": round(min(box["h"] / height, 1.0), 5),
        }

    def anchor(self, evidence: Optional[str], value: Any = None,
               page_hint: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Best rectangle for the quoted evidence, falling back to the value itself."""
        candidates = [text for text in (evidence, None if value is None else str(value)) if text]
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
                rects = self._rects(entry, start, end)
                if not rects:
                    continue
                # Prefer the quoted evidence over the bare value, an exact hit over a
                # partial one, and the page the extractor said it read.
                weight = score - (0.15 * rank) + (0.1 if entry["page"] == page_hint else 0.0)
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
