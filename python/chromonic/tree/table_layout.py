from __future__ import annotations

import dataclasses

from domonic import _fontmetrics
from domonic.layout import LayoutBox

from .. import fonts, style_bridge
from . import anonymous_boxes, box_model, dom, geometry, inline_formatting, replaced_elements




def _collapse_amounts(span, hit, uncollapsed, spacing_h: float) -> tuple:
    """`(pre_move, shrink, shift)` for a row item laid out over grid
    columns `span`, of which `hit` are `visibility: collapse`: each
    collapsed column takes its width and the border-spacing gap before
    it (the gap after it, for the table's first column). A gap before
    the item's own first column moves the item itself up (`pre_move`);
    the rest narrows it (`shrink`); everything after it in the row moves
    by the total (`shift`). Chrome on column-visibility-004.xht: a cell
    spanning a collapsed 100px column and a visible one is 102px wide --
    the visible column plus the gap between them -- and starts where
    the collapsed column did, less the gap."""
    lost = sum(uncollapsed[c] for c in hit if c < len(uncollapsed))
    gaps = spacing_h * len(hit)
    pre_move = spacing_h if (span and span[0] in hit and span[0] > 0) else 0.0
    return pre_move, lost + gaps - pre_move, lost + gaps



def _row_child_ids_with_rowspan_placeholders(tree, row, entries, row_style, node_map, projection) -> list:
    """The Taffy children of a table row: its cells in layout order, with
    a `anonymous_boxes._RowspanPlaceholder` leaf ahead of any cell whose grid column
    isn't the next one -- the gap is covered by a cell spanning down
    from an earlier row (table-height-algorithm-010.xht: a `rowspan=10`
    first cell, every later row's only cell sits in column 1). A
    placeholder carries its column's basis/grow/min so it shares the
    row's width exactly as the spanning cell does in its own row."""
    table = dom._layout_parent(row)
    while table is not None and not getattr(table, "_chromonic_is_table_root", False):
        table = dom._layout_parent(table)
    if table is None:
        return [child_id for _child, _style, child_id in entries]
    positions = {id(cell): (c, colspan)
                 for cell, _r, c, _rowspan, colspan in (getattr(table, "_chromonic_table_grid_cells", None) or ())}
    if not any(id(child) in positions for child, _style, _id in entries):
        return [child_id for _child, _style, child_id in entries]
    columns_max = getattr(table, "_chromonic_table_columns_max", None) or []
    columns_min = getattr(table, "_chromonic_table_columns_min", None) or []
    fixed = getattr(table, "_chromonic_table_fixed", False)
    collapsed = getattr(table, "_chromonic_table_collapsed_columns", None) or set()
    column_count = max(len(getattr(table, "_chromonic_table_columns", None) or ()), len(columns_max))
    rtl = getattr(row, "_chromonic_row_rtl", False)
    holders = row.__dict__.setdefault("_chromonic_rowspan_placeholders", {})

    def placeholder(column: int):
        holder = holders.get(column)
        if holder is None:
            holder = holders[column] = anonymous_boxes._RowspanPlaceholder(row, column)
        width = float(columns_max[column]) if column < len(columns_max) else 0.0
        minimum = float(columns_min[column]) if column < len(columns_min) else 0.0
        holder.collapse = (0.0, 0.0, 0.0)
        if column in collapsed:
            # Laid out at the column's real width like a cell there, and
            # narrowed to nothing afterwards (see the cell branch of
            # `build()` on `visibility: collapse` columns).
            uncollapsed = getattr(table, "_chromonic_table_columns_uncollapsed", None) or []
            width = float(uncollapsed[column]) if column < len(uncollapsed) else 0.0
            spacing_h = getattr(table, "_chromonic_border_spacing", (0.0, 0.0))[0]
            holder.collapse = _collapse_amounts(range(column, column + 1), [column], uncollapsed, spacing_h)
        style = inline_formatting._inline_text_style(row_style)
        style.update({"height": 0.0, "flex_basis": max(0.0, width), "min_width": max(0.0, minimum),
                      "flex_grow": 0.0 if fixed else (width if width > 0.0 else 1.0),
                      "flex_shrink": 0.0 if (fixed or collapsed) else 1.0})
        node = projection.upsert(holder, style, [], None, None) if projection else tree.new_leaf(style)
        node_map[node] = holder
        return node

    ids: list = []
    expected = column_count - 1 if rtl else 0
    for child, _style, child_id in entries:
        position = positions.get(id(child))
        if position is not None:
            column, colspan = position
            if rtl:
                for slot in range(expected, column + colspan - 1, -1):
                    ids.append(placeholder(slot))
                expected = column - 1
            else:
                for slot in range(expected, column):
                    ids.append(placeholder(slot))
                expected = column + colspan
        ids.append(child_id)
    # Columns after the row's last cell -- spanned from above, or simply
    # missing (a ragged row) -- stay empty: a cell never grows into them
    # (empty-cells-applies-to-016.xht: a two-column table whose first row
    # has one cell keeps that cell at its column's width).
    for slot in (range(expected, -1, -1) if rtl else range(expected, column_count)):
        ids.append(placeholder(slot))
    return ids



def _is_table_row_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() == "table-row"



def _is_table_cell_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() == "table-cell"



def _is_table_root_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() in ("table", "inline-table")



_ROW_GROUP_TAG_KIND = {"thead": "header", "tfoot": "footer", "tbody": "body"}



def _row_group_kind(tag: str, computed) -> "str | None":
    """`None` if `tag`/`computed` isn't a table row-group at all (a plain
    wrapper `walk` should recurse through transparently); otherwise which
    of the three CSS 2.1 17.5.3 row-group buckets it is."""
    if tag in _ROW_GROUP_TAG_KIND:
        return _ROW_GROUP_TAG_KIND[tag]
    display = (getattr(computed, "display", "") or "").strip().lower()
    if display == "table-header-group":
        return "header"
    if display == "table-footer-group":
        return "footer"
    if display == "table-row-group":
        return "body"
    return None



def _table_rows(table_element, computed_cache) -> list:
    """Every table-row belonging to `table_element`'s own table -- a
    literal `<tr>`, or any element computing `display:table-row` --
    reached transparently through any depth of row-group wrapper
    (`table-row-group`/`-header-group`/`-footer-group`, or a literal
    `<tbody>`/`<thead>`/`<tfoot>`). Stops at a nested table's own
    boundary -- its rows belong to it, not this one.

    CSS 2.1 17.5.3: header/body/footer row-groups always *display* in that
    fixed order regardless of source order -- a `<tfoot>` authored first
    still renders last. A row with no row-group ancestor counts as an
    implicit body row. Order within each bucket stays DOM order."""
    buckets: dict[str, list] = {"header": [], "body": [], "footer": []}
    # Only the first header group and first footer group get special
    # placement -- any further one is an ordinary body group in source
    # order, as Chrome does -- border-spacing-applies-to-010.xht.
    claimed: set = set()

    def walk(node, kind: str):
        for child in anonymous_boxes._normalized_child_nodes(node, computed_cache):
            if not dom._is_element(child):
                continue
            tag = (getattr(child, "tagName", "") or "").lower()
            if tag in dom._NON_RENDERING_TAGS:
                continue
            child_computed, child_style = dom._describe(child, computed_cache)
            if not dom._renders(child_style):
                continue
            if box_model._is_absolutely_positioned(child_style):
                # CSS 2.1 9.7: an absolutely/fixed positioned element's
                # display blockifies regardless of its specified value --
                # pulled entirely out of the row/row-group structure, not
                # even walked transparently. top-applies-to-001.xht.
                continue
            if tag == "tr" or _is_table_row_display(child_computed):
                buckets[kind].append(child)
                continue
            if tag == "table" or _is_table_root_display(child_computed):
                continue  # a nested table's own rows aren't this table's
            group_kind = _row_group_kind(tag, child_computed)
            if group_kind in ("header", "footer"):
                if group_kind in claimed:
                    group_kind = "body"
                else:
                    claimed.add(group_kind)
            walk(child, group_kind if group_kind is not None else kind)

    walk(table_element, "body")
    return buckets["header"] + buckets["body"] + buckets["footer"]



def _row_cells(row_element, computed_cache) -> list:
    """Every table-cell directly inside `row_element` -- a literal `<td>`/
    `<th>`, or any element computing `display:table-cell`. CSS 2.1
    17.2.1 only ever generates a cell as a row's *direct* child (an
    anonymous cell wraps other content instead), so this deliberately
    doesn't recurse."""
    cells: list = []
    for child in anonymous_boxes._normalized_child_nodes(row_element, computed_cache):
        if not dom._is_element(child):
            continue
        tag = (getattr(child, "tagName", "") or "").lower()
        if tag in dom._NON_RENDERING_TAGS:
            continue
        child_computed, child_style = dom._describe(child, computed_cache)
        if not dom._renders(child_style):
            continue
        if box_model._is_absolutely_positioned(child_style):
            continue  # CSS 2.1 9.7: blockified, not a real cell -- see `_table_rows`'s walk()
        if tag in ("td", "th") or _is_table_cell_display(child_computed):
            cells.append(child)
    return cells



