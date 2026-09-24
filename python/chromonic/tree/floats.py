from __future__ import annotations

import dataclasses
import math

from domonic.layout import LayoutBox

from . import box_model, dom, geometry, inline_formatting




def _cleared_y(computed, active_floats, current_y: float) -> float:
    """The minimum y `computed`'s own clear property requires, given
    `active_floats` (`_fix_float_flow_after_block_sibling`'s running list of
    {side, edge, top, bottom} for every float already packed in this
    container) -- CSS 2.1 9.5.2: a cleared box's top border edge must be
    at or below the bottom outer edge of every earlier float on the
    cleared side(s). Applies to a clearing float exactly as much as a
    clearing block (9.5.2 doesn't exempt one). Returns `current_y`
    unchanged when there's nothing to clear."""
    if computed is None:
        return current_y
    clear_value = (getattr(computed, "clear", None) or "none").strip().lower()
    if clear_value not in ("left", "right", "both"):
        return current_y
    required = current_y
    for active in active_floats:
        if clear_value == "both" or active["side"] == clear_value:
            required = max(required, active["bottom"])
    return required



def _fix_float_shrink_to_fit_width(tree_obj, node_map: dict) -> bool:
    """CSS 2.1 10.3.5/10.3.6: a floated box with width:auto is sized by
    shrink-to-fit, not stretched to fill its containing block -- chromonic
    has no real float implementation, so a floated element reaches this
    point laid out as an ordinary full-width block first.

    Re-runs Taffy's compute() for just this element at available_width=None
    (max-content), re-laying-out the real subtree so descendants reflow
    into the narrower width too, then shifts the subtree to its real page
    position. Only ever shrinks.

    Returns whether any subtree was actually shifted -- the caller uses
    this to skip a redundant `_publish_inline_formatting` republish on the
    large majority of layouts that have no floats needing this."""
    shifted = False
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None or not box_model._is_floated(resolved[0]):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("width") != "auto":
            continue
        node_id = by_id.get(id(element))
        if node_id is None:
            continue
        boxes = tree_obj.compute(node_id, None, None)
        own = boxes.get(node_id)
        if own is None:
            continue
        new_width = own[2]
        if new_width >= box.width:
            continue  # shrink-to-fit never grows a box past its available width
        float_value = getattr(resolved[0], "float", None)
        float_value = (float_value or "").strip().lower()
        target_x = (box.x + box.width - new_width) if float_value == "right" else box.x
        geometry._write_boxes(boxes, node_map)
        dx = target_x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            geometry._shift_recomputed_subtree(element, dx, dy, boxes, node_map)
            shifted = True
    return shifted



