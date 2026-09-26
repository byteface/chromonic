from __future__ import annotations

import dataclasses
import re

from . import anonymous_boxes, box_model, dom, geometry
from .box import box_of



_GRID_AREA_SPAN_RE = re.compile(r"^span\s+(\d+)$", re.I)

_GRID_AREA_LINE_RE = re.compile(r"^[+-]?\d+$")



def _parse_grid_area_token(token: str):
    """One /-separated grid-area component -> the same (line-number,
    ("span", n), or None-for-auto) shape src/lib.rs's parse_grid_placement
    accepts. A named line/area (real but not modelled -- see PLAN.md)
    falls back to None/auto rather than guessing a line number."""
    token = token.strip()
    if not token or token.lower() == "auto":
        return None
    span = _GRID_AREA_SPAN_RE.match(token)
    if span:
        return ("span", int(span.group(1)))
    if _GRID_AREA_LINE_RE.match(token):
        return int(token)
    return None  # a named line/custom-ident -- not modelled, falls back to auto



def _parse_grid_area(area: str):
    """CSS Grid 1 8.3.1 grid-area: <row-start> [/ <column-start> [/
    <row-end> [/ <column-end>]]] -- omitted trailing components are auto.
    Returns ((row_start, row_end), (col_start, col_end))."""
    parts = [p.strip() for p in area.split("/")]
    parts += ["auto"] * (4 - len(parts))
    row_start, col_start, row_end, col_end = (_parse_grid_area_token(p) for p in parts[:4])
    return (row_start, row_end), (col_start, col_end)



def _css_order(computed) -> int:
    """The computed order (CSS Flexbox 5.4) as an int; 0 when unset,
    unparsable, or for an anonymous item with no computed style."""
    try:
        return int(float(getattr(computed, "order", 0) or 0))
    except (TypeError, ValueError):
        return 0



def _is_flex_or_grid_item(element) -> bool:
    """Whether element's parent box is a real author flex or grid
    container -- then width:auto on this block is a flex/grid item's
    content-sized (then flexed/stretched by Taffy) width, never CSS 2.1
    10.3.3's fill-the-containing-block -- align-items-baseline-row-horz.html."""
    parent = dom._layout_parent(element)
    if parent is None or not hasattr(parent, "__dict__"):
        return False
    resolved = box_of(parent).resolved_style
    if resolved is None:
        return False
    display = getattr(resolved[1].display, "value", "")
    return display in _FLEX_DISPLAYS or display in ("grid", "inline-grid")



_FLEX_DISPLAYS = ("flex", "inline-flex", "-webkit-flex", "-webkit-inline-flex", "-ms-flexbox")



