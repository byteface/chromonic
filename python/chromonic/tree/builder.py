from __future__ import annotations

import math

from domonic import _fontmetrics
from domonic.layout import Keyword, Length

from .. import fonts, style_bridge
from .._native import Tree, layout_text
from . import anonymous_boxes, box_model, dom, flex_grid, inline_formatting, replaced_elements, table_layout




def _wants_horizontal_flow(element, computed, style_obj) -> bool:
    return box_model._is_inline_level(element, style_obj) or box_model._is_floated(computed)



def _approximate_inline_flow(
    element, style: dict, child_elements: list, child_computeds: list, child_styles: list, computed,
) -> None:
    """Approximates real inline/float flow (Taffy has no native mode for either)
    as `display:flex; flex-wrap:wrap` when 2+ children and 80%+ "want" horizontal
    flow (`_wants_horizontal_flow`: inline-level tag default, or `float`). Found
    via suckless.org (nav links rendering one-per-line) and wikipedia.org (a
    float-based grid). Majority not unanimity: suckless.org's own nav is eight
    `<a>`s plus one `display:block` `<span>`. Heuristic only -- no real mixed
    inline text, no float clearing/text-wrap-around-floats, just whole-element
    flex-wrap."""
    element.__dict__.pop("_chromonic_float_flow_children", None)
    element.__dict__.pop("_chromonic_float_flow_qualifies", None)
    for child in child_elements:
        child.__dict__.pop("_chromonic_force_full_row_width", None)
        child.__dict__.pop("_chromonic_no_flex_shrink", None)
    if style["display"] != "block":
        return  # already flex/grid/none -- an explicit layout mode wins
    if len(child_elements) < 2 and not any(box_model._is_floated(cc) for cc in child_computeds):
        # CSS 2.1 9.5: a lone float still needs real positioning (flush-right
        # for float:right) -- floats-rule3-outside-right-001.xht.
        return
    qualifies = [
        _wants_horizontal_flow(child, child_computed, child_style)
        for child, child_computed, child_style in zip(child_elements, child_computeds, child_styles)
    ]
    # Majority not unanimity; a single float qualifies alone regardless of
    # its fraction of the children (CSS 2.1 9.5 always narrows the container).
    if not any(box_model._is_floated(cc) for cc in child_computeds) and sum(qualifies) < len(child_elements) * 0.8:
        return
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    # Flexbox's default stretch inflated an 18px float to a 100px container
    # (fixed-table-layout-005.xht).
    style["align_items"] = "flex-start"
    style["align_content"] = "flex-start"
    # `_fix_float_flow_after_block_sibling` needs the real child/qualifies
    # lists -- flex-wrap alone has no "block sibling forces a new line" concept.
    element._chromonic_float_flow_children = list(child_elements)
    element._chromonic_float_flow_qualifies = list(qualifies)
    # CSS 2.1 9.2.1: an ordinary block child always fills the containing
    # block, auto-width or not -- flex-wrap would shrink-to-fit it instead.
    for child, ok, child_computed, child_style in zip(child_elements, qualifies, child_computeds, child_styles):
        if not ok:
            child._chromonic_force_full_row_width = True
        elif box_model._is_floated(child_computed) or isinstance(child_style.get("width"), (int, float)):
            # CSS 2.1 10.3.5 / ordinary inline: an explicit-width inline-level
            # box or float is never shrink-to-fit, free to overflow --
            # floats-rule3-outside-right-001.xht (float) and an equivalent
            # inline-block case both confirmed flex-shrink:1 shrinking it wrongly.
            child._chromonic_no_flex_shrink = True
    inline_tag_qualifies = any(
        box_model._is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
    )
    if inline_tag_qualifies and style["gap"] == (0.0, 0.0):
        # Real inline flow spaces via whitespace text nodes, unmeasured here --
        # approximated as one space width; skipped for a purely float-qualified
        # group (already spaced by margins).
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        bold = _fontmetrics.is_bold(computed.fontWeight)
        space_width = _fontmetrics.advance_width(" ", font_size, bold)
        style["gap"] = (0.0, space_width)



