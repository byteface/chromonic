"""CSS 2.1 17.5 table layout, run from inside Taffy's layout recursion.

A table is a `Tree.new_table` node (`src/lib.rs`'s `compute_table_layout`).
Its `_TableLayoutPlan.measure` is called with the table's available content
width and answers in a short conversation: first it asks for every cell's
min-content and max-content widths, then -- once it has resolved the
columns -- for every cell's height at its column width, and finally it
returns the position and size of every caption, row group, row and cell.
Rust lays each cell out at exactly that size, so nothing is adjusted after
layout: row heights, rowspans, baseline alignment, vertical-align, a
specified table height and visibility:collapse are all decided here, once,
against real measurements.

Coordinates in placements are relative to the table's content box, which
starts after the table's border and padding -- and, in the separated
borders model, after the perimeter border-spacing, which `builder` folds
into the table's padding (CSS 2.1 17.6.1)."""

from __future__ import annotations

from .. import style_bridge
from . import box_model, dom, table_layout
from .box import box_of


def _px(value) -> "float | None":
    return float(value) if isinstance(value, (int, float)) else None


def _horizontal_edges(native: dict) -> float:
    padding = native.get("padding") or (0.0,) * 4
    border = native.get("border") or (0.0,) * 4
    return sum(box_model._numeric_edge(v) for v in (padding[1], padding[3], border[1], border[3]))


def _vertical_align(cell) -> str:
    computed = box_of(cell).computed_style
    align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower() if computed is not None else "baseline"
    return align if align in ("top", "middle", "bottom") else "baseline"


