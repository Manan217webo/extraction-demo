"""Locate a value on the page.

The parser returns one rectangle for a whole table, so a row's position has to
be interpolated inside it, and interpolation drifts wherever printed rows are not
the same height. Two backends can replace that guess:

- OpenAI vision (`OPENAI_VISION_MODEL`) looks at the page image. Accurate on this
  form when a frontier model is used; billed per page, not per box.
- Tesseract (the default when that env is unset) OCRs the page locally and snaps
  the parser's column onto the printed label's row.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Optional

import fields

log = logging.getLogger("extraction.vision")

MAX_AREA = 0.10
MIN_SIDE = 0.002
MIN_CONF = 15
_ALNUM = re.compile(r"[^0-9a-z]+")


@dataclass
class Word:
    text: str
    norm: str
    x: float
    y: float
    w: float
    h: float
    line: tuple[int, int, int]
    conf: float
    taken: bool = False

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Span:
    words: list[Word]
    needle: str
    score: float = 0.0

    @property
    def box(self) -> dict[str, float]:
        x0 = min(w.x for w in self.words)
        y0 = min(w.y for w in self.words)
        x1 = max(w.x + w.w for w in self.words)
        y1 = max(w.y + w.h for w in self.words)
        return {
            "x": round(x0, 5),
            "y": round(y0, 5),
            "w": round(x1 - x0, 5),
            "h": round(y1 - y0, 5),
        }


GRID = 1000.0
# The model's time grows with the number of targets in one request: seven
# targets placed in ~10s, forty in ~30s. A page's targets are split into
# batches of this size and the batches run concurrently, so a dense page
# costs about one batch's worth of time rather than all of them in series.
BATCH = 14

OPENAI_RULES = """You are shown one page of a scanned clinical case report form.

For each target you are given the printed label beside the value and the value
itself, as already read from this page. Your only job is to say WHERE on this
page that field sits. Do not re-read it and do not correct it.

Return TWO rectangles for each target, and they are joined afterwards:

  - x0,y0,x1,y1      the value itself — the handwritten digits, the ticked
                     option, the characters in the comb boxes, plus any unit
                     printed immediately after them ("beats/min", "(mmHg)")
  - lx0,ly0,lx1,ly1  the printed label this value answers to. In a table that is
                     the row heading in the left-hand column ("Pulse rate"); on a
                     line of prose it is the printed question before the value.
                     Set label_found false, and the four numbers to 0, only when
                     no such label is printed.

Giving them separately is what lets a crop of the value be read on its own: the
saved image shows "Pulse rate ... 088 beats/min", not a stray 088.

Keep each rectangle tight to its own text. Do not stretch the value rectangle
across the rest of the table row (other columns, other options, comments).

Coordinates are integers on a 0 to 1000 grid, left to right and top to bottom,
where 0,0 is the top-left corner of the page and 1000,1000 the bottom-right.
x0,y0 is the top-left of the rectangle and x1,y1 the bottom-right, so x1 > x0 and
y1 > y0 always.

Each target names the "section" it belongs to — the titled CRF block it was read
from, such as VITAL SIGNS or PHYSICAL EXAMINATION. Find the target INSIDE that
block only. Several blocks ask the same question, so the same label and the same
value can appear more than once on the page; the section is what tells them
apart. A box drawn in the wrong block is wrong even when it reads the same.

