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


def _px(value) -> "float | None":
    return float(value) if isinstance(value, (int, float)) else None


def _horizontal_edges(native: dict) -> float:
    padding = native.get("padding") or (0.0,) * 4
    border = native.get("border") or (0.0,) * 4
    return sum(box_model._numeric_edge(v) for v in (padding[1], padding[3], border[1], border[3]))


def _vertical_align(cell) -> str:
    computed = getattr(cell, "_chromonic_computed_style", None)
    align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower() if computed is not None else "baseline"
    return align if align in ("top", "middle", "bottom") else "baseline"


class _TableLayoutPlan:
    """One table's layout: see the module docstring."""

    def __init__(self, table, computed_cache):
        self.table = table
        self.computed_cache = computed_cache

    # -- inputs gathered at build time -----------------------------------

    def _node(self, element) -> "int | None":
        return element.__dict__.get("_chromonic_node_id") if hasattr(element, "__dict__") else None

    def _specified_border_width(self, element) -> "tuple[str, float] | None":
        """A cell's or column's specified width as a border-box length
        (`("px", w)`) or a fraction of the table (`("pct", f)`)."""
        spec = element.__dict__.get("_chromonic_cell_specified_width")
        native = getattr(element, "_chromonic_native_style", None) or {}
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

    # -- the algorithm ------------------------------------------------------

    def measure(self, available_width, _available_height, known_width=None, _known_height=None,
                responses=None, fills_width=False):
        table = self.table
        responses = responses if responses is not None else {}
        rows = list(getattr(table, "_chromonic_table_rows", None) or ())
        cells = list(getattr(table, "_chromonic_table_grid_cells", None) or ())
        columns = list(getattr(table, "_chromonic_table_columns", None) or ())
        captions = list(getattr(table, "_chromonic_table_captions", None) or ())
        bottom_captions = {id(c) for c in (getattr(table, "_chromonic_table_bottom_captions", None) or ())}
        collapsed_columns = set(getattr(table, "_chromonic_table_collapsed_columns", None) or ())
        spacing_h, spacing_v = getattr(table, "_chromonic_border_spacing", (0.0, 0.0))
        rtl = bool(getattr(table, "_chromonic_table_rtl", False))
        column_count = max([c + span for _cell, _r, c, _rs, span in cells] + [len(columns)] + [0])
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
                mn = max(mn, spec[1])
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
            if k in collapsed_columns:
                col_min[k] = col_max[k] = 0.0
        visible = [k for k in range(column_count) if k not in collapsed_columns]
        gaps = spacing_h * max(0, len(visible) - 1)
        min_total = sum(col_min) + gaps
        max_total = sum(col_max) + gaps
        caption_min = max([responses[("i", self._node(c))][0] for c in captions] + [0.0])

        # 3. The table's used grid width.
        native = getattr(table, "_chromonic_native_style", None) or {}
        spec = table.__dict__.get("_chromonic_table_specified_width")
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

        fixed = bool(getattr(table, "_chromonic_table_fixed", False)) and target is not None
        if fixed:
            col_widths = table_layout._compute_fixed_column_widths(
                table, [entry for entry in cells], column_count, columns, width, spacing_h, self.computed_cache)
            col_widths = list(col_widths) + [0.0] * (column_count - len(col_widths))
            for k in collapsed_columns:
                if k < len(col_widths):
                    col_widths[k] = 0.0
            width = max(width, sum(col_widths) + gaps)
        else:
            col_widths = self._distribute(width - gaps, col_min, col_max, col_pct, visible)
        grid_width = sum(col_widths) + gaps
        outer_width = max(grid_width, caption_min)

        # Column start edges, left to right (mirrored for rtl, CSS 2.1 17.5).
        col_x = []
        x = 0.0
        for k in range(column_count):
            col_x.append(x)
            x += col_widths[k] + (spacing_h if k in visible and k != (visible[-1] if visible else -1) else 0.0)
        if rtl:
            col_x = [grid_width - (col_x[k] + col_widths[k]) for k in range(column_count)]

        def span_width(c, colspan):
            covered = [k for k in range(c, min(c + colspan, column_count))]
            shown = [k for k in covered if k not in collapsed_columns]
            return sum(col_widths[k] for k in covered) + spacing_h * max(0, len(shown) - 1)

        def span_x(c, colspan):
            covered = list(range(c, min(c + colspan, column_count))) or [c]
            return min(col_x[k] for k in covered) if covered[0] < column_count else 0.0

        # 4. Heights: each cell and caption laid out at its width.
        caption_boxes = {}
        requests = []
        for caption in captions:
            margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
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
        collapsed_rows = {index for index, row in enumerate(rows) if getattr(row, "_chromonic_row_collapsed", False)}
        row_height = []
        for row in rows:
            native_row = getattr(row, "_chromonic_native_style", None) or {}
            row_height.append(max(_px(native_row.get("min_height")) or 0.0, _px(native_row.get("height")) or 0.0))
        row_above = [0.0] * len(rows)
        row_below = [0.0] * len(rows)
        cell_info = {}
        for cell, r, c, rowspan, colspan in cells:
            _w, h, first, _last = measured(cell, cell_widths[id(cell)])
            align = _vertical_align(cell)
            native_cell = getattr(cell, "_chromonic_native_style", None) or {}
            padding = native_cell.get("padding") or (0.0,) * 4
            border = native_cell.get("border") or (0.0,) * 4
            inner_bottom = h - box_model._numeric_edge(padding[2]) - box_model._numeric_edge(border[2])
            baseline = first
            if baseline is None and table_layout._table_cell_has_content(cell, self.computed_cache):
                baseline = inner_bottom
            cell_info[id(cell)] = (h, baseline, align, inner_bottom)
            if rowspan == 1 and r not in collapsed_rows:
                if align == "baseline" and baseline is not None:
                    row_above[r] = max(row_above[r], baseline)
                    row_below[r] = max(row_below[r], h - baseline)
                else:
                    row_height[r] = max(row_height[r], h)
        row_baseline = []
        for r in range(len(rows)):
            if r in collapsed_rows:
                row_height[r] = 0.0
                row_baseline.append(0.0)
                continue
            row_height[r] = max(row_height[r], row_above[r] + row_below[r])
            row_baseline.append(row_above[r])
        for cell, r, c, rowspan, colspan in cells:
            if rowspan <= 1:
                continue
            spanned = [k for k in range(r, min(r + rowspan, len(rows))) if k not in collapsed_rows]
            if not spanned:
                continue
            have = sum(row_height[k] for k in spanned) + spacing_v * (len(spanned) - 1)
            need = cell_info[id(cell)][0]
            if need > have:
                share = (need - have) / len(spanned)
                for k in spanned:
                    row_height[k] += share
        visible_rows = [k for k in range(len(rows)) if k not in collapsed_rows]
        grid_height = sum(row_height) + spacing_v * max(0, len(visible_rows) - 1)
        caption_heights = {}
        for caption in captions:
            margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
            caption_heights[id(caption)] = (box_model._numeric_edge(margin[0]),
                                           measured(caption, caption_boxes[id(caption)][1])[1],
                                           box_model._numeric_edge(margin[2]))
        captions_height = sum(sum(v) for v in caption_heights.values())
        # A specified table height (a minimum, CSS 2.1 17.5.3) is taken up by
        # the rows, in proportion to their heights (evenly when all are 0).
        specified_height = table.__dict__.get("_chromonic_table_specified_height")
        if isinstance(specified_height, (int, float)) and visible_rows:
            padding = native.get("padding") or (0.0,) * 4
            border = native.get("border") or (0.0,) * 4
            vertical = sum(box_model._numeric_edge(v) for v in (padding[0], padding[2], border[0], border[2]))
            content_target = float(specified_height) - (vertical if native.get("box_sizing") == "border-box" else 0.0)
            extra = content_target - captions_height - grid_height
            if extra > 0.01:
                total = sum(row_height[k] for k in visible_rows)
                for k in visible_rows:
                    row_height[k] += extra * (row_height[k] / total if total > 0 else 1.0 / len(visible_rows))
                grid_height += extra

        # 6. Placements.
        placements = []
        y = 0.0
        for caption in [c for c in captions if id(c) not in bottom_captions]:
            mt, h, mb = caption_heights[id(caption)]
            ml, w = caption_boxes[id(caption)]
            placements.append((self._node(caption), ml, y + mt, w, h, 0.0))
            y += mt + h + mb
        grid_top = y
        row_y = []
        cursor = grid_top
        for k in range(len(rows)):
            row_y.append(cursor)
            if k not in collapsed_rows:
                cursor += row_height[k] + spacing_v
        placed_groups = set()
        for k, row in enumerate(rows):
            group = dom._layout_parent(row)
            if (group is not None and group is not table and id(group) not in placed_groups
                    and self._node(group) is not None):
                members = [i for i, other in enumerate(rows) if dom._layout_parent(other) is group]
                top = row_y[members[0]]
                bottom = max(row_y[i] + row_height[i] for i in members)
                placements.append((self._node(group), 0.0, top, grid_width, bottom - top, -1.0))
                placed_groups.add(id(group))
            if self._node(row) is not None:
                placements.append((self._node(row), 0.0, row_y[k], grid_width, row_height[k], -1.0))
            for cell, r, c, rowspan, colspan in cells:
                if r != k:
                    continue
                spanned = [i for i in range(r, min(r + rowspan, len(rows))) if i not in collapsed_rows]
                height = (sum(row_height[i] for i in spanned) + spacing_v * max(0, len(spanned) - 1)) if spanned else 0.0
                h, baseline, align, inner_bottom = cell_info[id(cell)]
                if align == "baseline" and baseline is not None and rowspan == 1:
                    offset = row_baseline[r] - baseline
                elif align == "middle":
                    offset = (height - h) / 2.0
                elif align == "bottom":
                    offset = height - h
                else:
                    offset = 0.0
                offset = max(0.0, offset)
                cell.__dict__["_chromonic_content_offset_y"] = offset
                placements.append((self._node(cell), span_x(c, colspan), row_y[r], cell_widths[id(cell)],
                                   max(height, 0.0), offset))
        y = grid_top + grid_height
        for caption in [c for c in captions if id(c) in bottom_captions]:
            mt, h, mb = caption_heights[id(caption)]
            ml, w = caption_boxes[id(caption)]
            placements.append((self._node(caption), ml, y + mt, w, h, 0.0))
            y += mt + h + mb
        # CSS 2.1 10.8.1: a table's baseline is its first row's.
        first_row = visible_rows[0] if visible_rows else None
        baseline = (row_y[first_row] + (row_baseline[first_row] or row_height[first_row])
                    if first_row is not None else None)
        table.__dict__["_chromonic_table_baseline_offset"] = baseline
        table.__dict__["_chromonic_table_columns_used"] = list(col_widths)
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
