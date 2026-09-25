from __future__ import annotations

import dataclasses

from domonic.layout import LayoutBox

from . import box_model, dom, flex_grid, geometry, inline_finalize




def _fix_relative_rtl_insets(node_map: dict) -> None:
    """CSS 2.1 9.4.3: a position:relative box with both left and right set
    is over-constrained -- left wins in an ltr containing block, right in
    an rtl one. Taffy always takes left; an rtl box is moved from left to
    -right here -- position-relative-010.xht, relpos-calcs-006.xht."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "relative":
            continue
        inset = style.get("inset") or ()
        if len(inset) != 4 or (inset[3] == "auto" and inset[1] == "auto"):
            continue
        if style.get("display") != "block" or element.__dict__.get("_chromonic_split_container") is not None:
            continue
        parent = dom._layout_parent(element)
        parent_box = parent.__dict__.get("_layout_box") if parent is not None and hasattr(parent, "__dict__") else None
        if parent_box is None:
            continue
        if dom._element_direction(parent, getattr(parent, "_chromonic_computed_style", None)) != "rtl":
            continue
        # `_fix_rtl_block_positioning` leaves relative boxes alone, so this
        # one is still at Taffy's left-aligned spot plus Taffy's left; CSS
        # 2.1 10.3.3 puts an rtl block against the containing block's right
        # edge before the relative offset applies.
        parent_padding = parent.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_x = parent_box.x + parent_box.border_left + parent_padding[3]
        content_width = parent_box.client_width - parent_padding[1] - parent_padding[3]
        margin = style.get("margin") or (0.0,) * 4
        margin_right = _resolve_inset(margin[1], content_width) or 0.0
        base_x = content_x + content_width - margin_right - box.width
        if style.get("width") == "auto" and not (isinstance(margin[3], str) or isinstance(margin[1], str)):
            base_x = content_x + (_resolve_inset(margin[3], content_width) or 0.0)
        left_v = _resolve_inset(inset[3], content_width) if inset[3] != "auto" else None
        right_v = _resolve_inset(inset[1], content_width) if inset[1] != "auto" else None
        offset = -right_v if right_v is not None else (left_v or 0.0)
        dx = (base_x + offset) - box.x
        if abs(dx) > 1e-6:
            geometry._shift_subtree(element, dx, 0.0)



def _is_root_anchored(element) -> bool:
    """Whether `element` is `position:absolute`/`fixed` with no positioned
    ancestor -- its containing block is the viewport itself. Shared by
    `_fix_viewport_anchored_positioning` and `_apply_root_margin_offset`
    (which must not shift such elements)."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is None or not box_model._is_absolutely_positioned(resolved[1]):
        return False
    return _find_containing_block_ancestor(element) is None



