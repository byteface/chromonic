from __future__ import annotations

import math

from domonic import _fontmetrics
from domonic.layout import Keyword, Length

from .. import fonts, style_bridge
from .._native import Tree, layout_text
from . import table_formatting, anonymous_boxes, box_model, dom, flex_grid, inline_formatting, replaced_elements, table_layout
from .box import box_of




def _setup_table_root(element, style, computed, computed_cache, tag_name):
    """Body of `build()`'s `is_table_root` branch, extracted verbatim: resolves
    the table's grid/columns/borders/spacing and stashes them as `element._chromonic_table_*`
    attributes for the row/cell branches (in later, separate `build()` calls) to read."""
    box_of(element).is_table_root = True
    # A table box establishes a block formatting context (CSS 2.1
    # 9.4.1): a caption's top margin stays inside it, never collapsing
    # through into the table's own (table-anonymous-block-011.xht: a
    # `margin-top: 2em` caption in a `margin-top: 2em` table sits 4em
    # below the preceding border in Chrome, not 2em).
    style["establishes_bfc"] = True
    box_of(element).border_collapse = computed.borderCollapse == "collapse"
    # The table's grid, resolved once here for everything below and
    # for the post-layout passes (`_distribute_table_extra_height`):
    # rows in CSS 2.1 17.5.3 display order, each row's own cells, and
    # every cell's grid position (`table_layout._table_grid` -- rowspan/colspan
    # occupancy included).
    rows = table_layout._table_rows(element, computed_cache)
    cells, column_count = table_layout._table_grid(rows, computed_cache)
    # Declared columns past the cells' last one still exist -- a table
    # of nothing but a `width: 5em` column is 80px wide in Chrome
    # (table-column-rendering-001.xht).
    column_count = max(column_count, len(table_layout._table_columns(element, computed_cache, None)))
    box_of(element).table_rows = rows
    box_of(element).table_grid_cells = cells
    box_of(element).table_columns = table_layout._table_columns(element, computed_cache, column_count)
    # CSS 2.1 17.5.5: a `visibility: collapse` column (or column
    # group) is 0px wide -- its cells with it, and one border-spacing
    # gap goes with it (column-visibility-003.xht: four 128px `<col>`s
    # with one collapsed make a 392px table: three columns, four
    # gaps) -- while its cells still take part in the row layout.
    collapsed_columns: set = set()
    for index, (column, group) in enumerate(box_of(element).table_columns):
        for owner in (column, group):
            if owner is None:
                continue
            try:
                visibility = (getattr(dom._describe(owner, computed_cache)[0], "visibility", "") or "")
            except Exception:
                visibility = ""
            if visibility.strip().lower() == "collapse":
                collapsed_columns.add(index)
    box_of(element).table_collapsed_columns = collapsed_columns
    # CSS 2.1 17.5: an `rtl` table's first column is the rightmost -- rows
    # lay cells out right-to-left (row-reverse, see the row branch) and the
    # collapsed-border grid lines mirror.
    box_of(element).table_rtl = dom._element_direction(element, computed) == "rtl"
    row_cells: dict = {}
    content_rows: set = set()
    for cell, row_index, _col, rowspan, _colspan in cells:
        row_cells.setdefault(id(rows[row_index]), []).append(cell)
        # A row a content-bearing cell spans down into counts as having
        # content too -- table-height-algorithm-018.xht.
        if table_layout._table_cell_has_content(cell, computed_cache):
            content_rows.update(range(row_index, min(row_index + rowspan, len(rows))))
    for index, row in enumerate(rows):
        box_of(row).table_cells = row_cells.get(id(row), [])
        # CSS 2.1 17.5.5: visibility:collapse on a row or its row group
        # removes it (0px, `_collapse_rows_in`) while its cells still size
        # the columns -- row-visibility-001..004.xht.
        collapsed = False
        node = row
        while node is not None and node is not element:
            visibility = ""
            try:
                visibility = (getattr(dom._describe(node, computed_cache)[0], "visibility", "") or "")
            except Exception:
                visibility = ""
            if visibility.strip().lower() == "collapse":
                collapsed = True
                break
            node = dom._layout_parent(node)
        box_of(row).row_collapsed = collapsed
    if box_of(element).border_collapse:
        # CSS 2.1 17.6.2: each side of a collapsed grid line keeps only its
        # own half of the winning width -- `table_layout._resolve_collapsed_table_borders`
        # runs the real 17.6.2.1 conflict resolution per segment; cells read
        # their halves from `box.collapsed_cell_borders` below.
        # Table box: own border halved, padding makes up the rest of the
        # winning perimeter width -- border-collapse-001.xht,
        # border-collapse-005.html.
        cell_borders, perimeter = table_layout._resolve_collapsed_table_borders(
            element, computed, rows, cells, column_count, computed_cache,
            rtl=box_of(element).table_rtl)
        box_of(element).collapsed_cell_borders = cell_borders
        own = [box_model._numeric_edge(value) for value in style["border"]]
        style["border"] = [value / 2.0 for value in own]
        style.update({
            "box_sizing": "border-box",
            # CSS 2.1 17.6.2: a table has no padding of its own in this model.
            "padding": [max(0.0, perimeter[i] / 2.0 - own[i] / 2.0) for i in range(4)],
        })
    else:
        box_of(element).pop("collapsed_cell_borders", None)
    # CSS 2.1 17.5.3: a table's specified height is a minimum, not a cap
    # (`_distribute_table_extra_height` hands out surplus/needs the original
    # value) -- `min_height` is exactly that semantic in Taffy.
    box_of(element).table_specified_height = (
        style["height"] if isinstance(style["height"], (int, float)) else None)
    if style["height"] != "auto":
        if style["min_height"] in ("auto", 0.0):
            style["min_height"] = style["height"]
        style["height"] = "auto"
    if tag_name == "table":
        # HTML UA stylesheet: `table { box-sizing: border-box }`.
        style["box_sizing"] = "border-box"
    elif style["box_sizing"] != "border-box" and not box_of(element).border_collapse:
        # CSS 2.1 17.6.1: a `display:table` element's width/height excludes
        # border-spacing too (it's modelled as extra padding here) -- convert
        # a definite size to the equivalent border box up front so the
        # spacing padding added later stays inside it. separated-border-
        # model-004.xht. A percentage size can't be converted and is left as-is.
        padding = [box_model._numeric_edge(v) for v in style["padding"]]
        border = [box_model._numeric_edge(v) for v in style["border"]]
        if isinstance(style["width"], (int, float)):
            style["width"] = style["width"] + padding[1] + padding[3] + border[1] + border[3]
            style["box_sizing"] = "border-box"
        if isinstance(style["min_height"], (int, float)) and box_of(element).table_specified_height is not None:
            vertical = padding[0] + padding[2] + border[0] + border[2]
            style["min_height"] = style["min_height"] + vertical
            box_of(element).table_specified_height = style["min_height"]
            style["box_sizing"] = "border-box"
    # CSS 2.1 17.6.1: border-spacing only applies in the separate border
    # model; `ua_style.py` supplies the UA default (2px) domonic has none of.
    if box_of(element).border_collapse:
        box_of(element).border_spacing = (0.0, 0.0)
    else:
        parts = (computed.borderSpacing or "0px").split() or ["0px"]

        def spacing_px(text, default):
            # `ex` resolves against the real x-height (`domonic_ex_unit_patch`),
            # not a flat half-em guess -- border-spacing-083.xht. Truncated to
            # whole pixels, matching Blink -- border-spacing-036.xht.
            text = (text or "").strip()
            if text.endswith("%"):
                # CSS 2.1 17.6.1: lengths only -- a percentage is invalid and
                # dropped (domonic keeps it, logged in PLAN.md) --
                # border-spacing-percentage-001.xht.
                return 0.0
            if text.lower().endswith("ex"):
                from .. import domonic_ex_unit_patch
                resolved = domonic_ex_unit_patch._resolve_ex_px(text, computed)
                if resolved is not None:
                    return math.floor(resolved)
            value = _fontmetrics.parse_length(text, default=default)
            return math.floor(value) if value >= 0.0 else value

        spacing_h = spacing_px(parts[0], 0.0)
        spacing_v = spacing_px(parts[1], spacing_h) if len(parts) > 1 else spacing_h
        if spacing_h < 0.0 or spacing_v < 0.0:
            # CSS 2.1 17.6.1: negative border-spacing is invalid and dropped
            # to the UA default -- domonic's cascade doesn't (logged in
            # PLAN.md), reproduced here.
            spacing_h = spacing_v = 2.0
        box_of(element).border_spacing = (spacing_h, spacing_v)
        # No rows (columns): no spacing on that axis at the table's edges
        # either -- a caption-only table is its caption's height, as in
        # Chrome (floats-in-table-caption-001.html).
        if not box_of(element).table_rows:
            spacing_v = 0.0
        if not box_of(element).table_columns:
            spacing_h = 0.0
        if spacing_h or spacing_v:
            # The same gap also separates the table's own edge from
            # its outermost row/column (CSS 2.1 17.6.1's spacing
            # model treats the border as just one more grid line) --
            # `elif is_table_cell` below's per-row horizontal `gap`
            # handles *between* cells; this is the perimeter -- on top
            # CSS 2.1 17.6.1: table border to bordering cell = table padding
            # + border spacing -- separated-border-model-001.xht.
            own_padding = style["padding"]
            style["padding"] = [
                own_padding[0] + spacing_v if isinstance(own_padding[0], (int, float)) else spacing_v,
                own_padding[1] + spacing_h if isinstance(own_padding[1], (int, float)) else spacing_h,
                own_padding[2] + spacing_v if isinstance(own_padding[2], (int, float)) else spacing_v,
                own_padding[3] + spacing_h if isinstance(own_padding[3], (int, float)) else spacing_h,
            ]
    box_of(element).table_fixed = computed.tableLayout == "fixed" and style["width"] != "auto"
    # CSS 2.1 17.5.2: a specified table width is a floor the columns may
    # widen past (`table_formatting` resolves the used width), so it reaches
    # Taffy as a minimum; the table node sizes itself from its content.
    box_of(element).table_specified_width = style["width"]
    if style["width"] != "auto":
        if style["min_width"] in ("auto", 0.0):
            style["min_width"] = style["width"]
        style["width"] = "auto"
    # Never stretched by a block parent: a table's width is its own.
    style["is_table"] = True