def _cell_span(cell, attr: str) -> int:
    raw = cell.getAttribute(attr) if hasattr(cell, "getAttribute") else None
    try:
        return max(1, int(raw)) if raw else 1
    except ValueError:
        return 1



def _table_grid(rows, computed_cache) -> tuple:
    """`([(cell, row_index, col_index, rowspan, colspan), ...], column_count)`
    -- CSS 2.1 17.5.1's grid occupancy for `rows` (already in display
    order): a cell lands in the first slot of its row not already claimed
    by a `rowspan` from an earlier row, then claims `rowspan` x `colspan`
    slots of its own. `rowspan` is clamped to the rows that actually
    exist (HTML's `rowspan="0"`, "to the end of the row group", is read
    as 1 -- rare enough not to model)."""
    occupied: set = set()
    cells: list = []
    column_count = 0
    # CSS 2.1 17.5: a cell never spans past its own row group --
    # table-visual-layout-016.xht's `rowspan=2` on a group's last row
    # spans that row alone; the next group's row keeps column 0.
    groups = [id(dom._layout_parent(row)) for row in rows]
    for row_index, row in enumerate(rows):
        col = 0
        group_end = row_index
        while group_end + 1 < len(rows) and groups[group_end + 1] == groups[row_index]:
            group_end += 1
        for cell in _row_cells(row, computed_cache):
            while (row_index, col) in occupied:
                col += 1
            colspan = _cell_span(cell, "colspan")
            rowspan = min(_cell_span(cell, "rowspan"), group_end - row_index + 1)
            for r in range(row_index, row_index + rowspan):
                for c in range(col, col + colspan):
                    occupied.add((r, c))
            cells.append((cell, row_index, col, rowspan, colspan))
            column_count = max(column_count, col + colspan)
            col += colspan
    return cells, column_count



def _table_columns(table_element, computed_cache, column_count: int) -> list:
    """`[(column_element | None, column_group_element | None)]`, one entry
    per grid column: the `<col>`/`display:table-column` (and enclosing
    `<colgroup>`/`table-column-group`) that styles it, per CSS 2.1 17.2.1's
    `span` rules -- a `<colgroup>` with no `<col>` children spans its own
    `span` columns itself. Columns beyond `column_count` (declared but
    holding no cell) are dropped; missing ones are `(None, None)`."""
    columns: list = []

    def add(column, group, span):
        for _ in range(span):
            columns.append((column, group))

    for child in dom._child_nodes(table_element):
        if not dom._is_element(child):
            continue
        tag = (getattr(child, "tagName", "") or "").lower()
        child_computed, child_style = dom._describe(child, computed_cache)
        if box_model._is_absolutely_positioned(child_style):
            continue  # CSS 2.1 9.7: blockified, no longer a column (top-applies-to-005.xht)
        display = (getattr(child_computed, "display", "") or "").strip().lower()
        if tag == "colgroup" or display == "table-column-group":
            cols = [
                node for node in dom._child_nodes(child)
                if dom._is_element(node) and (
                    (getattr(node, "tagName", "") or "").lower() == "col"
                    or (getattr(dom._describe(node, computed_cache)[0], "display", "") or "").strip().lower()
                    == "table-column")
            ]
            if cols:
                for col in cols:
                    add(col, child, _cell_span(col, "span"))
            else:
                add(None, child, _cell_span(child, "span"))
        elif tag == "col" or display == "table-column":
            add(child, None, _cell_span(child, "span"))
    if column_count is None:
        return columns  # every declared column, untrimmed
    while len(columns) < column_count:
        columns.append((None, None))
    return columns[:column_count]



# CSS 2.1 17.6.2.1 border conflict resolution, steps 3 and 4: at equal
# width, style decides (`double` strongest, `inset` weakest); at equal
# style, the element type decides (a cell beats its row beats the row
# group beats the column beats the column group beats the table).
_BORDER_STYLE_PRIORITY = {"double": 8, "solid": 7, "dashed": 6, "dotted": 5,
                          "ridge": 4, "outset": 3, "groove": 2, "inset": 1}

_BORDER_ORIGIN_PRIORITY = {"cell": 6, "row": 5, "row-group": 4, "column": 3,
                           "column-group": 2, "table": 1}

_BORDER_SIDE_ATTR = {"top": "Top", "right": "Right", "bottom": "Bottom", "left": "Left"}



def _border_candidate(computed, side: str, origin: str) -> tuple:
    """`(style, width_px, origin_priority)` for one element's own border
    on `side` -- one contender for a collapsed grid line."""
    attr = _BORDER_SIDE_ATTR[side]
    border_style = (getattr(computed, f"border{attr}Style", None) or "none").strip().lower()
    width = _fontmetrics.parse_length(getattr(computed, f"border{attr}Width", None), default=0.0)
    return (border_style, width, _BORDER_ORIGIN_PRIORITY[origin])



def _resolve_collapsed_border(candidates) -> float:
    """The used width of one collapsed grid-line segment, CSS 2.1 17.6.2.1:
    any `hidden` contender suppresses the whole segment; `none` contenders
    never win (a segment nobody styles has no border); otherwise the
    widest wins, then the strongest style, then the strongest origin.
    Only the winning *width* matters for geometry -- which color/style
    actually paints is a separate concern."""
    if any(style == "hidden" for style, _width, _origin in candidates):
        return 0.0
    real = [c for c in candidates if c[0] != "none" and c[1] > 0.0]
    if not real:
        return 0.0
    return max(real, key=lambda c: (c[1], _BORDER_STYLE_PRIORITY.get(c[0], 0), c[2]))[1]