def _setup_table_root(element, style, computed, computed_cache, tag_name):
    """Body of `build()`'s `is_table_root` branch, extracted verbatim: resolves
    the table's grid/columns/borders/spacing and stashes them as `element._chromonic_table_*`
    attributes for the row/cell branches (in later, separate `build()` calls) to read."""
    element._chromonic_is_table_root = True
    element.__dict__.pop("_chromonic_table_growth_propagated", None)
    # The previous pass's column resolution must not leak into this
    # one: `table_layout._compute_table_column_widths` measures every cell in a
    # scratch tree, and the cell branch (`dom._layout_parent` reaches this
    # real table from there) would read last pass's per-cell width and
    # discard the cell's own `width` -- border-conflict-example-
    # 001.xht's `width: 2em` cells came out 52px on every relayout
    # after the first (69px), i.e. on any resize or image load.
    for stale in ("_chromonic_table_column_widths", "_chromonic_table_column_min_widths",
                  "_chromonic_table_columns_max", "_chromonic_table_columns_min",
                  "_chromonic_table_columns_uncollapsed", "_chromonic_table_fixed"):
        element.__dict__.pop(stale, None)
    # A table box establishes a block formatting context (CSS 2.1
    # 9.4.1): a caption's top margin stays inside it, never collapsing
    # through into the table's own (table-anonymous-block-011.xht: a
    # `margin-top: 2em` caption in a `margin-top: 2em` table sits 4em
    # below the preceding border in Chrome, not 2em).
    style["establishes_bfc"] = True
    element._chromonic_border_collapse = computed.borderCollapse == "collapse"
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
    element._chromonic_table_rows = rows
    element._chromonic_table_grid_cells = cells
    element._chromonic_table_columns = table_layout._table_columns(element, computed_cache, column_count)
    # CSS 2.1 17.5.5: a `visibility: collapse` column (or column
    # group) is 0px wide -- its cells with it, and one border-spacing
    # gap goes with it (column-visibility-003.xht: four 128px `<col>`s
    # with one collapsed make a 392px table: three columns, four
    # gaps) -- while its cells still take part in the row layout.
    collapsed_columns: set = set()
    for index, (column, group) in enumerate(element._chromonic_table_columns):
        for owner in (column, group):
            if owner is None:
                continue
            try:
                visibility = (getattr(dom._describe(owner, computed_cache)[0], "visibility", "") or "")
            except Exception:
                visibility = ""
            if visibility.strip().lower() == "collapse":
                collapsed_columns.add(index)
    element._chromonic_table_collapsed_columns = collapsed_columns
    element._chromonic_table_cell_columns = {
        id(cell): (c, min(c + colspan, column_count) - c) for cell, _r, c, _rs, colspan in cells}
    # CSS 2.1 17.5: an `rtl` table's first column is the rightmost -- rows
    # lay cells out right-to-left (row-reverse, see the row branch) and the
    # collapsed-border grid lines mirror.
    element._chromonic_table_rtl = dom._element_direction(element, computed) == "rtl"
    row_cells: dict = {}
    content_rows: set = set()
    for cell, row_index, _col, rowspan, _colspan in cells:
        row_cells.setdefault(id(rows[row_index]), []).append(cell)
        # A row a content-bearing cell spans down into counts as having
        # content too -- table-height-algorithm-018.xht.
        if table_layout._table_cell_has_content(cell, computed_cache):
            content_rows.update(range(row_index, min(row_index + rowspan, len(rows))))
    for index, row in enumerate(rows):
        row._chromonic_table_cells = row_cells.get(id(row), [])
        row._chromonic_table_row_empty = index not in content_rows
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
        row._chromonic_row_collapsed = collapsed
    if element._chromonic_border_collapse:
        # CSS 2.1 17.6.2: each side of a collapsed grid line keeps only its
        # own half of the winning width -- `table_layout._resolve_collapsed_table_borders`
        # runs the real 17.6.2.1 conflict resolution per segment; cells read
        # their halves from `_chromonic_collapsed_cell_borders` below.
        # Table box: own border halved, padding makes up the rest of the
        # winning perimeter width -- border-collapse-001.xht,
        # border-collapse-005.html.
        cell_borders, perimeter = table_layout._resolve_collapsed_table_borders(
            element, computed, rows, cells, column_count, computed_cache,
            rtl=element._chromonic_table_rtl)
        element._chromonic_collapsed_cell_borders = cell_borders
        own = [box_model._numeric_edge(value) for value in style["border"]]
        style["border"] = [value / 2.0 for value in own]
        style.update({
            "box_sizing": "border-box",
            # CSS 2.1 17.6.2: a table has no padding of its own in this model.
            "padding": [max(0.0, perimeter[i] / 2.0 - own[i] / 2.0) for i in range(4)],
        })
    else:
        element.__dict__.pop("_chromonic_collapsed_cell_borders", None)
    # CSS 2.1 17.5.2.2 auto layout: each column sizes to its widest cell,
    # not an equal share -- `table_layout._compute_table_column_widths`. A fixed
    # table with width:auto still uses auto layout (Chrome too) --
    # empty-cells-applies-to-014.xht.
    column_widths = (table_layout._compute_table_column_widths(cells, computed_cache)
                     if computed.tableLayout != "fixed" or style["width"] == "auto"
                     else {"cells": {}, "cells_min": {}, "columns": [], "columns_min": []})
    # CSS 2.1 17.5.2.2: a `<col>`'s width is that column's minimum --
    # column-width-001.xht.
    columns_list, columns_min_list = column_widths["columns"], column_widths["columns_min"]
    # Auto layout only -- fixed layout resolves columns itself
    # (`table_layout._compute_fixed_column_widths` / `_enforce_fixed_column_boxes` for a
    # percentage-width table, fixed-table-layout-023.xht).
    auto_layout = computed.tableLayout != "fixed" or style["width"] == "auto"
    for c, (column, group) in enumerate(element._chromonic_table_columns if auto_layout else ()):
        specified = None
        for owner in (column, group):
            if owner is None:
                continue
            try:
                value = style_bridge._len(dom._describe(owner, computed_cache)[1].width)
            except Exception:
                value = None
            if isinstance(value, (int, float)):
                specified = float(value)
                break
        if specified is None or specified <= 0.0:
            continue
        while len(columns_list) <= c:
            columns_list.append(0.0)
        while len(columns_min_list) <= c:
            columns_min_list.append(0.0)
        if specified > columns_list[c]:
            columns_list[c] = specified
        if specified > columns_min_list[c]:
            columns_min_list[c] = specified
        for cell, _row_index, c0, _rowspan, colspan in cells:
            span = range(c0, min(c0 + colspan, column_count))
            if c in span:
                column_widths["cells"][id(cell)] = sum(columns_list[cc] for cc in span if cc < len(columns_list))
                if colspan == 1:
                    column_widths["cells_min"][id(cell)] = max(
                        column_widths["cells_min"].get(id(cell), 0.0), specified)
    element._chromonic_table_column_widths = column_widths["cells"]
    element._chromonic_table_column_min_widths = column_widths["cells_min"]
    element._chromonic_table_columns_max = column_widths["columns"]
    element._chromonic_table_columns_min = column_widths["columns_min"]
    # A collapsed column's cells still lay out at their real width (Chrome
    # sizes the row from that, column-visibility-004.xht); collapsed widths
    # come out of items/rows/table box afterward (`_settle_collapsed_cells_in`).
    element._chromonic_table_columns_uncollapsed = list(column_widths["columns"])
    # CSS 2.1 17.5.3: a table's specified height is a minimum, not a cap
    # (`_distribute_table_extra_height` hands out surplus/needs the original
    # value) -- `min_height` is exactly that semantic in Taffy.
    element._chromonic_table_specified_height = (
        style["height"] if isinstance(style["height"], (int, float)) else None)
    if style["height"] != "auto":
        if style["min_height"] in ("auto", 0.0):
            style["min_height"] = style["height"]
        style["height"] = "auto"
    if tag_name == "table":
        # HTML UA stylesheet: `table { box-sizing: border-box }`.
        style["box_sizing"] = "border-box"
    elif style["box_sizing"] != "border-box" and not element._chromonic_border_collapse:
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
        if isinstance(style["min_height"], (int, float)) and element._chromonic_table_specified_height is not None:
            vertical = padding[0] + padding[2] + border[0] + border[2]
            style["min_height"] = style["min_height"] + vertical
            element._chromonic_table_specified_height = style["min_height"]
            style["box_sizing"] = "border-box"
    # CSS 2.1 17.6.1: border-spacing only applies in the separate border
    # model; `ua_style.py` supplies the UA default (2px) domonic has none of.
    if element._chromonic_border_collapse:
        element._chromonic_border_spacing = (0.0, 0.0)
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
        element._chromonic_border_spacing = (spacing_h, spacing_v)
        if spacing_h:
            # A colspan'd cell's box also covers the gaps it spans --
            # table-visual-layout-013.xht.
            widths = element._chromonic_table_column_widths
            for cell, _row_index, c, _rowspan, colspan in cells:
                if colspan > 1 and id(cell) in widths:
                    widths[id(cell)] += spacing_h * (min(c + colspan, column_count) - c - 1)
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
            # `is_table_row` gives every row a top margin for the gap before
            # it, which would double up with the table's own padding-top
            # just set above for the first displayed row (CSS 2.1 17.5.3
            # header/body/footer order) -- marked here so that row can skip
            # its own top margin instead.
            for row in rows:
                row.__dict__.pop("_chromonic_is_first_table_row", None)
            # First visible row skips its top margin; a collapsed one takes
            # none either -- CSS 2.1 17.5.5, row-visibility-004.xht.
            first_visible = True
            for row in rows:
                if getattr(row, "_chromonic_row_collapsed", False):
                    row._chromonic_is_first_table_row = True
                elif first_visible:
                    row._chromonic_is_first_table_row = True
                    first_visible = False
    element._chromonic_table_fixed = computed.tableLayout == "fixed" and style["width"] != "auto"
    if element._chromonic_table_fixed and isinstance(style["width"], (int, float)):
        # CSS 2.1 17.5.2.1, `table_layout._compute_fixed_column_widths`: only a definite
        # pixel width gets the exact algorithm; a percentage-width fixed
        # table keeps the flex approximation instead (cell branch).
        spacing_h = element._chromonic_border_spacing[0]
        horizontal = sum(box_model._numeric_edge(v) for v in style["padding"][1::2]) + sum(
            box_model._numeric_edge(v) for v in style["border"][1::2])
        content_width = style["width"] - (horizontal if style["box_sizing"] == "border-box" else 0.0)
        fixed = table_layout._compute_fixed_column_widths(
            element, cells, column_count, element._chromonic_table_columns, content_width,
            spacing_h, computed_cache)
        # CSS 2.1 17.5.2.1: the table widens when its columns need more than
        # specified -- fixed-table-layout-010.xht/-016.xht.
        needed = sum(fixed) + spacing_h * max(0, column_count - 1)
        if needed > content_width + 0.5:
            style["width"] = needed + (horizontal if style["box_sizing"] == "border-box" else 0.0)
        element._chromonic_table_columns_uncollapsed = list(fixed)
        element._chromonic_table_columns_max = fixed
        element._chromonic_table_columns_min = []
        element._chromonic_table_column_min_widths = {}
        element._chromonic_table_column_widths = {
            id(cell): sum(fixed[c:min(c + colspan, column_count)])
            + spacing_h * max(0, min(c + colspan, column_count) - c - 1)
            for cell, _row_index, c, _rowspan, colspan in cells
        }
    # CSS 2.1 17.5.2.2: max-content width is what `_fix_table_shrink_to_fit_width`
    # shrinks an auto-width table to (Taffy's own flex-row estimate ran a few
    # px short and over-shrank cells -- border-conflict-w-002.xht); min-content
    # is a hard floor (min_width) so a too-narrow container overflows rather
    # than crushing cells. Both include inter-column spacing and this box's
    # own padding/border, matching box_sizing.
    columns_max = getattr(element, "_chromonic_table_columns_max", None) or []
    columns_min = getattr(element, "_chromonic_table_columns_min", None) or []
    spacing_h = element._chromonic_border_spacing[0]
    gaps = max(0, len(columns_max) - 1) * spacing_h
    edges = [box_model._numeric_edge(v) for v in style["padding"]] + [box_model._numeric_edge(v) for v in style["border"]]
    horizontal = edges[1] + edges[3] + edges[5] + edges[7]
    outer = horizontal if style["box_sizing"] == "border-box" else 0.0
    element._chromonic_table_max_content_width = (sum(columns_max) + gaps + horizontal) if columns_max else None
    floor = sum(columns_min) + gaps + outer
    if columns_min and floor > 0.0 and style["min_width"] in ("auto", 0.0):
        style["min_width"] = floor
    elif columns_min and isinstance(style["min_width"], (int, float)):
        style["min_width"] = max(style["min_width"], floor)



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
        # Taffy has no table mode -- a plain flex row approximates ordinary
        # fixed/equal-column table geometry.
        style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap"})
        row_table = dom._layout_parent(element)
        while row_table is not None and not getattr(row_table, "_chromonic_is_table_root", False):
            row_table = dom._layout_parent(row_table)
        # rtl table rows lay cells out right-to-left -- not via Taffy's
        # row-reverse (placed every zero-basis flex-grown empty cell at the
        # same end position, border-conflict-element-002.xht), so children
        # are built in reversed DOM order instead (see `children` below).
        element._chromonic_row_rtl = bool(row_table is not None
                                          and getattr(row_table, "_chromonic_table_rtl", False))
        # CSS 2.1 17.5.3: a row's specified `height` is a minimum -- its
        # tallest cell can always make it taller (see the table root's own
        # `height` handling above for the same reasoning).
        if style["height"] != "auto":
            if style["min_height"] in ("auto", 0.0):
                style["min_height"] = style["height"]
            style["height"] = "auto"
        ancestor = dom._layout_parent(element)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = dom._layout_parent(ancestor)
        spacing_h, spacing_v = getattr(ancestor, "_chromonic_border_spacing", (0.0, 0.0)) if ancestor is not None else (0.0, 0.0)
        if spacing_h:
            style["gap"] = (0.0, spacing_h)
        if spacing_v and not getattr(element, "_chromonic_is_first_table_row", False):
            # Between-row spacing: no shared flex container across rows
            # (row-groups just stack them in ordinary block flow) to hang
            # a `gap` off, so a real top margin does it instead -- every
            # row except the first (which would double up with the
            # table's own `padding-top`, already the first row's own gap
            # -- see where that's set above). Nothing plays the same role
            # for a *bottom* margin on the last row: the table's
            # margin never applies to a table-row (CSS 2.1 17.4), so
            # repurposing it for the before-gap costs nothing real.
            style["margin"] = [spacing_v, 0.0, 0.0, 0.0]
    elif is_table_cell:
        # CSS 2.1 17.4: margin doesn't apply to a cell either --
        # table-visual-layout-002.xht.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        ancestor = dom._layout_parent(element)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = dom._layout_parent(ancestor)
        if ancestor is not None and getattr(ancestor, "_chromonic_border_collapse", False):
            # CSS 2.1 17.6.2: this cell's box includes half of each of its
            # four collapsed grid lines' winning widths, resolved once for
            # the whole table (`table_layout._resolve_collapsed_table_borders`) regardless
            # of the cell's own declaration -- border-conflict-style-001.xht.
            # A cell outside the resolved grid falls back to halving its own.
            resolved = getattr(ancestor, "_chromonic_collapsed_cell_borders", {}).get(id(element))
            style["border"] = (list(resolved) if resolved is not None else
                               [value / 2.0 if isinstance(value, (int, float)) else value
                                for value in style["border"]])
        column_width = None
        if ancestor is not None:
            column_width = getattr(ancestor, "_chromonic_table_column_widths", {}).get(id(element))
        # CSS 2.1 17.5.5: a cell in a collapsed column lays out at the
        # column's real width, then narrows afterward (`_settle_collapsed_cells_in`)
        # -- column-visibility-001..004.xht.
        collapse = (0.0, 0.0, 0.0)
        collapsed_columns = getattr(ancestor, "_chromonic_table_collapsed_columns", None) if ancestor is not None else None
        if collapsed_columns and column_width is not None:
            position = getattr(ancestor, "_chromonic_table_cell_columns", {}).get(id(element))
            if position is not None:
                span = range(position[0], position[0] + position[1])
                hit = [c for c in span if c in collapsed_columns]
                if hit:
                    uncollapsed = getattr(ancestor, "_chromonic_table_columns_uncollapsed", None) or []
                    spacing_h = getattr(ancestor, "_chromonic_border_spacing", (0.0, 0.0))[0]
                    column_width = (sum(uncollapsed[c] for c in span if c < len(uncollapsed))
                                    + spacing_h * (len(span) - 1))
                    collapse = table_layout._collapse_amounts(span, hit, uncollapsed, spacing_h)
        element._chromonic_cell_collapse = collapse
        if column_width is not None or style["width"] == "auto":
            if column_width is not None:
                # CSS 2.1 17.5.2.2: a cell's own width is only a minimum for
                # its column -- the column (measured with this cell's width
                # already honoured, `table_layout._compute_table_column_widths`) can be
                # wider, and the cell takes that. border-conflict-style-005.xht.
                style["width"] = "auto"
                # flex_grow proportional to the column's intrinsic width (not
                # uniform 1.0) so surplus room favours the column that wants it.
                #
                # CSS 2.1 17.5.2: a column's one width is every cell's border
                # box in it. flex_basis sizes the content box, so the cell's
                # own padding/border comes off the basis here (box-sizing is
                # left alone -- forcing border-box would also make min_height
                # a border-box minimum) -- border-conflict-w-001.xht.
                if style["box_sizing"] == "border-box":
                    basis = column_width
                else:
                    horizontal = [style["padding"][1], style["padding"][3],
                                  style["border"][1], style["border"][3]]
                    basis = (column_width - sum(box_model._numeric_edge(v) for v in horizontal)
                             if all(isinstance(v, (int, float)) for v in horizontal) else column_width)
                # An empty column (max-content 0) still shares surplus width
                # -- anonymous-table-box-width-001.xht. grow=1.0 not something
                # tiny: flexbox only hands out the fraction of free space equal
                # to the grow-factor sum when that sum is below 1.
                # Column's min-content width is this cell's own floor too
                # (content-box, same edge subtraction as the basis).
                column_min = (getattr(ancestor, "_chromonic_table_column_min_widths", {}).get(id(element))
                              if ancestor is not None else None)
                if column_min is not None and style["box_sizing"] != "border-box":
                    horizontal_edges = [style["padding"][1], style["padding"][3],
                                        style["border"][1], style["border"][3]]
                    column_min = (column_min - sum(box_model._numeric_edge(v) for v in horizontal_edges)
                                  if all(isinstance(v, (int, float)) for v in horizontal_edges) else None)
                style.update({"flex_grow": column_width if column_width > 0.0 else 1.0,
                              "flex_shrink": 1.0, "flex_basis": max(0.0, basis),
                              "min_width": max(0.0, column_min) if column_min is not None else 0.0})
                if getattr(ancestor, "_chromonic_table_fixed", False):
                    # CSS 2.1 17.5.2.1: fixed layout resolved every column
                    # exactly -- nothing left to grow or shrink.
                    style.update({"flex_grow": 0.0, "flex_shrink": 0.0, "min_width": 0.0})
            else:
                # Colspan'd, or intrinsic measurement failed -- equal-share fallback.
                style.update({"flex_grow": 1.0, "flex_shrink": 1.0,
                              "flex_basis": 0.0, "min_width": 0.0})
        elif ancestor is not None and getattr(ancestor, "_chromonic_table_fixed", False):
            # A specified-width cell in a fixed % table whose width couldn't
            # resolve up front: rigid, auto cells share the rest.
            style.update({"flex_grow": 0.0, "flex_shrink": 0.0, "min_width": 0.0})
        if collapsed_columns:
            # Overflows by the collapsed widths until `_settle_collapsed_cells_in`
            # narrows it -- nothing may squeeze to make room meanwhile.
            style["flex_shrink"] = 0.0
        # CSS 2.1 17.5.3: a cell's specified height is a minimum too.
        if style["height"] != "auto":
            if style["min_height"] in ("auto", 0.0):
                style["min_height"] = style["height"]
            style["height"] = "auto"



