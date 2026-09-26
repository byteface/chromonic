from __future__ import annotations

import dataclasses

from domonic import _fontmetrics
from domonic.layout import LayoutBox

from .. import fonts, style_bridge
from . import anonymous_boxes, box_model, dom, flex_grid, geometry, inline_formatting, positioning
from .box import box_of



def _merge_adjacent_same_line_rects(rects) -> list:
    """Combine consecutive rects (already in the order they were placed --
    left-to-right within one line) that share a `y` into one wider rect,
    the way multiple runs on the same line (e.g. text either side of a
    nested `<b>`) collapse into a single `getClientRects()` entry."""
    merged = []
    for rect in rects:
        if merged and abs(merged[-1][1] - rect[1]) < 0.01:
            previous = merged[-1]
            merged[-1] = (previous[0], previous[1],
                          rect[0] + rect[2] - previous[0],
                          max(previous[3], rect[3]))
        else:
            merged.append(rect)
    return merged



def _inline_relative_offset(owner, stop, container_box) -> "tuple[float, float]":
    """The CSS 2.1 9.4.3 offset a fragment owned by `owner` carries from
    every `position: relative` inline between it and the plan element
    `stop` (exclusive): `left` (else `-right`) and `top` (else
    `-bottom`), each summed up the chain; a percentage resolves against
    the plan element's box (its containing block, near enough)."""
    dx = dy = 0.0
    node = owner
    while node is not None and node is not stop and dom._is_element(node):
        resolved = box_of(node).resolved_style
        if resolved is not None and _is_flattened_inline(node):
            style_obj = resolved[1]
            position = getattr(style_obj.position, "value", style_obj.position)
            if position == "relative":
                edges = style_obj.inset

                def resolve(value, base):
                    length = style_bridge._len(value)
                    if isinstance(length, (int, float)):
                        return float(length)
                    if isinstance(length, tuple) and length[0] == "pct":
                        return length[1] * base
                    return None

                left = resolve(edges.left, container_box.client_width)
                right = resolve(edges.right, container_box.client_width)
                top = resolve(edges.top, container_box.client_height)
                bottom = resolve(edges.bottom, container_box.client_height)
                rtl = dom._element_direction(node, resolved[0]) == "rtl"
                if left is not None and right is not None:
                    dx += -right if rtl else left
                elif left is not None:
                    dx += left
                elif right is not None:
                    dx -= right
                if top is not None:
                    dy += top
                elif bottom is not None:
                    dy -= bottom
        node = getattr(node, "parentElement", None)
    return dx, dy



def _is_flattened_inline(element) -> bool:
    """An inline element flattened into an enclosing plan's text runs
    this pass (`_build_text_runs_from_nodes` marks it; `build()` clears
    the mark for anything given a Taffy box of its own) -- its reported
    box is its fragments' (or descendants') union. A computed `display:
    inline` alone won't do: domonic reports that for a `<td>` too
    (abspos-027.xht's cell lost its real box to its text's union)."""
    if not dom._is_element(element) or isinstance(element, anonymous_boxes._AnonymousTableBox):
        return False
    return bool(box_of(element).flattened_inline)