def _resolve_collapsed_table_borders(table_element, table_computed, rows, cells, column_count,
                                     computed_cache, rtl: bool = False) -> tuple:
    """CSS 2.1 17.6.2: every horizontal and vertical grid line of a
    `border-collapse:collapse` table, resolved segment by segment
    (`_resolve_collapsed_border`) from every element whose border meets
    it -- the two cells either side, the row(s) whose edge it is, a row
    group's edge, the column(s) either side, a column group's edge, and
    the table's own border at the perimeter. Rows/row groups contribute
    their left/right borders only at the table's left/right edges, and
    columns/column groups their top/bottom only at the top/bottom edges,
    exactly as the spec's grid model has them.

    Returns `({id(cell): (top, right, bottom, left)}, (top, right, bottom,
    left))`: each cell's *half* of the winning width on each of its four
    edges (the collapsed line straddles the grid edge, so each side's box
    only ever includes half -- a cell spanning several segments takes the
    widest of them), plus the widest winner along each table perimeter.
    An empty grid (no rows or no cells at all) has no cells to resolve
    against, so its perimeter is just the table's own border."""
    row_count = len(rows)
    if row_count == 0 or column_count == 0:
        return {}, tuple(_resolve_collapsed_border([_border_candidate(table_computed, side, "table")])
                         for side in ("top", "right", "bottom", "left"))
    horizontal = [[[] for _ in range(column_count)] for _ in range(row_count + 1)]
    vertical = [[[] for _ in range(column_count + 1)] for _ in range(row_count)]

    # Vertical grid lines are numbered in *logical* column order (line 0
    # before column 0). In an `rtl` table column 0 is the rightmost, so a
    # box's physical left border meets the line at the logical *end* of
    # its span and its right border the line at the logical start.
    def left_line(start, end):
        return end if rtl else start

    def right_line(start, end):
        return start if rtl else end

    for cell, r, c, rowspan, colspan in cells:
        computed = dom._describe(cell, computed_cache)[0]
        edges = {side: _border_candidate(computed, side, "cell") for side in _BORDER_SIDE_ATTR}
        r_end, c_end = min(r + rowspan, row_count), min(c + colspan, column_count)
        for cc in range(c, c_end):
            horizontal[r][cc].append(edges["top"])
            horizontal[r_end][cc].append(edges["bottom"])
        for rr in range(r, r_end):
            vertical[rr][left_line(c, c_end)].append(edges["left"])
            vertical[rr][right_line(c, c_end)].append(edges["right"])

    groups = []
    for row in rows:
        ancestor = dom._layout_parent(row)
        group = None
        while ancestor is not None and ancestor is not table_element:
            tag = (getattr(ancestor, "tagName", "") or "").lower()
            if _row_group_kind(tag, dom._describe(ancestor, computed_cache)[0]) is not None:
                group = ancestor
                break
            ancestor = dom._layout_parent(ancestor)
        groups.append(group)
    for r, row in enumerate(rows):
        computed = dom._describe(row, computed_cache)[0]
        for cc in range(column_count):
            horizontal[r][cc].append(_border_candidate(computed, "top", "row"))
            horizontal[r + 1][cc].append(_border_candidate(computed, "bottom", "row"))
        vertical[r][left_line(0, column_count)].append(_border_candidate(computed, "left", "row"))
        vertical[r][right_line(0, column_count)].append(_border_candidate(computed, "right", "row"))
        group = groups[r]
        if group is None:
            continue
        computed = dom._describe(group, computed_cache)[0]
        if r == 0 or groups[r - 1] is not group:
            for cc in range(column_count):
                horizontal[r][cc].append(_border_candidate(computed, "top", "row-group"))
        if r == row_count - 1 or groups[r + 1] is not group:
            for cc in range(column_count):
                horizontal[r + 1][cc].append(_border_candidate(computed, "bottom", "row-group"))
        vertical[r][left_line(0, column_count)].append(_border_candidate(computed, "left", "row-group"))
        vertical[r][right_line(0, column_count)].append(_border_candidate(computed, "right", "row-group"))

    columns = _table_columns(table_element, computed_cache, column_count)
    for c, (column, group) in enumerate(columns):
        if column is not None:
            computed = dom._describe(column, computed_cache)[0]
            for rr in range(row_count):
                vertical[rr][left_line(c, c + 1)].append(_border_candidate(computed, "left", "column"))
                vertical[rr][right_line(c, c + 1)].append(_border_candidate(computed, "right", "column"))
            horizontal[0][c].append(_border_candidate(computed, "top", "column"))
            horizontal[row_count][c].append(_border_candidate(computed, "bottom", "column"))
        if group is not None:
            computed = dom._describe(group, computed_cache)[0]
            if c == 0 or columns[c - 1][1] is not group:
                # First column of this group: contribute its left/right
                # borders once, at the physical edges of its whole span.
                span_end = c + 1
                while span_end < column_count and columns[span_end][1] is group:
                    span_end += 1
                for rr in range(row_count):
                    vertical[rr][left_line(c, span_end)].append(_border_candidate(computed, "left", "column-group"))
                    vertical[rr][right_line(c, span_end)].append(_border_candidate(computed, "right", "column-group"))
            horizontal[0][c].append(_border_candidate(computed, "top", "column-group"))
            horizontal[row_count][c].append(_border_candidate(computed, "bottom", "column-group"))

    for cc in range(column_count):
        horizontal[0][cc].append(_border_candidate(table_computed, "top", "table"))
        horizontal[row_count][cc].append(_border_candidate(table_computed, "bottom", "table"))
    for rr in range(row_count):
        vertical[rr][left_line(0, column_count)].append(_border_candidate(table_computed, "left", "table"))
        vertical[rr][right_line(0, column_count)].append(_border_candidate(table_computed, "right", "table"))

    h_width = [[_resolve_collapsed_border(segment) for segment in line] for line in horizontal]
    v_width = [[_resolve_collapsed_border(segment) for segment in line] for line in vertical]
    cell_borders: dict = {}
    for cell, r, c, rowspan, colspan in cells:
        r_end, c_end = min(r + rowspan, row_count), min(c + colspan, column_count)
        top = max((h_width[r][cc] for cc in range(c, c_end)), default=0.0)
        bottom = max((h_width[r_end][cc] for cc in range(c, c_end)), default=0.0)
        left = max((v_width[rr][left_line(c, c_end)] for rr in range(r, r_end)), default=0.0)
        right = max((v_width[rr][right_line(c, c_end)] for rr in range(r, r_end)), default=0.0)
        cell_borders[id(cell)] = (top / 2.0, right / 2.0, bottom / 2.0, left / 2.0)
    perimeter = (
        max(h_width[0], default=0.0),
        max((v_width[rr][right_line(0, column_count)] for rr in range(row_count)), default=0.0),
        max(h_width[row_count], default=0.0),
        max((v_width[rr][left_line(0, column_count)] for rr in range(row_count)), default=0.0),
    )
    return cell_borders, perimeter



def _table_cell_has_content(cell, computed_cache) -> bool:
    """Whether `cell` holds anything that renders -- real text, or a child
    element that isn't `display:none`. What decides which rows a table's
    surplus height goes to (see `_distribute_table_extra_height`)."""
    if dom._rendering_text_content(cell).strip():
        return True
    return bool(dom._child_elements(cell, computed_cache))



def _compute_table_column_widths(cells, computed_cache) -> dict:
    """`{id(cell_element): resolved_width}` for every cell, colspan'd or
    not -- a deliberately minimal CSS 2.1 17.5.2.2 "auto" table-layout
    pass, enough for ordinary HTML tables (and `display:table`-styled
    arbitrary elements, see `_table_rows`/`_row_cells`). `cells` is
    `_table_grid`'s occupancy list, so a `rowspan` from an earlier row
    already shifts this row's cells into their real columns.

    1. Each colspan-1 cell's max-content width; a column's width is the
       widest same-column cell across every row.
    2. A colspan'd cell's width is the sum of its columns; if its own
       *minimum* content width needs more, spread the shortfall evenly
       across just the columns it spans (never the whole table).
    3. Resolve every cell to one definite pixel width from the final
       column widths, before `build()` ever measures inline content --
       Taffy's own flex-measurement guessing never enters into it."""
    per_column: dict[int, float] = {}
    per_column_min: dict[int, float] = {}
    single_cells: dict[int, int] = {}  # id(cell) -> col_index, colspan == 1
    span_cells: list = []  # (cell, start_col, colspan)
    for cell, _row_index, col_index, _rowspan, colspan in cells:
        if colspan == 1:
            width = replaced_elements._measure_intrinsic_width(cell, computed_cache)
            single_cells[id(cell)] = col_index
            if width is not None:
                per_column[col_index] = max(per_column.get(col_index, 0.0), width)
            # CSS 2.1 17.5.2.2: a column can't be narrower than its widest
            # cell's minimum content width plus padding/borders -- the floor
            # a table sits on when its container is too narrow, overflowing
            # rather than squeezing -- collapsing-border-model-005.xht.
            # `replaced_elements._measure_intrinsic_width` already built this cell in a scratch
            # tree, so `_chromonic_native_style` carries its resolved edges.
            native = getattr(cell, "_chromonic_native_style", None) or {}
            edges = list(native.get("padding") or (0.0,) * 4) + list(native.get("border") or (0.0,) * 4)
            horizontal = box_model._numeric_edge(edges[1]) + box_model._numeric_edge(edges[3]) + box_model._numeric_edge(edges[5]) + box_model._numeric_edge(edges[7])
            minimum = (replaced_elements._measure_min_content_width(cell, computed_cache) or 0.0) + horizontal
            if width is not None:
                # Never past the real max-content -- the token measure
                # doesn't know a child's negative margin, table-height-
                # algorithm-026.xht.
                minimum = min(minimum, width)
            per_column_min[col_index] = max(per_column_min.get(col_index, 0.0), minimum)
        else:
            span_cells.append((cell, col_index, colspan))

    # Grow only the columns a colspan'd cell covers, measured against the
    # base column widths (not ones already grown by an earlier colspan) --
    # the largest shortfall wins (`max`), not an accumulating sum, or a run
    # of same-span rows would compound into a wildly inflated column.
    extra_per_column: dict[int, float] = {}
    for cell, start_col, colspan in span_cells:
        covered = range(start_col, start_col + colspan)
        base_sum = sum(per_column.get(c, 0.0) for c in covered)
        needed = replaced_elements._measure_min_content_width(cell, computed_cache)
        if needed is not None and needed > base_sum:
            extra = (needed - base_sum) / colspan
            for c in covered:
                extra_per_column[c] = max(extra_per_column.get(c, 0.0), extra)
    for c, extra in extra_per_column.items():
        per_column[c] = per_column.get(c, 0.0) + extra

    resolved: dict[int, float] = {
        cell_id: per_column[col] for cell_id, col in single_cells.items() if col in per_column
    }
    resolved_min: dict[int, float] = {
        cell_id: per_column_min[col] for cell_id, col in single_cells.items() if col in per_column_min
    }
    for cell, start_col, colspan in span_cells:
        total = sum(per_column.get(c, 0.0) for c in range(start_col, start_col + colspan))
        if total > 0.0:
            resolved[id(cell)] = total
    column_count = max((c for c in list(per_column) + list(per_column_min)), default=-1) + 1
    return {
        "cells": resolved,
        "cells_min": resolved_min,
        "columns": [per_column.get(c, 0.0) for c in range(column_count)],
        "columns_min": [per_column_min.get(c, 0.0) for c in range(column_count)],
    }



