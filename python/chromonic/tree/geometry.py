from __future__ import annotations

import dataclasses

from domonic.layout import LayoutBox

from . import box_model, dom
from .box import box_of




def _write_boxes(boxes, node_map):
    """Publish native geometry back onto the authoritative Domonic nodes."""
    for node_id, box in boxes.items():
        (x, y, w, h), (bt, br, bb, bl), (pt, pr, pb, pl), (mt, mr, mb, ml) = box
        element = node_map[node_id]
        state = element.__dict__
        # Same private-state assignment domonic's set_layout_box wrappers
        # ultimately do -- done directly here to skip them per node.
        state["_layout_box"] = LayoutBox(
            x=x, y=y, width=w, height=h,
            client_width=w - bl - br,
            client_height=h - bt - bb,
            border_top=bt, border_left=bl,
            # Taffy's used margins (auto resolved) -- what getComputedStyle()
            # reports for a declared `auto` (CSS 2.1 10.3.3).
            margin_top=mt, margin_right=mr, margin_bottom=mb, margin_left=ml,
        )
        layout_state = box_of(element)
        layout_state.padding = (pt, pr, pb, pl)
        # Fresh geometry no longer carries the position:relative-inline
        # offset `_apply_inline_rel_offset` applied to this box.
        layout_state.inline_rel_offset = None



def _grow_box_height(element, delta: float) -> None:
    box = element.__dict__.get("_layout_box")
    if box is not None and delta:
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, height=box.height + delta, client_height=box.client_height + delta)



def _grow_and_reflow(element, delta: float, *, stop_at=None, grow_self: bool = True) -> None:
    """`element` just needed `delta` more height than Taffy gave it: grow
    its box, move every later in-flow sibling down, and carry the same
    growth up through each auto-height ancestor with its later siblings
    likewise -- stopping at the first ancestor with a non-auto height.
    `stop_at` names an ancestor that still grows but propagates no
    further.
    `grow_self=False` propagates a growth already applied to element's
    own box."""
    if grow_self:
        _grow_box_height(element, delta)
    _shift_later_siblings_for_height_delta(element, delta)
    child = element
    ancestor = dom._layout_parent(element)
    while ancestor is not None and dom._is_element(ancestor):
        native = box_of(ancestor).native_style
        if native is None or native.get("height") != "auto":
            break
        if ancestor is stop_at:
            _grow_box_height(ancestor, delta)
            break
        # An ancestor grows by what its flow now needs, not blindly by
        # delta: when the grown box is last in flow and the ancestor was
        # already taller (sized by a taller sibling sharing the same line
        # -- table-vertical-align-baseline-008.xht), only the part of the
        # new bottom edge that overflows counts, which may be nothing.
        growth = _needed_ancestor_growth(ancestor, child, delta)
        if growth <= 0.01:
            break
        _grow_box_height(ancestor, growth)
        # A table row grown this way (a nested table inside one of its
        # cells got taller) keeps every cell as tall as the row.
        for cell in box_of(ancestor).table_cells or ():
            if cell is not child:
                _grow_box_height(cell, growth)
        _shift_later_siblings_for_height_delta(ancestor, growth)
        child, delta = ancestor, growth
        ancestor = dom._layout_parent(ancestor)



def _needed_ancestor_growth(ancestor, child, delta: float) -> float:
    """How much ancestor's height:auto box must grow now that its in-flow
    child is `delta` taller (later siblings already shifted by that much).
    `delta` when anything follows the child in flow; else the part of the
    child's new bottom margin edge below the ancestor's content edge,
    capped at delta."""
    ancestor_box = ancestor.__dict__.get("_layout_box")
    child_box = child.__dict__.get("_layout_box")
    if ancestor_box is None or child_box is None:
        return delta
    seen_self = False
    for sibling in dom._child_nodes(ancestor):
        if sibling is child:
            seen_self = True
            continue
        if not seen_self or not dom._is_element(sibling) or sibling.__dict__.get("_layout_box") is None:
            continue
        sibling_style = box_of(sibling).native_style or {}
        if sibling_style.get("position") in ("absolute", "fixed"):
            continue
        return delta
    margin = (box_of(child).native_style or {}).get("margin") or (0.0,) * 4
    child_bottom = child_box.y + child_box.height + box_model._numeric_edge(margin[2])
    padding = box_of(ancestor).get("padding", (0.0,) * 4)
    content_bottom = ancestor_box.y + ancestor_box.border_top + ancestor_box.client_height - padding[2]
    return max(0.0, min(delta, child_bottom - content_bottom))