Set found to false when the value is not visible inside that block, or when you
cannot tell which of several cells within it is meant. A false is far more useful
than a guess: a rectangle in the wrong row puts a clinical value against the
wrong parameter."""

OPENAI_SCHEMA = {
    "type": "object",
    "properties": {
        "boxes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "found": {"type": "boolean"},
                    "x0": {"type": "integer"},
                    "y0": {"type": "integer"},
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "label_found": {"type": "boolean"},
                    "lx0": {"type": "integer"},
                    "ly0": {"type": "integer"},
                    "lx1": {"type": "integer"},
                    "ly1": {"type": "integer"},
                },
                "required": ["id", "found", "x0", "y0", "x1", "y1",
                             "label_found", "lx0", "ly0", "lx1", "ly1"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["boxes"],
    "additionalProperties": False,
}


def _openai_model() -> str:
    return (os.getenv("OPENAI_VISION_MODEL") or "").strip()


def backend() -> str:
    """openai when OPENAI_VISION_MODEL is set, otherwise local tesseract."""
    if _openai_model() and fields.configured():
        return "openai"
    if _tesseract_available():
        return "tesseract"
    return ""


def model_name() -> str:
    if backend() == "openai":
        return _openai_model()
    return "tesseract"


def _tesseract_cmd() -> Optional[str]:
    """The tesseract binary, wherever this machine keeps it.

    The installer on Windows puts it under Program Files and does not add it
    to PATH, so `shutil.which` misses it there. Without these paths the
    locator silently had no backend on Windows: every /locate answered 503
    and, since only located boxes are drawn, the page showed no boxes at all.
    """
    program_files = [
        os.environ.get("ProgramFiles") or r"C:\Program Files",
        os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        os.environ.get("LOCALAPPDATA") or "",
    ]
    for candidate in (
        (os.getenv("TESSERACT_CMD") or "").strip(),
        shutil.which("tesseract") or "",
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
        *[os.path.join(base, "Tesseract-OCR", "tesseract.exe")
          for base in program_files if base],
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return _tesseract_cmd() is not None


def configured() -> bool:
    return backend() in {"openai", "tesseract"}


def _alnum(text: str) -> str:
    return _ALNUM.sub("", (text or "").lower())


def _needles(value: Any, tokens: bool = False) -> list[str]:
    """Collapsed forms of a value that might appear in OCR output."""
    text = "" if value is None else str(value).strip()
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    collapsed = _alnum(text)
    add(collapsed)

    # A date is held as ISO but written on the page in the order the boxes under
    # it ask for — "DD MMM YY" here, digits in comb boxes. Searching for
    # 20260216 can never match a page reading 160226, so every ordering the
    # form might use is offered.
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        year, month, day = iso.groups()
        short = year[2:]
        for form in (day + month + short, day + month + year,
                     short + month + day, year + month + day,
                     month + day + short, month + day + year):
            add(form)

    if collapsed.isdigit():
        stripped = collapsed.lstrip("0") or "0"
        add(stripped)
        for width in (2, 3, 4):
            add(stripped.zfill(width))
    if tokens:
        for token in re.findall(r"[a-z0-9]{4,}", text.lower()):
            add(_alnum(token))
    return out


def _decode(data_url: str):
    from PIL import Image, ImageOps

    _, _, payload = data_url.partition(",")
    image = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")
    return ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)


def _run_ocr(image, config: str) -> list[Word]:
    import pytesseract

    cmd = _tesseract_cmd()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, config=config,
    )
    width, height = image.size
    words: list[Word] = []
    n = len(data.get("text") or [])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < MIN_CONF:
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        w, h = float(data["width"][i]), float(data["height"][i])
        if w <= 0 or h <= 0 or width <= 0 or height <= 0:
            continue
        words.append(Word(
            text=text,
            norm=_alnum(text),
            x=left / width,
            y=top / height,
            w=w / width,
            h=h / height,
            line=(int(data["block_num"][i]), int(data["par_num"][i]),
                  int(data["line_num"][i])),
            conf=conf,
        ))
    return [w for w in words if w.norm]


def _merge_words(passes: list[list[Word]]) -> list[Word]:
    """Keep the first pass's words; add later-pass words that don't overlap them."""
    kept: list[Word] = []
    for batch in passes:
        for word in batch:
            if any(_overlap(word, other) > 0.55 and word.norm == other.norm
                   for other in kept):
                continue
            kept.append(word)
    kept.sort(key=lambda w: (w.y, w.x))
    return kept


def _overlap(a: Word, b: Word) -> float:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = min(a.w * a.h, b.w * b.h)
    return inter / area if area else 0.0


def _same_row(a: Word, b: Word) -> bool:
    if a.line == b.line:
        return True
    # Comb-box digits often land in adjacent tesseract lines.
    return abs(a.cy - b.cy) < max(a.h, b.h) * 0.8


def _digitish(text: str) -> str:
    return text.translate(str.maketrans("oilbs", "01185"))


def _close(a: Word, b: Word) -> bool:
    if not _same_row(a, b):
        return False
    gap = b.x - (a.x + a.w)
    if gap < 0:
        return abs(gap) < max(a.w, b.w)
    # Comb-box digits sit in neighbouring cells with a gap bigger than the glyph.
    if len(a.norm) <= 2 and len(b.norm) <= 2:
        return gap < 0.04
    return gap < max(a.w, b.w, 0.02) * 1.8


def _spans_for(words: list[Word], needle: str, loose: bool = False) -> list[Span]:
    if not needle:
        return []
    out: list[Span] = []
    n = len(words)
    for i in range(n):
        if words[i].taken or not words[i].norm:
            continue
        # Glued OCR ("tRespiratoryrate") still contains the printed label.
        if loose and len(needle) >= 4 and needle in words[i].norm:
            out.append(Span(words=[words[i]], needle=needle))
            continue
        acc = ""
        used: list[Word] = []
        for j in range(i, n):
            word = words[j]
            if word.taken or not word.norm:
                continue
            if used and not _close(used[-1], word):
                break
            acc += word.norm
            used.append(word)
            compared = _digitish(acc) if needle.isdigit() else acc
            if compared == needle:
                out.append(Span(words=list(used), needle=needle))
                break
            if len(compared) > len(needle):
                break
    return out


def _hint_box(target: dict[str, Any]) -> Optional[dict[str, float]]:
    rects = target.get("rects") or []
    usable = [r for r in rects
              if isinstance(r, dict) and all(k in r for k in ("x", "y", "w", "h"))]
    if not usable:
        return None
    x0 = min(r["x"] for r in usable)
    y0 = min(r["y"] for r in usable)
    x1 = max(r["x"] + r["w"] for r in usable)
    y1 = max(r["y"] + r["h"] for r in usable)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _label_box(words: list[Word], target: dict[str, Any]) -> Optional[dict[str, float]]:
    """Printed label beside the value — locator first, then the CRF field name."""
    hint = _hint_box(target)
    for text in (target.get("locator"), target.get("label")):
        if not text or len(str(text).split()) > 8:
            continue
        for needle in _needles(text, tokens=True):
            if len(needle) < 4:
                continue
            spans = _spans_for(words, needle, loose=True)
            if not spans:
                continue
            best = min(spans, key=lambda s: _hint_distance(s, hint) if hint else s.box["y"])
            return best.box
    return None


def _aligned(box: dict[str, float], anchor: dict[str, float], slack: float = 0.03) -> bool:
    cy = box["y"] + box["h"] / 2
    ay = anchor["y"] + anchor["h"] / 2
    return abs(cy - ay) <= max(slack, anchor["h"] * 2.2)


def _union(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    x0 = min(a["x"], b["x"])
    y0 = min(a["y"], b["y"])
    x1 = max(a["x"] + a["w"], b["x"] + b["w"])
    y1 = max(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _snap(hint: Optional[dict[str, float]], label: dict[str, float]) -> dict[str, float]:
    """Parser column on the label's row, then expanded to include the label."""
    pad = max(label["h"] * 0.45, 0.004)
    y = max(0.0, label["y"] - pad)
    h = min(1.0 - y, label["h"] + 2 * pad)
    if hint:
        value = {"x": hint["x"], "y": y, "w": hint["w"], "h": h}
        return _union(label, value)
    x = min(label["x"] + label["w"] + 0.008, 0.85)
    return _union(label, {"x": x, "y": y, "w": 0.12, "h": h})