def _compute_fixed_column_widths(table_element, cells, column_count, columns, content_width,
                                 spacing_h, computed_cache) -> list:
    """CSS 2.1 17.5.2.1 fixed table layout: one border-box width per
    column, decided by (1) a `<col>`/column-group element's own `width`,
    else (2) a first-row cell's non-auto `width` (its content width plus
    its own padding and borders -- collapsed halves, in that model --
    divided evenly over a colspan), else (3) an even share of whatever
    the table's content width leaves over after spacing. Cells in later
    rows never matter, and content is free to overflow its column. If
    every column is specified and the table is still wider, the surplus
    goes to all of them in proportion. Confirmed against fixed-table-
    layout-003a01..f08.xht (padding/border/`box-sizing` variants of one
    80px cell in a 400px table all resolving to a 200px column)."""
    widths: list = [None] * column_count
    # A percentage resolves against the table's content width less every
    # inter-column gap -- fixed-table-layout-017.xht.
    percentage_base = max(0.0, content_width - spacing_h * max(0, column_count - 1))
    for c, (column, _group) in enumerate(columns):
        # Only a <col>'s own width -- a column group's is ignored in fixed
        # layout -- fixed-table-layout-013.xht/-014.xht.
        if column is None:
            continue
        width = style_bridge._len(dom._describe(column, computed_cache)[1].width)
        if isinstance(width, (int, float)):
            widths[c] = float(width)
        elif isinstance(width, tuple) and width[0] == "pct":
            widths[c] = width[1] * percentage_base
    collapsed = getattr(table_element, "_chromonic_collapsed_cell_borders", None) or {}
    for cell, row_index, c, _rowspan, colspan in cells:
        if row_index != 0:
            continue
        computed, style_obj = dom._describe(cell, computed_cache)
        width = style_bridge._len(style_obj.width)
        if isinstance(width, tuple) and width[0] == "pct":
            width = width[1] * percentage_base
        if not isinstance(width, (int, float)):
            continue
        if getattr(style_obj.boxSizing, "value", "") == "border-box":
            border_box = float(width)
        else:
            padding = sum(_fontmetrics.parse_length(getattr(computed, name, None), default=0.0)
                          for name in ("paddingLeft", "paddingRight"))
            if id(cell) in collapsed:
                border = collapsed[id(cell)][1] + collapsed[id(cell)][3]
            else:
                border = sum(_fontmetrics.parse_length(getattr(computed, name, None), default=0.0)
                             for name in ("borderLeftWidth", "borderRightWidth"))
            border_box = float(width) + padding + border
        span_end = min(c + colspan, column_count)
        share = (border_box - spacing_h * (span_end - c - 1)) / max(1, span_end - c)
        for cc in range(c, span_end):
            if widths[cc] is None:
                widths[cc] = max(0.0, share)
    specified = sum(w for w in widths if w is not None)
    unspecified = [i for i, w in enumerate(widths) if w is None]
    available = content_width - spacing_h * max(0, column_count - 1) - specified
    if unspecified:
        share = max(0.0, available) / len(unspecified)
        for i in unspecified:
            widths[i] = share
    elif available > 0.0 and specified > 0.0:
        widths = [w + available * w / specified for w in widths]
    return [float(w or 0.0) for w in widths]


# CSS 2.1 17.4/CSS Tables 3: computed `display` keywords for the internal
# table boxes margin never applies to, regardless of what tag carries the
# value -- `display:table`/`inline-table` (the outer table box itself,
# where margin still applies normally) are deliberately not in this set.
_TABLE_INTERNAL_DISPLAYS = frozenset({
    "table-row-group", "table-header-group", "table-footer-group",
    "table-row", "table-cell", "table-column-group", "table-column",
})



def _fix_table_shrink_to_fit_width(tree_obj, node_map: dict) -> bool:
    """CSS 2.1 17.5.2: an outer table/inline-table box with width:auto
    shrink-to-fits like a float or inline-block, not stretched full-width
    like an ordinary block. Chromonic's table root has no dedicated Taffy
    display mode (plain "block"), so it reaches this point laid out
    full-width first -- same starting point `_fix_float_shrink_to_fit_width`
    corrects for floats, reusing the identical technique (a fresh
    max-content `tree.compute()` for just this subtree).

    Returns whether any subtree was actually shifted -- see
    `_fix_float_shrink_to_fit_width`'s return value for why the caller needs this."""
    shifted = False
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    # Reversed: node_map is in Taffy creation order (children before their
    # parent), so a nested table got shrunk first and then overwritten by
    # the outer table's own recompute (`geometry._write_boxes` covers the whole
    # subtree) -- outer first, inner last, keeps both.
    for element in reversed(list(node_map.values())):
        if not dom._is_element(element):
            continue
        if not getattr(element, "_chromonic_is_table_root", False):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("width") != "auto":
            continue
        node_id = by_id.get(id(element))
        if node_id is None:
            continue
        max_content = getattr(element, "_chromonic_table_max_content_width", None)
        if max_content is not None:
            max_content = max(max_content, getattr(element, "_chromonic_table_caption_min_width", 0.0) or 0.0)
            # CSS 2.1 17.5.2.2 max-content width, from `_setup_table_root`'s resolved
            # columns, laid out as a definite width so the row's cells land
            # exactly on their bases -- captions never widen it (a wide
            # caption wraps to the grid, caption-side-example-001.xht).
            new_width = min(box.width, max_content)
            grow = False
            if new_width >= box.width - 1e-6:
                # Shrink-to-fit never grows past its available width -- but
                # an inline-table flex item can come out of Taffy narrower
                # than its columns, sized from its text alone --
                # inline-table-001.xht. With room in the containing block
                # it takes its full max-content width.
                parent = dom._layout_parent(element)
                parent_box = parent.__dict__.get("_layout_box") if parent is not None and hasattr(parent, "__dict__") else None
                room = None
                if parent_box is not None:
                    parent_padding = parent.__dict__.get("_chromonic_padding", (0.0,) * 4)
                    room = parent_box.client_width - parent_padding[1] - parent_padding[3]
                if not (_is_inline_table_box(element) and box.width < max_content - 0.5
                        and room is not None and max_content <= room + 0.5):
                    continue
                new_width = max_content
                grow = True
            margin = style.get("margin") or (0.0,) * 4
            available = new_width + box_model._numeric_edge(margin[1]) + box_model._numeric_edge(margin[3])
            boxes = tree_obj.compute(node_id, available, None)
        else:
            grow = False
            boxes = tree_obj.compute(node_id, None, None)
        own = boxes.get(node_id)
        if own is None:
            continue
        new_width = own[2]
        if new_width >= box.width and not grow:
            continue  # shrink-to-fit never grows a box past its available width
        geometry._write_boxes(boxes, node_map)
        # Recomputed from scratch: every correction made inside this
        # subtree before now is gone with it (the caller re-runs them).
        shifted = True
        dx = box.x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            geometry._shift_recomputed_subtree(element, dx, dy, boxes, node_map)
    return shifted



def _is_inline_table_box(element) -> bool:
    """An `inline-table` -- a real element's computed display, or a CSS
    2.1 17.2.1 anonymous one generated inside inline content."""
    if isinstance(element, anonymous_boxes._AnonymousTableBox):
        return element.kind == "inline-table"
    computed = getattr(element, "_chromonic_computed_style", None)
    return (getattr(computed, "display", "") or "").strip().lower() == "inline-table" if computed is not None else False