def _finalize_inline_owner_boxes(owner_accum) -> None:
    """Merge each inline owner's accumulated fragment rects -- gathered
    across every `_InlineFormattingPlan` that published fragments for it
    (CSS 2.1 9.2.1.1's split contributes from multiple independent plans
    belonging to the same owner) -- into its final box.inline_boxes/
    _layout_box/box.owned_fragments, once per owner per pass.

    Rects are grouped by split segment (run["split_group"]) rather than
    merged as one re-sorted list -- getClientRects() preserves document
    order, not a geometric sort.

    CSS 2.1 9.2.1: an inline's line-box fragments cover nested inline
    descendants' content too -- each owner's merged rects are folded into
    every tracked inline ancestor's, deepest owner first."""
    # An inline wrapper flattened into the plan with no text of its own is
    # never a fragment owner, so it would get no box at all -- Chrome
    # reports its descendants' union -- abspos-inline-003.xht. It borrows
    # its descendants' rects here; box.native_style set means the
    # element has a real Taffy box already and stops the walk.
    for key, (owner, groups, _fragments, outer_groups) in list(owner_accum.items()):
        if not groups:
            continue
        parent = getattr(owner, "parentElement", None)
        while parent is not None and _is_flattened_inline(parent):
            entry = owner_accum.get(id(parent))
            if entry is None:
                # Only an ancestor with no fragments of its own -- one that
                # has some folds its descendants into them below instead.
                # Its box spans its descendants' margin boxes.
                entry = owner_accum[id(parent)] = (parent, {}, [], {})
                for group_key, rects in outer_groups.items():
                    entry[1].setdefault(group_key, []).extend(rects)
                    entry[3].setdefault(group_key, []).extend(rects)
            parent = getattr(parent, "parentElement", None)
    own_merged = {}
    own_merged_outer = {}
    for key, (owner, groups, _fragments, outer_groups) in owner_accum.items():
        if not groups:
            continue
        group_keys = sorted(groups, key=lambda key: (key is not None, key))
        merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
        own_merged[key] = [rect for group in merged_groups for rect in group]
        own_merged_outer[key] = [rect for key_ in group_keys
                                 for rect in _merge_adjacent_same_line_rects(outer_groups.get(key_, groups[key_]))]

    def _depth(owner) -> int:
        depth = 0
        node = getattr(owner, "parentElement", None)
        while node is not None:
            depth += 1
            node = getattr(node, "parentElement", None)
        return depth

    # descendant_only[key]: every rect contributed by key's inline
    # descendants, excluding key's own -- kept separate since only the
    # horizontal extent folds upward, never the vertical.
    descendant_only = {key: [] for key in own_merged}
    for key in sorted(own_merged, key=lambda key: _depth(owner_accum[key][0]), reverse=True):
        parent = getattr(owner_accum[key][0], "parentElement", None)
        while parent is not None:
            parent_key = id(parent)
            if parent_key in descendant_only:
                descendant_only[parent_key].extend(own_merged_outer[key])
                descendant_only[parent_key].extend(descendant_only[key])
                break
            parent = getattr(parent, "parentElement", None)

    for key, (owner, groups, fragments, _outer_groups) in owner_accum.items():
        if key not in own_merged:
            continue
        # CSS 2.1 9.2.1: a wrapping inline's fragments span the full
        # horizontal extent of everything nested inside on that line, but
        # its own height/position come only from its own font metrics --
        # a taller nested child extends visually without growing it or
        # splitting it into extra fragments.
        desc = descendant_only[key]
        if desc:
            desc_left = min(r[0] for r in desc)
            desc_right = max(r[0] + r[2] for r in desc)
            # Vertically too: a position:relative descendant's shifted box
            # stretches its inline ancestor's rect -- position-relative-032.xht.
            desc_top = min(r[1] for r in desc)
            desc_bottom = max(r[1] + r[3] for r in desc)
            all_merged = [
                (min(rx, desc_left), min(ry, desc_top),
                 max(rx + rw, desc_right) - min(rx, desc_left),
                 max(ry + rh, desc_bottom) - min(ry, desc_top))
                for rx, ry, rw, rh in own_merged[key]
            ]
        else:
            all_merged = own_merged[key]
        # getClientRects(): real Chrome exposes one extra rect per in-flow
        # block interruption (CSS 2.1 9.2.1.1) -- the anonymous block box
        # wrapping the interrupting block, not the block's own (possibly
        # narrower) box. It's width:auto, 100% of owner's containing
        # block, on top of the real leading/trailing fragments.
        interruption_blocks = box_of(owner).interruption_blocks or ()
        if interruption_blocks:
            container = box_of(owner).split_container
            container_box = container.__dict__.get("_layout_box") if container is not None else None
            cpt, cpr, cpb, cpl = (box_of(container).get("padding", (0.0,) * 4)
                                  if container is not None else (0.0,) * 4)
            group_keys = sorted(groups, key=lambda key: (key is not None, key))
            merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
            self_edges = box_of(owner).split_self_edges
            self_left, self_right, self_top = self_edges or (0.0, 0.0, 0.0)
            if self_edges and merged_groups:
                # `owner` is a real Taffy node here (the direct-child split
                # shape), so every one of its text-leaf children already
                # sits physically shifted right by owner's own real
                # border-left/padding-left/top (Taffy applies that to every
                # child alike). Every rect in every group needs that shift
                # undone first, then the real edge re-applied only to the
                # true leading/trailing rects: left-widening the first rect
                # of the leading group, right-widening the last rect of the
                # trailing one -- exactly which fragments a real inline
                # box's edges show up on. The vertical shift has no such
                # edge-widening counterpart -- every segment's box_height
                # already carries the full top+bottom edge unconditionally.
                if self_top:
                    merged_groups = [
                        [(rx, ry - self_top, rw, rh) for rx, ry, rw, rh in group]
                        for group in merged_groups
                    ]
                if self_left:
                    merged_groups = [
                        [(rx - self_left, ry, rw, rh) for rx, ry, rw, rh in group]
                        for group in merged_groups
                    ]
                    first = merged_groups[0][0]
                    merged_groups[0][0] = (first[0], first[1], first[2] + self_left, first[3])
                if self_right:
                    last = merged_groups[-1][-1]
                    merged_groups[-1][-1] = (last[0], last[1], last[2] + self_right, last[3])
            final_rects: list = []
            # `(index into final_rects, [contributing blocks])` for every
            # marker rect appended below -- `_resync_interruption_marker_
            # heights` needs this to patch each marker's height back in
            # sync once the float-auto-height corrections (which run
            # after this) have given its block(s) their real, final
            # height; a merged marker (two blocks with a skipped, empty
            # segment between them) tracks both.
            marker_positions: list = []
            for index, group in enumerate(merged_groups):
                # An interior segment (between two interruption blocks) with
                # no real content gets no fragment of its own in real
                # Chrome. The leading/trailing 0x0 case is different -- that
                # one is a real, if empty, fragment of the wrapper's own
                # remaining content on that side.
                interior_empty = (
                    0 < index < len(merged_groups) - 1
                    and len(group) == 1 and group[0][2] == 0.0 and group[0][3] == 0.0
                )
                if not interior_empty:
                    final_rects.extend(group)
                if index < len(interruption_blocks):
                    block = interruption_blocks[index]
                    block_box = block.__dict__.get("_layout_box")
                    if block_box is not None and container_box is not None:
                        block_y = block_box.y
                        if container is owner:
                            # The direct-child shape: container is owner
                            # itself, forced to width:100% of its own
                            # containing block, so the marker uses the whole
                            # border box, uninset by owner's own edges.
                            # block_box.y still needs the same border-top
                            # correction the text rects got.
                            marker_x = container_box.x
                            marker_width = container_box.width
                            block_y = block_y - self_top
                        else:
                            marker_x = container_box.x + container_box.border_left + cpl
                            marker_width = container_box.client_width - cpl - cpr
                        # CSS 2.1 9.2.1.1's anonymous block box wraps the
                        # interrupting block, but the marker rect reports
                        # the block's own border box, not a margin-inflated
                        # union.
                        marker_rect = (marker_x, block_y, marker_width, block_box.height)
                        # Two markers with a skipped, genuinely-empty
                        # interior segment between them are visually
                        # contiguous -- Chrome reports one merged rect.
                        prev = final_rects[-1] if final_rects else None
                        if (interior_empty and prev is not None
                                and abs(prev[0] - marker_rect[0]) < 0.01
                                and abs(prev[2] - marker_rect[2]) < 0.01
                                and abs(prev[1] + prev[3] - marker_rect[1]) < 0.01):
                            final_rects[-1] = (prev[0], prev[1], prev[2], prev[3] + marker_rect[3])
                            marker_positions[-1][1].append(block)
                        else:
                            final_rects.append(marker_rect)
                            marker_positions.append([len(final_rects) - 1, [block]])
                    elif block_box is not None:
                        final_rects.append((block_box.x, block_box.y, block_box.width, 0.0))
                        marker_positions.append([len(final_rects) - 1, [block]])
            box_of(owner).inline_boxes = final_rects
            box_of(owner).marker_positions = marker_positions
            all_merged = final_rects  # the block interruption also grows getBoundingClientRect()
        else:
            box_of(owner).inline_boxes = all_merged
        # getBoundingClientRect() unions every getClientRects() rect except
        # zero-width/height ones -- all_merged/final_rects themselves stay
        # unfiltered; only the union bounds here drop them. When every rect
        # is degenerate (e.g. a float-only interruption marker collapsing
        # to 0 height, CSS 2.1 9.5), Chrome's bounding rect is 0x0 at the
        # position of the last one, not a union spanning first to last.
        bounding_rects = [r for r in all_merged if r[2] != 0.0 and r[3] != 0.0] or all_merged[-1:]
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right-left, height=bottom-top,
            client_width=right-left, client_height=bottom-top,
        )
        box_of(owner).has_layout_children = True
        # Text-range fragments stay scoped to this owner's own direct text,
        # never unioned across a nested element boundary.
        box_of(owner).owned_fragments = fragments