class _TableLayoutPlan:
    """One table's layout: see the module docstring."""

    def __init__(self, table, computed_cache):
        self.table = table
        self.computed_cache = computed_cache

    # -- inputs gathered at build time -----------------------------------

    def _node(self, element) -> "int | None":
        return box_of(element).node_id if hasattr(element, "__dict__") else None

    def _specified_border_width(self, element) -> "tuple[str, float] | None":
        """A cell's or column's specified width as a border-box length
        (`("px", w)`) or a fraction of the table (`("pct", f)`)."""
        spec = box_of(element).cell_specified_width
        native = box_of(element).native_style or {}
        if spec is None:
            return None
        if isinstance(spec, tuple) and spec[0] == "pct":
            return ("pct", float(spec[1]))
        if isinstance(spec, (int, float)):
            width = float(spec)
            if native.get("box_sizing") != "border-box":
                width += _horizontal_edges(native)
            return ("px", width)
        return None

    def _column_width(self, owner):
        """A `<col>`/`<colgroup>`'s specified width, if any."""
        if owner is None:
            return None
        spec = self._specified_border_width(owner)
        if spec is not None:
            return spec
        try:
            width = style_bridge._len(dom._describe(owner, self.computed_cache)[1].width)
        except Exception:
            return None
        if isinstance(width, (int, float)) and width > 0:
            return ("px", float(width))
        if isinstance(width, tuple) and width[0] == "pct":
            return ("pct", width[1])
        return None

    # -- the algorithm ------------------------------------------------------

    def measure(self, available_width, _available_height, known_width=None, _known_height=None,
                responses=None, fills_width=False):
        table = self.table
        responses = responses if responses is not None else {}
        rows = list(box_of(table).table_rows or ())
        cells = list(box_of(table).table_grid_cells or ())
        columns = list(box_of(table).table_columns or ())
        captions = list(box_of(table).table_captions or ())
        bottom_captions = {id(c) for c in (box_of(table).table_bottom_captions or ())}
        collapsed_columns = set(box_of(table).table_collapsed_columns or ())
        spacing_h, spacing_v = box_of(table).get("border_spacing", (0.0, 0.0))
        rtl = bool(box_of(table).get("table_rtl", False))
        # Columns past the last cell exist only when they have a width of
        # their own (a table of nothing but `<col style="width: 5em">` is
        # 80px wide; bare trailing `<col>`s add no columns or spacing --
        # separated-border-model-006.xht).
        cell_columns = max([c + span for _cell, _r, c, _rs, span in cells] + [0])
        sized_columns = [index + 1 for index, (column, group) in enumerate(columns)
                         if any(self._column_width(owner) is not None for owner in (column, group))]
        column_count = max([cell_columns] + sized_columns)
        columns = columns[:column_count]
        cells = [entry for entry in cells if self._node(entry[0]) is not None]
        captions = [c for c in captions if self._node(c) is not None]

        # 1. Intrinsic widths of every cell and caption.
        wanted = [(("i", self._node(e)), self._node(e)) for e in [c[0] for c in cells] + captions]
        missing = [(key, node) for key, node in wanted if key not in responses]
        if missing:
            return ("intrinsic", missing)

        # 2. Column minimum/maximum widths (CSS 2.1 17.5.2.2).
        col_min = [0.0] * column_count
        col_max = [0.0] * column_count
        col_pct = [0.0] * column_count
        for index, (column, group) in enumerate(columns[:column_count]):
            for owner in (column, group):
                if owner is None:
                    continue
                spec = self._specified_border_width(owner)
                if spec is None:
                    try:
                        width = style_bridge._len(dom._describe(owner, self.computed_cache)[1].width)
                    except Exception:
                        width = None
                    spec = ("px", width) if isinstance(width, (int, float)) else (
                        ("pct", width[1]) if isinstance(width, tuple) and width[0] == "pct" else None)
                if spec is None:
                    continue
                if spec[0] == "px" and spec[1] > 0:
                    col_min[index] = max(col_min[index], spec[1])
                    col_max[index] = max(col_max[index], spec[1])
                elif spec[0] == "pct":
                    col_pct[index] = max(col_pct[index], spec[1])
                break
        spanning = []
        for cell, _r, c, _rowspan, colspan in cells:
            mn, mx = responses[("i", self._node(cell))]
            spec = self._specified_border_width(cell)
            if spec is not None and spec[0] == "px":
                # A cell's specified width is its column's preferred width; a
                # squeezed table can still take the column down to its
                # content's minimum (table-anonymous-block-012.xht).
                mx = max(mn, spec[1])
            span = min(colspan, column_count - c)
            if span <= 1:
                col_min[c] = max(col_min[c], mn)
                col_max[c] = max(col_max[c], mx)
                if spec is not None and spec[0] == "pct":
                    col_pct[c] = max(col_pct[c], spec[1])
            else:
                spanning.append((c, span, mn, mx))
        # A spanning cell's excess over the columns it covers goes to them
        # in proportion to their maximum widths (evenly when all are 0),
        # narrowest spans first.
        for c, span, mn, mx in sorted(spanning, key=lambda entry: entry[1]):
            covered = [k for k in range(c, c + span)]
            gaps = spacing_h * (span - 1)
            for values, need in ((col_min, mn), (col_max, max(mn, mx))):
                have = sum(values[k] for k in covered) + gaps
                if need <= have:
                    continue
                weights = [col_max[k] for k in covered]
                total = sum(weights)
                for k, weight in zip(covered, weights):
                    values[k] += (need - have) * (weight / total if total > 0 else 1.0 / span)
        for k in range(column_count):
            col_max[k] = max(col_max[k], col_min[k])
        # CSS 2.1 17.5.5: a visibility:collapse column is sized as if visible
        # (its cells lay out at that width), then removed from the table's
        # width along with one gap.
        visible = [k for k in range(column_count) if k not in collapsed_columns]
        all_columns = list(range(column_count))
        full_gaps = spacing_h * max(0, column_count - 1)
        gaps = spacing_h * max(0, len(visible) - 1)
        min_total = sum(col_min) + full_gaps
        max_total = sum(col_max) + full_gaps
        caption_min = max([responses[("i", self._node(c))][0] for c in captions] + [0.0])

        # 3. The table's used grid width.
        native = box_of(table).native_style or {}
        spec = box_of(table).table_specified_width
        edges = _horizontal_edges(native)
        target = None
        if fills_width and available_width is not None and available_width >= 0:
            target = available_width
        elif isinstance(spec, (int, float)):
            target = float(spec) - (edges if native.get("box_sizing") == "border-box" else 0.0)
        elif isinstance(spec, tuple) and spec[0] == "pct" and available_width is not None and available_width >= 0:
            margin = native.get("margin") or (0.0,) * 4
            containing = available_width + edges + box_model._numeric_edge(margin[1]) + box_model._numeric_edge(margin[3])
            target = spec[1] * containing - edges
        if available_width is None:
            width = max(min_total, target if target is not None else max_total)
        elif available_width < 0:
            width = min_total
        elif target is not None:
            width = max(min_total, target)
        else:
            width = max(min_total, min(available_width, max_total))

        # CSS 2.1 17.4: a caption wider than the table widens the table. The
        # caption spans the table's border box; the grid sits inside the
        # table's padding and border.
        width = max(width, caption_min - edges)
        fixed = bool(box_of(table).get("table_fixed", False)) and target is not None
        if fixed:
            # CSS 2.1 17.5.2.1: fixed layout never looks at cell content --
            # the table is its specified width unless its columns (or a
            # caption) need more.
            width = max(target, caption_min - edges)
            full_widths = table_layout._compute_fixed_column_widths(
                table, [entry for entry in cells], column_count, columns, width, spacing_h, self.computed_cache)
            full_widths = list(full_widths) + [0.0] * (column_count - len(full_widths))
        else:
            full_widths = self._distribute(width - full_gaps, col_min, col_max, col_pct, all_columns)
        col_widths = [0.0 if k in collapsed_columns else full_widths[k] for k in range(column_count)]
        # With no columns at all the grid still has the table's width.
        grid_width = (sum(col_widths) + gaps) if column_count else max(0.0, width)
        outer_width = grid_width  # the grid already covers any caption (above)

        # Column start edges, left to right (mirrored for rtl, CSS 2.1 17.5).
        # Spacing only between visible columns: a collapsed one sits flush
        # against the one before it.
        col_x = []
        x = 0.0
        seen_visible = False
        for k in range(column_count):
            if k not in collapsed_columns:
                if seen_visible:
                    x += spacing_h
                seen_visible = True
            col_x.append(x)
            x += col_widths[k]
        if rtl:
            col_x = [grid_width - (col_x[k] + col_widths[k]) for k in range(column_count)]

        def span_width(c, colspan):
            # A cell lays out at its columns' uncollapsed width (17.5.5).
            covered = [k for k in range(c, min(c + colspan, column_count))]
            return sum(full_widths[k] for k in covered) + spacing_h * max(0, len(covered) - 1)

        def span_x(c, colspan):
            covered = list(range(c, min(c + colspan, column_count))) or [c]
            return min(col_x[k] for k in covered) if covered[0] < column_count else 0.0

        # 4. Heights: each cell and caption laid out at its width.
        caption_boxes = {}
        requests = []
        for caption in captions:
            margin = (box_of(caption).native_style or {}).get("margin") or (0.0,) * 4
            ml, mr = box_model._numeric_edge(margin[3]), box_model._numeric_edge(margin[1])
            caption_boxes[id(caption)] = (ml, max(0.0, outer_width - ml - mr))
            key = ("l", self._node(caption), round(caption_boxes[id(caption)][1], 3))
            if key not in responses:
                requests.append((key, self._node(caption), caption_boxes[id(caption)][1], None))
        cell_widths = {}
        for cell, _r, c, _rowspan, colspan in cells:
            w = span_width(c, colspan)
            cell_widths[id(cell)] = w
            key = ("l", self._node(cell), round(w, 3))
            if key not in responses:
                requests.append((key, self._node(cell), w, None))
        if requests:
            return ("layout", requests)

        def measured(element, w):
            return responses[("l", self._node(element), round(w, 3))]

        # 5. Row heights (CSS 2.1 17.5.3).
        collapsed_rows = {index for index, row in enumerate(rows) if box_of(row).get("row_collapsed", False)}
        row_height = []
        for row in rows:
            native_row = box_of(row).native_style or {}
            row_height.append(max(_px(native_row.get("min_height")) or 0.0, _px(native_row.get("height")) or 0.0))
        row_above = [0.0] * len(rows)
        row_below = [0.0] * len(rows)
        cell_info = {}
        cell_need = {}
        for cell, r, c, rowspan, colspan in cells:
            _w, h, first, _last = measured(cell, cell_widths[id(cell)])
            align = _vertical_align(cell)
            native_cell = box_of(cell).native_style or {}
            padding = native_cell.get("padding") or (0.0,) * 4
            border = native_cell.get("border") or (0.0,) * 4
            inner_bottom = h - box_model._numeric_edge(padding[2]) - box_model._numeric_edge(border[2])
            baseline = first
            if baseline is None and table_layout._table_cell_has_content(cell, self.computed_cache):
                baseline = inner_bottom
            spec_h = _px(box_of(cell).cell_specified_height)
            if spec_h is not None and native_cell.get("box_sizing") != "border-box":
                spec_h += sum(box_model._numeric_edge(v) for v in (padding[0], padding[2], border[0], border[2]))
            cell_info[id(cell)] = (h, baseline, align, inner_bottom)
            need_h = max(h, spec_h or 0.0)
            cell_need[id(cell)] = need_h
            if align == "baseline" and baseline is not None:
                # A baseline-aligned cell -- spanning rows or not -- sets its
                # first row's baseline (CSS 2.1 17.5.3).
                row_above[r] = max(row_above[r], baseline)
            if rowspan == 1:
                if align == "baseline" and baseline is not None:
                    row_below[r] = max(row_below[r], h - baseline)
                row_height[r] = max(row_height[r], need_h if align != "baseline" or baseline is None else (spec_h or 0.0))
        row_baseline = []
        for r in range(len(rows)):
            row_height[r] = max(row_height[r], row_above[r] + row_below[r])
            row_baseline.append(row_above[r])
        for cell, r, c, rowspan, colspan in cells:
            if rowspan <= 1:
                continue
            spanned = list(range(r, min(r + rowspan, len(rows))))
            if not spanned:
                continue
            have = sum(row_height[k] for k in spanned) + spacing_v * (len(spanned) - 1)
            need = cell_need[id(cell)]
            h, baseline, align, _inner = cell_info[id(cell)]
            if align == "baseline" and baseline is not None:
                need = max(need, row_baseline[r] - baseline + h)
            if need > have:
                # In proportion to the spanned rows' heights, evenly when
                # they're all empty (table-height-algorithm-010/014.xht).
                weights = [row_height[k] for k in spanned]
                total = sum(weights)
                for k, weight in zip(spanned, weights):
                    row_height[k] += (need - have) * (weight / total if total > 0 else 1.0 / len(spanned))
        # CSS 2.1 17.5.5: a visibility:collapse row is sized as if visible
        # (cells spanning it lay out at that height), then removed.
        full_row_height = list(row_height)
        for k in collapsed_rows:
            row_height[k] = 0.0
        visible_rows = [k for k in range(len(rows)) if k not in collapsed_rows]
        # Row groups with no rows still stack in their place, as tall as
        # their own specified height (0 without one) -- border-collapse-005.html,
        # empty-cells-applies-to-008.xht.
        empty_groups: list = []
        position = 0
        for group in box_of(table).table_sections or ():
            members = [i for i, row in enumerate(rows) if dom._layout_parent(row) is group]
            if members:
                position = members[-1] + 1
            elif self._node(group) is not None:
                group_style = box_of(group).native_style or {}
                group_height = max(_px(group_style.get("height")) or 0.0, _px(group_style.get("min_height")) or 0.0)
                empty_groups.append((position, group, group_height))
        grid_height = (sum(row_height) + spacing_v * max(0, len(visible_rows) - 1)
                       + sum(height for _p, _g, height in empty_groups))
        caption_heights = {}
        for caption in captions:
            margin = (box_of(caption).native_style or {}).get("margin") or (0.0,) * 4
            caption_heights[id(caption)] = (box_model._numeric_edge(margin[0]),
                                           measured(caption, caption_boxes[id(caption)][1])[1],
                                           box_model._numeric_edge(margin[2]))
        # A specified table height (a minimum, CSS 2.1 17.5.3) is taken up by
        # the rows, in proportion to their heights (evenly when all are 0).
        specified_height = box_of(table).table_specified_height
        if isinstance(specified_height, (int, float)):
            padding = native.get("padding") or (0.0,) * 4
            border = native.get("border") or (0.0,) * 4
            vertical = sum(box_model._numeric_edge(v) for v in (padding[0], padding[2], border[0], border[2]))
            content_target = float(specified_height) - (vertical if native.get("box_sizing") == "border-box" else 0.0)
            # Captions sit outside the table box (CSS 2.1 17.4): the height is the grid's.
            extra = content_target - grid_height
            if extra > 0.01 and not visible_rows:
                grid_height += extra  # no rows: the (empty) grid keeps the height
            elif extra > 0.01:
                # To rows with content, except those with a height of their
                # own (the row's or a cell's), in proportion to their heights;
                # evenly when they're all empty -- matching Chrome
                # (border-conflict-element-001.xht, table-height-algorithm-014.xht).
                content_rows = set()
                sized_rows = set()
                for cell, r, c, rowspan, colspan in cells:
                    if table_layout._table_cell_has_content(cell, self.computed_cache):
                        content_rows.update(range(r, min(r + rowspan, len(rows))))
                    if (_px(box_of(cell).cell_specified_height) or 0.0) > 0.0:
                        sized_rows.add(r)
                for k, row in enumerate(rows):
                    if (_px((box_of(row).native_style or {}).get("min_height")) or 0.0) > 0.0:
                        sized_rows.add(k)
                targets = [k for k in visible_rows if k in content_rows] or visible_rows
                targets = [k for k in targets if k not in sized_rows] or targets
                total = sum(row_height[k] for k in targets)
                for k in targets:
                    row_height[k] += extra * (row_height[k] / total if total > 0 else 1.0 / len(targets))
                grid_height += extra
                for k in targets:
                    full_row_height[k] = row_height[k]

        # 6. Placements.
        placements = []
        y = 0.0
        for caption in [c for c in captions if id(c) not in bottom_captions]:
            mt, h, mb = caption_heights[id(caption)]
            ml, w = caption_boxes[id(caption)]
            placements.append((self._node(caption), ml, y + mt, w, h, 0.0, w, h))
            y += mt + h + mb
        grid_top = y
        row_y = []
        cursor = grid_top
        seen_visible = False
        empty_group_y = {}
        for k in range(len(rows) + 1):
            for position, group, group_height in empty_groups:
                if position == k:
                    empty_group_y[id(group)] = cursor
                    cursor += group_height
            if k == len(rows):
                break
            if k not in collapsed_rows:
                if seen_visible:
                    cursor += spacing_v
                seen_visible = True
            row_y.append(cursor)
            cursor += row_height[k]
        for _position, group, group_height in empty_groups:
            placements.append((self._node(group), 0.0, empty_group_y[id(group)], grid_width, group_height, -1.0,
                               grid_width, group_height))
        placed_groups = set()
        for k, row in enumerate(rows):
            group = dom._layout_parent(row)
            if (group is not None and group is not table and id(group) not in placed_groups
                    and self._node(group) is not None):
                members = [i for i, other in enumerate(rows) if dom._layout_parent(other) is group]
                top = row_y[members[0]]
                bottom = max(row_y[i] + row_height[i] for i in members)
                placements.append((self._node(group), 0.0, top, grid_width, bottom - top, -1.0, grid_width, bottom - top))
                placed_groups.add(id(group))
            if self._node(row) is not None:
                placements.append((self._node(row), 0.0, row_y[k], grid_width, row_height[k], -1.0, grid_width, row_height[k]))
            for cell, r, c, rowspan, colspan in cells:
                if r != k:
                    continue
                all_spanned = list(range(r, min(r + rowspan, len(rows))))
                spanned = [i for i in all_spanned if i not in collapsed_rows]
                # The box: what's visible of the cell once collapsed rows and
                # columns are removed. Its content: laid out at the full,
                # uncollapsed size (CSS 2.1 17.5.5).
                # A cell that starts in a collapsed row is hidden with it,
                # whatever it spans (row-visibility-004.xht).
                height = (sum(row_height[i] for i in spanned) + spacing_v * max(0, len(spanned) - 1)
                          if spanned and r not in collapsed_rows else 0.0)
                layout_height = sum(full_row_height[i] for i in all_spanned) + spacing_v * max(0, len(all_spanned) - 1)
                covered = list(range(c, min(c + colspan, column_count)))
                shown = [k for k in covered if k not in collapsed_columns]
                # From its first spanned column's left edge to its last one's
                # right edge; a collapsed column sits flush, zero wide.
                width = ((max(col_x[k] + col_widths[k] for k in covered) - min(col_x[k] for k in covered))
                         if shown else 0.0)
                h, baseline, align, inner_bottom = cell_info[id(cell)]
                if align == "baseline" and baseline is not None:
                    offset = row_baseline[r] - baseline
                elif align == "middle":
                    offset = (layout_height - h) / 2.0
                elif align == "bottom":
                    offset = layout_height - h
                else:
                    offset = 0.0
                offset = max(0.0, offset)
                box_of(cell).content_offset_y = offset
                placements.append((self._node(cell), span_x(c, colspan), row_y[r], width,
                                   max(height, 0.0), offset, cell_widths[id(cell)], max(layout_height, 0.0)))
        y = grid_top + grid_height
        for caption in [c for c in captions if id(c) in bottom_captions]:
            mt, h, mb = caption_heights[id(caption)]
            ml, w = caption_boxes[id(caption)]
            placements.append((self._node(caption), ml, y + mt, w, h, 0.0, w, h))
            y += mt + h + mb
        # CSS 2.1 10.8.1: a table's baseline is its first row's -- the
        # baseline its baseline-aligned cells share, else the bottom of its
        # cells, and the row's top when it has no cells at all (Chrome;
        # empty-cells-applies-to-011.xht).
        first_row = visible_rows[0] if visible_rows else None
        baseline = None
        if first_row is not None:
            row_cells = [entry for entry in cells if entry[1] == first_row]
            if row_baseline[first_row]:
                baseline = row_y[first_row] + row_baseline[first_row]
            elif row_cells:
                # The bottom of the cells' final content boxes (17.5.3), not
                # of their measured content (caption-side-applies-to-007.xht).
                def content_bottom(cell):
                    native_cell = box_of(cell).native_style or {}
                    padding = native_cell.get("padding") or (0.0,) * 4
                    border = native_cell.get("border") or (0.0,) * 4
                    return (row_height[first_row] - box_model._numeric_edge(padding[2])
                            - box_model._numeric_edge(border[2]))
                baseline = row_y[first_row] + max(content_bottom(cell) for cell, *_ in row_cells)
            else:
                baseline = row_y[first_row]
        # The exact column widths, for the `<col>` boxes
        # (`table_layout._publish_table_column_boxes`).
        box_of(table).table_columns_max = list(col_widths)
        return ("done", outer_width, y, baseline, placements)

    @staticmethod
    def _distribute(width, col_min, col_max, col_pct, visible) -> list:
        """Column widths for `width` of grid space (CSS 2.1 17.5.2.2, as
        browsers implement it): percentage columns take their share first;
        the rest get their maximum widths plus any surplus in proportion to
        those maxima, or, short of that, their minimums plus a proportional
        part of the room between minimum and maximum."""
        widths = [0.0] * len(col_min)
        remaining = width
        auto = []
        for k in visible:
            if col_pct[k] > 0:
                widths[k] = max(col_min[k], col_pct[k] * width)
                remaining -= widths[k]
            else:
                auto.append(k)
        if not auto:
            if remaining > 0.01 and visible:
                share = remaining / len(visible)
                for k in visible:
                    widths[k] += share
            return widths
        min_sum = sum(col_min[k] for k in auto)
        max_sum = sum(col_max[k] for k in auto)
        if remaining >= max_sum:
            extra = remaining - max_sum
            for k in auto:
                widths[k] = col_max[k] + extra * (col_max[k] / max_sum if max_sum > 0 else 1.0 / len(auto))
        elif remaining > min_sum and max_sum > min_sum:
            fraction = (remaining - min_sum) / (max_sum - min_sum)
            for k in auto:
                widths[k] = col_min[k] + (col_max[k] - col_min[k]) * fraction
        else:
            for k in auto:
                widths[k] = col_min[k]
        return widths