def _distribute_table_extra_height_in(table) -> None:
    """CSS 2.1 17.5.3: when a table's specified height (a minimum -- see
    `build()`'s table-root min_height handling) leaves surplus below its
    rows, the surplus is handed to the rows rather than left as empty
    table-box space -- each grown row's cells grow with it, later rows and
    row groups move/grow to match.

    Rows with real content share the surplus in proportion to height; only
    when every row is empty is it split evenly -- matches Chrome,
    border-conflict-element-001.xht. Cell content stays top-aligned where
    Taffy put it; vertical-align:middle isn't built yet."""
    for element in (table,):  # one table per call; `continue` below means "done"
        box = element.__dict__.get("_layout_box")
        rows = [row for row in (getattr(element, "_chromonic_table_rows", None) or ())
                if row.__dict__.get("_layout_box") is not None]
        if box is None:
            continue
        if not rows:
            # No rows, but a specified height: the empty grid is still that
            # tall, below any captions -- table-caption-margins-001.xht.
            specified = getattr(element, "_chromonic_table_specified_height", None)
            if specified is None:
                continue
            captions = 0.0
            for caption in (list(getattr(element, "_chromonic_table_captions", None) or ())):
                caption_box = caption.__dict__.get("_layout_box")
                if caption_box is not None:
                    margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                    captions += caption_box.height + box_model._numeric_edge(margin[0]) + box_model._numeric_edge(margin[2])
            native = getattr(element, "_chromonic_native_style", None) or {}
            padding_top, _pr, padding_bottom, _pl = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
            border_bottom = box.height - box.client_height - box.border_top
            chrome = box.border_top + padding_top + border_bottom + padding_bottom
            grid = specified if native.get("box_sizing") == "border-box" else specified + chrome
            growth = (captions + grid) - box.height
            if growth > 0.5:
                geometry._grow_box_height(element, growth)
            continue
        padding_top, _pr, padding_bottom, _pl = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        border_bottom = box.height - box.client_height - box.border_top
        inner_bottom = box.y + box.height - border_bottom - padding_bottom
        captions_height = 0.0
        for caption in getattr(element, "_chromonic_table_captions", None) or ():
            caption_box = caption.__dict__.get("_layout_box")
            if caption_box is not None:
                margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                captions_height += caption_box.height + box_model._numeric_edge(margin[0]) + box_model._numeric_edge(margin[2])
        for caption in getattr(element, "_chromonic_table_bottom_captions", None) or ():
            caption_box = caption.__dict__.get("_layout_box")
            if caption_box is not None:
                # Margins included: the table box is a BFC, so a bottom
                # caption's `margin-bottom: 10em` sits inside it
                # (table-anonymous-block-012.xht).
                margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                inner_bottom -= caption_box.height + box_model._numeric_edge(margin[0]) + box_model._numeric_edge(margin[2])
        first_box = rows[0].__dict__["_layout_box"]
        last_box = rows[-1].__dict__["_layout_box"]
        rows_extent = (last_box.y + last_box.height) - first_box.y
        chrome_height = box.border_top + padding_top + border_bottom + padding_bottom
        # Surplus already inside the box (a min_height Taffy honoured with
        # nothing but caption-free rows to fill it) ...
        extra = inner_bottom - (last_box.y + last_box.height)
        # ... or, with captions in the box, the specified height applies to
        # the grid alone (CSS 2.1 17.4: captions sit outside the table box)
        # -- the grid may need to grow past what the box currently holds,
        # and the box with it -- border-collapse-applies-to-015.xht.
        specified = getattr(element, "_chromonic_table_specified_height", None)
        if specified is not None:
            native = getattr(element, "_chromonic_native_style", None) or {}
            target_grid_box = specified if native.get("box_sizing") == "border-box" else specified + chrome_height
            extra = max(extra, target_grid_box - (rows_extent + chrome_height))
        if extra <= 0.5:
            continue
        growth = (captions_height + rows_extent + chrome_height + extra) - box.height
        if growth > 0.5:
            # The table's own box only: `_settle_table` propagates the
            # table's net growth to what follows it, once.
            geometry._grow_box_height(element, growth)
        # CSS 2.1 17.5.3: a row (or its cells) with a real specified height
        # isn't a candidate for the surplus -- only auto/content-height rows
        # share it. wpt/css/css-grid/grid-model/display-grid.html.
        def _has_specified_height(row) -> bool:
            # A row's/cell's declared height is converted to min_height
            # (height reset to auto) in `_setup_table_row_or_cell` per CSS
            # 2.1 17.5.3 -- so min_height is the signal to read here.
            row_native = getattr(row, "_chromonic_native_style", None) or {}
            if isinstance(row_native.get("min_height"), (int, float)) and row_native["min_height"] > 0.0:
                return True
            return any(isinstance((getattr(cell, "_chromonic_native_style", None) or {}).get("min_height"),
                                  (int, float))
                       and (getattr(cell, "_chromonic_native_style", None) or {})["min_height"] > 0.0
                       for cell in getattr(row, "_chromonic_table_cells", None) or ())
        non_empty = [row for row in rows if not getattr(row, "_chromonic_table_row_empty", False)] or rows
        targets = [row for row in non_empty if not _has_specified_height(row)] or non_empty
        weights = [row.__dict__["_layout_box"].height for row in targets]
        total = sum(weights)
        deltas = ({id(row): extra * weight / total for row, weight in zip(targets, weights)}
                  if total > 0.0 else {id(row): extra / len(targets) for row in targets})
        shift = 0.0
        seen_ancestors: set = set()
        group_growth: dict = {}
        for row in rows:
            # A row group's box starts where its first row does: moved by
            # the shift accumulated before that row, grown by its rows' gains.
            ancestor = dom._layout_parent(row)
            while ancestor is not None and ancestor is not element:
                if id(ancestor) not in seen_ancestors:
                    seen_ancestors.add(id(ancestor))
                    if shift:
                        geometry._shift_box(ancestor, 0.0, shift)
                ancestor = dom._layout_parent(ancestor)
            if shift:
                geometry._shift_subtree(row, 0.0, shift)
            delta = deltas.get(id(row), 0.0)
            if delta:
                geometry._grow_box_height(row, delta)
                for cell in getattr(row, "_chromonic_table_cells", None) or ():
                    geometry._grow_box_height(cell, delta)
                ancestor = dom._layout_parent(row)
                while ancestor is not None and ancestor is not element:
                    group_growth[id(ancestor)] = (ancestor, group_growth.get(id(ancestor), (ancestor, 0.0))[1] + delta)
                    ancestor = dom._layout_parent(ancestor)
                shift += delta
        for ancestor, growth in group_growth.values():
            geometry._grow_box_height(ancestor, growth)