def _shift_later_siblings_for_height_delta(element, delta: float) -> None:
    """When element's own height just changed by `delta` (a post-hoc
    correction, after Taffy already stacked its siblings using the old
    value), every later DOM sibling sharing its parent's ordinary block
    flow needs the same vertical shift -- Taffy positioned each one
    immediately after the previous sibling's own now-stale box.
    Absolutely/fixed-positioned siblings are excluded: their position
    doesn't derive from preceding-sibling flow at all. A display:none
    sibling is excluded too, by `_shift_subtree` itself."""
    parent = getattr(element, "parentNode", None)
    if parent is None or not dom._is_element(parent):
        return
    parent_native = box_of(parent).native_style or {}
    if parent_native.get("display") == "flex" and parent_native.get("flex_direction") in ("row", "row-reverse"):
        # Siblings laid out side by side (a table row's cells, the
        # inline-content approximation's items) don't follow element
        # vertically -- nothing to move -- table-height-algorithm-026.xht.
        return
    seen_self = False
    for sibling in dom._child_nodes(parent):
        if sibling is element:
            seen_self = True
            continue
        if not seen_self or not dom._is_element(sibling):
            continue
        sibling_style = box_of(sibling).native_style or {}
        if sibling_style.get("position") in ("absolute", "fixed"):
            continue
        if sibling.__dict__.get("_layout_box") is None:
            continue
        _shift_subtree(sibling, 0.0, delta)



def _shift_box(node, dx: float, dy: float) -> None:
    box = node.__dict__.get("_layout_box")
    if box is not None:
        node.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=box.width, height=box.height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
    # An inline element's per-line rects (`_publish_inline_formatting`'s
    # box.inline_boxes, what it reports as its client rects) move
    # with it -- column-visibility-004.xht.
    rects = box_of(node).inline_boxes
    if rects:
        box_of(node).inline_boxes = [
            (rect[0] + dx, rect[1] + dy) + tuple(rect[2:]) for rect in rects]



def _shift_recomputed_subtree(element, dx: float, dy: float, boxes, node_map: dict) -> None:
    """After a shrink-to-fit recompute of element's subtree
    (`_write_boxes(boxes)`, positions relative to the subtree's own origin),
    move exactly what that recompute produced: an absolutely positioned
    descendant anchored to a containing block outside the subtree kept
    its real page position and must stay put -- top-applies-to-001.xht.
    Elements with no Taffy node of their own (inline boxes published from
    fragments) are left to the caller's re-publish."""
    recomputed = {id(node_map[node_id]) for node_id in boxes if node_id in node_map}

    def walk(node):
        resolved = box_of(node).resolved_style
        if resolved is not None and not dom._renders(resolved[1]):
            return
        if id(node) not in recomputed:
            if resolved is not None and box_model._is_absolutely_positioned(resolved[1]):
                return
        else:
            _shift_box(node, dx, dy)
        for fragment in box_of(node).inline_fragments or ():
            if id(fragment) in recomputed:
                _shift_box(fragment, dx, dy)
        for box in (box_of(node).anonymous_table_boxes or {}).values():
            if id(box) in recomputed:
                _shift_box(box, dx, dy)
            walk_anonymous_children(box)
        for child in dom._child_nodes(node):
            if dom._is_element(child):
                walk(child)

    def walk_anonymous_children(box):
        for inner in (box_of(box).anonymous_table_boxes or {}).values():
            if id(inner) in recomputed:
                _shift_box(inner, dx, dy)
            walk_anonymous_children(inner)

    walk(element)



def _shift_subtree(element, dx: float, dy: float) -> None:
    """Shift element and everything painted inside it by (dx, dy) -- used
    to carry a corrected element's position through to its descendants,
    whose boxes Taffy computed as offsets from element's own
    now-corrected origin. A uniform shift preserves every internal
    relationship Taffy already got right.

    Skips element entirely when it's currently display:none -- it was
    never given a real Taffy node this pass, so its stale _layout_box
    must not keep being shifted on top of whatever was last published,
    or the correction compounds forever across relayouts."""
    resolved = box_of(element).resolved_style
    if resolved is not None and not dom._renders(resolved[1]):
        return
    _shift_box(element, dx, dy)
    for fragment in box_of(element).inline_fragments or ():
        _shift_box(fragment, dx, dy)
    # Anonymous table boxes generated under element (CSS 2.1 17.2.1) aren't
    # in childNodes -- shifted here; the real nodes they wrap are still
    # reached once, through the DOM walk below.
    _shift_anonymous_boxes(element, dx, dy)
    for child in dom._child_nodes(element):
        if dom._is_element(child):
            _shift_subtree(child, dx, dy)



def _shift_anonymous_boxes(element, dx: float, dy: float) -> None:
    for box in (box_of(element).anonymous_table_boxes or {}).values():
        _shift_box(box, dx, dy)
        _shift_anonymous_boxes(box, dx, dy)