def _setup_table_row_or_cell(element, style, computed, style_obj, tag_name,
                              is_table_row: bool, is_table_cell: bool) -> None:
    """Body of `build()`'s row/row-group margin-and-width reset plus the
    is_table_row/is_table_cell if/elif, extracted verbatim."""
    if is_table_row or (not box_model._is_absolutely_positioned(style_obj)
                        and table_layout._row_group_kind(tag_name, computed) is not None):
        # CSS 2.1 17.6.1: rows/row groups/columns/column groups carry no
        # border of their own in the separated model; in the collapsing
        # model their borders only contend for the shared grid lines
        # (`table_layout._resolve_collapsed_table_borders`, folded into cells' halves).
        style["border"] = [0.0, 0.0, 0.0, 0.0]
        # CSS 2.1 17.4: nor a margin -- table-visual-layout-002.xht.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        # CSS 2.1: width doesn't apply to table rows/row groups --
        # empty-cells-applies-to-008.xht.
        style["width"] = "auto"
    if is_table_row:
        # CSS 2.1 17.5.3: a row's specified `height` is a minimum -- its
        # tallest cell can always make it taller.
        if style["height"] != "auto":
            if style["min_height"] in ("auto", 0.0):
                style["min_height"] = style["height"]
            style["height"] = "auto"
    elif is_table_cell:
        # CSS 2.1 17.4: margin doesn't apply to a cell either --
        # table-visual-layout-002.xht.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        ancestor = dom._layout_parent(element)
        while ancestor is not None and not box_of(ancestor).get("is_table_root", False):
            ancestor = dom._layout_parent(ancestor)
        if ancestor is not None and box_of(ancestor).get("border_collapse", False):
            # CSS 2.1 17.6.2: this cell's box includes half of each of its
            # four collapsed grid lines' winning widths, resolved once for
            # the whole table (`table_layout._resolve_collapsed_table_borders`) regardless
            # of the cell's own declaration -- border-conflict-style-001.xht.
            # A cell outside the resolved grid falls back to halving its own.
            resolved = box_of(ancestor).get("collapsed_cell_borders", {}).get(id(element))
            style["border"] = (list(resolved) if resolved is not None else
                               [value / 2.0 if isinstance(value, (int, float)) else value
                                for value in style["border"]])
        # The table algorithm (`table_formatting`) sizes every cell from its
        # columns; a specified width only feeds the column widths.
        box_of(element).cell_specified_width = style["width"] if style["width"] != "auto" else None
        style["width"] = "auto"
        # CSS 2.1 17.5.3: a cell's specified height is a minimum for its
        # row (`table_formatting`), not for the cell's content -- which
        # vertical-align then positions within the taller cell.
        box_of(element).cell_specified_height = style["height"] if style["height"] != "auto" else None
        style["height"] = "auto"