def _table_cell_content_height(cell, inner_top: float) -> "float | None":
    """How tall `cell`'s own content actually is, measured from the top of
    its content box: a text-only cell's lines, otherwise the bottom margin
    edge of its lowest child box. `None` when there's nothing to align."""
    if not getattr(cell, "_chromonic_has_layout_children", False):
        lines = getattr(cell, "_chromonic_text_lines", None) or []
        line_height = float(getattr(cell, "_chromonic_line_height", 0.0) or 0.0)
        return len(lines) * line_height if lines and line_height else None
    bottom = None
    for child in _layout_children(cell):
        if not dom._is_element(child):
            continue
        box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
        child_bottom = box.y + box.height + box_model._numeric_edge(margin[2])
        bottom = child_bottom if bottom is None else max(bottom, child_bottom)
    if bottom is None:
        return None
    content = bottom - inner_top
    if cell.__dict__.get("_chromonic_flex_row_members"):
        # Inline-level content (the flex-row approximation): its line box
        # is at least the strut's line-height tall however small the
        # boxes on it (empty-cells-008.xht: a cell holding one 0x0 image
        # has an 18px line, so `vertical-align: middle` moves nothing).
        paint_style = getattr(cell, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = inline_formatting._parse_font_weight(paint_style.get("font_weight"))
        _ascent, _descent, normal = fonts.text_metrics(family, font_size, weight >= 600,
                                                       fonts.is_italic(paint_style.get("font_style")))
        resolved = inline_formatting._resolved_line_height(paint_style.get("line_height"))
        content = max(content, resolved if resolved is not None else normal)
    return content



def _layout_children(element):
    """`element`'s child *boxes* as laid out: the CSS 2.1 17.2.1/9.2.1.1
    anonymous boxes generated around its children this pass where there
    are any (`anonymous_boxes._normalized_child_nodes`), else its DOM children."""
    normalized = element.__dict__.get("_chromonic_normalized_children") if hasattr(element, "__dict__") else None
    return normalized if normalized is not None else dom._child_nodes(element)



def _table_cell_baseline(cell, box, padding) -> "float | None":
    """CSS 2.1 17.5.3: a cell's baseline is that of its first in-flow line
    box (or row); with neither, synthesized from the content's own extent,
    not the cell box Taffy stretched to its row --
    empty-cells-applies-to-008.xht. `None` for an empty cell: no baseline,
    no part in row alignment -- table-vertical-align-baseline-008.xht."""
    baseline = box_model._first_baseline(cell)
    if baseline is not None:
        return baseline
    inner_top = box.y + box.border_top + padding[0]
    content_height = _table_cell_content_height(cell, inner_top)
    if content_height is None:
        return None
    return inner_top + content_height



def _table_row_baseline(row, row_box) -> float:
    """CSS 2.1 17.5.3: the lowest baseline of the row's baseline-aligned
    cells; with none, the bottom content edge of its lowest cell."""
    aligned = []
    lowest = None
    for cell in getattr(row, "_chromonic_table_cells", None) or ():
        box = cell.__dict__.get("_layout_box")
        if box is None:
            continue
        computed = getattr(cell, "_chromonic_computed_style", None)
        align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower() if computed is not None else "baseline"
        padding = cell.__dict__.get("_chromonic_padding", (0.0,) * 4)
        border_bottom = box.height - box.client_height - box.border_top
        content_bottom = box.y + box.height - border_bottom - padding[2]
        lowest = content_bottom if lowest is None else max(lowest, content_bottom)
        if align in ("top", "middle", "bottom"):
            continue
        baseline = _table_cell_baseline(cell, box, padding)
        if baseline is not None:
            aligned.append(baseline)
    if aligned:
        return max(aligned)
    if lowest is not None:
        return lowest
    # A row with no cells at all: its baseline is its top, matching Chrome
    # -- empty-cells-applies-to-011.xht.
    return row_box.y



def _align_table_cell_baselines_in(table) -> None:
    """CSS 2.1 17.5.4: every cell whose vertical-align means baseline for a
    cell (baseline itself, or anything but top/middle/bottom) has its
    content pushed down to meet the row's lowest baseline; a cell with no
    line box contributes its bottom content edge. A cell pushed past its
    row's height makes the row (and table) taller. Runs before
    middle/bottom alignment and surplus-height distribution, which need
    final row heights. table-vertical-align-baseline-001.xht,
    table-height-algorithm-019.xht."""
    for element in (table,):
        for row in getattr(element, "_chromonic_table_rows", None) or ():
            entries = []
            for cell in getattr(row, "_chromonic_table_cells", None) or ():
                cell.__dict__.pop("_chromonic_content_offset_y", None)
                box = cell.__dict__.get("_layout_box")
                computed = getattr(cell, "_chromonic_computed_style", None)
                if box is None or computed is None:
                    continue
                align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower()
                if align in ("top", "middle", "bottom"):
                    continue
                padding = cell.__dict__.get("_chromonic_padding", (0.0,) * 4)
                baseline = _table_cell_baseline(cell, box, padding)
                if baseline is None:
                    continue
                entries.append((cell, baseline, box, padding))
            if len(entries) < 2:
                continue
            row_baseline = max(baseline for _cell, baseline, _box, _padding in entries)
            growth = 0.0
            for cell, baseline, box, padding in entries:
                shift = row_baseline - baseline
                if shift <= 0.5:
                    continue
                inner_top = box.y + box.border_top + padding[0]
                content_height = _table_cell_content_height(cell, inner_top)
                if getattr(cell, "_chromonic_has_layout_children", False):
                    if (getattr(cell, "_chromonic_inline_plan", None) is not None
                            or cell.__dict__.get("_chromonic_inline_fragments")):
                        continue
                    for child in _layout_children(cell):
                        if dom._is_element(child):
                            geometry._shift_subtree(child, 0.0, shift)
                else:
                    cell._chromonic_content_offset_y = shift
                if content_height is not None:
                    inner_height = box.client_height - padding[0] - padding[2]
                    growth = max(growth, shift + content_height - inner_height)
            if growth > 0.5:
                geometry._grow_and_reflow(row, growth, stop_at=element)
                for cell in getattr(row, "_chromonic_table_cells", None) or ():
                    geometry._grow_box_height(cell, growth)



def _settle_table(table) -> None:
    """Settle `table`'s vertical geometry -- baseline-align cells, hand
    specified-height surplus to rows, place middle/bottom cell content --
    once per Taffy result (`geometry._write_boxes` resets the mark), then carry
    the table's net growth to what follows it exactly once per pass (a
    second settle after a shrink-to-fit recompute finds ancestors/siblings
    already moved). Called innermost-table-first from the table pipeline,
    and on demand by `box_model._first_baseline`: an inline-table's baseline is read
    by flex-row alignment before the table pipeline runs, and must see
    settled rows -- table-vertical-align-baseline-008.xht."""
    if table.__dict__.get("_chromonic_table_settled"):
        return
    table.__dict__["_chromonic_table_settled"] = True
    before = table.__dict__.get("_layout_box")
    # Rowspans first, collapsing after: a visibility:collapse row still
    # takes its share of a spanning cell's height before being flattened --
    # row-visibility-003.xht.
    _settle_rowspan_cells_in(table)
    _settle_collapsed_cells_in(table)
    _collapse_rows_in(table)
    _align_table_cell_baselines_in(table)
    _distribute_table_extra_height_in(table)
    _cover_spanned_rows_in(table)
    _align_table_cell_content_in(table)
    after = table.__dict__.get("_layout_box")
    if before is None or after is None:
        return
    net = after.height - before.height
    already = table.__dict__.get("_chromonic_table_growth_propagated", 0.0)
    pending = net - already
    if pending > 0.01:
        geometry._grow_and_reflow(table, pending, grow_self=False)
    table.__dict__["_chromonic_table_growth_propagated"] = max(already, net)



def _cell_natural_height(cell) -> float:
    """The border-box height `cell` needs on its own: its content (or its
    specified `height`, a minimum) plus its padding and borders --
    regardless of how tall Taffy stretched it to match its row."""
    box = cell.__dict__.get("_layout_box")
    if box is None:
        return 0.0
    padding = cell.__dict__.get("_chromonic_padding", (0.0,) * 4)
    vertical = (box.height - box.client_height) + padding[0] + padding[2]
    content = _table_cell_content_height(cell, box.y + box.border_top + padding[0]) or 0.0
    native = cell.__dict__.get("_chromonic_native_style") or {}
    minimum = native.get("min_height")
    minimum = float(minimum) if isinstance(minimum, (int, float)) else 0.0
    if native.get("box_sizing") == "border-box":
        return max(content + vertical, minimum)
    return max(content, minimum) + vertical



def _resize_table_row(table, row, delta: float, exclude=frozenset()) -> None:
    """Grow (or, negative `delta`, shrink) `row` inside `table`: its cells
    with it (bar those in `exclude`), everything after it in the table
    moved, each enclosing row group and the table itself resized."""
    if abs(delta) < 0.01:
        return
    geometry._grow_box_height(row, delta)
    for cell in getattr(row, "_chromonic_table_cells", None) or ():
        if id(cell) not in exclude:
            geometry._grow_box_height(cell, delta)
    geometry._shift_later_siblings_for_height_delta(row, delta)
    ancestor = dom._layout_parent(row)
    while ancestor is not None and ancestor is not table:
        geometry._grow_box_height(ancestor, delta)
        geometry._shift_later_siblings_for_height_delta(ancestor, delta)
        ancestor = dom._layout_parent(ancestor)
    if ancestor is table:
        geometry._grow_box_height(table, delta)



def _settle_collapsed_cells_in(table) -> None:
    """CSS 2.1 17.5.5, after layout: every row item laid out at a
    visibility:collapse column's real width is narrowed by the collapsed
    width, everything after it in the row moved up by that plus the lost
    border-spacing gap. Items were built with flex-shrink:0, so their
    Taffy positions are exactly the uncollapsed ones this works from."""
    if not getattr(table, "_chromonic_table_collapsed_columns", None):
        return
    rtl = getattr(table, "_chromonic_table_rtl", False)
    lost = 0.0
    narrowed: list = []
    for row in getattr(table, "_chromonic_table_rows", None) or ():
        items = []
        for cell in getattr(row, "_chromonic_table_cells", None) or ():
            box = cell.__dict__.get("_layout_box")
            if box is not None:
                items.append((cell, box, getattr(cell, "_chromonic_cell_collapse", (0.0, 0.0, 0.0)), True))
        for holder in (row.__dict__.get("_chromonic_rowspan_placeholders") or {}).values():
            box = holder.__dict__.get("_layout_box")
            if box is not None:
                items.append((holder, box, getattr(holder, "collapse", (0.0, 0.0, 0.0)), False))
        if not any(shift for _item, _box, (_pre, _shrink, shift), _is_cell in items):
            continue
        items.sort(key=lambda item: item[1].x, reverse=rtl)
        moved = 0.0
        for item, box, (pre_move, shrink, shift), is_cell in items:
            # The gap before this item's own collapsed first column goes
            # with it -- column-visibility-003.xht.
            moved += pre_move
            if moved:
                dx = moved if rtl else -moved
                if is_cell:
                    geometry._shift_subtree(item, dx, 0.0)
                else:
                    geometry._shift_box(item, dx, 0.0)
                box = item.__dict__["_layout_box"]
            if shrink:
                width = max(0.0, box.width - shrink)
                item.__dict__["_layout_box"] = dataclasses.replace(
                    box, width=width, client_width=max(0.0, box.client_width - shrink),
                    x=box.x + (box.width - width) if rtl else box.x)
            moved += shift - pre_move
        narrowed.append((row, moved))
        lost = max(lost, moved)
    if lost <= 0.01:
        return
    # Rows, their groups, and an auto-width table box all give up the
    # same width -- column-visibility-004.xht.

    def narrow(element, amount: float) -> None:
        box = element.__dict__.get("_layout_box")
        if box is None or amount <= 0.01:
            return
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, width=max(0.0, box.width - amount), client_width=max(0.0, box.client_width - amount),
            x=box.x + amount if rtl else box.x)

    seen: set = set()
    for row, moved in narrowed:
        narrow(row, moved)
        ancestor = dom._layout_parent(row)
        while ancestor is not None and ancestor is not table:
            if id(ancestor) not in seen:
                seen.add(id(ancestor))
                narrow(ancestor, moved)
            ancestor = dom._layout_parent(ancestor)
    native = table.__dict__.get("_chromonic_native_style") or {}
    if native.get("width") == "auto":
        narrow(table, lost)