def _setup_table_caption(element, style, computed, parent, is_table_caption: bool) -> None:
    """Body of `build()`'s `is_table_caption` branch, extracted verbatim: pulls a
    caption's margins outward past the table's own border+padding so it spans the
    table wrapper box, CSS 2.1 17.4."""
    if is_table_caption and parent is not None and getattr(parent, "_chromonic_is_table_root", False):
        # CSS 2.1 17.4: a caption belongs to the table wrapper box, spanning
        # its full outer width, not the table box itself -- chromonic has no
        # separate wrapper, so the caption's margins compensate: pulled
        # outward past the table's own border+padding, with the opposite
        # margin pushing the rows back by the same amount so the border+
        # padding still sit between caption and first/last row.
        # basic-css-table-001.xht. An author margin still applies on top; a
        # percentage one is left alone.
        parent_style = getattr(parent, "_chromonic_native_style", None) or {}
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
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    if (style["width"] == "auto" and element._chromonic_tag_name != "body"
            and not flex_grid._is_flex_or_grid_item(element)):
        # Once split, `element` stands in for its CSS 2.1 9.2.1.1 anonymous
        # block box pieces, which always fill their containing block at
        # width:auto regardless of `element`'s own nominal display (Taffy's
        # "auto" means shrink-to-fit, not fill) -- wpt/css/CSS2/linebox/
        # inline-box-001.xht. `<body>` is excluded: it has its own more
        # accurate root-width machinery (`_constrain_root_to_document_element`/
        # `_apply_root_margin_offset`) that a plain pct(1.0) here would
        # override with a wrong viewport-relative answer -- wpt/css/CSS2/
        # normal-flow/block-in-inline-empty-001.xht. box-sizing:border-box
        # alongside it so padding/border don't stick out past the container.
        style["width"] = ("pct", 1.0)
        style["box_sizing"] = "border-box"
    owner_cache = element.__dict__.setdefault("_chromonic_split_plan_owners", {})
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
            plan_style["width"] = ("pct", 1.0) if style["display"] == "block" else "auto"
            measure_key = ("inline-context", tuple(element._chromonic_paint_style.items()), tuple(
                ("break", id(run["element"])) if run.get("break") else
                ("escapee", id(run["element"])) if run.get("escapee") else
                (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
                 tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
                 run["leading"], run["trailing"], run["top_edge"], run["atomic_width"],
                 run.get("margin_start", 0.0))
                for run in plan.runs
            ))
            if projection is not None and not projection.measure_changed(owner, measure_key):
                plan = owner._chromonic_inline_plan
            owner._chromonic_inline_plan = plan
            owner._chromonic_native_style = plan_style
            measure = (plan.measure
                       if projection is None or projection.measure_changed(owner, measure_key) else None)
            piece_id = (projection.upsert(owner, plan_style, [], measure, measure_key)
                        if projection else tree.new_text_leaf(plan_style, measure))
            node_map[piece_id] = owner
            # An "escapee" run (an out-of-flow element mixed into this
            # segment) marks its static-position slot; its real subtree is
            # built here and added to `escapees`, landing one edge from its
            # real containing block.
            for run in plan.runs:
                if run.get("escapee"):
                    escapee_child = run["element"]
                    escapee_is_cb = box_model._establishes_containing_block(run["style"])
                    escapee_id = build(
                        tree, escapee_child, node_map, computed=run["computed"], style_obj=run["style"],
                        computed_cache=computed_cache, is_containing_block=escapee_is_cb,
                        escapees=escapees if not is_containing_block else own_escapees,
                        reuse_styles=reuse_styles, projection=projection,
                    )
                    (own_escapees if is_containing_block else escapees).append(escapee_id)
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