def _setup_table_caption(element, style, computed, parent, is_table_caption: bool) -> None:
    """Body of `build()`'s `is_table_caption` branch, extracted verbatim: pulls a
    caption's margins outward past the table's own border+padding so it spans the
    table wrapper box, CSS 2.1 17.4."""
    if is_table_caption and parent is not None and box_of(parent).get("is_table_root", False):
        # CSS 2.1 17.4: a caption belongs to the table wrapper box, spanning
        # its full outer width, not the table box itself -- chromonic has no
        # separate wrapper, so the caption's margins compensate: pulled
        # outward past the table's own border+padding, with the opposite
        # margin pushing the rows back by the same amount so the border+
        # padding still sit between caption and first/last row.
        # basic-css-table-001.xht. An author margin still applies on top; a
        # percentage one is left alone.
        parent_style = box_of(parent).native_style or {}
        bt, br, bb, bl = (box_model._numeric_edge(v) for v in parent_style.get("border", (0.0,) * 4))
        pt, pr, pb, pl = (box_model._numeric_edge(v) for v in parent_style.get("padding", (0.0,) * 4))
        at_bottom = (getattr(computed, "captionSide", "") or "top").strip().lower() == "bottom"
        margin = list(style["margin"])

        def adjusted(value, delta):
            return value + delta if isinstance(value, (int, float)) else value

        margin[3] = adjusted(margin[3], -(bl + pl))
        margin[1] = adjusted(margin[1], -(br + pr))
        if at_bottom:
            margin[0] = adjusted(margin[0], bb + pb)
            margin[2] = adjusted(margin[2], -(bb + pb))
        else:
            margin[0] = adjusted(margin[0], -(bt + pt))
            margin[2] = adjusted(margin[2], bt + pt)
        style["margin"] = margin



