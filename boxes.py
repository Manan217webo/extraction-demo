"""Turn a model's rectangle into a box the viewer can draw.

Models give corners on a 0..1000 grid for the value and, separately, for the
printed label it answers to. Both the field read and the box locator ask for
them the same way, so the joining and sanity checks live here once.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("extraction.boxes")

GRID = 1000.0
MAX_AREA = 0.10
MIN_SIDE = 0.002

# The value and its label, as both schemas ask for them.
PROPERTIES = {
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
}


def sane_grid(box: dict[str, Any]) -> Optional[dict[str, float]]:
    """0..1000 corners as 0..1 fractions, or None if unbelievable."""
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


def rect(box: Optional[dict[str, Any]], name: Any = None) -> Optional[dict[str, float]]:
    """The value's box, widened along its row to the printed label."""
    if not box or not box.get("found"):
        return None
    # The label and the value are asked for separately and joined here, so a
    # row-spanning box is arithmetic rather than something the model has to
    # get right in one go. The label is only allowed to widen the box along
    # the row: a label found on another line would drag the box across half
    # the page, which is worse than the value alone.
    merged = dict(box)
    if box.get("label_found"):
        try:
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
        except (KeyError, TypeError):
            joined, overlap, value_h = merged, 0, 1
        if overlap >= 0.4 * value_h:
            merged = joined
        else:
            log.info("label for %s ignored, not on the value's row "
                     "(value y %s-%s, label y %s-%s)", name,
                     box.get("y0"), box.get("y1"), box.get("ly0"), box.get("ly1"))
    found = sane_grid(merged)
    if found is None:
        log.info("box for %s rejected: %s", name, box)
    return found