def _build_from_inline_plan(tree, element, style, inline_plan, css_display_value, *, computed_cache,
        escapees, own_escapees, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `inline_plan is not None` branch: a single measured text
    leaf from `inline_formatting._InlineFormattingPlan`, plus a recursively-built child per
    escapee run."""
    element._chromonic_has_layout_children = True
    # `style["display"]` is already Taffy-mapped to "block", so it can't
    # distinguish a genuine block element from an inline/inline-block one
    # built as an atomic flex item elsewhere -- `css_display_value` (the
    # real pre-mapping computed display) does.
    if css_display_value == "block" and style["width"] == "auto" and not flex_grid._is_flex_or_grid_item(element):
        style["width"] = ("pct", 1.0)
        # A real block's width:auto shrinks to leave room for its own
        # padding/border; Taffy has no calc(100% - padding), so a content-box
        # pct(1.0) lets padding/border stick out past the container instead
        # -- a `<div style="padding-left:2em"><span>|</span></div>` overflowed
        # by exactly its own padding. box-sizing:border-box makes pct(1.0)
        # mean the border-box total instead, matching a real block.
        style["box_sizing"] = "border-box"
    measure_key = ("inline-context", tuple(element._chromonic_paint_style.items()), tuple(
        ("break", id(run["element"])) if run.get("break") else
        ("escapee", id(run["element"])) if run.get("escapee") else
        (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
         tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
         run["leading"], run["trailing"], run["top_edge"], run["atomic_width"],
         run.get("margin_start", 0.0))
        for run in inline_plan.runs
    ))
    if projection is not None and not projection.measure_changed(element, measure_key):
        # Taffy retains the callback bound to the existing plan. Publish
        # that plan's placements too, including when cached layout is used.
        inline_plan = element._chromonic_inline_plan
    element._chromonic_inline_plan = inline_plan
    measure = (inline_plan.measure
               if projection is None or projection.measure_changed(element, measure_key) else None)
    node_id = (projection.upsert(element, style, [], measure, measure_key)
               if projection else tree.new_text_leaf(style, measure))
    # An out-of-flow element mixed into this inline content is only a
    # marker run (its static position); its real box is built here and
    # handed to the nearest holding ancestor -- this leaf has no Taffy
    # children of its own (abspos-inline-001.xht).
    for run in inline_plan.runs:
        if run.get("escapee"):
            escapee_id = build(
                tree, run["element"], node_map, computed=run["computed"], style_obj=run["style"],
                computed_cache=computed_cache,
                is_containing_block=box_model._establishes_containing_block(run["style"]),
                escapees=escapees if escapees is not None else own_escapees,
                reuse_styles=reuse_styles, projection=projection,
            )
            (escapees if escapees is not None else own_escapees).append(escapee_id)
    return node_id



def _build_inline_flex_row(tree, element, style, computed, inline_items, *, computed_cache,
        is_containing_block, own_escapees, escapees, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `elif inline_items:` branch: the flex-row approximation of
    inline flow for mixed text/element content that inline_formatting._make_inline_formatting_plan
    declined to build a real plan for."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    # paint.py falls back to raw textContent when it believes there are no
    # layout children -- a ::before/::after-only element must force this
    # True or paint draws the text twice.
    element._chromonic_has_layout_children = True
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    style["align_items"] = "baseline"
    # CSS 2.1 16.2 text-align: justify_content is a direct equivalent for a
    # single unjustified line (flex-wrap repeats it per wrapped row, matching
    # per-line text-align) -- wpt/css/CSS2/visudet/line-height-203.html.
    # justify/start/end not remapped: no simple justify-content distributes
    # text like real justification, and start/end need direction awareness
    # this approximation doesn't have (`_InlineFormattingPlan._apply_text_align`
    # makes the same physical-only simplification).
    text_align_value = (getattr(computed, "textAlign", "") or "").strip().lower()
    if dom._element_direction(element, computed) == "rtl":
        # CSS 2.1 9.10: an rtl line packs right-to-left against the right
        # edge -- flexbox-mbp-horiz-001-rtl.xhtml. Taffy's row-reverse is
        # exactly that; text-align maps with physical sides swapped.
        style["flex_direction"] = "row-reverse"
        if text_align_value in ("left",):
            style["justify_content"] = "flex-end"
        elif text_align_value == "center":
            style["justify_content"] = "center"
    elif text_align_value in ("right", "end"):
        style["justify_content"] = "flex-end"
    elif text_align_value == "center":
        style["justify_content"] = "center"
    font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
    paint_style = element._chromonic_paint_style
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = inline_formatting._parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    one = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
    spaced = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
    space_width = max(0.0, spaced - 2 * one)
    normal_child_ids = []
    fragments = []
    # Document-order record of every real Taffy child this row got (escapees
    # excluded) -- `_fix_flex_row_baseline_alignment` needs this exact
    # membership/order since Taffy's own baseline placement can make `y`
    # alone untrustworthy for "which wrapped row".
    row_members = []
    # CSS 2.1 9.5: a float mixed into running text is pulled out of normal
    # flow, but this approximation still places it as an ordinary flex-row
    # member for a real content-sized box and a line to sit on --
    # `_fix_inline_float_position` corrects only its final x afterward. Text
    # doesn't (yet) reflow around the float's rectangle -- logged in PLAN.md.
    inline_floats = []
    for item_index, (kind, item, text, child_computed, child_style) in enumerate(inline_items):
        if kind == "element":
            # An absolutely-positioned item counts as "inline" here
            # regardless of its real display -- still escapes to its real
            # containing-block ancestor when `element` isn't one.
            child_is_cb = box_model._establishes_containing_block(child_style)
            if box_model._is_absolutely_positioned(child_style) and not is_containing_block:
                child_id = build(
                    tree, item, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=escapees,
                    reuse_styles=reuse_styles, projection=projection,
                )
                escapees.append(child_id)
            else:
                if getattr(item, "_chromonic_leading_collapsed_space", False) and space_width > 0.0:
                    # Collapsed whitespace before this element is a real
                    # space on the line -- inline-table-001.xht. A spacer
                    # leaf, since the element's own box can't carry a margin
                    # it didn't declare.
                    spacers = element.__dict__.setdefault("_chromonic_inline_spacers", {})
                    spacer = spacers.get(id(item))
                    if spacer is None:
                        spacer = spacers[id(item)] = anonymous_boxes._InlineSpacer(item)
                    spacer_style = inline_formatting._inline_text_style(style)
                    spacer_style.update({"width": space_width, "height": 0.0, "flex_shrink": 0.0})
                    spacer_id = (projection.upsert(spacer, spacer_style, [], None, None)
                                 if projection else tree.new_leaf(spacer_style))
                    node_map[spacer_id] = spacer
                    normal_child_ids.append(spacer_id)
                # Same fix as `_approximate_inline_flow`'s `_chromonic_no_flex_shrink`:
                # an explicit-width row member would otherwise shrink to fit
                # the line under flexbox's default flex-shrink:1. Always set
                # (not just when true) so a stale flag can't outlive a reused
                # element's earlier pass.
                item._chromonic_no_flex_shrink = (
                    not box_model._is_floated(child_computed) and isinstance(child_style.width, Length))
                normal_child_ids.append(build(
                    tree, item, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                    reuse_styles=reuse_styles, projection=projection,
                ))
                if box_model._is_floated(child_computed):
                    # CSS 2.1 10.8.1: baseline alignment only considers
                    # in-flow boxes -- `_fix_inline_float_position` positions
                    # a float afterward.
                    inline_floats.append(item)
                else:
                    row_members.append(item)
            if isinstance(item, dom._PseudoElement):
                # Not a real DOM child -- reaches paint only via this
                # side-channel list, same as retained text fragments.
                fragments.append(item)
            continue
        fragment_style = inline_formatting._inline_text_style(style)
        raw = getattr(getattr(item, "source", None), "textContent", "") or ""
        leading = space_width if (raw[:1].isspace() or
                                  getattr(item, "_chromonic_leading_collapsed_space", False)) else 0.0
        has_later_in_flow_item = any(
            later_kind == "text" or not box_model._is_absolutely_positioned(later_style)
            for later_kind, _later_item, _later_text, _later_computed, later_style
            in inline_items[item_index + 1:]
        )
        trailing = space_width if raw[-1:].isspace() and has_later_in_flow_item else 0.0
        fragment_style["margin"] = [0.0, trailing, 0.0, leading]
        item._chromonic_native_style = fragment_style
        item._chromonic_paint_style = element._chromonic_paint_style
        measure_key = _measure_key(item._chromonic_paint_style, text)
        measure = (inline_formatting._make_measure(item._chromonic_paint_style, text, item)
                   if projection is None or projection.measure_changed(item, measure_key) else None)
        child_id = (projection.upsert(item, fragment_style, [], measure, measure_key)
                    if projection else tree.new_text_leaf(fragment_style, measure))
        node_map[child_id] = item
        normal_child_ids.append(child_id)
        fragments.append(item)
        row_members.append(item)
    element._chromonic_inline_fragments = fragments
    element._chromonic_flex_row_members = row_members
    element._chromonic_inline_floats = inline_floats
    all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
    node_id = (projection.upsert(element, style, all_child_ids, None, None)
               if projection else tree.new_with_children(style, all_child_ids))
    return node_id



def _build_block_children(tree, element, style, computed, children, *, computed_cache, is_containing_block,
        is_table_row, own_escapees, escapees, reuse_styles, projection, node_map) -> int:
    """Body of build()'s `elif children:` branch: ordinary block-flow children, each
    recursively built, grouped into rowspan placeholders (a table row) or inline
    runs (everything else)."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    _approximate_inline_flow(
        element,
        style,
        [child for child, _computed, _child_style in children],
        [child_computed for _child, child_computed, _child_style in children],
        [child_style for _child, _computed, child_style in children],
        computed,
    )
    normal_child_ids = []
    normal_entries = []
    for child, child_computed, child_style in children:
        child_is_cb = box_model._establishes_containing_block(child_style)
        if box_model._is_absolutely_positioned(child_style) and not is_containing_block:
            # `element` isn't a valid containing block -- build the child
            # normally, hand its node id to the real ancestor `escapees` belongs to.
            child_id = build(
                tree, child, node_map, computed=child_computed, style_obj=child_style,
                computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=escapees,
                reuse_styles=reuse_styles, projection=projection,
                is_grid_item=style["display"] == "grid",
            )
            escapees.append(child_id)
        else:
            child_id = build(
                tree, child, node_map, computed=child_computed, style_obj=child_style,
                computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                reuse_styles=reuse_styles, projection=projection,
                is_grid_item=style["display"] == "grid",
            )
            normal_child_ids.append(child_id)
            normal_entries.append((child, child_style, child_id))
    if normal_entries and is_table_row:
        # Cells are never inline-level, so the inline-run grouping
        # below has nothing to do for a row; its slots under a
        # `rowspan` need holding instead.
        normal_child_ids = table_layout._row_child_ids_with_rowspan_placeholders(
            tree, element, normal_entries, style, node_map, projection)
    elif normal_entries:
        normal_child_ids = inline_formatting._group_inline_element_runs(
            tree, element, normal_entries, style, node_map, projection,
        )
    all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
    node_id = (projection.upsert(element, style, all_child_ids, None, None)
               if projection else tree.new_with_children(style, all_child_ids))
    return node_id



