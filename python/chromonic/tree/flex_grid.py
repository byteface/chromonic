from __future__ import annotations

import dataclasses
import re

from . import anonymous_boxes, box_model, dom, geometry




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
    resolved = parent.__dict__.get("_chromonic_resolved_style")
    if resolved is None:
        return False
    display = getattr(resolved[1].display, "value", "")
    return display in _FLEX_DISPLAYS or display in ("grid", "inline-grid")



_FLEX_DISPLAYS = ("flex", "inline-flex", "-webkit-flex", "-webkit-inline-flex", "-ms-flexbox")



def _fix_flex_baseline_alignment(node_map: dict) -> None:
    """CSS Flexbox 8.3: an author display:flex row whose items align on
    baseline (align-items, or an item's align-self). Taffy only knows a
    baseline for a measured text leaf; an item whose text lives further
    down (a <div> holding an <a>, align-self-006.html) gets its bottom
    edge synthesized instead, so every such item was bottom-aligned. Each
    flex line is re-aligned here on the items' real first (or last)
    baselines, and when that makes the line taller than Taffy made it,
    stretched/centred/end-aligned items in the line, later lines and a
    height:auto container follow."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        if not native or native.get("display") != "flex":
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS:
            continue
        computed = resolved[0]
        direction = (getattr(computed, "flexDirection", "row") or "row").strip().lower()
        if direction not in ("row", "row-reverse"):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        container_align, _safe = box_model._alignment_parts(getattr(computed, "alignItems", "normal"))
        items = []
        children = element.__dict__.get("_chromonic_normalized_children") or dom._child_nodes(element)
        for child in children:
            if not dom._is_element(child) and not isinstance(child, anonymous_boxes._AnonymousTableBox):
                continue
            child_native = child.__dict__.get("_chromonic_native_style")
            child_box = child.__dict__.get("_layout_box")
            if child_native is None or child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            align = "auto"
            if child_resolved is not None:
                if not dom._renders(child_resolved[1]):
                    continue
                align, _safe = box_model._alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
            if align == "auto":
                align = container_align
            items.append((child, align))
        if not any(align in box_model._BASELINE_ALIGNMENTS for _child, align in items):
            continue
        # Visual order is `order`-sorted DOM order (stable), the same
        # order `build()` handed Taffy the items in.
        items.sort(key=lambda entry: _css_order((getattr(entry[0], "_chromonic_resolved_style", None) or (None,))[0]))
        # Flex lines: visual order runs along the main axis, so a line
        # breaks wherever the main-axis position turns back.
        lines: list = []
        current: list = []
        prev_x = prev_bottom = None
        for child, align in items:
            child_box = child.__dict__["_layout_box"]
            x = child_box.x
            turned = (prev_x is not None and (
                (x < prev_x - 0.01 if direction == "row" else x > prev_x + 0.01)
                or (abs(x - prev_x) <= 0.01 and child_box.y >= prev_bottom - 0.01)))
            if turned:
                lines.append(current)
                current = []
            current.append((child, align))
            prev_x = x
            prev_bottom = child_box.y + child_box.height
        if current:
            lines.append(current)
        total_delta = 0.0
        for line in lines:
            if total_delta:
                for child, _align in line:
                    geometry._shift_subtree(child, 0.0, total_delta)
            entries = []
            for child, align in line:
                child_box = child.__dict__["_layout_box"]
                margin = (child.__dict__.get("_chromonic_native_style") or {}).get("margin") or (0.0,) * 4
                mt, mb = box_model._numeric_edge(margin[0]), box_model._numeric_edge(margin[2])
                entries.append((child, align, child_box, mt, mb))
            line_top = min(child_box.y - mt for _c, _a, child_box, mt, _mb in entries)
            old_bottom = max(child_box.y + child_box.height + mb for _c, _a, child_box, _mt, mb in entries)
            refs = []
            for child, align, child_box, mt, mb in entries:
                if align not in box_model._BASELINE_ALIGNMENTS:
                    continue
                if align == "last-baseline":
                    own = box_model._element_own_baseline(child)
                else:
                    absolute = box_model._first_baseline(child)
                    own = None if absolute is None else absolute - child_box.y
                if own is None:
                    own = child_box.height  # no line box: synthesized from the border-box bottom
                refs.append((child, child_box, mt, mb, own))
            if not refs:
                continue
            line_baseline = max(mt + own for _c, _b, mt, _mb, own in refs)
            new_bottom = old_bottom
            for child, child_box, mt, mb, own in refs:
                new_y = line_top + line_baseline - own
                if abs(new_y - child_box.y) > 0.01:
                    geometry._shift_subtree(child, 0.0, new_y - child_box.y)
                new_bottom = max(new_bottom, new_y + child_box.height + mb)
            delta = new_bottom - old_bottom
            if delta <= 0.01:
                continue
            for child, align, child_box, mt, mb in entries:
                if align in box_model._BASELINE_ALIGNMENTS:
                    continue
                child_box = child.__dict__["_layout_box"]
                child_native = child.__dict__.get("_chromonic_native_style") or {}
                if align in ("stretch", "normal") and child_native.get("height") == "auto":
                    child.__dict__["_layout_box"] = dataclasses.replace(
                        child_box, height=child_box.height + delta,
                        client_height=child_box.client_height + delta)
                elif align == "center":
                    geometry._shift_subtree(child, 0.0, delta / 2.0)
                elif align in ("flex-end", "end", "self-end"):
                    geometry._shift_subtree(child, 0.0, delta)
            total_delta += delta
        if total_delta > 0.01 and native.get("height") == "auto":
            box = element.__dict__["_layout_box"]
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=box.height + total_delta, client_height=box.client_height + total_delta)
            geometry._shift_later_siblings_for_height_delta(element, total_delta)



def _fix_flex_safe_alignment(node_map: dict) -> None:
    """CSS Box Alignment 3 safe: an alignment that would make content
    overflow its container falls back to start instead. Taffy has no
    overflow-position notion (style_bridge._align_keyword drops the safe
    prefix), so here, after layout, an in-flow flex item whose safe-aligned
    cross size exceeds its single-line container's content box is moved
    to the cross start, and a safe justify-content whose items overflow
    the main axis packs them from the main start --
    flexbox-safe-overflow-position-001.html."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if (not native or native.get("display") != "flex" or resolved is None
                or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        computed = resolved[0]
        row = (getattr(computed, "flexDirection", "row") or "row").strip().lower() in ("row", "row-reverse")
        justify, justify_safe = box_model._alignment_parts(getattr(computed, "justifyContent", "normal"))
        items_align, items_safe = box_model._alignment_parts(getattr(computed, "alignItems", "normal"))
        pad = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_x = box.x + box.border_left + pad[3]
        content_y = box.y + box.border_top + pad[0]
        content_w = box.client_width - pad[1] - pad[3]
        content_h = box.client_height - pad[0] - pad[2]
        items = []
        for child in element.__dict__.get("_chromonic_normalized_children") or dom._child_nodes(element):
            if not (dom._is_element(child) or isinstance(child, anonymous_boxes._AnonymousTableBox)):
                continue
            child_native = child.__dict__.get("_chromonic_native_style") or {}
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            align, safe = "auto", False
            if child_resolved is not None:
                if not dom._renders(child_resolved[1]):
                    continue
                align, safe = box_model._alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
            if align == "auto":
                align, safe = items_align, items_safe
            margin = child_native.get("margin") or (0.0,) * 4
            mt, mr, mb, ml = (box_model._numeric_edge(edge) for edge in margin)
            items.append((child, child_box, align, safe, mt, mr, mb, ml))
        if not items:
            continue
        # Cross axis, per item.
        for child, child_box, align, safe, mt, mr, mb, ml in items:
            if not safe or align in ("start", "flex-start", "self-start", "normal", "stretch", "left"):
                continue
            if row:
                if child_box.height + mt + mb > content_h + 0.01:
                    geometry._shift_subtree(child, 0.0, content_y + mt - child_box.y)
            else:
                if child_box.width + ml + mr > content_w + 0.01:
                    geometry._shift_subtree(child, content_x + ml - child_box.x, 0.0)
        # Main axis, whole line (single-line containers only). In a
        # -reverse direction flex-start is the physical end, so safe
        # flex-start overflowing also packs from the physical start --
        # flexbox-safe-overflow-position-003.html.
        reverse = (getattr(computed, "flexDirection", "row") or "row").strip().lower().endswith("-reverse")
        overflow_keywords = ("center", "end", "flex-end", "right", "space-around", "space-evenly") + (
            ("flex-start", "space-between", "normal") if reverse else ())
        if justify_safe and justify in overflow_keywords:
            if row:
                total = sum(b.width + ml + mr for _c, b, _a, _s, _mt, mr, _mb, ml in items)
                if total > content_w + 0.01:
                    cursor = content_x
                    for child, child_box, _a, _s, _mt, mr, _mb, ml in items:
                        geometry._shift_subtree(child, cursor + ml - child_box.x, 0.0)
                        cursor += ml + child_box.width + mr
            else:
                total = sum(b.height + mt + mb for _c, b, _a, _s, mt, _mr, mb, _ml in items)
                if total > content_h + 0.01:
                    cursor = content_y
                    for child, child_box, _a, _s, mt, _mr, mb, _ml in items:
                        geometry._shift_subtree(child, 0.0, cursor + mt - child_box.y)
                        cursor += mt + child_box.height + mb