def _collapse_rows_in(table) -> None:
    """CSS 2.1 17.5.5: a `visibility: collapse` row is 0px tall, its
    cells with it, everything below moved up -- the cells' content still
    sized the columns during layout, which is the point of `collapse`
    over `display: none`. Runs after the rowspan pass, before the
    baseline/height distribution."""
    for row in getattr(table, "_chromonic_table_rows", None) or ():
        if not getattr(row, "_chromonic_row_collapsed", False):
            continue
        box = row.__dict__.get("_layout_box")
        if box is None:
            continue
        for cell in getattr(row, "_chromonic_table_cells", None) or ():
            cell_box = cell.__dict__.get("_layout_box")
            if cell_box is not None and cell_box.height > 0.01:
                cell.__dict__["_layout_box"] = dataclasses.replace(cell_box, height=0.0, client_height=0.0)
        if box.height > 0.01:
            _resize_table_row(table, row, -box.height, exclude={id(cell) for cell in row._chromonic_table_cells})



def _spanning_cells(table) -> "tuple[list, list]":
    """`(rows, [(cell, first_row_index, rows_spanned), ...])` for every
    `rowspan > 1` cell of `table` with a box, spans clamped to the rows
    that exist."""
    rows = [row for row in (getattr(table, "_chromonic_table_rows", None) or ())
            if row.__dict__.get("_layout_box") is not None]
    spanning = []
    for cell, r, _c, rowspan, _colspan in getattr(table, "_chromonic_table_grid_cells", None) or ():
        count = min(r + rowspan, len(rows)) - r
        if rowspan > 1 and count > 1 and cell.__dict__.get("_layout_box") is not None:
            spanning.append((cell, r, count))
    return rows, spanning



def _settle_rowspan_cells_in(table) -> None:
    """CSS 2.1 17.5.3 for rowspan: a spanning cell's height is spread over
    the rows it spans, not loaded onto the first. Taffy stretched the first
    row (and its cells) to the spanning cell's whole content height, so
    first the row is brought back to what its other cells/own height need,
    then any remaining shortfall is dealt to the spanned rows in
    proportion to height (equally if all empty). The cell's own box is
    fitted last, by `_cover_spanned_rows_in`, once row heights are final.
    table-height-algorithm-010.xht, -018.xht."""
    rows, spanning = _spanning_cells(table)
    if not spanning:
        return
    spacing_v = getattr(table, "_chromonic_border_spacing", (0.0, 0.0))[1]
    starters: dict = {}
    for cell, r, _count in spanning:
        starters.setdefault(r, set()).add(id(cell))
    for r, ids in starters.items():
        row = rows[r]
        box = row.__dict__["_layout_box"]
        native = row.__dict__.get("_chromonic_native_style") or {}
        minimum = native.get("min_height")
        needed = float(minimum) if isinstance(minimum, (int, float)) else 0.0
        for cell in getattr(row, "_chromonic_table_cells", None) or ():
            if id(cell) not in ids:
                needed = max(needed, _cell_natural_height(cell))
        if box.height - needed > 0.5:
            _resize_table_row(table, row, needed - box.height, exclude=ids)
    for cell, r, count in spanning:
        spanned = rows[r:r + count]
        available = sum(row.__dict__["_layout_box"].height for row in spanned) + spacing_v * (count - 1)
        deficit = _cell_natural_height(cell) - available
        if deficit <= 0.5:
            continue
        weights = [row.__dict__["_layout_box"].height for row in spanned]
        total = sum(weights)
        for row, weight in zip(spanned, weights):
            _resize_table_row(table, row, deficit * (weight / total if total > 0.0 else 1.0 / count))



def _cover_spanned_rows_in(table) -> None:
    """Fit every `rowspan` cell's box to the rows it spans (their extent,
    inter-row spacing included) -- after the rows' heights are final."""
    rows, spanning = _spanning_cells(table)
    for cell, r, count in spanning:
        if getattr(rows[r], "_chromonic_row_collapsed", False):
            continue  # its row collapsed: 0px tall like every cell of that row (row-visibility-004.xht)
        first = rows[r].__dict__["_layout_box"]
        last = rows[r + count - 1].__dict__["_layout_box"]
        height = (last.y + last.height) - first.y
        box = cell.__dict__["_layout_box"]
        if abs(box.height - height) > 0.01:
            cell.__dict__["_layout_box"] = dataclasses.replace(
                box, height=height, client_height=height - (box.height - box.client_height))



def _settle_tables(node_map: dict) -> None:
    """Innermost tables first: a cell holding a nested table takes its
    baseline from that table's first row, which must be settled (and its
    growth propagated) before the outer row aligns on it."""
    tables = [element for element in node_map.values()
              if getattr(element, "_chromonic_is_table_root", False)]

    def depth(element) -> int:
        count = 0
        parent = dom._layout_parent(element)
        while parent is not None:
            count += 1
            parent = dom._layout_parent(parent)
        return count

    tables.sort(key=depth, reverse=True)
    for table in tables:
        _settle_table(table)



def _align_table_cell_content_in(table) -> None:
    """CSS 2.1 17.5.4: a cell's vertical-align positions its content within
    the (full-row-height) cell box -- middle centres it, bottom sinks it;
    top/baseline (baseline approximated as top) leave it where Taffy put
    it. Chrome's UA default is middle for td/th (`ua_style.py`), so this
    fires for most real tables -- border-conflict-style-001.xht.

    Runs after `_distribute_table_extra_height_in`, once row heights are final.
    A text-only cell gets `_chromonic_content_offset_y` (paint.py/the
    harness apply it); a cell with block children has each child's subtree
    shifted for real. Mixed inline content is left top-aligned for now --
    shifting shared `_InlineFormattingPlan` fragment lists safely needs the
    shrink-to-fit passes' re-publish machinery."""
    for element in (table,):
        for row in getattr(element, "_chromonic_table_rows", None) or ():
            for cell in getattr(row, "_chromonic_table_cells", None) or ():
                box = cell.__dict__.get("_layout_box")
                computed = getattr(cell, "_chromonic_computed_style", None)
                if box is None or computed is None:
                    continue
                align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower()
                if align not in ("middle", "bottom"):
                    continue  # a baseline-aligned cell's offset was set by `_align_table_cell_baselines`
                cell.__dict__.pop("_chromonic_content_offset_y", None)
                padding_top, _pr, padding_bottom, _pl = cell.__dict__.get("_chromonic_padding", (0.0,) * 4)
                inner_top = box.y + box.border_top + padding_top
                inner_height = box.client_height - padding_top - padding_bottom
                content_height = _table_cell_content_height(cell, inner_top)
                if content_height is None:
                    continue
                slack = inner_height - content_height
                if slack <= 0.5:
                    continue
                offset = slack / 2.0 if align == "middle" else slack
                if getattr(cell, "_chromonic_has_layout_children", False):
                    if (getattr(cell, "_chromonic_inline_plan", None) is not None
                            or cell.__dict__.get("_chromonic_inline_fragments")):
                        continue
                    for child in _layout_children(cell):
                        if dom._is_element(child):
                            geometry._shift_subtree(child, 0.0, offset)
                else:
                    cell._chromonic_content_offset_y = offset



