from __future__ import annotations

import dataclasses

from domonic.layout import LayoutBox

from . import box_model, dom, flex_grid, geometry, inline_finalize
from .box import box_of



def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning, or `None` if none exists.

    CSS 2.1 10.1 rule 4: a position:fixed box's containing block is always
    the viewport (`None` here) -- unlike position:absolute's rule 3, no
    ancestor's own position can ever substitute for it. Confirmed on
    position-absolute-005.xht: a fixed box nested inside an absolute
    ancestor anchored to that ancestor instead of the viewport."""
    # `box_of(element).native_style["position"]` can't distinguish this --
    # style_bridge._position() maps CSS fixed to the same Taffy-level
    # "absolute" string (Taffy has no native fixed concept).
    # `box.resolved_style`'s LayoutStyle.position is the pre-mapping
    # value that still keeps fixed distinct.
    own_resolved = box_of(element).resolved_style
    if own_resolved is not None:
        own_position = own_resolved[1].position
        if getattr(own_position, "value", own_position) == "fixed":
            return None
    parent = getattr(element, "parentElement", None)
    while parent is not None:
        resolved = box_of(parent).resolved_style
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



def _fix_absolute_width_against_containing_block(node_map: dict) -> None:
    """Only for a containing block formed by an inline (a relatively
    positioned `<span>`), which has no box of its own in Taffy -- every
    other case is Taffy's own absolute layout.

    CSS 2.1 10.3.7: a position:absolute box with width:auto and both
    left/right definite has its width solved from the constraint equation
    (`left + margin-left + width + margin-right + right == containing
    block width`) --  Taffy leaves width:auto at whatever
    the content happened to measure instead --
    position-absolute-percentage-inherit-001.xht: measured 0 instead of
    the ~169px CSS 2.1 10.3.7 solves for.

    Also applies CSS 2.1 10.4's min/max-width clamp to that solved width:
    once clamping replaces it with a fixed min/max-width, the box is back
    to the ordinary all-three-definite case, so an auto margin re-absorbs
    the slack the clamp freed (same equal-split/zero-one-side rule as
    CSS 2.1 10.3.7) -- otherwise a max-width-clamped,
    centered box would sit flush against left instead --
    position-absolute-width-025.xht.

    Deliberately narrow
    beside it: only overwrites width/x, children shifted but not relaid
    out to the new width, a deliberate simplification."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        style = box_of(element).native_style
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
        if not inline_finalize._is_flattened_inline(containing):
            continue  # a real Taffy containing block: Taffy solved this already
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
        style = box_of(element).native_style
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
            resolved = box_of(ancestor).resolved_style
            if resolved is not None and box_model._establishes_containing_block(resolved[1]):
                if inline_finalize._is_flattened_inline(ancestor):
                    inline_cb = ancestor
                break
            ancestor = getattr(ancestor, "parentElement", None)
        cb_rects = (box_of(inline_cb).inline_boxes or []) if inline_cb is not None else []
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
        if box_of(element).static_anchored:
            # `src/lib.rs` already placed it at its static position (a
            # placeholder left in its static parent's flow).
            continue
        inline_static_position = box_of(element).static_position
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
            parent_pad_top, parent_pad_right, _parent_pad_bottom, parent_pad_left = box_of(parent).get("padding", (0.0,) * 4)
            # CSS 2.1 10.1: a block's hypothetical static position stacks
            # top-to-bottom regardless of direction, but horizontally
            # follows the same direction:rtl flush-right rule
            # Taffy's block layout applies to a real sibling --
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
                sibling_style = box_of(sibling).native_style
                sibling_box = sibling.__dict__.get("_layout_box")
                if sibling_style is None or sibling_box is None:
                    continue
                if sibling_style.get("position") in ("absolute", "fixed"):
                    continue  # out of flow -- doesn't move the static-position cursor
                sibling_resolved = box_of(sibling).resolved_style
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
    parent_resolved = box_of(parent).resolved_style
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
    pad_top, pad_right, pad_bottom, pad_left = box_of(parent).get("padding", (0.0,) * 4)
    content_x = parent_box.x + parent_box.border_left + pad_left
    content_y = parent_box.y + parent_box.border_top + pad_top
    content_w = parent_box.client_width - pad_left - pad_right
    content_h = parent_box.client_height - pad_top - pad_bottom
    margin = style.get("margin") or (0.0,) * 4
    mt, mr, mb, ml = (box_model._numeric_edge(edge) for edge in margin)
    outer_w = box.width + ml + mr
    outer_h = box.height + mt + mb

    child_computed = (box_of(element).resolved_style or (None,))[0]
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
    child_resolved = box_of(element).resolved_style
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