def _fix_float_flow_after_block_sibling(node_map: dict) -> None:
    """CSS 2.1 9.5: a float starts at or below the current block-flow
    position, at the containing block's edge, never wherever a previous
    sibling's box happened to end horizontally. `_approximate_inline_flow`
    stands in for real float layout with plain flex-wrap, which has no
    notion of this -- a row only wraps on width overflow, so a paragraph
    followed by floats packed them onto its own row instead of below it.

    Runs after Taffy's flex-wrap layout, using the qualifying split
    `_approximate_inline_flow` recorded on `element`. Narrow on purpose: only
    applies when every qualifying child is a real float, not merely
    inline-level -- a group with even one qualifying-but-not-floated child
    leaves Taffy's own flex-wrap result alone (real inline-flow
    approximation, e.g. a nav bar, relies on its own gap/wrap handling).
    Does not require at least one non-qualifying block sibling -- a pure
    all-float group needs this same left/right packing just as much
    (confirmed: two floats with no other sibling packed side-by-side, both
    flush-left, via Taffy's flex-wrap, float:right never consulted at
    all). When it applies, every child's position is recomputed by simple
    left-to-right block/float packing."""
    for element in list(node_map.values()):
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None:
            continue
        def out_of_flow(child) -> bool:
            resolved = getattr(child, "_chromonic_resolved_style", None)
            return resolved is not None and box_model._is_absolutely_positioned(resolved[1])

        if any(is_flow and not out_of_flow(child) and not box_model._is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        # An absolutely positioned child is no sibling in this flow at all
        # -- position-absolute-007.xht.
        children, qualifies = zip(*[(child, is_flow) for child, is_flow in zip(children, qualifies)
                                    if not out_of_flow(child)]) if any(
            not out_of_flow(child) for child in children) else ((), ())
        if not children:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = box.x + box.border_left + pl
        content_right = content_left + (box.client_width - pl - pr)
        cursor_x = content_left
        right_cursor_x = content_right
        cursor_y = box.y + box.border_top + pt
        row_bottom = cursor_y
        # CSS 2.1 9.5: every still-uncleared float narrows the line box of
        # every row it overlaps, not just the one it first packed onto --
        # tracked here so a later block's margin:auto resolves against the
        # narrowed band, not the full content width.
        active_floats: list = []
        # `_adjust_body_collapsed_margins` may have already folded element's
        # own top margin together with this first child's (CSS 2.1 8.3.1
        # adjoining-margins collapse) -- treated as already-resolved 0 here.
        first_margin_collapsed = getattr(element, "_chromonic_margin_collapsed", False)
        # CSS 2.1 8.3.1: adjoining margins collapse into one; a float
        # between two blocks doesn't break the chain. `pending_margins`
        # accumulates it; a real block resolves the whole set via
        # `inline_formatting._collapse_margin_set`.
        pending_margins: list = []
        block_bottom = cursor_y
        for index, (child, is_flow) in enumerate(zip(children, qualifies)):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            mt, mr, mb, ml = (box_model._numeric_edge(v) for v in margin)
            if not is_flow and (getattr(child, "tagName", "") or "").lower() == "br":
                # CSS 2.1 9.2.2/9.5.2: a <br> among floats is a forced line
                # break, not a block -- its empty line box sits at the
                # current flow position beside the floats, narrowed by them
                # like any line box; its own clear (the `br { clear: both
                # }` idiom separating rows of floated test containers)
                # applies clearance to what follows the break, never to the
                # break's own line.
                line_top = block_bottom + inline_formatting._collapse_margin_set(pending_margins)
                line_height = child_box.height
                left = content_left
                for active in active_floats:
                    if (active["side"] == "left" and active["top"] < line_top + line_height
                            and active["bottom"] > line_top):
                        left = max(left, active["edge"])
                glyph_height = child.__dict__.get("_chromonic_br_glyph_height")
                new_y, new_height = line_top, line_height
                if glyph_height is not None and glyph_height < line_height:
                    # Chrome reports the break's own inline box (the font's
                    # content area, centred in the line), not the whole line box.
                    new_y = line_top + math.floor((line_height - glyph_height) / 2)
                    new_height = glyph_height
                child.__dict__["_layout_box"] = LayoutBox(
                    x=left, y=new_y, width=0.0, height=new_height,
                    client_width=0.0, client_height=new_height, border_top=0.0, border_left=0.0)
                block_bottom = line_top + line_height
                child_computed = (getattr(child, "_chromonic_resolved_style", None) or (None,))[0]
                block_bottom = _cleared_y(child_computed, active_floats, block_bottom)
                # The clearance is part of the container's flow extent
                # (`_fix_float_flow_container_auto_height`). Stored relative to
                # the break's own box: a later pass may shift the whole
                # container, and an absolute y would go stale.
                child.__dict__["_chromonic_br_flow_bottom"] = block_bottom - new_y
                pending_margins = []
                row_bottom = cursor_y = block_bottom
                cursor_x = content_left
                right_cursor_x = content_right
                continue
            if not is_flow:
                if index == 0 and first_margin_collapsed:
                    mt = 0.0
                pending_margins.append(mt)
                if inline_formatting._block_margins_collapse_through(child, child_box):
                    # Own top/bottom margin joins the same adjoining set --
                    # nothing resolves yet, so this empty block's zero-size
                    # position is a best-effort placement.
                    pending_margins.append(mb)
                    new_x = content_left + ml
                    new_y = block_bottom + inline_formatting._collapse_margin_set(pending_margins)
                    dx, dy = new_x - child_box.x, new_y - child_box.y
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        geometry._shift_subtree(child, dx, dy)
                    cursor_x = content_left
                    continue
                # An ordinary in-flow block: own row, at the containing
                # block's edge, below everything placed so far -- narrowed
                # by a still-active float (CSS 2.1 9.5) only if this child
                # establishes its own BFC (9.4.1); otherwise its border box
                # may extend behind one.
                collapsed = inline_formatting._collapse_margin_set(pending_margins)
                new_y = block_bottom + collapsed
                narrowed_left = content_left
                narrowed_right = content_right
                child_computed = (getattr(child, "_chromonic_resolved_style", None) or (None,))[0]
                new_y = _cleared_y(child_computed, active_floats, new_y)
                child_native_style = getattr(child, "_chromonic_native_style", None) or {}
                child_has_explicit_width = child_native_style.get("width") != "auto"
                if box_model._establishes_bfc(child_computed):
                    # CSS 2.1 9.5: a box establishing its own BFC must not
                    # overlap any float still active at its top -- an
                    # explicit-width box too wide for what's left at this
                    # new_y drops further, past whichever float is
                    # blocking it, renarrowed there, repeated since
                    # dropping past one float can still leave another in
                    # the way -- floats-wrap-top-below-bfc-002l.xht.
                    #
                    # width:auto never needs this push-down check -- it
                    # just fills whatever narrowed space is left rather
                    # than needing to fit an already-decided size. Using
                    # the still-full-row child_box.width as the fit check
                    # here (as the fixed-width case does) was wrong for
                    # auto-width boxes -- floats-wrap-bfc-001-left-overflow.xht.
                    while True:
                        narrowed_left = content_left
                        narrowed_right = content_right
                        # A real interval overlap, not just "hasn't ended
                        # yet" -- a float whose top is still below this
                        # box's new_y hasn't started yet either, and mustn't
                        # narrow a box placed above it.
                        blocking = [
                            a for a in active_floats
                            if a["top"] < new_y + child_box.height and a["bottom"] > new_y
                        ]
                        for active in blocking:
                            if active["side"] == "left":
                                narrowed_left = max(narrowed_left, active["edge"])
                            else:
                                narrowed_right = min(narrowed_right, active["edge"])
                        if (not child_has_explicit_width or not blocking
                                or child_box.width <= narrowed_right - narrowed_left):
                            break
                        new_y = min(active["bottom"] for active in blocking)
                ml_auto = margin[3] == "auto"
                mr_auto = margin[1] == "auto"
                if ml_auto or mr_auto:
                    available = max(0.0, narrowed_right - narrowed_left)
                    remaining = available - child_box.width
                    if ml_auto and mr_auto:
                        ml = mr = remaining / 2.0
                    elif ml_auto:
                        ml = remaining - mr
                    else:
                        mr = remaining - ml
                if narrowed_left > content_left + 1e-6:
                    # CSS 2.1 9.5: a BFC box's border box must clear the
                    # float; its own margin may run under it --
                    # flexbox_fbfc2.html.
                    new_x = max(narrowed_left, content_left + ml)
                else:
                    new_x = narrowed_left + ml
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    geometry._shift_subtree(child, dx, dy)
                block_bottom = new_y + child_box.height
                pending_margins = [mb]
                row_bottom = cursor_y = block_bottom
                cursor_x = content_left
                right_cursor_x = content_right
                continue
            if pending_margins:
                # A float never participates in margin collapsing itself
                # (CSS 2.1 8.3.1 only adjoins in-flow block boxes), but it
                # still starts below whatever vertical space a pending
                # collapsed margin resolves to -- resolved here, once; a
                # later ordinary block starts its own fresh chain from
                # block_bottom as if this float were never there.
                block_bottom = block_bottom + inline_formatting._collapse_margin_set(pending_margins)
                cursor_y = row_bottom = block_bottom
                pending_margins = []
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            child_computed = child_resolved[0] if child_resolved is not None else None
            float_side = "left"
            if child_computed is not None:
                float_value = (getattr(child_computed, "float", None) or "").strip().lower()
                if float_value == "right":
                    float_side = "right"
            # CSS 2.1 9.5.2: clear applies to a floated box exactly as much
            # as an ordinary block -- pushes its top (and cursor_y/row_bottom,
            # derived from it below) past whatever it's clearing, before
            # this float's own placement is computed.
            cleared_y = _cleared_y(child_computed, active_floats, cursor_y)
            if cleared_y > cursor_y:
                cursor_y = row_bottom = cleared_y
                cursor_x = content_left
                right_cursor_x = content_right
            if float_side == "right":
                # float:right packs flush to the containing block's right
                # content edge, not the left-to-right packing below (CSS
                # 2.1 9.5.1).
                start_x = right_cursor_x - mr - child_box.width
                # CSS 2.1 9.5.1 rule 7: a float's outer top may not be
                # higher than any earlier float it would otherwise overlap.
                # Triggered by the overlap itself (start_x < cursor_x) --
                # floats-wrap-top-below-bfc-002l.xht.
                if start_x < cursor_x:
                    cursor_y = row_bottom
                    right_cursor_x = content_right
                    start_x = right_cursor_x - mr - child_box.width
                new_x, new_y = start_x, cursor_y + mt
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    geometry._shift_subtree(child, dx, dy)
                right_cursor_x = new_x - ml
                bottom = new_y + child_box.height + mb
                row_bottom = max(row_bottom, bottom)
                active_floats.append({"side": "right", "edge": new_x - ml, "top": new_y, "bottom": bottom})
                continue
            start_x = cursor_x + ml
            # Symmetric with the right-float branch above -- the overlap
            # itself is the trigger.
            if start_x + child_box.width + mr > right_cursor_x:
                cursor_x = content_left
                cursor_y = row_bottom
                start_x = cursor_x + ml
            new_x, new_y = start_x, cursor_y + mt
            dx, dy = new_x - child_box.x, new_y - child_box.y
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                geometry._shift_subtree(child, dx, dy)
            cursor_x = new_x + child_box.width + mr
            bottom = new_y + child_box.height + mb
            row_bottom = max(row_bottom, bottom)
            active_floats.append({"side": "left", "edge": cursor_x, "top": new_y, "bottom": bottom})



def _bfc_descendant_float_bottom(element, floor: float) -> float:
    """The deepest bottom-margin-edge of any float inside `element`'s own
    BFC (CSS 2.1 10.6.7) -- descends through non-BFC-establishing
    descendants (an ordinary wrapper isn't a float's containing block;
    the nearest real BFC ancestor still owns it), stopping at any
    descendant that establishes its own BFC."""
    best = floor
    for child in dom._child_nodes(element):
        if not dom._is_element(child):
            continue
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is None:
            continue
        computed, style_obj = resolved
        if box_model._is_absolutely_positioned(style_obj):
            continue
        child_box = child.__dict__.get("_layout_box")
        if child_box is None:
            continue
        if box_model._is_floated(computed):
            native = getattr(child, "_chromonic_native_style", None) or {}
            margin = native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            best = max(best, child_box.y + child_box.height + box_model._numeric_edge(margin[2]))
            continue
        if box_model._establishes_bfc(computed):
            continue
        best = max(best, _bfc_descendant_float_bottom(child, floor))
    return best



def _has_ratio_derived_height(native: dict) -> bool:
    """CSS Sizing 4 aspect-ratio: when height is auto but width is definite
    and a ratio was declared, the used height comes from the ratio
    (Taffy's aspect_ratio field already resolves it), not from summed
    content -- so a recompute-from-children pass must leave this one
    alone. css-sizing/aspect-ratio/block-aspect-ratio-010.html: recomputed
    to 600px (summed children) instead of the ratio's own 100px. Narrow on
    purpose: min-height clamping past the ratio is a separate rule this
    doesn't attempt, and an indefinite width leaves content-based sizing
    exactly right."""
    return (isinstance(native.get("aspect_ratio"), (int, float))
            and isinstance(native.get("width"), (int, float))
            and native.get("height") == "auto")



def _fix_nested_bfc_float_auto_height(node_map: dict) -> None:
    """The same CSS 2.1 10.6.3/10.6.7 rule `_fix_float_flow_container_auto_height`
    applies (a height:auto box never counts a float unless it establishes
    a BFC) but for the cases that heuristic doesn't reach: a lone float
    (the only child of an ordinary wrapper div) reaches Taffy as a plain
    in-flow block, its full height counted toward the wrapper's
    auto-height -- Taffy has no notion it should be excluded.

    A BFC-establishing ancestor further up needs the opposite correction,
    recursing past that non-BFC wrapper to find the float, since its own
    auto-height counts every descendant float in its formatting context."""
    for element in node_map.values():
        if not dom._is_element(element):
            continue
        if getattr(element, "_chromonic_float_flow_children", None) is not None:
            continue  # already handled by _fix_float_flow_container_auto_height
        if getattr(element, "_chromonic_tag_name", None) == "body":
            continue  # _adjust_body_collapsed_margins owns body
        if getattr(element, "_chromonic_is_table_root", False):
            # A table box (a BFC too) is sized by the table pipeline
            # (`_settle_table`) from its rows and captions -- often anonymous
            # boxes with no DOM children to read here at all --
            # caption-side-applies-to-017.xht, table-margin-004.xht.
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        if _has_ratio_derived_height(native):
            continue
        if native.get("display") in ("flex", "grid"):
            # float always computes to none on a flex/grid item, so this
            # function's whole premise never applies to one, even though
            # `box_model._establishes_bfc` says it establishes a BFC. Its
            # block-flow-style recompute isn't equivalent to Taffy's own
            # flex/grid sizing, so it must never touch one.
            continue
        if not getattr(element, "_chromonic_has_layout_children", False):
            # A genuine leaf was never sized by summing child
            # contributions -- its height:auto is already a real,
            # correctly-measured text/line-box result.
            continue
        if getattr(element, "_chromonic_inline_plan", None) is not None:
            # A genuine single Taffy leaf measured whole by plan.measure()
            # -- its inline children were flattened into the plan's runs,
            # never built as real Taffy nodes, so they have no _layout_box
            # this function's "sum child bottoms" logic could read.
            # Recomputing from childNodes silently discarded the plan's
            # already-correct measured height -- wpt/css/CSS2/visudet/
            # content-height-001.html: overwritten from 200px to 129px.
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = box_model._establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        has_float_child = False
        for child in dom._child_nodes(element):
            if not dom._is_element(child):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            if child_resolved is None:
                continue
            child_computed, child_style_obj = child_resolved
            if box_model._is_absolutely_positioned(child_style_obj):
                continue
            if box_model._is_floated(child_computed):
                has_float_child = True
                continue
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            normal_bottom = max(normal_bottom, child_box.y + child_box.height + box_model._numeric_edge(margin[2]))
        if not has_float_child and not establishes_bfc:
            continue  # nothing this pass would change -- leave Taffy's own result alone
        content_bottom = normal_bottom
        if establishes_bfc:
            content_bottom = max(content_bottom, _bfc_descendant_float_bottom(element, content_top))
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            geometry._shift_later_siblings_for_height_delta(element, delta)



def _fix_float_flow_container_auto_height(node_map: dict) -> None:
    """CSS 2.1 10.6.3/10.6.7: an element's height:auto is the max extent of
    its in-flow content's bottom margin edge -- a float contributes too,
    but only if the element establishes a BFC (9.4.1). Taffy's flex-wrap
    row-summing (`_approximate_inline_flow`'s stand-in for real float
    layout) instead adds each wrapped row's height together,
    double-counting a float row and a later normal-flow row that both
    start from the same content top.

    Runs after `_fix_float_flow_after_block_sibling` has placed every child
    at its real, float-aware position -- recomputes the container's
    height from those final positions."""
    for element in node_map.values():
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None:
            continue
        def out_of_flow(child) -> bool:
            resolved = getattr(child, "_chromonic_resolved_style", None)
            return resolved is not None and box_model._is_absolutely_positioned(resolved[1])

        if any(is_flow and not out_of_flow(child) and not box_model._is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        if getattr(element, "_chromonic_tag_name", None) == "body":
            # `_adjust_body_collapsed_margins` already owns body's auto-height
            # with extra precision this generic version doesn't replicate.
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        if _has_ratio_derived_height(native):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = box_model._establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        float_bottom = content_top
        for child, is_flow in zip(children, qualifies):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or out_of_flow(child):
                continue  # an absolutely positioned child never sizes its parent (abspos-008.xht)
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            bottom = child_box.y + child_box.height + box_model._numeric_edge(margin[2])
            br_flow_bottom = child.__dict__.get("_chromonic_br_flow_bottom")
            if br_flow_bottom is not None and not is_flow:
                bottom = max(bottom, child_box.y + br_flow_bottom)  # a `<br clear>`'s clearance
            if is_flow:
                float_bottom = max(float_bottom, bottom)
            else:
                normal_bottom = max(normal_bottom, bottom)
        content_bottom = max(normal_bottom, float_bottom) if establishes_bfc else normal_bottom
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            # Taffy already stacked every later sibling using this
            # element's stale, pre-fix height -- must be propagated.
            geometry._shift_later_siblings_for_height_delta(element, delta)



def _fix_inline_float_position(node_map: dict) -> None:
    """CSS 2.1 9.5: correct the position of a float found mixed into
    running text (`elif inline_items:`'s flex-row-of-text approximation
    marks these on `element._chromonic_inline_floats`, excluded from that
    row's baseline alignment). Taffy already gave each one a real,
    content-sized box wrapped onto some row -- treated as a stand-in for
    "which line it interrupted" (its own y), corrected only in x: flush to
    the container's content edge (rule 1), dropped below any earlier
    same-container float it would overlap (rule 7). Does not narrow
    surrounding text around the float's rectangle -- logged in PLAN.md."""
    for element in list(node_map.values()):
        floats = getattr(element, "_chromonic_inline_floats", None) if hasattr(element, "__dict__") else None
        if not floats:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = box.x + box.border_left + pl
        content_right = content_left + (box.client_width - pl - pr)
        active_floats: list = []
        for child in floats:
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            child_computed = child_resolved[0] if child_resolved is not None else None
            side = "left"
            if child_computed is not None:
                float_value = (getattr(child_computed, "float", None) or "").strip().lower()
                if float_value == "right":
                    side = "right"
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
            mt, mr, mb, ml = (box_model._numeric_edge(v) for v in margin)
            top = child_box.y
            top = _cleared_y(child_computed, active_floats, top)
            # Rule 7: this float's outer top may not be higher than any
            # earlier same-container float it would overlap -- dropped
            # below the lowest blocking one, same collision check
            # `_fix_float_flow_after_block_sibling` uses for block-level floats.
            while True:
                blocking = [a for a in active_floats
                            if a["top"] < top + child_box.height and a["bottom"] > top]
                if not blocking:
                    break
                new_top = min(a["bottom"] for a in blocking)
                if new_top <= top + 1e-6:
                    break
                top = new_top
            if side == "right":
                new_x = content_right - mr - child_box.width
                left_blocking = [a for a in active_floats if a["side"] == "left"
                                 and a["top"] < top + child_box.height and a["bottom"] > top]
                if left_blocking:
                    new_x = max(new_x, max(a["edge"] for a in left_blocking))
            else:
                new_x = content_left + ml
                right_blocking = [a for a in active_floats if a["side"] == "right"
                                  and a["top"] < top + child_box.height and a["bottom"] > top]
                if right_blocking:
                    new_x = min(new_x, min(a["edge"] for a in right_blocking) - child_box.width)
            dx, dy = new_x - child_box.x, top - child_box.y
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                geometry._shift_subtree(child, dx, dy)
                child_box = child.__dict__["_layout_box"]
            edge = child_box.x + child_box.width if side == "left" else child_box.x
            active_floats.append({"side": side, "edge": edge, "top": child_box.y,
                                  "bottom": child_box.y + child_box.height + mb})