def _enforce_fixed_column_boxes(node_map: dict) -> None:
    """CSS 2.1 17.5.2.1: in a fixed-layout table every cell's box *is* its
    column span -- even when the cell's own padding and borders add up to
    more than that (Chrome keeps a 0px-wide box for a `50%` cell's red
    neighbours in fixed-table-layout-025.xht/-030.xht, padding and all).
    Taffy never sizes a box below its own padding+border, so after
    layout each cell of an exactly-resolved fixed table is pinned to its
    column: moved to the column's start, given the column's width, its
    content moved with it."""
    for element in list(node_map.values()):
        if not getattr(element, "_chromonic_table_fixed", False):
            continue
        cells = getattr(element, "_chromonic_table_grid_cells", None)
        if not cells:
            continue
        spacing_h = getattr(element, "_chromonic_border_spacing", (0.0, 0.0))[0]
        columns = getattr(element, "_chromonic_table_columns_max", None)
        if not columns:
            # A percentage-width fixed table: content width was unknown at
            # build time, so the exact CSS 2.1 17.5.2.1 algorithm runs here
            # against the box Taffy gave it -- fixed-table-layout-023.xht.
            box = element.__dict__.get("_layout_box")
            grid_columns = getattr(element, "_chromonic_table_columns", None) or []
            if box is None or not grid_columns:
                continue
            padding = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
            content_width = box.client_width - padding[1] - padding[3]
            cache: dict = {}
            for node in [entry[0] for entry in cells] + [owner for pair in grid_columns for owner in pair
                                                          if owner is not None]:
                prior = getattr(node, "_chromonic_resolved_style", None)
                if prior is not None:
                    cache[id(node)] = prior
            columns = _compute_fixed_column_widths(
                element, cells, len(grid_columns), grid_columns, content_width, spacing_h, cache)
            element._chromonic_table_columns_max = columns
        rtl = getattr(element, "_chromonic_table_rtl", False)
        column_count = len(columns)
        starts = [0.0] * column_count
        for c in range(1, column_count):
            starts[c] = starts[c - 1] + columns[c - 1] + spacing_h
        by_row: dict = {}
        for cell, row_index, c, _rowspan, colspan in cells:
            by_row.setdefault(row_index, []).append((cell, c, min(c + colspan, column_count)))
        rows = getattr(element, "_chromonic_table_rows", None) or []
        for row_index, row in enumerate(rows):
            row_box = row.__dict__.get("_layout_box")
            if row_box is None:
                continue
            row_padding = row.__dict__.get("_chromonic_padding", (0.0,) * 4)
            origin = row_box.x + row_box.border_left + row_padding[3]
            row_content_width = row_box.client_width - row_padding[1] - row_padding[3]
            for cell, c, c_end in by_row.get(row_index, ()):
                box = cell.__dict__.get("_layout_box")
                if box is None or c >= column_count:
                    continue
                width = sum(columns[c:c_end]) + spacing_h * max(0, c_end - c - 1)
                if rtl:
                    x = origin + row_content_width - (starts[c] + width)
                else:
                    x = origin + starts[c]
                dx = x - box.x
                if abs(dx) > 1e-6:
                    geometry._shift_subtree(cell, dx, 0.0)
                    box = cell.__dict__["_layout_box"]
                if abs(box.width - width) > 1e-6:
                    cell.__dict__["_layout_box"] = dataclasses.replace(
                        box, width=width, client_width=max(0.0, width - (box.width - box.client_width)))



def _column_elements(table_element) -> list:
    """Every `<col>`/`<colgroup>` (or `table-column`/`table-column-group`)
    element directly under `table_element`, groups' columns included, in
    DOM order -- `_table_columns` without the per-column expansion."""
    found: list = []
    for child in dom._child_nodes(table_element):
        if not dom._is_element(child):
            continue
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and box_model._is_absolutely_positioned(resolved[1]):
            continue  # CSS 2.1 9.7: blockified, a real box of its own (top-applies-to-005.xht)
        tag = (getattr(child, "tagName", "") or "").lower()
        computed = getattr(child, "_chromonic_computed_style", None)
        display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
        if display == "":
            try:
                display = (getattr(dom._describe(child, {})[0], "display", "") or "").strip().lower()
            except Exception:
                display = ""
        if tag == "colgroup" or display == "table-column-group":
            found.append(child)
            for node in dom._child_nodes(child):
                if dom._is_element(node):
                    node_tag = (getattr(node, "tagName", "") or "").lower()
                    if node_tag == "col":
                        found.append(node)
                    else:
                        try:
                            if (getattr(dom._describe(node, {})[0], "display", "") or "").strip().lower() == "table-column":
                                found.append(node)
                        except Exception:
                            pass
        elif tag == "col" or display == "table-column":
            found.append(child)
    return found



def _publish_table_column_boxes(node_map: dict) -> None:
    """CSS 2.1 17.2.1: a table-column/-group box "is not rendered" -- no
    Taffy node (`dom._NON_RENDERING_TAGS`), but Chrome still answers
    getBoundingClientRect() with the union of its columns' cells across
    every row -- basic-css-table-001.xht. Published here, after every
    row/cell box is final, purely so the element reports that rect --
    nothing paints it."""
    for element in list(node_map.values()):
        if not getattr(element, "_chromonic_is_table_root", False):
            continue
        columns = getattr(element, "_chromonic_table_columns", None) or []
        cells = getattr(element, "_chromonic_table_grid_cells", None) or []
        rows = [row for row in (getattr(element, "_chromonic_table_rows", None) or ())
                if row.__dict__.get("_layout_box") is not None]
        table_box = element.__dict__.get("_layout_box")
        if table_box is None:
            continue
        ranges: dict = {}
        for index, (column, group) in enumerate(columns):
            for owner in (column, group):
                if owner is None:
                    continue
                first, last, _owner = ranges.get(id(owner), (index, index, owner))
                ranges[id(owner)] = (min(first, index), max(last, index), owner)

        def publish_empty(owner) -> None:
            # A cell-less column: Chrome reports its specified width (if
            # any) and no height, at the table box's origin --
            # separated-border-model-006.xht, empty-cells-applies-to-012.xht.
            width = 0.0
            try:
                specified = style_bridge._len(dom._describe(owner, {})[1].width)
            except Exception:
                specified = None
            if isinstance(specified, (int, float)):
                width = max(0.0, float(specified))
            owner.__dict__["_layout_box"] = LayoutBox(
                x=table_box.x, y=table_box.y, width=width, height=0.0,
                client_width=width, client_height=0.0, border_top=0.0, border_left=0.0)

        # Column elements past the grid's last column have no column of
        # their own -- reported empty, like any cell-less column --
        # separated-border-model-006.xht.
        for owner in _column_elements(element):
            if id(owner) not in ranges:
                publish_empty(owner)
        if not columns or not cells or not rows:
            for _first, _last, owner in ranges.values():
                publish_empty(owner)
            continue
        top = min(row.__dict__["_layout_box"].y for row in rows)
        bottom = max(row.__dict__["_layout_box"].y + row.__dict__["_layout_box"].height for row in rows)
        # Column edges from every cell edge landing on a grid line: a
        # column's left edge is where some cell starts, or failing that
        # just past the previous column's right edge (a colspan-covered
        # column has no cell edge of its own); likewise its right edge.
        spacing_h = getattr(element, "_chromonic_border_spacing", (0.0, 0.0))[0]
        column_count = len(columns)
        starts: dict = {}
        ends: dict = {}
        for cell, _r, c, _rowspan, colspan in cells:
            box = cell.__dict__.get("_layout_box")
            if box is None:
                continue
            starts[c] = min(starts.get(c, box.x), box.x)
            end_column = min(c + colspan, column_count) - 1
            ends[end_column] = max(ends.get(end_column, box.x + box.width), box.x + box.width)
        # A fixed-layout table knows every column's exact width, which
        # also places the columns under the middle of a colspan (no cell
        # edge of their own at all).
        exact = (getattr(element, "_chromonic_table_columns_max", None)
                 if getattr(element, "_chromonic_table_fixed", False) else None)
        for first, last, owner in ranges.values():
            left = starts.get(first)
            if left is None and first > 0 and (first - 1) in ends:
                left = ends[first - 1] + spacing_h
            right = ends.get(last)
            if right is None and (last + 1) in starts:
                right = starts[last + 1] - spacing_h
            if exact and len(exact) == column_count:
                span_width = sum(exact[first:last + 1]) + spacing_h * (last - first)
                if left is None and right is not None:
                    left = right - span_width
                elif right is None and left is not None:
                    right = left + span_width
                elif left is None and right is None and starts:
                    origin = min(starts.values()) - (sum(exact[:min(starts)]) + spacing_h * min(starts))
                    left = origin + sum(exact[:first]) + spacing_h * first
                    right = left + span_width
            if left is None or right is None:
                publish_empty(owner)
                continue
            width = max(0.0, right - left)
            # A zero-width column reports an entirely empty rect (no
            # height either, fixed-table-layout-014.xht), at the table
            # box's own top, not the rows' -- column-visibility-003.xht.
            height = max(0.0, bottom - top) if width > 0.0 else 0.0
            owner.__dict__["_layout_box"] = LayoutBox(
                x=left, y=top if width > 0.0 else table_box.y, width=width, height=height,
                client_width=width, client_height=height,
                border_top=0.0, border_left=0.0,
            )
