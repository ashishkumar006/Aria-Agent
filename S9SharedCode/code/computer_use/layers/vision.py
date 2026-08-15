"""Layer E — vision fallback (charter §6, §10).

Escalation trigger + set-of-marks prompt builder. Cost knob = trigger
threshold. Vision is ~10x the per-turn cost of L2b, so escalate only as a
genuine last resort.

This module now ALSO implements the actual set-of-marks drawing: given a
screenshot (base64 PNG) and the AX tree's `elements` array, draw numbered
dashed boxes over each element's bounding box, and produce a legend mapping
numbers to `[element_index N] Role "Label"`. The vision model sees the
annotated image + legend and returns a click at (x, y).
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from ..prompts import USER_VISION_TEMPLATE

try:
    from PIL import Image, ImageDraw
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False


@dataclass
class VisionFallback:
    """Escalation trigger + set-of-marks call. Cost knob = trigger threshold."""
    max_l2b_failures: int = 2

    def should_escalate(self, l2b_failures: int, reason: str | None) -> bool:
        if reason in ("ax_tree_empty", "element_missing", "visual_only_goal"):
            return True
        return l2b_failures >= self.max_l2b_failures

    def build_som_prompt(self, goal: str, legend: str) -> str:
        return USER_VISION_TEMPLATE.format(goal=goal, legend=legend)


def draw_set_of_marks(screenshot_b64: str, elements: list[dict],
                      max_boxes: int = 40) -> tuple[str, str]:
    """Draw numbered dashed boxes over UI elements on a screenshot.

    Args:
        screenshot_b64: base64-encoded PNG (no data: prefix).
        elements: list of AX tree element dicts, each with optional
                  `bbox` / `bounds` / `rectangle` keys giving pixel coords.
        max_boxes: cap the number of boxes drawn (perf + clarity).

    Returns:
        (annotated_b64, legend_text)
        annotated_b64 is a new base64 PNG with boxes + numbers drawn.
        legend_text maps each number to its element description.
    """
    if not _HAVE_PIL:
        # No PIL: return the original image and a text legend only.
        legend = "\n".join(
            f"[{e.get('element_index')}] <{e.get('role')}>{e.get('label')}</{e.get('role')}>"
            for e in elements[:max_boxes]
        )
        return screenshot_b64, legend

    img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64))).convert("RGB")
    draw = ImageDraw.Draw(img)
    legend_lines = []
    for i, e in enumerate(elements[:max_boxes]):
        # cua-driver 0.19 reports element geometry under `frame`
        # ({x, y, w, h}); older/other drivers may use bbox/bounds/
        # rectangle/rect. Support all shapes.
        bbox = (e.get("frame") or e.get("bbox") or e.get("bounds")
                or e.get("rectangle") or e.get("rect") or {})
        # bbox may be {x, y, width, height} or {x1, y1, x2, y2}.
        x, y, w, h = _parse_bbox(bbox)
        if x is None:
            continue
        # Draw a dashed rectangle (approximate with short segments).
        _draw_dashed_rect(draw, x, y, x + w, y + h, fill=(255, 0, 0), width=2)
        # Number label near top-left.
        draw.text((x + 2, y + 2), str(i + 1), fill=(255, 255, 0))
        legend_lines.append(
            f"[{i + 1}] <{e.get('role', '?')}>{e.get('label', e.get('name', ''))}</{e.get('role', '?')}>")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    annotated_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return annotated_b64, "\n".join(legend_lines)


def _parse_bbox(bbox: dict) -> tuple[int | None, int | None, int, int]:
    """Normalize various bbox shapes to (x, y, w, h)."""
    if not bbox:
        return None, None, 0, 0
    # cua-driver 0.19 uses {x, y, w, h} inside `frame`.
    if "x" in bbox and "y" in bbox:
        w = bbox.get("width") or bbox.get("w")
        h = bbox.get("height") or bbox.get("h")
        if w is not None and h is not None:
            return int(bbox["x"]), int(bbox["y"]), int(w), int(h)
        if "x2" in bbox and "y2" in bbox:
            return (int(bbox["x"]), int(bbox["y"]),
                    int(bbox["x2"]) - int(bbox["x"]), int(bbox["y2"]) - int(bbox["y"]))
    if "x1" in bbox and "y1" in bbox and "x2" in bbox and "y2" in bbox:
        return (int(bbox["x1"]), int(bbox["y1"]),
                int(bbox["x2"]) - int(bbox["x1"]), int(bbox["y2"]) - int(bbox["y1"]))
    return None, None, 0, 0


def _draw_dashed_rect(draw, x1: int, y1: int, x2: int, y2: int,
                      fill=(255, 0, 0), width: int = 2, dash: int = 6):
    """Draw a dashed rectangle (PIL has no native dash)."""
    # Top & bottom edges.
    for x in range(x1, x2, dash * 2):
        draw.line([(x, y1), (min(x + dash, x2), y1)], fill=fill, width=width)
        draw.line([(x, y2), (min(x + dash, x2), y2)], fill=fill, width=width)
    # Left & right edges.
    for y in range(y1, y2, dash * 2):
        draw.line([(x1, y), (x1, min(y + dash, y2))], fill=fill, width=width)
        draw.line([(x2, y), (x2, min(y + dash, y2))], fill=fill, width=width)