def _build_br_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s `tag_name == "br"` branch: a zero-width, one-line-height
    strut leaf (CSS 2.1 9.2.2 -- a `<br>` never generates an ordinary block box)."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    paint_style = element._chromonic_paint_style
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
    # The break's own inline box (Chrome's client rect when it shares a
    # line with floats -- see the `<br>` branch of `_fix_float_flow_after_block_sibling`).
    element.__dict__["_chromonic_br_glyph_height"] = ascent + descent
    element.__dict__.pop("_chromonic_br_flow_bottom", None)  # stale from an earlier pass
    element._chromonic_text_lines = []
    node_id = (projection.upsert(element, style, [], None, None)
               if projection else tree.new_leaf(style))
    return node_id



def _build_replaced_leaf(tree, element, style, tag_name, *, projection) -> int:
    """Body of build()'s img/canvas/svg/iframe branch: a replaced-element leaf sized
    by its own intrinsic-size resolver."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    if tag_name == "img":
        replaced_elements._apply_image_intrinsic_size(style, element)
    elif tag_name == "canvas":
        replaced_elements._apply_canvas_intrinsic_size(style, element)
    elif tag_name == "iframe":
        replaced_elements._apply_iframe_intrinsic_size(style, element)
    else:
        replaced_elements._apply_svg_intrinsic_size(style, element)
    element._chromonic_text_lines = []
    img_measure = element.__dict__.get("_chromonic_img_measure")
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
    selected option's display text, or an empty leaf."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    text = replaced_elements._select_display_text(element)
    if text:
        measure_key = _measure_key(element._chromonic_paint_style, text)
        measure = (inline_formatting._make_measure(element._chromonic_paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def _build_form_control_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s input/textarea branch: a measured text leaf for its display
    text, or an empty leaf."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    text = replaced_elements._form_control_display_text(element)
    if text:
        measure_key = _measure_key(element._chromonic_paint_style, text)
        measure = (inline_formatting._make_measure(element._chromonic_paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    return node_id



def _build_text_leaf(tree, element, style, *, projection) -> int:
    """Body of build()'s final `else` branch (an ordinary element with no recognised
    special handling): a measured text leaf for its own text content, or an empty leaf."""
    element.__dict__.pop("_chromonic_inline_plan", None)
    element._chromonic_inline_fragments = []
    text = dom._own_text(element)
    if text:
        measure_key = _measure_key(element._chromonic_paint_style, text)
        measure = (inline_formatting._make_measure(element._chromonic_paint_style, text, element)
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    else:
        element._chromonic_text_lines = []
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
    element.__dict__.pop("_chromonic_flattened_inline", None)  # given a box of its own this pass
    if computed_cache is None:
        computed_cache = {}
    if computed is None or style_obj is None:
        computed, style_obj = dom._describe(element, computed_cache, reuse_styles=reuse_styles)
    style = getattr(element, "_chromonic_native_style", None) if reuse_styles else None
    if style is None:
        # Measured before this element's own style is published: the
        # scratch-tree measurement re-runs build() on this element and
        # overwrites its per-pass attributes, which the real pass rewrites anyway.
        intrinsic_width = replaced_elements._resolve_intrinsic_width_keyword(element, computed, style_obj, computed_cache)
        style = style_bridge.to_dict(style_obj)
        if intrinsic_width is not None:
            style["width"] = intrinsic_width
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
        element._chromonic_native_style = style
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
        parent_native = (dom._layout_parent(element).__dict__.get("_chromonic_native_style") or {})
        if parent_native.get("display") == "flex":
            main = "height" if (parent_native.get("flex_direction") or "row").startswith("column") else "width"
            style["flex_basis"] = "auto"
            style[main] = "auto"
    if isinstance(style.get("flex_basis"), tuple) and flex_grid._is_flex_or_grid_item(element):
        # CSS Flexbox 9.2.3 B: a percentage flex-basis against an indefinite
        # main size is treated as content, ignoring the item's own height
        # for its base size -- flex-basis-010.html.
        parent_native = (dom._layout_parent(element).__dict__.get("_chromonic_native_style") or {})
        if (parent_native.get("display") == "flex"
                and (parent_native.get("flex_direction") or "row").startswith("column")
                and parent_native.get("height") == "auto"):
            style["flex_basis"] = "auto"
            style["height"] = "auto"
    own_escapees = [] if is_containing_block else escapees
    tag_name = (getattr(element, "tagName", "") or "").lower()
    element._chromonic_tag_name = tag_name
    is_genuinely_inline = (
        tag_name not in box_model._REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and box_model._trusts_computed_inline(element, tag_name)
        # CSS Flexbox 4 / Grid 6.1: a flex/grid item's display is blockified.
        and not flex_grid._is_flex_or_grid_item(element)
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
    if getattr(element, "_chromonic_force_full_row_width", False) and style["width"] == "auto":
        # Set by `_approximate_inline_flow` for a non-floated, non-inline
        # block sibling -- an explicit author width is left alone; only
        # auto needs correcting, since real CSS block flow always fills
        # the containing block.
        style["flex_basis"] = ("pct", 1.0)
        # Flexbox's min-width:auto floor doesn't apply to this (non-flex)
        # element -- left in place, Taffy's shrink algorithm shrank the
        # margin to force a fit instead of letting the box overflow like
        # real block layout would: a 425px child in a 500px flex-wrap row
        # resolved width 425/margin 75 instead of width 400/margin 100.
        # Only when the author didn't set their own min-width.
        if style["min_width"] == "auto":
            style["min_width"] = 0.0
        # Same content-box-vs-border-box fix the other pct(1.0) substitutes
        # need -- content-box flex-basis:100% would let padding/border
        # stick out past the container.
        style["box_sizing"] = "border-box"
    if getattr(element, "_chromonic_no_flex_shrink", False):
        # Set by `_approximate_inline_flow` for any explicit-width inline-
        # level child (floated or not) -- flexbox's plain default `flex-
        # shrink:1` would otherwise let this row-packed item give up its
        # specified width to fit the flex line, which nothing in real
        # inline flow (or CSS 2.1 10.3.5 for a float specifically) ever does.
        style["flex_shrink"] = 0.0
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
        element._chromonic_table_bottom_captions = [entry[0] for entry in captions_bottom]
        element._chromonic_table_captions = [entry[0] for entry in captions_top + captions_bottom]
        # A caption's minimum width floors the shrink-to-fit table wrapper
        # too -- anonymous-table-box-width-001.xht. Consulted by
        # `_fix_table_shrink_to_fit_width`.
        caption_min = 0.0
        for caption, caption_computed, caption_style in captions_top + captions_bottom:
            width = style_bridge._len(caption_style.width)
            if isinstance(width, (int, float)):
                # Border box -- table-caption-horizontal-alignment-001.xht.
                if getattr(caption_style.boxSizing, "value", "") != "border-box":
                    width += sum(_fontmetrics.parse_length(getattr(caption_computed, name, None), default=0.0)
                                 for name in ("paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"))
                caption_min = max(caption_min, width)
            else:
                caption_min = max(caption_min, replaced_elements._measure_min_content_width(caption, computed_cache) or 0.0)
        element._chromonic_table_caption_min_width = caption_min
        if (caption_min > 0.0 and isinstance(style["width"], (int, float))
                and style["box_sizing"] == "border-box" and caption_min > style["width"]):
            # A caption wider than the table widens the table box itself --
            # table-anonymous-block-003.xht.
            style["width"] = caption_min
    if is_table_row and getattr(element, "_chromonic_row_rtl", False):
        children = children[::-1]  # see the `is_table_row` branch above
    element._chromonic_has_layout_children = bool(children)
    if tag_name == "button":
        replaced_elements._apply_button_intrinsic_width(style, element)
    # Replaced/control elements run their own dedicated branch below -- CSS
    # generated content doesn't apply to them.
    has_pseudo = tag_name not in dom._NO_GENERATED_CONTENT_TAGS and (
        getattr(element, "_chromonic_before_pseudo", None) is not None
        or getattr(element, "_chromonic_after_pseudo", None) is not None
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
            escapees=escapees, own_escapees=own_escapees, reuse_styles=reuse_styles,
            projection=projection, node_map=node_map,
        )
    elif inline_items:
        node_id = _build_inline_flex_row(
            tree, element, style, computed, inline_items, computed_cache=computed_cache,
            is_containing_block=is_containing_block, own_escapees=own_escapees, escapees=escapees,
            reuse_styles=reuse_styles, projection=projection, node_map=node_map,
        )
    elif children:
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
    elif tag_name in ("img", "canvas", "svg", "svg:svg", "iframe"):
        node_id = _build_replaced_leaf(tree, element, style, tag_name, projection=projection)
    elif tag_name == "select":
        node_id = _build_select_leaf(tree, element, style, projection=projection)
    elif tag_name in ("input", "textarea"):
        node_id = _build_form_control_leaf(tree, element, style, projection=projection)
    else:
        node_id = _build_text_leaf(tree, element, style, projection=projection)

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