def _hint_distance(span: Span, hint: Optional[dict[str, float]]) -> float:
    if not hint:
        return 0.0
    box = span.box
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    hx = hint["x"] + hint["w"] / 2
    hy = hint["y"] + hint["h"] / 2
    return abs(cx - hx) + abs(cy - hy) * 1.4


def _x_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    left = max(a["x"], b["x"])
    right = min(a["x"] + a["w"], b["x"] + b["w"])
    span = min(a["w"], b["w"])
    return max(0.0, right - left) / span if span else 0.0


def _score(span: Span, label: Optional[dict[str, float]],
           hint: Optional[dict[str, float]]) -> float:
    box = span.box
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    score = 1.0
    if hint:
        score += 2.5 * _x_overlap(box, hint)
        hy = hint["y"] + hint["h"] / 2
        score -= abs(cy - hy) * 10
        # Parser row is often a neighbour, not a random section.
        if abs(cy - hy) < max(hint["h"] * 2.5, 0.04):
            score += 1.5
    if label:
        ly = label["y"] + label["h"] / 2
        score -= abs(cy - ly) * 14
        if abs(cy - ly) < max(label["h"] * 1.6, 0.02):
            score += 2.0
        if box["x"] >= label["x"] - 0.01:
            score += 1.2
        score -= (abs(cx - (label["x"] + label["w"])) + abs(cy - ly)) * 3
    score -= box["w"] * box["h"] * 6
    return score