def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning, or `None` if none exists.

    CSS 2.1 10.1 rule 4: a position:fixed box's containing block is always
    the viewport (`None` here) -- unlike position:absolute's rule 3, no
    ancestor's own position can ever substitute for it. Confirmed on
    position-absolute-005.xht: a fixed box nested inside an absolute
    ancestor anchored to that ancestor instead of the viewport."""
    # `element._chromonic_native_style["position"]` can't distinguish this --
    # style_bridge._position() maps CSS fixed to the same Taffy-level
    # "absolute" string (Taffy has no native fixed concept).
    # `_chromonic_resolved_style`'s LayoutStyle.position is the pre-mapping
    # value that still keeps fixed distinct.
    own_resolved = getattr(element, "_chromonic_resolved_style", None)
    if own_resolved is not None:
        own_position = own_resolved[1].position
        if getattr(own_position, "value", own_position) == "fixed":
            return None
    parent = getattr(element, "parentElement", None)
    while parent is not None:
        resolved = getattr(parent, "_chromonic_resolved_style", None)
        if resolved is not None and box_model._establishes_containing_block(resolved[1]):
            return parent
        parent = getattr(parent, "parentElement", None)
    return None



def _resolve_inset(value, basis: float) -> "float | None":
    if value == "auto":
        return None
    if isinstance(value, tuple):  # ("pct", fraction)
        return value[1] * basis
    return float(value)



def _resolve_viewport_anchored_box(style: dict, box, viewport_height: float):
    """`(new_y, new_height)` for a root-anchored element, resolved against
    the true `viewport_height` instead of the root's own Taffy box --
    either may be `None`, meaning "leave as Taffy already computed it"."""
    top, _right, bottom, _left = style["inset"]
    top_v = _resolve_inset(top, viewport_height)
    bottom_v = _resolve_inset(bottom, viewport_height)
    margin_top, _mr, margin_bottom, _ml = style["margin"]
    mt = _resolve_inset(margin_top, viewport_height) or 0.0
    mb = _resolve_inset(margin_bottom, viewport_height) or 0.0
    height = style["height"]
    if isinstance(height, tuple) and height[0] == "pct":
        # A percentage height resolves against the containing block's own
        # height regardless of whether `bottom` is also set, unlike the
        # top+bottom-both-set case below.
        new_height = height[1] * viewport_height
        if top_v is not None:
            return top_v + mt, new_height
        if bottom_v is not None:
            return viewport_height - bottom_v - mb - new_height, new_height
        return None, new_height
    if bottom_v is None:
        # top alone (or neither) determines position, independent of the
        # containing block's height. A fixed box attached to a positioned
        # ancestor is at that ancestor's offset instead of the viewport's
        # -- position-absolute-005.xht -- so top is re-asserted here.
        return (top_v + mt if top_v is not None else None), None
    if top_v is None:
        # Bottom-anchored, top:auto -- box.height is already right, only
        # position needs correcting.
        return viewport_height - bottom_v - mb - box.height, None
    # Both top and bottom definite. If height is also definite, this is
    # over-constrained -- top (+height) alone determine the box, leave it
    # alone. If height is auto, the box stretches to fill the gap, which
    # does depend on the containing block's height.
    if height != "auto":
        return None, None
    return top_v + mt, viewport_height - top_v - mt - bottom_v - mb



def _resolve_viewport_anchored_box_x(style: dict, box, viewport_width: float, element=None):
    """`(new_x, new_width)` for a root-anchored element -- the horizontal
    counterpart to `_resolve_viewport_anchored_box`, needed because the
    root's own Taffy box (shrunk for its own margin) isn't always the true
    viewport width either."""
    _top, right, _bottom, left = style["inset"]
    left_v = _resolve_inset(left, viewport_width)
    right_v = _resolve_inset(right, viewport_width)
    _mt, margin_right, _mb, margin_left = style["margin"]
    ml = _resolve_inset(margin_left, viewport_width)
    mr = _resolve_inset(margin_right, viewport_width)
    width = style["width"]
    if isinstance(width, tuple) and width[0] == "pct":
        # Same opposite-inset-independent resolution as the height branch
        # above -- also catches the synthetic ("pct", 1.0) build() assigns
        # a width:auto block with inline content, which for a
        # root-anchored box must resolve against the true viewport width.
        new_width = width[1] * viewport_width
        if left_v is not None:
            return left_v + (ml or 0.0), new_width
        if right_v is not None:
            return viewport_width - right_v - (mr or 0.0) - new_width, new_width
        return None, new_width
    if right_v is None:
        # left alone determines position. Unlike the vertical counterpart,
        # this can't be left as Taffy computed it -- left_v may be a
        # percentage Taffy resolved against its own margin-shrunk root box
        # instead of the true viewport width.
        return (None if left_v is None else left_v + (ml or 0.0)), None
    if left_v is None:
        return viewport_width - right_v - (mr or 0.0) - box.width, None
    if width != "auto":
        return None, None
    # CSS 2.1 10.3.7 rule 5 (left/right definite, width auto): any auto
    # margin is 0 while solving for width, then 10.4 clamps that solved
    # width to min/max-width; once clamped, an auto margin re-absorbs the
    # slack the clamp freed (equal split, or the zero-one-side exception
    # `_fix_absolute_horizontal_auto_margins` applies) -- same reasoning as
    # `_fix_absolute_width_against_containing_block`'s ancestor case.
    new_width = viewport_width - left_v - (ml or 0.0) - right_v - (mr or 0.0)
    max_width_v = _resolve_inset(style.get("max_width"), viewport_width)
    min_width_v = _resolve_inset(style.get("min_width"), viewport_width)
    if max_width_v is not None and new_width > max_width_v:
        new_width = max_width_v
    elif min_width_v is not None and new_width < min_width_v:
        new_width = min_width_v
    else:
        return left_v + (ml or 0.0), new_width
    remaining = viewport_width - left_v - new_width - right_v
    cml, cmr = ml, mr
    if cml is None and cmr is None:
        if remaining < 0:
            if dom._element_direction(element) == "rtl":
                cmr, cml = 0.0, remaining
            else:
                cml, cmr = 0.0, remaining
        else:
            cml = cmr = remaining / 2.0
    elif cml is None:
        cml = remaining - cmr
    elif cmr is None:
        cmr = remaining - cml
    return left_v + cml, new_width



def _fix_absolute_horizontal_auto_margins(node_map: dict) -> None:
    """CSS 2.1 10.3.7: for a position:absolute box whose left/width/right
    are all definite, any auto margin absorbs the remaining slack of
    `left + margin-left + width + margin-right + right == containing block
    width`, split evenly if both are auto. Taffy's own absolute
    positioning doesn't solve this, so this corrects the box's x after the fact.

    Only handled with a real containing-block ancestor -- a root-anchored
    box is corrected separately by `_fix_viewport_anchored_positioning`.

    Also handles exactly one of left/right being auto (case 3/5): any auto
    margin resolves to 0, and the missing inset is solved from the
    constraint equation.

    And the fully over-constrained case (rule 1/10.3.8): left/width/right/
    both margins all definite -- Chrome drops the specified right (left in
    rtl) and re-solves from left + margin-left; Taffy instead positions
    from right -- absolute-replaced-width-071.xht."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        margin = style.get("margin")
        inset = style.get("inset")
        if not margin or not inset:
            continue
        margin_left, margin_right = margin[3], margin[1]
        left, right = inset[3], inset[1]
        left_auto, right_auto = left == "auto", right == "auto"
        if style.get("width") == "auto" or (left_auto and right_auto):
            continue  # under-constrained differently -- not this equation
        margin_auto = margin_left == "auto" or margin_right == "auto"
        over_constrained = not left_auto and not right_auto and not margin_auto
        if not margin_auto and not over_constrained:
            continue  # nothing left for this fix-up to solve
        containing = _find_containing_block_ancestor(element)
        if containing is None:
            continue  # root-anchored -- handled by the viewport-anchored fix instead
        cb_box = containing.__dict__.get("_layout_box")
        if cb_box is None:
            continue
        cb_width = cb_box.client_width
        cb_content_x = cb_box.x + cb_box.border_left
        if over_constrained:
            if dom._element_direction(containing) == "rtl":
                right_v = _resolve_inset(right, cb_width) or 0.0
                mr = _resolve_inset(margin_right, cb_width) or 0.0
                new_x = cb_content_x + cb_width - right_v - mr - box.width
            else:
                left_v = _resolve_inset(left, cb_width) or 0.0
                ml = _resolve_inset(margin_left, cb_width) or 0.0
                new_x = cb_content_x + left_v + ml
        elif not left_auto and not right_auto:
            left_v = _resolve_inset(left, cb_width) or 0.0
            right_v = _resolve_inset(right, cb_width) or 0.0
            remaining = cb_width - left_v - box.width - right_v
            ml = None if margin_left == "auto" else (_resolve_inset(margin_left, cb_width) or 0.0)
            mr = None if margin_right == "auto" else (_resolve_inset(margin_right, cb_width) or 0.0)
            if ml is None and mr is None:
                if remaining < 0:
                    # CSS 2.1 10.3.7: an equal split of negative slack would
                    # give both margins a negative value -- the leading-edge
                    # margin (left in ltr, right in rtl) is pinned to 0 and
                    # the other absorbs all of it.
                    if dom._element_direction(containing) == "rtl":
                        mr, ml = 0.0, remaining
                    else:
                        ml, mr = 0.0, remaining
                else:
                    ml = mr = remaining / 2.0
            elif ml is None:
                ml = remaining - mr
            else:
                mr = remaining - ml
            new_x = cb_content_x + left_v + ml
        elif left_auto:
            right_v = _resolve_inset(right, cb_width) or 0.0
            new_x = cb_content_x + cb_width - right_v - box.width
        else:
            left_v = _resolve_inset(left, cb_width) or 0.0
            new_x = cb_content_x + left_v
        dx = new_x - box.x
        if abs(dx) > 1e-6:
            geometry._shift_subtree(element, dx, 0.0)