def _build_split_pieces(tree, element, style, split_pieces, *, computed_cache, is_containing_block,
        escapees, own_escapees, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `split_pieces is not None` branch: one ordinary block-flow
    child per CSS 2.1 9.2.1.1 split piece (a measured text leaf for a "plan"
    piece, a recursive build() for a "block" piece)."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    # Its pieces are ordinary block-level boxes: Taffy's block layout
    # stretches them across the containing block itself. (Forcing
    # `width: 100%` here made every such box's min-content width equal the
    # space it was offered -- a table cell holding `<a><img
    # style="display:block"></a>` swallowed its whole table row.)
    owner_cache = box_of(element).setdefault("split_plan_owners", {})
    piece_ids = []
    plan_index = 0
    for kind, *payload in split_pieces:
        if kind == "plan":
            (plan,) = payload
            owner = owner_cache.get(plan_index)
            if owner is None:
                owner = owner_cache[plan_index] = anonymous_boxes._AnonymousInlineRun(None, element)
            plan_index += 1
            plan_style = inline_formatting._inline_text_style(style)
            box_of(owner).native_style = plan_style
            # An "escapee" run (an out-of-flow element mixed into this
            # segment) marks its static-position slot; its real subtree
            # lands one edge from its real containing block.
            piece_id = _build_inline_node(
                tree, owner, plan, plan_style, computed_cache=computed_cache,
                escapee_sink=own_escapees if is_containing_block else escapees,
                reuse_styles=reuse_styles, projection=projection, node_map=node_map,
            )
        else:
            child, child_computed, child_style = payload
            child_is_cb = box_model._establishes_containing_block(child_style)
            piece_id = build(
                tree, child, node_map, computed=child_computed, style_obj=child_style,
                computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                reuse_styles=reuse_styles, projection=projection,
            )
        piece_ids.append(piece_id)
    for stale in [key for key in owner_cache if key >= plan_index]:
        owner_cache.pop(stale)
    all_child_ids = piece_ids + (own_escapees if is_containing_block else [])
    node_id = (projection.upsert(element, style, all_child_ids, None, None)
               if projection else tree.new_with_children(style, all_child_ids))
    return node_id



_SIZING_KEYWORDS = {
    "min-content": "min-content", "max-content": "max-content", "fit-content": "fit-content",
    "stretch": "stretch", "fill-available": "stretch", "available": "stretch",
}


def _sizing_keyword(raw) -> "str | None":
    """A computed `width`/`height` that is a CSS Sizing 3 keyword (vendor
    spellings included), as `src/lib.rs` names it; None otherwise."""
    text = (raw or "").strip().lower() if isinstance(raw, str) else ""
    for prefix in ("-webkit-", "-moz-"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return _SIZING_KEYWORDS.get(text)



# The initial containing block's children (`tree._build_root`): every
# `position: fixed` box, whatever its ancestors (CSS 2.1 10.1 rule 4). None
# when the layout has no viewport.
_viewport_sink: "list | None" = None


def _is_fixed(style_obj) -> bool:
    position = getattr(style_obj, "position", None)
    return getattr(position, "value", position) == "fixed"


def _escapee_sink(style_obj, sink):
    """Where an out-of-flow box built away from its static parent goes:
    the viewport for `fixed`, else its containing block's list `sink`."""
    return _viewport_sink if _is_fixed(style_obj) and _viewport_sink is not None else sink


def _static_anchor(tree, element, escapee_id, *, rtl, projection, node_map) -> int:
    """A zero-size placeholder for `element` (an absolutely positioned box
    built under its containing block, `escapee_id`) to sit in its static
    parent's flow, linked so `src/lib.rs` places the box at its static
    position. Returns the placeholder's node id, for the caller to insert
    at the box's place in the flow."""
    anchor = box_of(element).static_anchor_obj
    if anchor is None:
        anchor = box_of(element).static_anchor_obj = anonymous_boxes._StaticAnchor(element)
    style = dict(anonymous_boxes._StaticAnchor.STYLE)
    anchor_id = (projection.upsert(anchor, style, [], None, None)
                 if projection else tree.new_leaf(style))
    node_map[anchor_id] = anchor
    tree.set_static_anchor(escapee_id, anchor_id, rtl)
    box_of(element).static_anchored = True
    return anchor_id



def _build_inline_node(tree, owner, plan, style, *, computed_cache, escapee_sink, reuse_styles,
        projection, node_map) -> int:
    """One inline formatting context as a `Tree.new_inline` node: `plan`
    lays its lines (`_InlineFormattingPlan.measure`, driven from Rust's
    `compute_inline_layout`), and every atomic inline-level box or float
    mixed into them is built here as a real child subtree, handed to the
    node in run order. An out-of-flow ("escapee") run's element is built
    too, but goes to `escapee_sink` -- the containing block that anchors it."""
    measure_key = ("inline-context", tuple(plan.parent_style.items()), tuple(
        ("break", id(run["element"])) if run.get("break") else
        ("escapee", id(run["element"])) if run.get("escapee") else
        ("atomic", id(run["element"]), run.get("float"), id(run["owner"]), run["leading"], run["trailing"],
         run.get("margin_start", 0.0), run.get("margin_end", 0.0), run.get("vertical_align")) if run.get("atomic") else
        (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
         tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
         run["leading"], run["trailing"], run["top_edge"], run["atomic_width"],
         run.get("margin_start", 0.0))
        for run in plan.runs
    ))
    if projection is not None and not projection.measure_changed(owner, measure_key):
        # The retained node keeps its callback bound to the existing plan;
        # that plan's placements are what publish() must report too.
        plan = box_of(owner).inline_plan
    box_of(owner).inline_plan = plan
    child_ids = []
    for run in plan.runs:
        if run.get("atomic"):
            run["atomic_index"] = len(child_ids)
            child_ids.append(build(
                tree, run["element"], node_map, computed=run["computed"], style_obj=run["style"],
                computed_cache=computed_cache,
                is_containing_block=box_model._establishes_containing_block(run["style"]),
                escapees=escapee_sink, reuse_styles=reuse_styles, projection=projection,
            ))
        elif run.get("escapee"):
            sink = _escapee_sink(run["style"], escapee_sink)
            escapee_id = build(
                tree, run["element"], node_map, computed=run["computed"], style_obj=run["style"],
                computed_cache=computed_cache,
                is_containing_block=box_model._establishes_containing_block(run["style"]),
                escapees=sink, reuse_styles=reuse_styles, projection=projection,
            )
            sink.append(escapee_id)
            # The plan places this placeholder where the box would sit in
            # the line (its static position); see `_InlineFormattingPlan.measure`.
            run["anchor_index"] = len(child_ids)
            tag = (getattr(run["element"], "tagName", "") or "").lower()
            child_ids.append(_static_anchor(
                tree, run["element"], escapee_id,
                rtl=plan.rtl and tag not in box_model._USUALLY_INLINE_TAGS,
                projection=projection, node_map=node_map))
    measure = (plan.measure
               if projection is None or projection.measure_changed(owner, measure_key) else None)
    node_id = (projection.upsert(owner, style, child_ids, measure, measure_key, kind="inline")
               if projection else tree.new_inline(style, measure, child_ids))
    node_map[node_id] = owner
    return node_id



def _build_from_inline_plan(tree, element, style, inline_plan, css_display_value, *, computed_cache,
        escapees, own_escapees, is_containing_block, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `inline_plan is not None` branch: `element` is the
    block container of one inline formatting context (`_build_inline_node`).

    When `element` is itself a containing block for absolutely positioned
    descendants (CSS 2.1 10.1), it stays an ordinary Taffy block node whose
    children are one anonymous inline node holding its lines plus those
    positioned boxes -- Taffy's block layout then resolves their insets
    against `element`, and the escapee runs' recorded static positions
    still apply. Otherwise `element` is the inline node itself and its
    positioned descendants go to the nearest containing-block ancestor."""
    box_of(element).has_layout_children = True
    # `style["display"]` is already Taffy-mapped to "block", so it can't
    # distinguish a genuine block element from an inline/inline-block one
    # built as an atomic box elsewhere -- `css_display_value` (the real
    # pre-mapping computed display) does.
    if not is_containing_block:
        return _build_inline_node(
            tree, element, inline_plan, style, computed_cache=computed_cache,
            escapee_sink=escapees if escapees is not None else own_escapees,
            reuse_styles=reuse_styles, projection=projection, node_map=node_map,
        )
    owner = box_of(element).inline_owner
    if owner is None:
        owner = box_of(element).inline_owner = anonymous_boxes._AnonymousInlineRun(None, element)
    owner_style = inline_formatting._inline_text_style(style)
    # The anonymous node spans the element's content box: a percentage
    # height on an atomic child then resolves against the element's definite
    # height (CSS 2.1 10.5), and against nothing when the element's is auto.
    owner_style["height"] = ("pct", 1.0)
    box_of(owner).native_style = owner_style
    box_of(owner).paint_style = box_of(element).paint_style
    inline_id = _build_inline_node(
        tree, owner, inline_plan, owner_style, computed_cache=computed_cache,
        escapee_sink=own_escapees, reuse_styles=reuse_styles, projection=projection, node_map=node_map,
    )
    all_child_ids = [inline_id] + own_escapees
    node_id = (projection.upsert(element, style, all_child_ids, None, None)
               if projection else tree.new_with_children(style, all_child_ids))
    return node_id



def _build_block_children(tree, element, style, computed, children, *, computed_cache, is_containing_block,
        is_table_row, own_escapees, escapees, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `elif children:` branch: ordinary block-flow children, each
    recursively built, grouped into rowspan placeholders (a table row) or inline
    runs (everything else)."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    normal_child_ids = []
    normal_entries = []
    for child, child_computed, child_style in children:
        child_is_cb = box_model._establishes_containing_block(child_style)
        fixed_to_viewport = _is_fixed(child_style) and _viewport_sink is not None
        if (box_model._is_absolutely_positioned(child_style) and not is_containing_block) or fixed_to_viewport:
            # `element` isn't this box's containing block -- build the child
            # normally, hand its node id to the real ancestor `escapees` belongs
            # to (the viewport, for `fixed`).
            sink = _escapee_sink(child_style, escapees)
            child_id = build(
                tree, child, node_map, computed=child_computed, style_obj=child_style,
                computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=sink,
                reuse_styles=reuse_styles, projection=projection,
                is_grid_item=style["display"] == "grid",
            )
            sink.append(child_id)
            if style["display"] == "block" and not is_table_row:
                # Its static position is here, in `element`'s block flow.
                anchor_id = _static_anchor(
                    tree, child, child_id, rtl=dom._element_direction(element, computed) == "rtl",
                    projection=projection, node_map=node_map)
                normal_child_ids.append(anchor_id)
        else:
            child_id = build(
                tree, child, node_map, computed=child_computed, style_obj=child_style,
                computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                reuse_styles=reuse_styles, projection=projection,
                is_grid_item=style["display"] == "grid",
            )
            normal_child_ids.append(child_id)
            normal_entries.append((child, child_style, child_id))
    all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
    if box_of(element).get("is_table_root", False):
        # CSS 2.1 17.5: the table lays out its own rows and cells.
        plan = table_formatting._TableLayoutPlan(element, computed_cache)
        node_id = (projection.upsert(element, style, all_child_ids, plan.measure, ("table", id(plan)), kind="table")
                   if projection else tree.new_table(style, plan.measure, all_child_ids))
        return node_id
    node_id = (projection.upsert(element, style, all_child_ids, None, None)
               if projection else tree.new_with_children(style, all_child_ids))
    return node_id



def _build_br_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s `tag_name == "br"` branch: a zero-width, one-line-height
    strut leaf (CSS 2.1 9.2.2 -- a `<br>` never generates an ordinary block box)."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    paint_style = box_of(element).paint_style
    font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
    family = paint_style.get("font_family", "") or ""
    if family == "none":
        family = ""
    weight = inline_formatting._parse_font_weight(paint_style.get("font_weight"))
    italic = fonts.is_italic(paint_style.get("font_style"))
    ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
    # An explicit `line-height: 0` must not be treated as unset.
    resolved_line_height = inline_formatting._resolved_line_height(paint_style.get("line_height"))
    line_height = resolved_line_height if resolved_line_height is not None else normal
    style["width"] = 0.0
    style["height"] = line_height
    box_of(element).text_lines = []
    node_id = (projection.upsert(element, style, [], None, None)
               if projection else tree.new_leaf(style))
    return node_id



def _build_replaced_leaf(tree, element, style, tag_name, *, projection) -> int:
    """Body of build()'s img/canvas/svg/iframe branch: a replaced-element leaf sized
    by its own intrinsic-size resolver."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    if tag_name == "img":
        replaced_elements._apply_image_intrinsic_size(style, element)
    elif tag_name == "canvas":
        replaced_elements._apply_canvas_intrinsic_size(style, element)
    elif tag_name == "iframe":
        replaced_elements._apply_iframe_intrinsic_size(style, element)
    elif tag_name == "video":
        replaced_elements._apply_video_intrinsic_size(style, element)
    else:
        replaced_elements._apply_svg_intrinsic_size(style, element)
    box_of(element).text_lines = []
    img_measure = box_of(element).img_measure
    if img_measure is not None:
        measure, measure_key = img_measure
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def _build_select_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s `tag_name == "select"` branch: a measured text leaf for its
    selected option's display text, or an empty leaf. `<select multiple>`/`size`
    (a listbox, not a closed dropdown) overrides the height to fit every row
    -- `paint.py`'s `_paint_listbox_rows` does the actual per-row painting;
    this only has to reserve the right box."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    rows = replaced_elements._listbox_row_count(element)
    box_of(element).listbox_rows = rows
    if rows:
        style["height"] = rows * replaced_elements.LISTBOX_ROW_HEIGHT
    text = replaced_elements._select_display_text(element)
    if text:
        measure_key = _measure_key(box_of(element).paint_style, text)
        measure = (inline_formatting._make_measure(box_of(element).paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        box_of(element).text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def _build_form_control_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s input/textarea branch: a measured text leaf for its display
    text, or an empty leaf."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    text = replaced_elements._form_control_display_text(element)
    if text:
        measure_key = _measure_key(box_of(element).paint_style, text)
        measure = (inline_formatting._make_measure(box_of(element).paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        box_of(element).text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def _build_text_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s final `else` branch (an ordinary element with no recognised
    special handling): a measured text leaf for its own text content, or an empty leaf."""
    box_of(element).pop("inline_plan", None)
    box_of(element).inline_fragments = []
    text = dom._own_text(element)
    if text:
        measure_key = _measure_key(box_of(element).paint_style, text)
        measure = (inline_formatting._make_measure(box_of(element).paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        # An inline node, not a bare leaf: its lines are laid against the
        # block formatting context's float bands (CSS 2.1 9.5).
        node_id = (projection.upsert(element, style, [], measure, measure_key, kind="inline")
                   if projection else tree.new_inline(style, measure, []))
    elif (getattr(box_of(element).computed_style, "display", "") or "").strip().lower() == "list-item":
        # An empty list item still has its marker's line box, and so a
        # baseline one line-ascent below its content top
        # (empty-cells-applies-to-003.xht).
        box_of(element).text_lines = []
        paint_style = box_of(element).paint_style or {}
        family = "" if paint_style.get("font_family") in (None, "none") else paint_style["font_family"]
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        ascent, descent, normal = fonts.text_metrics(
            family, font_size, inline_formatting._parse_font_weight(paint_style.get("font_weight")) >= 600,
            fonts.is_italic(paint_style.get("font_style")))
        line_height = inline_formatting._resolved_line_height(paint_style.get("line_height"))
        line_height = normal if line_height is None else line_height
        baseline = ascent + math.floor((line_height - ascent - descent) / 2)

        def measure(_w, _h, _kw=None, _kh=None, baseline=baseline):
            return (0.0, 0.0, baseline, baseline)

        measure_key = ("list-item-marker", baseline)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        box_of(element).text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created. `computed`/`style_obj`, if given, are already-computed
    (the caller's `dom._child_elements` call needed them too).

    `is_containing_block`/`escapees` implement CSS's real containing-block
    rule for position:absolute/fixed -- resolved against the nearest
    ancestor with position != static, or the viewport, not just the literal
    DOM parent. `is_containing_block=True` owns a fresh `escapees` list;
    a non-containing-block descendant's absolutely-positioned child is
    added to that list instead of its literal parent's Taffy children.
    An intermediate position:static element passes `escapees` straight through."""
    box_of(element).pop("flattened_inline", None)  # given a box of its own this pass
    if computed_cache is None:
        computed_cache = {}
    if computed is None or style_obj is None:
        computed, style_obj = dom._describe(element, computed_cache, reuse_styles=reuse_styles)
    style = box_of(element).native_style if reuse_styles else None
    if style is None:
        style = style_bridge.to_dict(style_obj)
        if style.get("position") == "absolute":
            # CSS 2.1 10.3.7/10.6.4: an absolutely positioned box's auto
            # margins absorb free space only when both insets and the size on
            # that axis are all non-auto; otherwise they are 0. (Taffy would
            # spread the space into them and centre the box -- abspos-009.xht.)
            inset = style.get("inset") or ["auto"] * 4
            margin = list(style.get("margin") or [0.0] * 4)
            if "auto" in (inset[1], inset[3], style.get("width")):
                margin[1] = 0.0 if margin[1] == "auto" else margin[1]
                margin[3] = 0.0 if margin[3] == "auto" else margin[3]
            if "auto" in (inset[0], inset[2], style.get("height")):
                margin[0] = 0.0 if margin[0] == "auto" else margin[0]
                margin[2] = 0.0 if margin[2] == "auto" else margin[2]
            style["margin"] = margin
        # CSS 2.1 9.10: Taffy's block and flex layout place children
        # right-to-left in an rtl box.
        if dom._element_direction(element, computed) == "rtl":
            style["direction"] = "rtl"
        # HTML's legacy alignment (`<center>`, `align=`): Taffy's block
        # layout also aligns narrower child blocks by it.
        legacy_align = {"-webkit-center": "legacy-center", "-moz-center": "legacy-center",
                        "-webkit-right": "legacy-right", "-moz-right": "legacy-right",
                        "-webkit-left": "legacy-left", "-moz-left": "legacy-left"}.get(
            (getattr(computed, "textAlign", "") or "").strip().lower())
        if legacy_align is not None:
            style["text_align"] = legacy_align
        # CSS Sizing 3 intrinsic keywords: Taffy sizes these itself.
        for axis, prop in (("width", "width"), ("height", "height")):
            keyword = _sizing_keyword(getattr(computed, prop, ""))
            if keyword is not None:
                style[axis] = keyword
        # CSS 2.1 9.5/9.5.2: Taffy's block layout places floats and applies
        # clearance itself, given the properties.
        # CSS 2.1 9.7: an absolutely positioned box's float computes to none.
        if box_model._is_floated(computed) and not box_model._is_absolutely_positioned(style_obj):
            style["float"] = computed.float.strip().lower()
        clear_value = (getattr(computed, "clear", "") or "").strip().lower()
        if clear_value in ("left", "right", "both"):
            style["clear"] = clear_value
        # CSS 2.1 8.3.1: non-visible overflow establishes a BFC (stops an
        # in-flow child's margin collapsing through) -- not modelled in
        # LayoutStyle/style_bridge.to_dict(), read straight off `computed`.
        # Taffy handles it given Style.overflow; unrecognised falls back to visible.
        _valid_overflow = ("visible", "clip", "hidden", "scroll", "auto")
        overflow_x = getattr(computed, "overflowX", "visible") or "visible"
        overflow_y = getattr(computed, "overflowY", "visible") or "visible"
        style["overflow"] = (
            overflow_x if overflow_x in _valid_overflow else "visible",
            overflow_y if overflow_y in _valid_overflow else "visible",
        )
        # justify-items isn't in domonic's recognised-property list, so
        # LayoutStyle has no field for it -- read from the raw cascade dict
        # instead, same workaround LayoutStyle.from_computed's own justifySelf needs.
        style["justify_items"] = style_bridge._align_keyword(
            Keyword((computed._resolved.get("justify-items") or "").strip().lower()), content=False)
        # grid-area isn't expanded into its four longhands by domonic's
        # cascade -- parsed straight from the raw cascade here, same as
        # justify-items above. css-grid/grid-items/grid-inline-order-property-painting-*.html.
        if style["grid_column"] == (None, None) and style["grid_row"] == (None, None):
            area = (computed._resolved.get("grid-area") or "").strip()
            if area and area.lower() != "auto":
                row, col = flex_grid._parse_grid_area(area)
                if row != (None, None):
                    style["grid_row"] = row
                if col != (None, None):
                    style["grid_column"] = col
        box_of(element).native_style = style
    if False and is_grid_item and style["min_width"] == "auto":
        # Disabled -- predates `flex_grid._is_flex_or_grid_item` excluding flex/grid
        # items from the generic width:auto->pct(1.0) substitution; forcing 0
        # here now would throw away a grid item's real automatic minimum
        # (CSS Grid 1 6.6) -- grid-layout-auto-tracks.html.
        style["min_width"] = 0.0
    if ((getattr(computed, "flexBasis", "") or "").strip().lower() == "content"
            and flex_grid._is_flex_or_grid_item(element)):
        # CSS Flexbox 7.2.3 flex-basis:content: base size is the item's
        # content size regardless of main-axis width/height --
        # flexbox-flex-basis-content-001a.html. Taffy has no `content`
        # keyword; the main-axis size is cleared so auto measures content.
        parent_native = (box_of(dom._layout_parent(element)).native_style or {})
        if parent_native.get("display") == "flex":
            main = "height" if (parent_native.get("flex_direction") or "row").startswith("column") else "width"
            style["flex_basis"] = "auto"
            style[main] = "auto"
    if isinstance(style.get("flex_basis"), tuple) and flex_grid._is_flex_or_grid_item(element):
        # CSS Flexbox 9.2.3 B: a percentage flex-basis against an indefinite
        # main size is treated as content, ignoring the item's own height
        # for its base size -- flex-basis-010.html.
        parent_native = (box_of(dom._layout_parent(element)).native_style or {})
        if (parent_native.get("display") == "flex"
                and (parent_native.get("flex_direction") or "row").startswith("column")
                and parent_native.get("height") == "auto"):
            style["flex_basis"] = "auto"
            style["height"] = "auto"
    own_escapees = [] if is_containing_block else escapees
    tag_name = (getattr(element, "tagName", "") or "").lower()
    box_of(element).tag_name = tag_name
    is_genuinely_inline = (
        tag_name not in box_model._REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and box_model._trusts_computed_inline(element, tag_name)
        # CSS Flexbox 4 / Grid 6.1: a flex/grid item's display is blockified.
        and not flex_grid._is_flex_or_grid_item(element)
        # CSS 2.1 9.7: so is a float's or an absolutely positioned box's.
        and not box_model._is_floated(computed)
        and not box_model._is_absolutely_positioned(style_obj)
    )
    if is_genuinely_inline:
        # CSS 2.1 10.3.1: width/height never apply to a non-replaced inline
        # box -- Taffy has no inline mode (mapped to "block").
        style["width"] = "auto"
        style["height"] = "auto"
        # CSS 2.1 10.3.1/10.6.1: vertical margins likewise don't affect a
        # non-replaced inline's height.
        margin = list(style["margin"])
        margin[0] = margin[2] = 0.0
        style["margin"] = margin
    # CSS 2.1 9.7: an absolutely/fixed positioned element's display
    # blockifies regardless of its specified value -- top-applies-to-001.xht,
    # bottom-applies-to-005.xht.
    table_internal_display = (
        "" if box_model._is_absolutely_positioned(style_obj) else getattr(style_obj.display, "value", "")
    )
    if table_internal_display in table_layout._TABLE_INTERNAL_DISPLAYS:
        # CSS 2.1 17.4/CSS Tables 3: margin never applies to an internal
        # table box -- only the outer display:table box keeps it.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        if table_internal_display != "table-cell":
            # Padding still applies to table-cell (CSS 2.1 17.6.1); every
            # other internal table box gets neither --
            # wpt/css/CSS2/margin-padding-clear/padding-applies-to-001.xht.
            style["padding"] = [0.0, 0.0, 0.0, 0.0]
    if ((getattr(style_obj.display, "value", "") == "inline-block"
            and box_model._trusts_computed_inline(element, tag_name))
            or getattr(style_obj.display, "value", "") in ("inline-flex", "inline-grid")):
        # inline-block establishes its own BFC (CSS 2.1 9.2.1) -- signalled
        # to Taffy via Contain::PAINT, same as overflow.
        style["establishes_bfc"] = True
    elif (getattr(computed, "display", "") or "").strip().lower() == "flow-root":
        # CSS Display 3: flow-root is a block container that establishes a
        # new BFC -- it contains its floats (floats-placement-005.html).
        style["establishes_bfc"] = True
    is_table_root = tag_name == "table" or table_layout._is_table_root_display(computed)
    is_table_row = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name == "tr" or table_layout._is_table_row_display(computed))
    is_table_cell = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name in ("td", "th") or table_layout._is_table_cell_display(computed))
    if is_table_root:
        _setup_table_root(element, style, computed, computed_cache, tag_name)
    _setup_table_row_or_cell(element, style, computed, style_obj, tag_name, is_table_row, is_table_cell)
    is_table_caption = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name == "caption"
        or (getattr(computed, "display", "") or "").strip().lower() == "table-caption")
    parent = dom._layout_parent(element)
    _setup_table_caption(element, style, computed, parent, is_table_caption)
    # `<select>`'s `<option>`s and `<iframe>`'s light-DOM children are never
    # real layout content -- treated as childless regardless of markup.
    children = [] if tag_name in ("select", "svg", "svg:svg", "iframe") else dom._child_elements(
        element, computed_cache, reuse_styles=reuse_styles
    )
    if style["display"] in ("flex", "grid") and len(children) > 1:
        # CSS Flexbox 5.4 / Grid: `order` reorders items (stable) --
        # flex-order.html, flexbox-anonymous-items-001.html. Absolutely
        # positioned children stay unsorted after the in-flow ones.
        if any(flex_grid._css_order(child_computed) for _child, child_computed, _style in children):
            children = sorted(children, key=lambda entry: flex_grid._css_order(entry[1]))
    if is_table_root and children:
        # CSS 2.1 17.5.3: row-groups display in header/body/footer order
        # regardless of source order -- `table_layout._table_rows` already reorders
        # for column-width measurement; this matches it for the table's own
        # visual stacking. CSS 2.1 17.4: a caption sits outside the row
        # groups entirely (top or bottom), never sorted among them.
        captions_top, captions_bottom, header, body, footer = [], [], [], [], []
        for entry in children:
            child_tag = (getattr(entry[0], "tagName", "") or "").lower()
            child_display = (getattr(entry[1], "display", "") or "").strip().lower()
            if box_model._is_absolutely_positioned(entry[2]):
                # CSS 2.1 9.7: blockified and out of flow -- top-applies-to-015.xht.
                body.append(entry)
                continue
            if child_tag == "caption" or child_display == "table-caption":
                side = (getattr(entry[1], "captionSide", "") or "top").strip().lower()
                (captions_bottom if side == "bottom" else captions_top).append(entry)
                continue
            kind = table_layout._row_group_kind(child_tag, entry[1]) or "body"
            # Same first-header/first-footer-only rule as `table_layout._table_rows`.
            if kind == "header" and header:
                kind = "body"
            elif kind == "footer" and footer:
                kind = "body"
            (header if kind == "header" else footer if kind == "footer" else body).append(entry)
        children = captions_top + header + body + footer + captions_bottom
        box_of(element).table_bottom_captions = [entry[0] for entry in captions_bottom]
        box_of(element).table_captions = [entry[0] for entry in captions_top + captions_bottom]
        box_of(element).table_sections = [
            entry[0] for entry in header + body + footer
            if table_layout._row_group_kind((getattr(entry[0], "tagName", "") or "").lower(), entry[1]) is not None]
    box_of(element).has_layout_children = bool(children)
    # Replaced/control elements run their own dedicated branch below -- CSS
    # generated content doesn't apply to them.
    has_pseudo = tag_name not in dom._NO_GENERATED_CONTENT_TAGS and (
        box_of(element).before_pseudo is not None
        or box_of(element).after_pseudo is not None
    )
    # CSS 2.1 17.2.1: a table/row-group/row never formats inline content of
    # its own -- loose text/inline children get wrapped into an anonymous
    # cell first (`_normalized_child_nodes`).
    is_table_container = is_table_root or is_table_row or (
        not box_model._is_absolutely_positioned(style_obj) and table_layout._row_group_kind(tag_name, computed) is not None)
    # CSS Flexbox 4 / Grid 6.1: every in-flow child of a flex/grid container
    # is a blockified item -- the container never formats inline content of
    # its own (real text got its anonymous item from `_wrap_inline_runs`).
    is_flex_or_grid_container = style["display"] in ("flex", "grid")
    inline_items = (inline_formatting._inline_mixed_content(element, children, element_is_inline=is_genuinely_inline)
                    if (children or has_pseudo) and not is_table_container
                    and not (is_flex_or_grid_container and not has_pseudo) else None)
    # td/th have no UA default in domonic (computed display is "inline") --
    # forced to "block" so `_InlineFormattingPlan`'s owner_display leaves
    # Taffy's already-correct column-width box alone.
    css_display_value = ("block" if (tag_name in ("td", "th") or is_table_cell)
                          else getattr(style_obj.display, "value", "").strip() or "block")
    split_pieces = (inline_formatting._split_inline_flow_around_blocks(
                         element, inline_items, style, css_display_value, computed_cache)
                     if inline_items else None)
    inline_plan = (inline_formatting._make_inline_formatting_plan(element, inline_items, style, css_display_value, computed_cache)
                   if inline_items and split_pieces is None else None)

    if split_pieces is not None:
        # CSS 2.1 9.2.1.1: an inline element split around an in-flow block
        # child -- see `inline_formatting._split_inline_flow_around_blocks`.
        node_id = _build_split_pieces(
            tree, element, style, split_pieces, computed_cache=computed_cache,
            is_containing_block=is_containing_block, escapees=escapees, own_escapees=own_escapees,
            reuse_styles=reuse_styles, projection=projection, node_map=node_map,
        )
    elif inline_plan is not None:
        node_id = _build_from_inline_plan(
            tree, element, style, inline_plan, css_display_value, computed_cache=computed_cache,
            escapees=escapees, own_escapees=own_escapees, is_containing_block=is_containing_block,
            reuse_styles=reuse_styles, projection=projection, node_map=node_map,
        )
    elif children or is_table_root:
        # A table is a table node even with no rows (a table of only
        # `<col>`s still has their widths -- table-column-rendering-001.xht).
        node_id = _build_block_children(
            tree, element, style, computed, children, computed_cache=computed_cache,
            is_containing_block=is_containing_block, is_table_row=is_table_row,
            own_escapees=own_escapees, escapees=escapees, reuse_styles=reuse_styles,
            projection=projection, node_map=node_map,
        )
    elif tag_name == "br":
        # CSS 2.1 9.2.2: a standalone `<br>` among block siblings (no
        # surrounding inline content to route it through the real
        # inline-formatting-plan machinery) falls through to here --
        # sized as a single line's own strut (zero width, one line-height),
        # same font-metrics math as `_empty_inline_strut_run`.
        # wpt/css/CSS2/mpc/padding-top-036.xht: was 784x0, Chrome's 0x18.
        node_id = _build_br_leaf(tree, element, style, projection=projection)
    elif tag_name in ("img", "canvas", "svg", "svg:svg", "iframe", "video"):
        node_id = _build_replaced_leaf(tree, element, style, tag_name, projection=projection)
    elif tag_name == "select":
        node_id = _build_select_leaf(tree, element, style, projection=projection)
    elif tag_name in ("input", "textarea"):
        node_id = _build_form_control_leaf(tree, element, style, projection=projection)
    else:
        node_id = _build_text_leaf(tree, element, style, projection=projection)

    if box_model._is_absolutely_positioned(style_obj) and box_of(element).pop("static_anchored", None):
        # Re-linked by the caller if this box still escapes its static parent.
        tree.set_static_anchor(node_id, None)
    box_of(element).node_id = node_id
    node_map[node_id] = element
    return node_id





def _measure_key(paint_style: dict, text: str) -> tuple:
    """Inputs that can change a Taffy leaf's intrinsic text measurement."""
    return (
        text, paint_style["font_family"], paint_style["font_size"],
        paint_style["font_weight"], paint_style["font_style"],
        paint_style["letter_spacing"], paint_style["word_spacing"],
        paint_style["line_height"], paint_style.get("white_space", "normal"),
    )
