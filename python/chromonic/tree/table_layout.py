from __future__ import annotations

import dataclasses

from domonic import _fontmetrics
from domonic.layout import LayoutBox

from .. import fonts, style_bridge
from . import anonymous_boxes, box_model, dom, geometry, inline_formatting, replaced_elements
from .box import box_of



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
    collapsed = box_of(table_element).collapsed_cell_borders or {}
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



def _is_inline_table_box(element) -> bool:
    """An `inline-table` -- a real element's computed display, or a CSS
    2.1 17.2.1 anonymous one generated inside inline content."""
    if isinstance(element, anonymous_boxes._AnonymousTableBox):
        return element.kind == "inline-table"
    computed = box_of(element).computed_style
    return (getattr(computed, "display", "") or "").strip().lower() == "inline-table" if computed is not None else False



def _column_elements(table_element) -> list:
    """Every `<col>`/`<colgroup>` (or `table-column`/`table-column-group`)
    element directly under `table_element`, groups' columns included, in
    DOM order -- `_table_columns` without the per-column expansion."""
    found: list = []
    for child in dom._child_nodes(table_element):
        if not dom._is_element(child):
            continue
        resolved = box_of(child).resolved_style
        if resolved is not None and box_model._is_absolutely_positioned(resolved[1]):
            continue  # CSS 2.1 9.7: blockified, a real box of its own (top-applies-to-005.xht)
        tag = (getattr(child, "tagName", "") or "").lower()
        computed = box_of(child).computed_style
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
        if not box_of(element).get("is_table_root", False):
            continue
        columns = box_of(element).table_columns or []
        cells = box_of(element).table_grid_cells or []
        rows = [row for row in (box_of(element).table_rows or ())
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
        spacing_h = box_of(element).get("border_spacing", (0.0, 0.0))[0]
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
        exact = (box_of(element).table_columns_max
                 if box_of(element).get("table_fixed", False) else None)
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