def _fix_absolute_vertical_auto_margins(node_map: dict) -> None:
    """CSS 2.1 10.6.4: the vertical counterpart to
    `_fix_absolute_horizontal_auto_margins` -- for a position:absolute box
    whose top/height/bottom are all definite, any auto margin-top/
    margin-bottom absorbs the remaining slack of `top + margin-top +
    height + margin-bottom + bottom == containing block height`, split
    evenly if both are auto. Unlike the horizontal rule, 10.6.4 has no
    direction-based "pin one side to zero" exception -- an equal split
    applies unconditionally, even negative. Taffy doesn't solve this, so
    this corrects the box's y after the fact.

    Only handled with a real containing-block ancestor -- root-anchored is
    `_fix_viewport_anchored_positioning`.

    Also handles exactly one of top/bottom auto (case 3/5): any auto
    margin resolves to 0, missing inset solved from the constraint
    equation. absolute-non-replaced-height-003.xht."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        margin = style.get("margin")
        inset = style.get("inset")
        if not margin or not inset:
            continue
        margin_top, margin_bottom = margin[0], margin[2]
        if margin_top != "auto" and margin_bottom != "auto":
            continue  # nothing left for this fix-up to solve
        top, bottom = inset[0], inset[2]
        top_auto, bottom_auto = top == "auto", bottom == "auto"
        if style.get("height") == "auto" or (top_auto and bottom_auto):
            continue  # under-constrained differently -- not this equation
        containing = _find_containing_block_ancestor(element)
        if containing is None:
            continue  # root-anchored -- handled by the viewport-anchored fix instead
        cb_box = containing.__dict__.get("_layout_box")
        if cb_box is None:
            continue
        cb_height = cb_box.client_height
        cb_content_y = cb_box.y + cb_box.border_top
        if not top_auto and not bottom_auto:
            top_v = _resolve_inset(top, cb_height) or 0.0
            bottom_v = _resolve_inset(bottom, cb_height) or 0.0
            remaining = cb_height - top_v - box.height - bottom_v
            mt = None if margin_top == "auto" else (_resolve_inset(margin_top, cb_height) or 0.0)
            mb = None if margin_bottom == "auto" else (_resolve_inset(margin_bottom, cb_height) or 0.0)
            if mt is None and mb is None:
                mt = mb = remaining / 2.0
            elif mt is None:
                mt = remaining - mb
            else:
                mb = remaining - mt
            new_y = cb_content_y + top_v + mt
        elif top_auto:
            bottom_v = _resolve_inset(bottom, cb_height) or 0.0
            new_y = cb_content_y + cb_height - bottom_v - box.height
        else:
            top_v = _resolve_inset(top, cb_height) or 0.0
            new_y = cb_content_y + top_v
        dy = new_y - box.y
        if abs(dy) > 1e-6:
            geometry._shift_subtree(element, 0.0, dy)



def _fix_absolute_width_against_containing_block(node_map: dict) -> None:
    """CSS 2.1 10.3.7: a position:absolute box with width:auto and both
    left/right definite has its width solved from the constraint equation
    (`left + margin-left + width + margin-right + right == containing
    block width`) -- the real-containing-block-ancestor counterpart to
    `_resolve_viewport_anchored_box_x`. Taffy leaves width:auto at whatever
    the content happened to measure instead --
    position-absolute-percentage-inherit-001.xht: measured 0 instead of
    the ~169px CSS 2.1 10.3.7 solves for.

    Also applies CSS 2.1 10.4's min/max-width clamp to that solved width:
    once clamping replaces it with a fixed min/max-width, the box is back
    to the ordinary all-three-definite case, so an auto margin re-absorbs
    the slack the clamp freed (same equal-split/zero-one-side rule as
    `_fix_absolute_horizontal_auto_margins`) -- otherwise a max-width-clamped,
    centered box would sit flush against left instead --
    position-absolute-width-025.xht.

    Deliberately as narrow as `_fix_absolute_horizontal_auto_margins`
    beside it: only overwrites width/x, children shifted but not relaid
    out to the new width, same simplification
    `_fix_viewport_anchored_positioning` accepts for the root-anchored case."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        if style.get("width") != "auto":
            continue
        inset = style.get("inset")
        if not inset:
            continue
        left, right = inset[3], inset[1]
        if left == "auto" or right == "auto":
            continue  # under-constrained differently -- not this equation
        containing = _find_containing_block_ancestor(element)
        if containing is None:
            continue  # root-anchored -- handled by the viewport-anchored fix instead
        cb_box = containing.__dict__.get("_layout_box")
        if cb_box is None:
            continue
        cb_width = cb_box.client_width
        cb_content_x = cb_box.x + cb_box.border_left
        margin = style.get("margin") or (0.0, 0.0, 0.0, 0.0)
        margin_left_raw, margin_right_raw = margin[3], margin[1]
        ml = _resolve_inset(margin_left_raw, cb_width)
        mr = _resolve_inset(margin_right_raw, cb_width)
        left_v = _resolve_inset(left, cb_width) or 0.0
        right_v = _resolve_inset(right, cb_width) or 0.0
        # CSS Sizing 4 aspect-ratio: a definite `height` alongside `width:
        # auto` and a preferred aspect ratio derives the used width from
        # that ratio, taking priority over this box's own left/right-inset
        # equation (confirmed on aspect-ratio/abspos-006.html: `height:
        # 100px; aspect-ratio: 1/1; left: 0; right: 0` -- Chrome's 100px
        # width, not the 500px the insets equation alone would solve for).
        # Narrowed to an already-*numeric* height (a resolved length, not
        # `auto` or an unresolved percent tuple) so this never fires for
        # the ordinary insets-only case this function otherwise handles.
        ratio = style.get("aspect_ratio")
        ratio_derived = (isinstance(ratio, (int, float)) and ratio > 0
                         and isinstance(style.get("height"), (int, float)))
        if ratio_derived:
            new_width = max(0.0, box.height * ratio)
        else:
            new_width = max(0.0, cb_width - left_v - (ml or 0.0) - right_v - (mr or 0.0))
        max_width_v = _resolve_inset(style.get("max_width"), cb_width)
        min_width_v = _resolve_inset(style.get("min_width"), cb_width)
        clamped = ratio_derived
        if max_width_v is not None and new_width > max_width_v:
            new_width, clamped = max_width_v, True
        elif min_width_v is not None and new_width < min_width_v:
            new_width, clamped = min_width_v, True
        # CSS 2.1 10.3.7 rule 5: with left/right both set and width solved,
        # an auto margin is 0 and the box sits at left -- regardless of
        # whether Taffy already solved the width --
        # absolute-non-replaced-width-015.xht.
        new_x = cb_content_x + left_v + (ml or 0.0)
        if clamped:
            remaining = cb_width - left_v - new_width - right_v
            cml, cmr = ml, mr
            if cml is None and cmr is None:
                if remaining < 0:
                    if dom._element_direction(containing) == "rtl":
                        cmr, cml = 0.0, remaining
                    else:
                        cml, cmr = 0.0, remaining
                else:
                    cml = cmr = remaining / 2.0
            elif cml is None:
                cml = remaining - cmr
            elif cmr is None:
                cmr = remaining - cml
            new_x = cb_content_x + left_v + cml
        if abs(new_width - box.width) <= 1e-6 and abs(new_x - box.x) <= 1e-6:
            continue
        border_and_padding = box.width - box.client_width
        # Resize first, at the box's current x -- `geometry._shift_subtree` below
        # (not a second x assignment here) carries this box and its
        # descendants over to new_x.
        element.__dict__["_layout_box"] = LayoutBox(
            x=box.x, y=box.y, width=new_width, height=box.height,
            client_width=max(0.0, new_width - border_and_padding), client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
        dx = new_x - box.x
        if abs(dx) > 1e-6:
            geometry._shift_subtree(element, dx, 0.0)



def _fix_absolute_static_position_fallback(node_map: dict) -> None:
    """CSS 2.1 10.3.7/10.6.4: an absolutely-positioned box with all-auto
    insets falls back to its static position -- where it would land as
    position:static. Taffy has no concept of this (an all-auto inset just
    resolves to 0, landing the box at its containing block's origin).

    A common-case approximation, not full normal-flow layout: the static
    position is the literal DOM parent's content-box origin with no
    earlier in-flow sibling, or directly below the last earlier sibling's
    margin box otherwise. Real static-position resolution needs a full
    shadow layout pass, not attempted here."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        inset = style.get("inset")
        if not inset:
            continue
        # CSS 2.1 10.3.7/10.6.4 resolve each axis independently -- top:82px
        # with left/right:auto still needs the horizontal static-position
        # fallback even though top already pins the vertical position.
        inset_top, inset_right, inset_bottom, inset_left = inset
        needs_x = inset_left == "auto" and inset_right == "auto"
        needs_y = inset_top == "auto" and inset_bottom == "auto"
        # CSS 2.1 10.1: a containing block formed by an inline ancestor
        # (a relative span flattened into its paragraph's plan, no Taffy
        # box of its own) is that ancestor's first inline box -- Taffy
        # anchored the element elsewhere instead -- abspos-inline-003.xht.
        inline_cb = None
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and dom._is_element(ancestor):
            resolved = getattr(ancestor, "_chromonic_resolved_style", None)
            if resolved is not None and box_model._establishes_containing_block(resolved[1]):
                if inline_finalize._is_flattened_inline(ancestor):
                    inline_cb = ancestor
                break
            ancestor = getattr(ancestor, "parentElement", None)
        cb_rects = (inline_cb.__dict__.get("_chromonic_inline_boxes") or []) if inline_cb is not None else []
        if cb_rects and not (needs_x and needs_y):
            first, last = cb_rects[0], cb_rects[-1]
            new_x, new_y = box.x, box.y
            if not needs_x:
                new_x = (first[0] + box_model._numeric_edge(inset_left) if inset_left != "auto"
                         else last[0] + last[2] - box_model._numeric_edge(inset_right) - box.width)
            if not needs_y:
                new_y = (first[1] + box_model._numeric_edge(inset_top) if inset_top != "auto"
                         else last[1] + last[3] - box_model._numeric_edge(inset_bottom) - box.height)
            if abs(new_x - box.x) > 1e-6 or abs(new_y - box.y) > 1e-6:
                geometry._shift_subtree(element, new_x - box.x, new_y - box.y)
                box = element.__dict__["_layout_box"]
        if not needs_x and not needs_y:
            continue
        # CSS 2.1 9.2.1.1/10.3.7: mixed into inline content
        # (`_build_text_runs_from_nodes`'s "escapee" runs), `element`'s real
        # static position is wherever the surrounding text's layout placed
        # it, not the literal DOM parent's content-box origin (the fallback
        # below) -- the literal parent is commonly an inline wrapper never
        # built as a Taffy node at all. `_InlineFormattingPlan.measure()`/
        # `.publish()` compute this directly and stash it here --
        # wpt/css/CSS2/positioning/abspos-007.xht.
        if element.__dict__.get("_chromonic_static_anchored"):
            # `src/lib.rs` already placed it at its static position (a
            # placeholder left in its static parent's flow).
            continue
        inline_static_position = getattr(element, "_chromonic_static_position", None)
        if inline_static_position is not None and inline_static_position[0] is not None:
            # The recorded position is the hypothetical box's margin edge;
            # its own margin still pushes the border box in from there
            # (line-height-201.html: `margin-left: 50px` on an abspos div).
            own_margin = style.get("margin") or (0.0,) * 4
            static_x = inline_static_position[0] + box_model._numeric_edge(own_margin[3])
            static_y = inline_static_position[1] + box_model._numeric_edge(own_margin[0])
        else:
            parent = getattr(element, "parentElement", None)
            parent_box = parent.__dict__.get("_layout_box") if parent is not None else None
            if parent_box is None:
                continue
            flex_static = _flex_container_static_position(parent, parent_box, element, box, style)
            if flex_static is not None:
                dx = (flex_static[0] - box.x) if needs_x else 0.0
                dy = (flex_static[1] - box.y) if needs_y else 0.0
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    geometry._shift_subtree(element, dx, dy)
                continue
            # The static position is where `element` would sit as an
            # ordinary position:static box -- pushed down by its own
            # margin-top (collapsing with a preceding sibling below).
            own_margin = style.get("margin") or (0.0,) * 4
            own_margin_top = box_model._numeric_edge(own_margin[0])
            own_margin_right = box_model._numeric_edge(own_margin[1])
            own_margin_left = box_model._numeric_edge(own_margin[3])
            # The static position is the parent's content-box origin, not
            # its border box.
            parent_pad_top, parent_pad_right, _parent_pad_bottom, parent_pad_left = (
                parent.__dict__.get("_chromonic_padding", (0.0,) * 4))
            # CSS 2.1 10.1: a block's hypothetical static position stacks
            # top-to-bottom regardless of direction, but horizontally
            # follows the same direction:rtl flush-right rule
            # `_fix_rtl_block_positioning` applies to a real sibling --
            # flush against the containing block's right content edge.
            # Its own margin-left/-right still pushes it in from that edge,
            # previously omitted here (unlike the vertical axis), so a
            # left/right:auto element's static position landed flush
            # against the padding edge with its own margin silently dropped.
            if dom._element_direction(parent) == "rtl":
                static_x = (parent_box.x + parent_box.border_left
                            + parent_box.client_width - parent_pad_left - parent_pad_right
                            - box.width - own_margin_right)
            else:
                static_x = parent_box.x + parent_box.border_left + parent_pad_left + own_margin_left
            static_y = parent_box.y + parent_box.border_top + parent_pad_top + own_margin_top
            for sibling in dom._child_nodes(parent):
                if sibling is element:
                    break
                if not dom._is_element(sibling):
                    continue
                sibling_style = getattr(sibling, "_chromonic_native_style", None)
                sibling_box = sibling.__dict__.get("_layout_box")
                if sibling_style is None or sibling_box is None:
                    continue
                if sibling_style.get("position") in ("absolute", "fixed"):
                    continue  # out of flow -- doesn't move the static-position cursor
                sibling_resolved = getattr(sibling, "_chromonic_resolved_style", None)
                if sibling_resolved is not None and box_model._is_floated(sibling_resolved[0]):
                    # A float doesn't move the block-flow position either --
                    # clear doesn't apply to abs boxes -- abspos-028.xht.
                    continue
                # sibling_box never includes margin, so the sibling's
                # trailing margin is added back explicitly, as the larger
                # of its own margin-bottom and this element's margin-top
                # (ordinary collapsing).
                #
                # A CSS-empty sibling is the exception: Taffy already
                # resolves its margin collapsing internally, so
                # sibling_box.y is already the fully-collapsed resting
                # position -- adding raw margin-bottom would double-count.
                sibling_empty = sibling_box.height == 0 and not any(
                    value not in (0.0, "auto") for name in ("padding", "border")
                    for value in sibling_style.get(name, ())
                )
                if sibling_empty:
                    static_y = sibling_box.y + sibling_box.height + own_margin_top
                else:
                    sibling_margin_bottom = box_model._numeric_edge((sibling_style.get("margin") or (0.0,) * 4)[2])
                    static_y = sibling_box.y + sibling_box.height + max(sibling_margin_bottom, own_margin_top)
            if inline_static_position is not None:
                # A block-level box escaped from an inline formatting
                # context: that context already knows the line it would
                # follow; only x comes from the block rule above.
                static_y = inline_static_position[1] + own_margin_top
        dx = (static_x - box.x) if needs_x else 0.0
        dy = (static_y - box.y) if needs_y else 0.0
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            geometry._shift_subtree(element, dx, dy)



def _flex_container_static_position(parent, parent_box, element, box, style):
    """CSS Flexbox 4.1: the static position of an absolutely-positioned
    child of a flex container is where it would land as the sole flex item
    -- the container's justify-content (main axis) and the child's
    align-self (cross axis, defaulting to align-items) apply to it, using
    the child's margin box against the container's content box --
    css-flexbox/abspos/flex-abspos-staticpos-*.html. Returns None for a
    parent that isn't a real CSS flex container (table rows and float
    wrappers are Taffy flex rows too, but static-position as ordinary
    block stacking)."""
    parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
    if parent_resolved is None:
        return None
    computed, layout_style = parent_resolved
    if getattr(layout_style.display, "value", "") not in flex_grid._FLEX_DISPLAYS:
        return None
    direction = (getattr(computed, "flexDirection", "row") or "row").strip().lower()
    row = direction in ("row", "row-reverse")
    reverse = direction.endswith("-reverse")
    wrap_reverse = (getattr(computed, "flexWrap", "nowrap") or "nowrap").strip().lower() == "wrap-reverse"
    rtl = dom._element_direction(parent, computed) == "rtl"
    pad_top, pad_right, pad_bottom, pad_left = parent.__dict__.get("_chromonic_padding", (0.0,) * 4)
    content_x = parent_box.x + parent_box.border_left + pad_left
    content_y = parent_box.y + parent_box.border_top + pad_top
    content_w = parent_box.client_width - pad_left - pad_right
    content_h = parent_box.client_height - pad_top - pad_bottom
    margin = style.get("margin") or (0.0,) * 4
    mt, mr, mb, ml = (box_model._numeric_edge(edge) for edge in margin)
    outer_w = box.width + ml + mr
    outer_h = box.height + mt + mb

    child_computed = (getattr(element, "_chromonic_resolved_style", None) or (None,))[0]
    child_rtl = dom._element_direction(element, child_computed) == "rtl"

    def place(keyword, safe, size, item, *, flex_flipped, start_flipped, self_flipped=None):
        # flex_flipped: flex-start is the axis's physical end (a -reverse
        # direction, or wrap-reverse on the cross axis); start_flipped:
        # writing-mode start is the physical end (rtl); self_flipped: same
        # for self-start/self-end, judged by the item's own direction --
        # flex-abspos-staticpos-align-self-rtl-004.html.
        if self_flipped is None:
            self_flipped = start_flipped
        if keyword in ("center", "space-around", "space-evenly"):
            if safe and item > size:
                keyword = "flex-start"
            else:
                return (size - item) / 2
        at_end = False
        if keyword in ("flex-end",):
            at_end = not flex_flipped
        elif keyword == "end":
            at_end = not start_flipped
        elif keyword == "self-end":
            at_end = not self_flipped
        elif keyword == "right":
            at_end = True
        elif keyword == "left":
            at_end = False
        elif keyword == "start":
            at_end = start_flipped
        elif keyword == "self-start":
            at_end = self_flipped
        elif keyword in ("baseline", "first-baseline", "last-baseline"):
            # Baseline alignment's fallback is writing-mode start/end,
            # untouched by wrap-reverse (unlike stretch/flex-start) --
            # flex-abspos-staticpos-align-self-002.html.
            at_end = (keyword == "last-baseline") != start_flipped
        else:  # flex-start, normal, stretch, space-between, auto...
            at_end = flex_flipped
        if safe and item > size:
            at_end = False
        return size - item if at_end else 0.0

    justify, justify_safe = box_model._alignment_parts(getattr(computed, "justifyContent", "normal"))
    align, align_safe = box_model._alignment_parts(getattr(computed, "alignSelf", "auto"))
    child_resolved = getattr(element, "_chromonic_resolved_style", None)
    if child_resolved is not None:
        align, align_safe = box_model._alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
    if align == "auto":
        # Only `auto` defers to the container's `align-items`; `normal`
        # on the child itself behaves as `start` for an abs child.
        align, align_safe = box_model._alignment_parts(getattr(computed, "alignItems", "normal"))
    if row:
        main = place(justify, justify_safe, content_w, outer_w,
                     flex_flipped=reverse != rtl, start_flipped=rtl)
        cross = place(align, align_safe, content_h, outer_h,
                      flex_flipped=wrap_reverse, start_flipped=False)
        return (content_x + main + ml, content_y + cross + mt)
    main = place(justify, justify_safe, content_h, outer_h,
                 flex_flipped=reverse, start_flipped=False)
    cross = place(align, align_safe, content_w, outer_w,
                  flex_flipped=wrap_reverse != rtl, start_flipped=rtl, self_flipped=child_rtl)
    return (content_x + cross + ml, content_y + main + mt)



def _fix_viewport_anchored_positioning(node_map: dict, viewport_height: float,
                                        viewport_width: "float | None" = None) -> None:
    """Correct the vertical position (and, when stretched, height) Taffy
    computed for any position:absolute/fixed element whose containing
    block is the document root itself.

    Taffy resolves such an element's top/bottom/height insets against the
    root's own Taffy box -- full document height for a scrollable page,
    not the viewport. CSS's real rule resolves against the initial
    containing block, which has the viewport's dimensions.

    Only called when a caller supplies a real viewport_height.
    `viewport_width`, when given, applies the same correction horizontally."""
    seen = set()
    for element in list(node_map.values()):
        if id(element) in seen or not dom._is_element(element):
            continue
        seen.add(id(element))
        if not _is_root_anchored(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None:
            continue
        new_y, new_height = _resolve_viewport_anchored_box(style, box, viewport_height)
        new_x, new_width = ((None, None) if viewport_width is None
                             else _resolve_viewport_anchored_box_x(style, box, viewport_width, element))
        if new_y is None and new_height is None and new_x is None and new_width is None:
            continue
        dy = (new_y - box.y) if new_y is not None else 0.0
        dx = (new_x - box.x) if new_x is not None else 0.0
        height = new_height if new_height is not None else box.height
        width = new_width if new_width is not None else box.width
        element.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=width, height=height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
        if dx or dy:
            for fragment in getattr(element, "_chromonic_inline_fragments", None) or ():
                geometry._shift_box(fragment, dx, dy)
            for child in dom._child_nodes(element):
                if dom._is_element(child):
                    geometry._shift_subtree(child, dx, dy)



def _fix_rtl_block_positioning(node_map: dict) -> None:
    """CSS 2.1 10.3.3: for an ordinary in-flow block-level box with
    `width`/`margin-left`/`margin-right` all non-`auto`, the over-
    constrained case ignores the *specified* `margin-right` and solves
    for it in `ltr` (Taffy's own default -- already flush against the
    specified `margin-left`, needing no correction) but ignores
    `margin-left` instead in `rtl`, honoring the real, specified
    `margin-right` and solving for `margin-left` -- which can come out
    smaller, larger, or even negative than whatever was actually written,
    not just 0. Taffy has no direction concept, so it always positions
    such a block from a literal, un-recalculated margin-left; this
    recomputes that one edge in both directions, per the spec's formula.

    Applies both to CSS 2.1 9.2.1.1's split interruption blocks (real
    containing block is `_chromonic_split_container`) and to an ordinary,
    un-split block child (containing block is its own parentElement)."""
    for element in node_map.values():
        if not dom._is_element(element):
            continue
        container = element.__dict__.get("_chromonic_split_container")
        if container is None:
            native_self = element.__dict__.get("_chromonic_native_style")
            if native_self is None or native_self.get("position") in ("absolute", "fixed"):
                continue
            if native_self.get("display") != "block":
                continue
            # native_self["position"] can't tell a genuine position:relative
            # apart from plain static -- style_bridge._position() maps both
            # to the same Taffy-level "relative" string. A real
            # position:relative box already gets its correct horizontal
            # offset from Taffy's native relative-position handling; this
            # function's margin-based recalculation (CSS 2.1 10.3.3's rule
            # for a non-positioned block) would silently discard that --
            # right-offset-002.xht, right-007.xht.
            own_resolved = element.__dict__.get("_chromonic_resolved_style")
            if own_resolved is not None:
                own_position = own_resolved[1].position
                if getattr(own_position, "value", own_position) == "relative":
                    continue
            container = getattr(element, "parentElement", None)
            if container is None:
                continue
        # CSS 2.1 10.3.3's ltr/rtl branch is decided by the containing
        # block's own direction, not a wrapping non-replaced inline's (it
        # never establishes one). A direction:ltr span wrapping a split
        # block inside an outer rtl container still positions the block by
        # the outer container's rtl, confirmed against Chrome.
        if dom._element_direction(container) != "rtl":
            continue
        # CSS 2.1 10.3.3 is the rule for a block in block flow. A flex/grid
        # item is positioned by its container's own algorithm --
        # right-aligning each independently here stacked every cell of an
        # rtl table row on top of each other -- border-conflict-element-002.xht.
        container_native = container.__dict__.get("_chromonic_native_style") or {}
        if container_native.get("display") in ("flex", "grid"):
            continue
        native = element.__dict__.get("_chromonic_native_style") or {}
        margin = native.get("margin") or (0.0, 0.0, 0.0, 0.0)
        margin_right = margin[1]
        if not isinstance(margin_right, (int, float)):
            continue  # `margin-right:auto` -- a different CSS 2.1 10.3.3 case, not this one
        box = element.__dict__.get("_layout_box")
        container_box = container.__dict__.get("_layout_box")
        if box is None or container_box is None:
            continue
        _cpt, cpr, _cpb, cpl = container.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_left = container_box.x + container_box.border_left + cpl
        content_right = content_left + container_box.client_width - cpl - cpr
        target_x = content_right - float(margin_right) - box.width
        delta = target_x - box.x
        if abs(delta) > 0.01:
            geometry._shift_subtree(element, delta, 0.0)