def _publish_inline_formatting(node_map) -> None:
    """Project shared line fragments after parent boxes reach final positions."""
    seen = set()
    owner_accum: dict = {}
    element_fragments_accum: dict = {}
    for element in node_map.values():
        if id(element) in seen:
            continue
        seen.add(id(element))
        plan = box_of(element).inline_plan
        box = element.__dict__.get("_layout_box")
        if plan is not None and box is not None:
            plan.publish(box, box_of(element).get("padding", (0.0,) * 4),
                         owner_accum, element_fragments_accum)
    _finalize_inline_owner_boxes(owner_accum)
    _fix_split_inline_relative_offset(entry[0] for entry in owner_accum.values())
    for element, fragments in element_fragments_accum.values():
        box_of(element).inline_fragments = fragments



def _fix_split_inline_relative_offset(owners) -> None:
    """CSS 2.1 9.4.3: position:relative's top/left offset shifts every box
    an element generates -- for a split inline (9.2.1.1), that includes
    the real block child's own box too. The split wrapper is never built
    as a real Taffy node, so Taffy's own position:relative handling never
    sees it -- reapplied by hand here, after `_finalize_inline_owner_boxes`
    has published its fragment geometry.

    Takes the owners `_finalize_inline_owner_boxes` just published rather
    than walking `node_map` -- a nested split wrapper never appears there."""
    for owner in owners:
        interruption_blocks = box_of(owner).interruption_blocks
        if not interruption_blocks:
            continue
        container = box_of(owner).split_container
        native = box_of(owner).native_style
        container_box = container.__dict__.get("_layout_box") if container is not None else None
        if container_box is None or native is None:
            continue
        cpt, cpr, cpb, cpl = box_of(container).get("padding", (0.0,) * 4)
        basis_width = container_box.client_width - cpl - cpr
        basis_height = container_box.client_height - cpt - cpb
        top, right, bottom, left = native.get("inset") or ("auto",) * 4
        top_v = positioning._resolve_inset(top, basis_height)
        bottom_v = positioning._resolve_inset(bottom, basis_height)
        left_v = positioning._resolve_inset(left, basis_width)
        right_v = positioning._resolve_inset(right, basis_width)
        dy = top_v if top_v is not None else (-bottom_v if bottom_v is not None else 0.0)
        dx = left_v if left_v is not None else (-right_v if right_v is not None else 0.0)
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        box = owner.__dict__.get("_layout_box")
        if box is not None:
            owner.__dict__["_layout_box"] = dataclasses.replace(box, x=box.x + dx, y=box.y + dy)
        inline_boxes = box_of(owner).inline_boxes
        if inline_boxes:
            box_of(owner).inline_boxes = [
                (rx + dx, ry + dy, rw, rh) for rx, ry, rw, rh in inline_boxes
            ]
        for fragment in box_of(owner).owned_fragments or ():
            fbox = fragment.__dict__.get("_layout_box")
            if fbox is not None:
                fragment._layout_box = dataclasses.replace(fbox, x=fbox.x + dx, y=fbox.y + dy)
        for block in interruption_blocks:
            geometry._shift_subtree(block, dx, dy)