def _fix_flex_rtl_mirroring(node_map: dict) -> None:
    """CSS Flexbox 5.1/8: in a direction:rtl flex container the main axis
    of a row runs right-to-left, and the cross axis of a column starts at
    the right -- both are the container's horizontal axis mirrored. Taffy
    has no writing direction, so every in-flow item's margin box is
    reflected here across the container's content box --
    flexbox-mbp-horiz-001-rtl.xhtml, flexbox-align-self-vert-rtl-001.xhtml.
    Runs after the other flex passes so it mirrors their final positions;
    absolutely positioned children keep their own direction-aware static position."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if (not native or native.get("display") != "flex" or resolved is None
                or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS):
            continue
        if dom._element_direction(element, resolved[0]) != "rtl":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pad = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_x = box.x + box.border_left + pad[3]
        content_w = box.client_width - pad[1] - pad[3]
        for child in element.__dict__.get("_chromonic_normalized_children") or dom._child_nodes(element):
            if not (dom._is_element(child) or isinstance(child, anonymous_boxes._AnonymousTableBox)):
                continue
            child_native = child.__dict__.get("_chromonic_native_style") or {}
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            if child_resolved is not None and not dom._renders(child_resolved[1]):
                continue
            margin = child_native.get("margin") or (0.0,) * 4
            ml, mr = box_model._numeric_edge(margin[3]), box_model._numeric_edge(margin[1])
            outer_left = child_box.x - ml
            outer_w = child_box.width + ml + mr
            new_outer_left = content_x + content_w - (outer_left - content_x) - outer_w
            dx = new_outer_left + ml - child_box.x
            if abs(dx) > 0.01:
                geometry._shift_subtree(child, dx, 0.0)