def _sane(box: dict[str, float]) -> Optional[dict[str, float]]:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    if w < MIN_SIDE or h < MIN_SIDE:
        return None
    if w * h > MAX_AREA:
        return None
    if not (0.0 <= x < x + w <= 1.0 and 0.0 <= y < y + h <= 1.0):
        x = max(0.0, min(x, 1.0))
        y = max(0.0, min(y, 1.0))
        w = max(MIN_SIDE, min(w, 1.0 - x))
        h = max(MIN_SIDE, min(h, 1.0 - y))
        if w * h > MAX_AREA:
            return None
    return {"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5)}


def _page(image_url: str, targets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    image = _decode(image_url)
    words = _merge_words([
        _run_ocr(image, "--oem 1 --psm 6"),
        _run_ocr(image, "--oem 1 --psm 11"),
    ])
    if not words:
        return {}

    found: dict[str, dict[str, float]] = {}
    for target in targets:
        label = _label_box(words, target)
        hint = _hint_box(target)
        raw = target.get("raw") if target.get("raw") not in (None, "") else target.get("value")
        needles = [n for n in _needles(raw) if len(n) >= 2]

        chosen: Optional[Span] = None
        best_score = -1e9
        for needle in needles:
            for span in _spans_for(words, needle):
                if any(w.taken for w in span.words):
                    continue
                if label and not _aligned(span.box, label):
                    continue
                if not label and hint:
                    if not _aligned(span.box, hint, slack=0.05) and _x_overlap(span.box, hint) < 0.25:
                        continue
                span.score = _score(span, label, hint)
                if span.score > best_score:
                    best_score = span.score
                    chosen = span

        rect = None
        if chosen is not None:
            box = chosen.box
            if label:
                box = _union(label, box)
            rect = _sane(box)
            if rect:
                for word in chosen.words:
                    word.taken = True
        elif label:
            # Handwriting in comb boxes rarely OCRs; the label row is still right.
            rect = _sane(_snap(hint, label))
        if rect:
            found[str(target["id"])] = rect
    return found


def _sane_grid(box: dict[str, Any]) -> Optional[dict[str, float]]:
    """OpenAI's 0..1000 corners as 0..1 fractions, or None if unbelievable."""
    try:
        x0, y0 = float(box["x0"]) / GRID, float(box["y0"]) / GRID
        x1, y1 = float(box["x1"]) / GRID, float(box["y1"]) / GRID
    except (KeyError, TypeError, ValueError):
        return None
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    width, height = x1 - x0, y1 - y0
    if width < MIN_SIDE or height < MIN_SIDE:
        return None
    if width * height > MAX_AREA:
        return None
    return {"x": round(x0, 5), "y": round(y0, 5),
            "w": round(width, 5), "h": round(height, 5)}


async def _openai_page(image: str, targets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    # The sheet stacks several CRFs and they repeat each other's questions: two
    # blocks ask whether an assessment was performed and two carry a date in the
    # same format. Naming the block a target belongs to is what makes those
    # tellable apart — without it the honest answer is to refuse, and the field
    # ends up with no box at all.
    listing = [
        {"id": t["id"],
         "section": t.get("section") or "",
         "label": t.get("locator") or t.get("label") or "",
         "value": str(t.get("raw") if t.get("raw") not in (None, "") else t.get("value"))}
        for t in targets
    ]
    messages = [
        {"role": "system", "content": OPENAI_RULES},
        {
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Locate each of these on the page:\n"
                         + json.dumps(listing, ensure_ascii=False, indent=1)},
                {"type": "image_url",
                 "image_url": {"url": image, "detail": "high"}},
            ],
        },
    ]
    try:
        data = await fields.complete(messages, OPENAI_SCHEMA, "field_boxes",
                                     model=_openai_model())
    except fields.ExtractionUnavailable as exc:
        log.warning("openai locate failed: %s", exc)
        return {}

    out: dict[str, dict[str, float]] = {}
    for box in data.get("boxes") or []:
        if not box.get("found"):
            continue
        # The label and the value are asked for separately and joined here, so a
        # row-spanning box is arithmetic rather than something the model has to
        # get right in one go. The label is only allowed to widen the box along
        # the row: a label found on another line would drag the box across half
        # the page, which is worse than the value alone.
        merged = dict(box)
        if box.get("label_found"):
            joined = {**box, "x0": min(box["x0"], box["lx0"]),
                      "y0": min(box["y0"], box["ly0"]),
                      "x1": max(box["x1"], box["lx1"]),
                      "y1": max(box["y1"], box["ly1"])}
            # A label belongs to the value's row when their vertical spans
            # overlap. Judging by the union's height rejected every label on
            # the real form: "Seated Systolic Blood Pressure" wraps to two
            # lines, so its box is twice the value's height and the union
            # failed a height cap meant to catch a label from another row.
            # Overlap is what actually separates the two cases.
            value_h = max(box["y1"] - box["y0"], 1)
            overlap = min(box["y1"], box["ly1"]) - max(box["y0"], box["ly0"])
            if overlap >= 0.4 * value_h:
                merged = joined
            else:
                log.info("openai label for %s ignored, not on the value's row "
                         "(value y %s-%s, label y %s-%s)", box.get("id"),
                         box["y0"], box["y1"], box["ly0"], box["ly1"])
        rect = _sane_grid(merged)
        if rect is None:
            log.info("openai box for %s rejected: %s", box.get("id"), box)
            continue
        out[str(box.get("id"))] = rect
    return out


async def _openai_locate(pages: dict[str, str],
                         targets: list[dict[str, Any]],
                         jobs: list[tuple[int, list[dict[str, Any]]]]) -> dict[str, dict[str, Any]]:
    results = await asyncio.gather(
        *(_openai_page(pages[str(page)], batch) for page, batch in jobs),
        return_exceptions=True,
    )
    found: dict[str, dict[str, Any]] = {}
    for (page, batch), result in zip(jobs, results):
        if isinstance(result, BaseException):
            log.warning("openai page %s failed: %s", page, result)
            continue
        for target in batch:
            rect = result.get(target["id"])
            if rect:
                found[target["id"]] = {"page": page, "rects": [rect]}
    log.info("openai %s placed %s of %s target(s)", _openai_model(),
             len(found), len(targets))
    return found


async def locate(pages: dict[str, str],
                 targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Boxes for every target we can place, keyed by the target's key.

    `pages` maps a page number to a data URL of that page rendered as an image.
    Targets carry the page they were read from; anything without an image, or that
    the backend cannot place, simply does not appear in the result.
    """
    if not configured():
        return {}

    jobs: list[tuple[int, list[dict[str, Any]]]] = []
    chunk = BATCH if backend() == "openai" else 10_000
    for number, image in pages.items():
        try:
            page = int(number)
        except (TypeError, ValueError):
            continue
        mine = [t for t in targets
                if t.get("page") == page and t.get("value") not in (None, "")]
        for start in range(0, len(mine), chunk):
            jobs.append((page, mine[start:start + chunk]))

    if not jobs:
        return {}

    if backend() == "openai":
        return await _openai_locate(pages, targets, jobs)

    results = await asyncio.gather(
        *(asyncio.to_thread(_page, pages[str(page)], batch) for page, batch in jobs),
        return_exceptions=True,
    )

    found: dict[str, dict[str, Any]] = {}
    for (page, batch), result in zip(jobs, results):
        if isinstance(result, BaseException):
            log.warning("tesseract page %s failed: %s", page, result)
            continue
        for target in batch:
            rect = result.get(target["id"])
            if rect:
                found[target["id"]] = {"page": page, "rects": [rect]}

    log.info("tesseract placed %s of %s target(s)", len(found), len(targets))
    return found
