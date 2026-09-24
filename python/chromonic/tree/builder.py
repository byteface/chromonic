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
    """A real browser lays a run of `display:inline`/`inline-block` children
    out left-to-right, wrapping onto new lines as needed -- ordinary CSS
    inline flow, needing nothing special in a page's own CSS (adjacent
    `<a>` tags in an unstyled nav list already do this by default, since
    `inline` is every element's own CSS initial value). Taffy has no inline
    flow at all; `style_bridge._display()` already collapses every
    non-flex/grid/none display to `"block"`, so without this, a horizontal
    nav bar like suckless.org's `<div id="menu"><a>home</a><a>dwm</a>...`
    (relying on nothing but that CSS default) renders as one link per line
    instead of a row -- found by comparing chromonic's output against a real
    browser's on a real page, not synthetically.

    This is a heuristic **approximation**, not real inline layout or real
    float layout, and deliberately conservative about *when* it fires: a
    container qualifies only if it has **two or more** children and
    **most** (80%+) of them "want" a horizontal flow (`_wants_horizontal_flow`
    -- either a tag a real UA stylesheet would default to inline, still
    computed as `inline`/`inline-block` (`box_model._is_inline_level`), or an
    explicit `float: left`/`right` (`box_model._is_floated`) -- the *other* real
    layout mode with no implementation here at all, needing this same
    treatment for the same reason: found next, comparing against Chrome on
    `https://www.wikipedia.org/`, whose "10 largest Wikipedias" grid is
    nothing but ten `float: left` boxes and no flexbox). The tag check (for
    the inline half) is what makes *that* half safe -- domonic gives
    *every* tag the raw CSS initial value `inline` with no UA stylesheet of
    its own (wrinkle #11), so trusting a computed "inline" by itself would
    also catch an entirely ordinary, unstyled `<div>` or `<p>` (an early
    version of this function did exactly that and broke plain
    single-paragraph layouts across this repo's own tests, none of which
    apply `ua_style.py`). `float` needs no such gate -- its initial value is
    always `none` regardless of tag, so any non-`none` value is unambiguous
    author intent, not a domonic-default false positive.

    A *majority*, not unanimity, is required: suckless.org's own nav is
    eight plain `<a>`s plus one `<span>` the site's own CSS gives `display:
    block` (almost certainly for unrelated dropdown/JS behaviour) -- one
    exception in a group of nine, which an "every child must qualify" rule
    let silently veto the whole nav back to one-link-per-line. Requiring 2+
    children means one incidentally-qualifying lone child (a single link in
    an otherwise block-shaped wrapper) doesn't trigger this either -- real
    patterns needing the approximation (nav bars, tag lists, badge rows,
    floated card grids) always have several.

    Qualifying containers are treated as `display:flex; flex-wrap:wrap`
    instead of plain block. Does **not** attempt real mixed inline content
    (text interleaved with inline elements, e.g. `<p>click <a>here</a>
    please</p>` -- already out of scope, see `dom._own_text`'s own docstring),
    does not merge adjacent text runs, does not clear floats or let
    non-floated content flow around them the way real float layout would,
    and does not implement inline-level *text* wrapping around floated/
    inline boxes -- only whole elements wrapping onto new rows, via
    ordinary flex-wrap."""
    element.__dict__.pop("_chromonic_float_flow_children", None)
    element.__dict__.pop("_chromonic_float_flow_qualifies", None)
    for child in child_elements:
        child.__dict__.pop("_chromonic_force_full_row_width", None)
        child.__dict__.pop("_chromonic_no_flex_shrink", None)
    if style["display"] != "block":
        return  # already flex/grid/none -- a real, explicit layout mode wins, no guessing over it
    if len(child_elements) < 2 and not any(box_model._is_floated(cc) for cc in child_computeds):
        # A *lone* floated child still needs real float positioning (flush
        # to its containing block's own edge, `float:right` in particular)
        # -- the usual "needs 2+ children" gate exists to avoid engaging
        # this whole approximation for an ordinary single-child block (the
        # overwhelmingly common case, and not a float), but a single
        # `float:left`/`right` child is exactly as unambiguous a signal
        # alone as it is alongside siblings (see the float half of
        # `_wants_horizontal_flow` above). Confirmed directly on floats-
        # rule3-outside-right-001.xht: a lone `float:right` child, with no
        # siblings to trigger this function at all, rendered flush-*left*
        # in its container instead -- `float` wasn't consulted for
        # positioning at all.
        return
    qualifies = [
        _wants_horizontal_flow(child, child_computed, child_style)
        for child, child_computed, child_style in zip(child_elements, child_computeds, child_styles)
    ]
    # A majority (not unanimity) qualifies, since each individual check is
    # already narrow. A single floated child is enough on its own though --
    # `float` is always explicit author intent (never a domonic tag-default
    # ambiguity), and CSS 2.1 9.5 has any float narrow its container
    # regardless of how small a fraction of the children it is.
    if not any(box_model._is_floated(cc) for cc in child_computeds) and sum(qualifies) < len(child_elements) * 0.8:
        return
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    # A float (or the block sibling standing beside it) is never stretched
    # vertically: with Flexbox's default `align-content: stretch` a single
    # line inside a container with a definite `height` fills that height,
    # and `align-items: stretch` then stretched every 18px float to 100px
    # (fixed-table-layout-005.xht's `#div1 { height: 100px }` reference).
    style["align_items"] = "flex-start"
    style["align_content"] = "flex-start"
    # `_fix_float_flow_after_block_sibling` needs to know which children
    # were real ordinary blocks -- plain flex-wrap has no notion that a
    # block sibling must force every later floated child onto a fresh line.
    element._chromonic_float_flow_children = list(child_elements)
    element._chromonic_float_flow_qualifies = list(qualifies)
    # An ordinary block child (CSS 2.1 9.2.1) always fills the containing
    # block's full width, `width:auto` or not -- plain flex-wrap would
    # shrink-to-fit it instead, so `build()` forces `flex_basis:100%` for
    # it here; explicit-width blocks are left alone.
    for child, ok, child_computed, child_style in zip(child_elements, qualifies, child_computeds, child_styles):
        if not ok:
            child._chromonic_force_full_row_width = True
        elif box_model._is_floated(child_computed) or isinstance(child_style.get("width"), (int, float)):
            # Any inline-level box with an explicit (non-`auto`) width is
            # never shrink-to-fit -- CSS 2.1 10.3.5 (floats) and the
            # ordinary inline model alike use the specified width outright,
            # free to overflow past the containing block rather than
            # shrink to stay inside it (the same "no min-width:auto floor"
            # fact `_chromonic_force_full_row_width` already relies on, but
            # the opposite problem: here it's `flex_shrink`, not a min-
            # width floor, doing the unwanted shrinking -- flex's plain
            # default `flex-shrink:1` lets this row-packed item give up
            # space to fit the flex line, which nothing in real inline
            # flow ever does). Originally float-only (confirmed directly
            # on floats-rule3-outside-right-001.xht: a lone `float:right`
            # child with `width:425px` inside a 400px-wide flex-wrap
            # container was shrunk to fit at 400px instead of staying
            # 425px and overflowing past the container's own left edge);
            # widened to every explicit-width inline-level child after the
            # identical bug was confirmed for a plain (non-floated)
            # `display:inline-block; width:300px` item, wrongly shrunk to
            # its 200px container instead of overflowing it.
            child._chromonic_no_flex_shrink = True
    inline_tag_qualifies = any(
        box_model._is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
    )
    if inline_tag_qualifies and style["gap"] == (0.0, 0.0):
        # Real inline flow gets its spacing from whitespace text nodes
        # between elements, which this project doesn't measure -- without
        # this, flex-wrap packs elements edge to edge. Approximated as one
        # space width in the container's font; skipped for a purely
        # float-qualified group, which already gets spacing from margins.
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        bold = _fontmetrics.is_bold(computed.fontWeight)
        space_width = _fontmetrics.advance_width(" ", font_size, bold)
        style["gap"] = (0.0, space_width)



def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created. `computed`/`style_obj`, if given, are `element`'s already-
    computed style (the caller's `dom._child_elements` call already needed
    them), so they're never computed twice.

    `is_containing_block`/`escapees` implement CSS's real containing-block
    rule for `position:absolute`/`fixed` -- resolved against the nearest
    ancestor with `position != static`, or the viewport, never just the
    literal DOM parent. Every call defaults to `is_containing_block=True`
    (matching the CSS initial containing block); when true, it owns a
    fresh `escapees` list, and any absolutely-positioned descendant whose
    literal parent isn't a containing block is added to *that* list
    instead of its literal parent's Taffy children, landing it one edge
    from its real containing block. An intermediate `position:static`
    element passes its inherited `escapees` straight through."""
    element.__dict__.pop("_chromonic_flattened_inline", None)  # given a box of its own this pass
    if computed_cache is None:
        computed_cache = {}
    if computed is None or style_obj is None:
        computed, style_obj = dom._describe(element, computed_cache, reuse_styles=reuse_styles)
    style = getattr(element, "_chromonic_native_style", None) if reuse_styles else None
    if style is None:
        # Measured *before* this element's own style is published: the
        # scratch-tree measurement re-runs `build()` on this very element
        # and overwrites its per-pass attributes, which the real pass
        # below then rewrites anyway.
        intrinsic_width = replaced_elements._resolve_intrinsic_width_keyword(element, computed, style_obj, computed_cache)
        style = style_bridge.to_dict(style_obj)
        if intrinsic_width is not None:
            style["width"] = intrinsic_width
        # Not modelled in `LayoutStyle`/`style_bridge.to_dict()` at all, so
        # read straight off `computed` here. CSS 2.1 8.3.1: a non-`visible`
        # `overflow` makes an element establish a new block formatting
        # context, which -- among other things -- stops an in-flow child's
        # margin from collapsing through it. Taffy (`src/lib.rs`) already
        # implements this correctly given `Style.overflow`; any value Rust's
        # `parse_overflow_axis` doesn't recognise falls back to "visible".
        _valid_overflow = ("visible", "clip", "hidden", "scroll", "auto")
        overflow_x = getattr(computed, "overflowX", "visible") or "visible"
        overflow_y = getattr(computed, "overflowY", "visible") or "visible"
        style["overflow"] = (
            overflow_x if overflow_x in _valid_overflow else "visible",
            overflow_y if overflow_y in _valid_overflow else "visible",
        )
        # `justify-items` (CSS Box Alignment 3, grid's own inline-axis
        # counterpart to `align-items` -- flexbox has no such axis) isn't
        # in domonic's recognised-property list at all, so `LayoutStyle`
        # has no field for it and `computed.justifyItems`'s own getter is
        # simply never populated (raises `AttributeError`). The raw
        # cascade dict itself isn't filtered by that list, though --
        # `LayoutStyle.from_computed`'s own `justifySelf` field already
        # reads the identically-unlisted `justify-self` this same way.
        style["justify_items"] = style_bridge._align_keyword(
            Keyword((computed._resolved.get("justify-items") or "").strip().lower()), content=False)
        # `grid-area` (the `grid-row-start / grid-column-start /
        # grid-row-end / grid-column-end` shorthand) isn't expanded into
        # its four longhands by domonic's cascade at all -- each longhand
        # stays its own initial `auto`, so every item using it (extremely
        # common; `grid-area: 1 / 1` places two items in the same cell in
        # `css-grid/grid-items/grid-inline-order-property-painting-*.html`)
        # falls through to ordinary auto-placement instead. Parsed
        # straight from the raw cascade here, the same raw-cascade
        # workaround `justify-items` above already needs.
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
        # Disabled: this predates `flex_grid._is_flex_or_grid_item`'s exclusion of
        # flex/grid items from the generic `width:auto` -> `pct(1.0)`
        # "fill the container" substitution elsewhere in `build()` -- back
        # when *every* auto-width block got that substitution regardless
        # of its parent, a grid item's own `width:100%` (the substitute)
        # read back to Taffy's automatic-minimum-size algorithm as a
        # definite, container-filling minimum, exactly the "feeds the
        # track its full available width" bug this worked around. With
        # that root cause gone, forcing 0 here instead throws away a
        # grid item's *real* automatic minimum (its min-content size,
        # CSS Grid 1 §6.6) -- confirmed on `grid-layout-auto-tracks.html`:
        # `.b`'s own 50px-wide child never contributed to its auto
        # column's width, which came out 0 instead of 50.
        style["min_width"] = 0.0
    if ((getattr(computed, "flexBasis", "") or "").strip().lower() == "content"
            and flex_grid._is_flex_or_grid_item(element)):
        # CSS Flexbox 7.2.3 `flex-basis: content`: the base size is the
        # item's content size, whatever its main-axis `width`/`height`
        # says (flexbox-flex-basis-content-001a.html: `width: 0px` items
        # still size to their text). Taffy has no `content` keyword; the
        # main-axis size is cleared so its `auto` basis measures content.
        parent_native = (dom._layout_parent(element).__dict__.get("_chromonic_native_style") or {})
        if parent_native.get("display") == "flex":
            main = "height" if (parent_native.get("flex_direction") or "row").startswith("column") else "width"
            style["flex_basis"] = "auto"
            style[main] = "auto"
    if isinstance(style.get("flex_basis"), tuple) and flex_grid._is_flex_or_grid_item(element):
        # CSS Flexbox 9.2.3 B: a percentage `flex-basis` against an
        # *indefinite* main size (a column container with `height: auto`)
        # is treated as `content`, and the item's own `height` is then
        # ignored for its base size (flex-basis-010.html: `flex: 0 0 0%;
        # height: 500px` holding a 100px child is 100px tall). Taffy
        # resolves the percentage against nothing and falls back to the
        # `height` instead.
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
        # CSS Flexbox 4 / Grid 6.1: a flex/grid item's `display` is
        # blockified -- an inline `<span>` item keeps its width/height/
        # vertical margins like any block.
        and not flex_grid._is_flex_or_grid_item(element)
    )
    if is_genuinely_inline:
        # CSS 2.1 10.3.1: `width`/`height` never apply to a non-replaced
        # inline box -- Taffy has no inline mode (mapped to plain "block"
        # by `style_bridge._display()`), which would otherwise treat an
        # authored width/height as a hard box size.
        style["width"] = "auto"
        style["height"] = "auto"
        # CSS 2.1 10.3.1/10.6.1: vertical margins likewise don't affect a
        # non-replaced inline's height -- left as ordinary flex-item
        # margins, Taffy would inflate the anonymous inline run's height.
        margin = list(style["margin"])
        margin[0] = margin[2] = 0.0
        style["margin"] = margin
    # CSS 2.1 9.7: an absolutely/fixed positioned element's `display`
    # blockifies regardless of its specified value -- none of the
    # table-internal-only rules below (margin/padding suppression,
    # row/cell classification) apply to it once blockified. Confirmed
    # directly on `top-applies-to-001.xht`/`bottom-applies-to-005.xht`:
    # an absolutely positioned `display:table-row-group`/`-column-group`
    # element must render as an ordinary ("block") absolutely positioned
    # box, not a real table-internal part.
    table_internal_display = (
        "" if box_model._is_absolutely_positioned(style_obj) else getattr(style_obj.display, "value", "")
    )
    if table_internal_display in table_layout._TABLE_INTERNAL_DISPLAYS:
        # CSS 2.1 17.4/CSS Tables 3: margin never applies to an internal
        # table box (row/row-group/cell/column/...) on any side -- only
        # the outer `display:table` box itself keeps it.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        if table_internal_display != "table-cell":
            # Unlike margin, padding *does* still apply to `table-cell`
            # (CSS 2.1 17.6.1 -- a cell's own padding is what visibly
            # separates its content from its border) -- every other
            # internal table box (row/row-group/column/column-group) gets
            # neither, same as margin. Confirmed directly on `wpt/css/
            # CSS2/margin-padding-clear/padding-applies-to-001.xht`
            # (`display:table-row-group; padding:50px` was still adding
            # 50px of space Chrome never does).
            style["padding"] = [0.0, 0.0, 0.0, 0.0]
    if ((getattr(style_obj.display, "value", "") == "inline-block"
            and box_model._trusts_computed_inline(element, tag_name))
            or getattr(style_obj.display, "value", "") in ("inline-flex", "inline-grid")):
        # `inline-block` establishes its own BFC (CSS 2.1 9.2.1), so an
        # in-flow child's margin must not collapse through it -- signalled
        # to Taffy the same way as `overflow`, via `Contain::PAINT`.
        style["establishes_bfc"] = True
    is_table_root = tag_name == "table" or table_layout._is_table_root_display(computed)
    is_table_row = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name == "tr" or table_layout._is_table_row_display(computed))
    is_table_cell = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name in ("td", "th") or table_layout._is_table_cell_display(computed))
    if is_table_root:
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
        # CSS 2.1 17.5: in an `rtl` table (its own `direction`, or HTML
        # `dir="rtl"`) the first column is the rightmost -- every row lays
        # its cells out right-to-left (`row-reverse`, see the row branch)
        # and the collapsed-border grid lines mirror accordingly.
        element._chromonic_table_rtl = dom._element_direction(element, computed) == "rtl"
        row_cells: dict = {}
        content_rows: set = set()
        for cell, row_index, _col, rowspan, _colspan in cells:
            row_cells.setdefault(id(rows[row_index]), []).append(cell)
            # A row a content-bearing cell spans down into counts as
            # having content too: table-height-algorithm-018.xht's
            # `height: 200px` table splits its surplus equally between
            # its two rows although the second row's only own cell is
            # empty -- the `rowspan=2` "Filler Text" cell covers it.
            if table_layout._table_cell_has_content(cell, computed_cache):
                content_rows.update(range(row_index, min(row_index + rowspan, len(rows))))
        for index, row in enumerate(rows):
            row._chromonic_table_cells = row_cells.get(id(row), [])
            row._chromonic_table_row_empty = index not in content_rows
            # CSS 2.1 17.5.5: `visibility: collapse` on a row, or on a
            # row group it sits in, removes the row from the rendering
            # (0px tall, its cells with it -- `_collapse_rows_in`) while
            # its cells still size the columns (row-visibility-001..
            # 004.xht). Rows of a collapsed group collapse with it.
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
            # CSS 2.1 17.6.2: a collapsed border straddles the grid edge,
            # so each box either side of it only ever includes *half* the
            # winning width -- the table's own box included, which is
            # why "the width of the table includes half the table border".
            # `table_layout._resolve_collapsed_table_borders` runs the real 17.6.2.1
            # conflict resolution per grid-line segment (replacing an
            # earlier "widest border anywhere, reserved on all four sides"
            # approximation that couldn't tell a 10px `hidden` from a
            # 10px `solid`, or a table bordered only on one side from one
            # bordered all round); every cell picks its own halves up
            # from `_chromonic_collapsed_cell_borders` in the
            # `is_table_cell` branch below.
            #
            # The table root's box: its own border halved (it only ever
            # draws its half of the perimeter line, exactly like a cell),
            # plus padding making up the difference to half the winning
            # perimeter width on each side -- zero when the table's own
            # border already *is* the widest thing at that edge (a
            # `border` on `table` and none on its cells), the full half
            # when the table has no border of its own and its cells do.
            # Confirmed directly on border-collapse-001.xht (5px cell
            # borders, no table border: 2.5px each side) and border-
            # collapse-005.html (an empty-`<tbody>` table with `border:2px`
            # and no cells at all: 1px each side).
            cell_borders, perimeter = table_layout._resolve_collapsed_table_borders(
                element, computed, rows, cells, column_count, computed_cache,
                rtl=element._chromonic_table_rtl)
            element._chromonic_collapsed_cell_borders = cell_borders
            own = [box_model._numeric_edge(value) for value in style["border"]]
            style["border"] = [value / 2.0 for value in own]
            style.update({
                "box_sizing": "border-box",
                # CSS 2.1 17.6.2: "in this model, a table does not have
                # padding" -- what's here is purely the reserved half of
                # the perimeter border, never author padding.
                "padding": [max(0.0, perimeter[i] / 2.0 - own[i] / 2.0) for i in range(4)],
            })
        else:
            element.__dict__.pop("_chromonic_collapsed_cell_borders", None)
        # Real "auto" table layout (CSS 2.1 17.5.2.2, not `table-layout:
        # fixed`) sizes each column to its widest cell's own content, not
        # an equal row share -- measured once per table so every same-
        # column cell agrees. See `table_layout._compute_table_column_widths`. Applies
        # equally to a literal `<table>` and any `display:table`/`inline-
        # table` arbitrary element -- `table_layout._table_rows`/`_row_cells` (which
        # this calls) recognise a table-row/-cell by computed `display`
        # too, not just tag name.
        # A `table-layout: fixed` table with `width: auto` uses the auto
        # algorithm (CSS 2.1 17.5.2.1 only defines fixed layout for a
        # non-auto width; Chrome does the same): empty-cells-applies-to-
        # 014.xht's `width: 1em` cell still takes its column's 57.78px.
        column_widths = (table_layout._compute_table_column_widths(cells, computed_cache)
                         if computed.tableLayout != "fixed" or style["width"] == "auto"
                         else {"cells": {}, "cells_min": {}, "columns": [], "columns_min": []})
        # CSS 2.1 17.5.2.2: a column element's `width` is that column's
        # minimum width (column-width-001.xht: a `width: 1in` column over
        # a `width: 0.5in` cell makes a 96px column, cell and table).
        columns_list, columns_min_list = column_widths["columns"], column_widths["columns_min"]
        # Auto layout only: fixed layout resolves column elements itself
        # (`table_layout._compute_fixed_column_widths`, or `_enforce_fixed_column_boxes`
        # for a percentage-width table, which must find these lists empty
        # -- fixed-table-layout-023.xht).
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
        # A collapsed column's cells are still laid out at its real width
        # (Chrome sizes the row from that: column-visibility-004.xht's
        # one-glyph collapsed cell makes a 100px row) -- the whole table
        # is laid out as if every column were visible, then the collapsed
        # widths are taken out of the items, rows and table box
        # (`_settle_collapsed_cells_in`).
        element._chromonic_table_columns_uncollapsed = list(column_widths["columns"])
        # CSS 2.1 17.5.3: a table's specified `height` is a *minimum* --
        # the table grows past it when its rows need more, and when they
        # need less the surplus is handed out to the rows (see
        # `_distribute_table_extra_height`, which also needs the original
        # value: the height applies to the table *grid*, captions
        # excluded), never left as empty space inside a fixed-height box
        # the way an ordinary block's `height` would. `min_height` is
        # exactly that semantic in Taffy.
        element._chromonic_table_specified_height = (
            style["height"] if isinstance(style["height"], (int, float)) else None)
        if style["height"] != "auto":
            if style["min_height"] in ("auto", 0.0):
                style["min_height"] = style["height"]
            style["height"] = "auto"
        if tag_name == "table":
            # HTML's UA stylesheet: `table { box-sizing: border-box }` -- a
            # `<table width=200>`/`table { width: 200px }` is 200px across
            # its border box, borders and (spacing) padding included.
            style["box_sizing"] = "border-box"
        elif style["box_sizing"] != "border-box" and not element._chromonic_border_collapse:
            # A `display: table` element keeps CSS's content-box default,
            # but CSS 2.1 17.6.1 defines a table's `width`/`height` as the
            # distance between its inner padding edges -- the border
            # spacing lies *inside* that distance. This project models the
            # perimeter spacing as extra padding, so a definite size is
            # converted to the equivalent border box (specified + the
            # element's own author padding and borders) up front; the
            # spacing padding added later then stays inside it. Confirmed
            # on separated-border-model-004.xht (`width: 200px; padding: 0
            # 50px; border-spacing: 50px 0; border: 100px`): the cell is
            # 100px wide in Chrome, 200 minus both 50px gaps. A percentage
            # size can't be converted here and is left as-is.
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
        # CSS 2.1 17.6.1: `border-spacing` (the gap between adjacent cells,
        # and between a cell and the table's own edge) only applies in the
        # default "separate" border model -- `border-collapse:collapse`
        # ignores it outright. Was unimplemented entirely (`0px` from every
        # cell always packed edge-to-edge) regardless of this table's own
        # spacing, including HTML's own implicit default (a real browser's
        # UA stylesheet gives `<table>` `border-spacing:2px` -- domonic has
        # no UA stylesheet of its own, so `ua_style.py` is this project's
        # substitute for that default, same as every other UA default here).
        if element._chromonic_border_collapse:
            element._chromonic_border_spacing = (0.0, 0.0)
        else:
            parts = (computed.borderSpacing or "0px").split() or ["0px"]

            def spacing_px(text, default):
                # `ex` resolves against the table's own font's real
                # x-height (`domonic_ex_unit_patch`), not `parse_length`'s
                # flat half-an-em guess -- border-spacing-083.xht's
                # `7.5ex` in 20px Ahem is 120px, not 75. Then truncated to
                # whole pixels: Blink stores border-spacing as an integer
                # (`1cm` is 37px there, border-spacing-036.xht, not
                # 37.795).
                text = (text or "").strip()
                if text.endswith("%"):
                    # CSS 2.1 17.6.1: `border-spacing` takes lengths only
                    # -- a percentage declaration is invalid and dropped.
                    # domonic keeps it (logged in PLAN.md), and the valid
                    # declaration it displaced is gone with it, so `0` is
                    # the best available reading (border-spacing-
                    # percentage-001.xht: `0px` then `20%` is 0px in Chrome).
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
                # CSS 2.1 17.6.1: `border-spacing` "may not be negative"
                # -- the declaration is invalid and dropped, leaving the
                # UA default (2px, `ua_style.py`) in force. domonic's
                # cascade accepts the negative value as-is (a domonic
                # bug, logged in PLAN.md), so the drop is reproduced here.
                spacing_h = spacing_v = 2.0
            element._chromonic_border_spacing = (spacing_h, spacing_v)
            if spacing_h:
                # A colspan'd cell's box also covers the gaps between the
                # columns it spans (table-visual-layout-013.xht: two 104px
                # columns and the 2px between them make a 210px cell).
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
                # of the table's own author padding, which the separated
                # model keeps (CSS 2.1 17.6.1: "the distance between the
                # table border and the bordering cell equals table padding
                # + border spacing", separated-border-model-001.xht).
                own_padding = style["padding"]
                style["padding"] = [
                    own_padding[0] + spacing_v if isinstance(own_padding[0], (int, float)) else spacing_v,
                    own_padding[1] + spacing_h if isinstance(own_padding[1], (int, float)) else spacing_h,
                    own_padding[2] + spacing_v if isinstance(own_padding[2], (int, float)) else spacing_v,
                    own_padding[3] + spacing_h if isinstance(own_padding[3], (int, float)) else spacing_h,
                ]
                # `is_table_row` below gives every row a `spacing_v` top
                # margin for the gap *before* it -- correct between two
                # rows, but for the very first displayed row (CSS 2.1
                # 17.5.3 header/body/footer order, not necessarily DOM
                # order) it would double up with the table's own
                # `padding-top` just set above (padding blocks margin
                # collapsing, so the two don't merge into one gap the way
                # two adjoining rows' margins do -- confirmed directly:
                # left in place, the first row sat `2 * spacing_v` below
                # the table's own top edge instead of one gap). Marked
                # here, once, so that row can skip just its own top
                # margin and leave the table's padding to provide it
                # alone.
                for row in rows:
                    row.__dict__.pop("_chromonic_is_first_table_row", None)
                # The first *visible* row skips its top margin; a
                # `visibility: collapse` row takes none either -- CSS 2.1
                # 17.5.5 removes the row and the spacing it brought
                # (row-visibility-004.xht: a collapsed first row leaves a
                # 38px table -- 2px, the 34px row, 2px).
                first_visible = True
                for row in rows:
                    if getattr(row, "_chromonic_row_collapsed", False):
                        row._chromonic_is_first_table_row = True
                    elif first_visible:
                        row._chromonic_is_first_table_row = True
                        first_visible = False
        element._chromonic_table_fixed = computed.tableLayout == "fixed" and style["width"] != "auto"
        if element._chromonic_table_fixed and isinstance(style["width"], (int, float)):
            # CSS 2.1 17.5.2.1 -- see `table_layout._compute_fixed_column_widths`. Needs
            # the table's real content width, so only a definite pixel
            # `width` gets the exact algorithm here; a percentage-width
            # fixed table keeps the flex approximation (specified cells
            # rigid, the rest sharing the remainder equally -- see the
            # cell branch). `columns_min` stays empty: a fixed layout has
            # no min-content floor, content simply overflows.
            spacing_h = element._chromonic_border_spacing[0]
            horizontal = sum(box_model._numeric_edge(v) for v in style["padding"][1::2]) + sum(
                box_model._numeric_edge(v) for v in style["border"][1::2])
            content_width = style["width"] - (horizontal if style["box_sizing"] == "border-box" else 0.0)
            fixed = table_layout._compute_fixed_column_widths(
                element, cells, column_count, element._chromonic_table_columns, content_width,
                spacing_h, computed_cache)
            # CSS 2.1 17.5.2.1: the table is as wide as its columns need
            # when that's more than it specified (fixed-table-layout-
            # 010.xht/-016.xht: four 25px columns make a 100px table out
            # of a 75px one).
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
        # The table's own intrinsic widths, straight from the resolved
        # columns (CSS 2.1 17.5.2.2): its max-content width is what
        # `_fix_table_shrink_to_fit_width` shrinks an auto-width table to
        # (Taffy's own estimate for a row of flex items came out a few px
        # under the real sum of the cells' bases plus padding/borders,
        # and `flex-shrink` then squeezed every cell -- confirmed on
        # border-conflict-w-002.xht, every cell ~1.5px short), and its
        # min-content width is a hard floor (`min_width`) so a table in a
        # too-narrow container overflows it rather than crushing its
        # cells. Both include the inter-column spacing and this box's own
        # padding (the perimeter spacing / collapsed half-borders) and
        # border, matching `box_sizing`.
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
    if is_table_row or (not box_model._is_absolutely_positioned(style_obj)
                        and table_layout._row_group_kind(tag_name, computed) is not None):
        # CSS 2.1 17.6.1: in the separated border model rows, row groups,
        # columns and column groups "cannot have borders" -- a `tr {
        # border: ... }` is simply ignored; in the collapsing model their
        # borders do count, but only as contenders for the shared grid
        # lines (`table_layout._resolve_collapsed_table_borders`, folded into the
        # cells' own halves), never as a box border of their own. Either
        # way the row/row-group box itself carries none.
        style["border"] = [0.0, 0.0, 0.0, 0.0]
        # CSS 2.1 17.4: nor a margin (`table_layout._TABLE_INTERNAL_DISPLAYS` above only
        # catches a computed `display`; a literal `<tr>`/`<tbody>` computes
        # `inline` in domonic, having no UA display rule) --
        # table-visual-layout-002.xht's `tbody, tr { margin: 50px }` must
        # add nothing.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        # CSS 2.1 `width` "applies to all elements but non-replaced inline
        # elements, table rows, and row groups" -- empty-cells-applies-to-
        # 008.xht's `display: table-row-group; width: 1em` sizes nothing
        # (its `height: 1em` does apply: Chrome reports the group 16px
        # tall).
        style["width"] = "auto"
    if is_table_row:
        # Taffy has no table formatting mode -- a plain flex row gives
        # ordinary fixed/equal-column tables the right basic geometry.
        style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap"})
        row_table = dom._layout_parent(element)
        while row_table is not None and not getattr(row_table, "_chromonic_is_table_root", False):
            row_table = dom._layout_parent(row_table)
        # An `rtl` table's row lays its cells out right-to-left. Not via
        # Taffy's `row-reverse`: with zero-basis, flex-grown items (empty
        # cells) it placed every cell at the same end position (confirmed
        # on border-conflict-element-002.xht) -- the row's children are
        # built in reversed DOM order instead (see where `children` is
        # gathered below), which a plain `row` lays out right-to-left.
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
            # `padding-bottom` alone already provides that gap, since
            # nothing after the last row needs to push further. `margin`
            # never actually applies to a table-row itself (CSS 2.1
            # 17.4), so repurposing it here costs nothing a real browser
            # would otherwise show.
            style["margin"] = [spacing_v, 0.0, 0.0, 0.0]
    elif is_table_cell:
        # CSS 2.1 17.4: margin doesn't apply to a cell either (a literal
        # `<td>` computes `inline`, so `table_layout._TABLE_INTERNAL_DISPLAYS` above
        # missed it -- `td { margin: 50px }` in table-visual-layout-002.xht).
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        ancestor = dom._layout_parent(element)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = dom._layout_parent(ancestor)
        if ancestor is not None and getattr(ancestor, "_chromonic_border_collapse", False):
            # CSS 2.1 17.6.2: this cell's box includes half of each of
            # its four collapsed grid lines' *winning* widths -- resolved
            # once for the whole table (`table_layout._resolve_collapsed_table_borders`,
            # see the `is_table_root` branch above) -- regardless of what
            # the cell itself declared: a neighbour's wider border, or a
            # `hidden` one, changes this box's size just as much as its
            # own does. Previously only an auto-width cell was halved at
            # all, and only ever from its own declared width: an explicit
            # `width: 3em` cell kept both full 5px borders (confirmed on
            # border-conflict-style-001.xht -- 58px wide against Chrome's
            # 55). A cell somehow outside the resolved grid falls back to
            # halving its own. Before the column-width basis below, which
            # subtracts these (resolved, not declared) borders.
            resolved = getattr(ancestor, "_chromonic_collapsed_cell_borders", {}).get(id(element))
            style["border"] = (list(resolved) if resolved is not None else
                               [value / 2.0 if isinstance(value, (int, float)) else value
                                for value in style["border"]])
        column_width = None
        if ancestor is not None:
            column_width = getattr(ancestor, "_chromonic_table_column_widths", {}).get(id(element))
        # CSS 2.1 17.5.5 `visibility: collapse` columns: a cell in one is
        # laid out at the column's real width (its content, and so its
        # row's height, exactly as if visible) and afterwards narrowed by
        # the collapsed columns' widths -- to 0px for a cell entirely in
        # collapsed columns -- with everything after it in the row moved
        # up by that plus the one border-spacing gap lost per collapsed
        # column (`_settle_collapsed_cells_in`). Confirmed on column-
        # visibility-001..004.xht.
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
                # CSS 2.1 17.5.2.2: a cell's own `width` is only a *minimum*
                # for its column -- the cell's box is always the column's
                # width, which another cell in the column (wider content,
                # or the same content plus wider collapsed borders) can
                # push past it. `table_layout._compute_table_column_widths` measured
                # this cell with its own `width` in force, so the column
                # already honours it as that minimum; the cell itself now
                # just takes the column. Confirmed on border-conflict-
                # style-005.xht: a `width: 3em` cell whose four collapsed
                # borders all resolved to `hidden` stayed 50px against its
                # column's (and Chrome's) 55.
                style["width"] = "auto"
                # `flex_grow` proportional to the column's own intrinsic width
                # (not uniform `1.0`) so extra room goes mostly to the column
                # that wants it, not a small fixed-content one.
                #
                # A column has one width, and it's the width of every
                # cell's *border box* in it (CSS 2.1 17.5.2 -- the grid's
                # column boundaries are what the cells' outer edges sit
                # on). `_measure_intrinsic_width` measures a cell's border
                # box, but `flex_basis` sizes its content box (`box-sizing`
                # is left alone: switching it to `border-box` would also
                # turn the cell's `min_height`, converted from `height`
                # above, into a border-box minimum), so the cell's own
                # horizontal padding/border comes off the basis here. As a
                # raw content-box basis it was added on top a second time,
                # and two cells in one column with different borders (a
                # collapsed `hidden` edge on one of them, border-conflict-
                # w-001.xht) came out different widths where Chrome keeps
                # both at the column's 50.55px. A percentage padding (not
                # resolvable here) keeps the old raw basis.
                if style["box_sizing"] == "border-box":
                    basis = column_width
                else:
                    horizontal = [style["padding"][1], style["padding"][3],
                                  style["border"][1], style["border"][3]]
                    basis = (column_width - sum(box_model._numeric_edge(v) for v in horizontal)
                             if all(isinstance(v, (int, float)) for v in horizontal) else column_width)
                # An empty column (max-content 0) still has to share the
                # table's surplus width -- a single `<td></td>` in a
                # 100px-wide table is 100px wide in Chrome (anonymous-
                # table-box-width-001.xht), not 0. `1.0`, not something
                # tiny: Flexbox only hands out the *fraction* of the free
                # space equal to the grow factors' sum when that sum is
                # below 1 (a lone `1e-3` grow left that cell 0.1px wide),
                # so the empty column's factor must itself reach 1. Beside
                # a real content column (grow = its own px width) it still
                # gets only a sliver, as Chrome's auto layout has it.
                # The column's min-content width is this cell's own floor
                # too (content-box, so the cell's own padding/borders come
                # off it the same way as the basis) -- `flex-shrink` may
                # take a cell down to it in a too-narrow table, never past.
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
                    # Fixed layout resolved every column exactly -- nothing
                    # left to grow or shrink (CSS 2.1 17.5.2.1).
                    style.update({"flex_grow": 0.0, "flex_shrink": 0.0, "min_width": 0.0})
            else:
                # Colspan'd, or intrinsic measurement failed -- fall back to the
                # original equal-share behaviour rather than guessing.
                style.update({"flex_grow": 1.0, "flex_shrink": 1.0,
                              "flex_basis": 0.0, "min_width": 0.0})
        elif ancestor is not None and getattr(ancestor, "_chromonic_table_fixed", False):
            # A specified-width cell in a fixed-layout table whose own width
            # couldn't be resolved up front (a percentage-width table):
            # rigid at its specified width, the auto cells share the rest.
            style.update({"flex_grow": 0.0, "flex_shrink": 0.0, "min_width": 0.0})
        if collapsed_columns:
            # The row's items overflow it by the collapsed widths until
            # `_settle_collapsed_cells_in` narrows them: nothing may be
            # squeezed to make room meanwhile.
            style["flex_shrink"] = 0.0
        # CSS 2.1 17.5.3: a cell's specified `height` is a minimum too --
        # content that needs more always gets it.
        if style["height"] != "auto":
            if style["min_height"] in ("auto", 0.0):
                style["min_height"] = style["height"]
            style["height"] = "auto"
    is_table_caption = not box_model._is_absolutely_positioned(style_obj) and (
        tag_name == "caption"
        or (getattr(computed, "display", "") or "").strip().lower() == "table-caption")
    parent = dom._layout_parent(element)
    if is_table_caption and parent is not None and getattr(parent, "_chromonic_is_table_root", False):
        # CSS 2.1 17.4: a caption belongs to the *table wrapper box*, not
        # the table box -- it sits above (or, `caption-side: bottom`,
        # below) the table's border/padding/background, spanning the
        # table box's full outer width. Chromonic has no separate wrapper
        # box: the `<table>` element's own Taffy node carries the table
        # box's border/padding and is what the caption is a child of. So
        # the caption's margins compensate: pulled outward past the
        # table's own border+padding on the left/right (its box spans the
        # table's border box), and at the top (or bottom) edge -- while
        # the opposite margin pushes the rows back down (up) by the same
        # amount, so the table box's border+padding still sit *between*
        # the caption and the first (last) row exactly as in Chrome.
        # Confirmed on basic-css-table-001.xht: the caption reported 1px
        # inside the table's 1px border on every side against Chrome's
        # full-width, flush-with-the-top caption. An author margin on the
        # caption still applies on top; a percentage one is left alone.
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
    if getattr(element, "_chromonic_force_full_row_width", False) and style["width"] == "auto":
        # Set by `_approximate_inline_flow` for a non-floated, non-inline
        # block sibling standing in for real float layout -- an explicit
        # author width is left alone; only `auto` needs correcting, since
        # real CSS block flow always fills the containing block.
        style["flex_basis"] = ("pct", 1.0)
        # Flexbox's `min-width:auto` gives every flex item an "automatic
        # minimum size" (roughly its min-content width) that `flex-shrink`
        # normally can't shrink it below -- real CSS Flexbox behavior, and
        # correct for a genuine flex item. But this element isn't one: it's
        # an ordinary block CSS 2.1 9.2.1 lays out at a fixed width (its
        # containing block's width minus its own margins) with no such
        # floor -- its content is free to overflow past that width exactly
        # like any other block, never forcing the *box itself* wider (or,
        # worse, shrinking its own margin to compensate -- confirmed
        # directly: with `min-width:auto` left in place, a 425px-wide
        # child inside this 500px-wide flex-wrap row forced the parent's
        # `flex-basis:100%; margin-right:100px` to resolve as width 425/
        # margin 75 instead of the correct width 400/margin 100 -- Taffy's
        # shrink algorithm, once content hits that auto-minimum floor,
        # shrinks the margin to force a fit rather than letting the box
        # overflow, the one thing real block layout would actually do
        # here). `min-width:0` removes that floor, matching real CSS block
        # sizing; only relevant when the author didn't set their own
        # `min-width` (an explicit one is real author intent, left alone).
        if style["min_width"] == "auto":
            style["min_width"] = 0.0
        # Same content-box-vs-border-box overflow the other two `pct(1.0)`
        # substitutes for real block `width:auto` need fixing (see their
        # own comments) -- a content-box `flex-basis:100%` lets this
        # element's own padding/border stick out past its container
        # instead of being carved out of it.
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
        # CSS Flexbox 5.4 / Grid: `order` reorders the items (stable, so
        # equal orders keep DOM order) -- Taffy lays children out in the
        # order given, so the reordering happens here (flex-order.html;
        # `flexbox-anonymous-items-001.html`'s anonymous items are 0).
        # Absolutely positioned children are handed on unsorted after the
        # in-flow ones; their static position doesn't follow `order`.
        if any(flex_grid._css_order(child_computed) for _child, child_computed, _style in children):
            children = sorted(children, key=lambda entry: flex_grid._css_order(entry[1]))
    if is_table_root and children:
        # CSS 2.1 17.5.3: row-groups always *display* in header/body/
        # footer order regardless of source order (a `<tfoot>` authored
        # first, so its totals reach the network before the body
        # finishes loading, is common markup) -- `table_layout._table_rows` already
        # reorders for column-width *measurement*; this reorders the
        # table's own direct Taffy children the same way so the visual
        # stacking (built from ordinary DOM-order block-child handling,
        # same as any other element) matches. A non-row-group direct
        # child (a bare `<tr>`, or anything else) counts as an implicit
        # body row/group, same as `table_layout._table_rows`'s own default.
        # CSS 2.1 17.4: a `<caption>`/`display:table-caption` sits outside
        # the row groups entirely, above them (`caption-side: top`, the
        # default) or below every one of them (`bottom`) -- never sorted
        # among the body groups the way a bare `<tr>` is.
        captions_top, captions_bottom, header, body, footer = [], [], [], [], []
        for entry in children:
            child_tag = (getattr(entry[0], "tagName", "") or "").lower()
            child_display = (getattr(entry[1], "display", "") or "").strip().lower()
            if box_model._is_absolutely_positioned(entry[2]):
                # CSS 2.1 9.7: blockified and out of flow -- neither a
                # caption nor a row group, and nothing for the table to
                # size around (top-applies-to-015.xht).
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
        # A caption can't be made narrower than its own minimum width (an
        # explicit `width`, or its longest unbreakable word), and the
        # table wrapper -- so the shrink-to-fit table -- is at least that
        # wide (anonymous-table-box-width-001.xht: a `width: 100px`
        # caption over one empty cell makes a 100px table in Chrome).
        # Consulted by `_fix_table_shrink_to_fit_width`.
        caption_min = 0.0
        for caption, caption_computed, caption_style in captions_top + captions_bottom:
            width = style_bridge._len(caption_style.width)
            if isinstance(width, (int, float)):
                # Its border box (table-caption-horizontal-alignment-
                # 001.xht: a `width: 200px` caption with 1px borders makes
                # a 202px table).
                if getattr(caption_style.boxSizing, "value", "") != "border-box":
                    width += sum(_fontmetrics.parse_length(getattr(caption_computed, name, None), default=0.0)
                                 for name in ("paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"))
                caption_min = max(caption_min, width)
            else:
                caption_min = max(caption_min, replaced_elements._measure_min_content_width(caption, computed_cache) or 0.0)
        element._chromonic_table_caption_min_width = caption_min
        if (caption_min > 0.0 and isinstance(style["width"], (int, float))
                and style["box_sizing"] == "border-box" and caption_min > style["width"]):
            # A caption wider than the table's specified width widens the
            # table box itself, as Chrome has it (table-anonymous-block-
            # 003.xht: a `width: 200px` caption over a `width: 100px`
            # table makes a 200px table, cell included).
            style["width"] = caption_min
    if is_table_row and getattr(element, "_chromonic_row_rtl", False):
        children = children[::-1]  # see the `is_table_row` branch above
    element._chromonic_has_layout_children = bool(children)
    if tag_name == "button":
        replaced_elements._apply_button_intrinsic_width(style, element)
    # Replaced/control elements always run their own dedicated branch below
    # -- CSS generated content doesn't apply to them, so a stray
    # `::before`/`::after` rule must not divert them into inline formatting.
    has_pseudo = tag_name not in dom._NO_GENERATED_CONTENT_TAGS and (
        getattr(element, "_chromonic_before_pseudo", None) is not None
        or getattr(element, "_chromonic_after_pseudo", None) is not None
    )
    # A table, row group or row never formats inline content of its own:
    # CSS 2.1 17.2.1 wraps any loose text/inline child in an anonymous
    # cell first (`_normalized_child_nodes`), which is where that content
    # is then laid out.
    is_table_container = is_table_root or is_table_row or (
        not box_model._is_absolutely_positioned(style_obj) and table_layout._row_group_kind(tag_name, computed) is not None)
    # CSS Flexbox 4 / Grid 6.1: every in-flow child of a flex or grid
    # container is a (blockified) flex/grid item, and whitespace-only text
    # is dropped -- the container never formats inline content of its own
    # (real text got its anonymous item from `_wrap_inline_runs`). A
    # container of `<span>`/inline-block children (`flex-direction-
    # column.html`, and every real-site nav bar) previously fell into the
    # inline-formatting path here and laid them out as one text line.
    is_flex_or_grid_container = style["display"] in ("flex", "grid")
    inline_items = (inline_formatting._inline_mixed_content(element, children, element_is_inline=is_genuinely_inline)
                    if (children or has_pseudo) and not is_table_container
                    and not (is_flex_or_grid_container and not has_pseudo) else None)
    # `<td>`/`<th>` have no UA default in domonic, so their computed
    # `display` is uninformatively "inline" -- forced to "block" here so
    # `_InlineFormattingPlan`'s `owner_display` leaves Taffy's own
    # (already-correct, full column-width) box alone instead of narrowing
    # it to just the cell's own text fragment. A `display:table-cell`
    # element on an arbitrary tag has a real, already-correct computed
    # value (`is_table_cell`, from earlier in this function) -- included
    # here for the same "leave Taffy's box alone" reason, not because its
    # own computed value is uninformative too.
    css_display_value = ("block" if (tag_name in ("td", "th") or is_table_cell)
                          else getattr(style_obj.display, "value", "").strip() or "block")
    split_pieces = (inline_formatting._split_inline_flow_around_blocks(
                         element, inline_items, style, css_display_value, computed_cache)
                     if inline_items else None)
    inline_plan = (inline_formatting._make_inline_formatting_plan(element, inline_items, style, css_display_value, computed_cache)
                   if inline_items and split_pieces is None else None)

    if split_pieces is not None:
        # CSS 2.1 9.2.1.1: an inline element split around an in-flow block
        # child -- see `inline_formatting._split_inline_flow_around_blocks`. Each piece
        # becomes its own ordinary block-flow child of `element` (never a
        # flex row): a "plan" piece is one measured text leaf (same
        # machinery as the single-inline-plan case below, just built once
        # per piece instead of once for the whole element), a "block" piece
        # is that child's own real, recursively-built subtree.
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if (style["width"] == "auto" and element._chromonic_tag_name != "body"
                and not flex_grid._is_flex_or_grid_item(element)):
            # Once split, `element` stands in for the sequence of CSS 2.1
            # 9.2.1.1 anonymous block boxes wrapping its own pieces --
            # ordinary block boxes, which always fill their containing
            # block at `width:auto` (Taffy's own "auto" here means shrink-
            # to-fit, not fill, the same reason the plain single-inline-
            # plan branch below needs this identical `("pct", 1.0)`
            # correction) regardless of `element`'s own nominal `display`.
            # Found on `wpt/css/CSS2/linebox/inline-box-001.xht`: a
            # `display:inline` `div1` split around a block child measured
            # `196px` (its own content's shrink-to-fit width) instead of
            # the real `784px` containing block.
            #
            # `<body>` itself is excluded: it already has its own, more
            # accurate root-width machinery (`_constrain_root_to_document_
            # element`/`_apply_root_margin_offset`, accounting for its own
            # UA margin against the true viewport) -- resolving a plain
            # `pct(1.0)` here instead would resolve against the *viewport*
            # directly (body has no further containing block of its own to
            # subtract its margin from), overriding that correct mechanism
            # with a wrong one. Found on `wpt/css/CSS2/normal-flow/block-
            # in-inline-empty-001.xht`: body's own child (a `<span>` with
            # no explicit display, so genuinely inline) split around its
            # block child *inside body's own `_split_inline_flow_around_
            # blocks` call* -- `element` here was body itself -- measuring
            # `800px` (the full viewport) instead of Chrome's `784px`
            # (`800px` minus body's own `8px` left/right UA margins).
            #
            # `box-sizing:border-box` alongside it for the same reason the
            # single-inline-plan branch below needs it: a content-box
            # `100%` would let this element's own padding/border stick
            # out past the container instead of being carved out of it,
            # the way a real auto-width block's actually is.
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
                # A run tagged "escapee" (an out-of-flow absolutely-
                # positioned element mixed into this segment) marks where
                # it sits for static-position purposes; building its real
                # subtree is this function's job, added to `escapees` (its
                # containing block is almost never this split wrapper
                # itself) so it lands one edge from its real one.
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
    elif inline_plan is not None:
        element._chromonic_has_layout_children = True
        # `style["display"]` is already Taffy-mapped ("block" for every
        # non-flex/grid box), so it can't distinguish a genuine block-level
        # element (width:auto stretches to fill its container) from an
        # inline/inline-block one recursively built as an atomic flex item
        # inside an ancestor's flex-row fallback (must stay content-sized).
        # `css_display_value`, the real pre-mapping computed display, does.
        if css_display_value == "block" and style["width"] == "auto" and not flex_grid._is_flex_or_grid_item(element):
            style["width"] = ("pct", 1.0)
            # `width:auto` on a real CSS block *shrinks* to leave room for
            # its own padding/border inside the containing block -- a
            # plain `pct(1.0)` substitute doesn't: Taffy has no `calc(100%
            # - <padding>)` dimension to ask for that directly, so a
            # content-box interpretation of `100%` makes this element's
            # own padding/border stick out past the container instead
            # (confirmed directly: a lone `<div style="padding-left:2em">
            # <span>|</span></div>` overflowed its 784px body by exactly
            # its own padding, landing at 848px). `box-sizing:border-box`
            # makes `100%` mean the *border box* total instead, which is
            # exactly what a real auto-width block's border box already
            # equals regardless of its own padding -- this is a Taffy-
            # geometry-only override (unrelated to whatever `box-sizing`
            # the page's own CSS/CSSOM reports, a separate system, see
            # `_chromonic_native_style`'s own docs), not a display change.
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
        # An out-of-flow (absolutely positioned) element mixed into this
        # inline content is only a marker run in the plan (its static
        # position); its real box is built here and handed to the nearest
        # ancestor that can hold it -- this text leaf has no Taffy
        # children of its own (abspos-inline-001.xht: the `<span
        # class="absolute">` nested two inlines deep in a `<p>` had no box
        # at all).
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
    elif inline_items:
        element.__dict__.pop("_chromonic_inline_plan", None)
        # `paint.py` falls back to drawing raw `textContent` when it
        # believes there are no layout children -- a `::before`/`::after`-
        # only element (no real child elements) reaches here with that
        # flag still unset, so it must be forced True or paint would draw
        # the raw text a second time on top of the fragment built below.
        element._chromonic_has_layout_children = True
        style["display"] = "flex"
        style["flex_direction"] = "row"
        style["flex_wrap"] = "wrap"
        style["align_items"] = "baseline"
        # CSS 2.1 16.2 `text-align`: this flex-row approximation's own
        # `elif inline_items:` mixed-text-and-elements content otherwise
        # always packs to the row's physical start (Taffy's own default,
        # unset `justify_content`), silently ignoring `right`/`center` --
        # `justify-content` is a real, direct equivalent for a *single*
        # unjustified line, which is genuinely what this approximation
        # already models one of (`flex-wrap:wrap` repeats it per wrapped
        # row too, matching real `text-align` applying per line, not once
        # for the whole block). `justify`/`start`/`end` deliberately not
        # remapped here -- no simple `justify-content` distributes text
        # the way real justification does, and `start`/`end` need real
        # `direction` awareness this approximation doesn't have elsewhere
        # either (`_InlineFormattingPlan._apply_text_align` next door
        # makes the same simplification, physical `left`/`right`/`center`
        # only). Confirmed on `wpt/css/CSS2/visudet/line-height-203.html`:
        # a `position:absolute; width:300px; text-align:right` box's own
        # inline-block `<span>` landed flush left instead of at the box's
        # own right edge.
        text_align_value = (getattr(computed, "textAlign", "") or "").strip().lower()
        if dom._element_direction(element, computed) == "rtl":
            # CSS 2.1 9.10: an rtl line lays its atomic inline boxes out
            # right-to-left, packed against the right edge (flexbox-mbp-
            # horiz-001-rtl.xhtml's two inline-block spacers) -- Taffy's
            # `row-reverse` is exactly that; `text-align` then maps with
            # its physical sides swapped (`left` is the reversed row's end).
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
        # Document-order record of every real Taffy child this flex row
        # got (text fragments and real elements alike, `escapee`s excluded
        # -- they never occupy row space) -- `_fix_flex_row_baseline_
        # alignment` needs this exact membership/order to know which
        # children share one wrapped row, since after Taffy's own
        # (sometimes wrong, see that function) baseline placement their
        # `y` positions alone can no longer be trusted to say so.
        row_members = []
        # CSS 2.1 9.5: a float mixed into running text (`<p>text <img
        # style="float:left"> more text</p>`) is pulled out of normal
        # flow -- it doesn't take a slot in the line the way an ordinary
        # inline/inline-block item does, and real text wraps around it.
        # This approximation still lets Taffy place it as an ordinary
        # flex-row member (so it gets a real, content-sized box and a
        # reasonable *line* to sit on, via the row's own wrap point) --
        # `_fix_inline_float_position` below corrects only its final x
        # (flush to the container's own left/right content edge, CSS
        # 2.1 9.5.1) afterward, leaving every other row member's Taffy
        # position untouched. Does not (yet) narrow surrounding text
        # around the float's rectangle -- a real "text reflows around
        # floats" implementation, out of scope here; logged in PLAN.md.
        inline_floats = []
        for item_index, (kind, item, text, child_computed, child_style) in enumerate(inline_items):
            if kind == "element":
                # An absolutely-positioned item counts as "inline" for
                # `inline_formatting._inline_mixed_content` regardless of its real display,
                # so it can land in this flex-row fallback too -- must
                # still escape to its real containing-block ancestor when
                # `element` (its literal DOM parent) isn't one, same as
                # the `elif children:` branch below already does.
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
                        # The collapsed whitespace before this element is a
                        # real space on the line (inline-table-001.xht: the
                        # inline-table after `<span>Filler Text</span>\n`
                        # starts one space, 4px, later) -- a spacer leaf,
                        # since the element's own box can't carry a margin
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
                    # Same fix as `_approximate_inline_flow`'s own
                    # `_chromonic_no_flex_shrink` (see its comment): an
                    # explicit-width inline-level box placed as a row
                    # member here is *also* built as a flex item
                    # (`row_members`, below), and flexbox's plain default
                    # `flex-shrink:1` would otherwise shrink it to fit the
                    # line instead of letting it overflow -- a separate
                    # occurrence of the identical bug, in this function's
                    # own `elif inline_items:` row-building rather than
                    # that one's `elif children:` fallback (confirmed
                    # directly: three plain `display:inline-block` spans
                    # with an explicit `width:300px` in a 200px container
                    # shrank to 200px here even after that other fix).
                    # Always set (not just when true) so a stale flag from
                    # a previous layout pass on a reused element can't
                    # outlive whatever no longer makes it apply.
                    item._chromonic_no_flex_shrink = (
                        not box_model._is_floated(child_computed) and isinstance(child_style.width, Length))
                    normal_child_ids.append(build(
                        tree, item, node_map, computed=child_computed, style_obj=child_style,
                        computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                        reuse_styles=reuse_styles, projection=projection,
                    ))
                    if box_model._is_floated(child_computed):
                        # Never a baseline participant (CSS 2.1 10.8.1
                        # baseline alignment only ever considers in-flow
                        # boxes) -- `_fix_inline_float_position` positions
                        # it afterward.
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
    elif children:
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
                # `element` isn't a valid containing block -- build the
                # child normally, but hand its node id to whichever real
                # ancestor `escapees` belongs to instead.
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
    elif tag_name == "br":
        # CSS 2.1 9.2.2: `<br>` is a forced inline line break -- it never
        # generates an ordinary block box at all, whether or not it sits
        # inside real mixed inline content. Inside a real paragraph's own
        # inline-formatting-plan (`_build_text_runs_from_nodes`'s "break"
        # runs, `_InlineFormattingPlan.measure()`'s own handling of them),
        # this element is never even reached recursively -- but a `<br>`
        # sitting directly among ordinary block siblings, with no
        # surrounding text or inline content to route it through that
        # machinery at all (`inline_formatting._inline_mixed_content`'s own gate correctly
        # declines a container whose other children are genuine blocks),
        # falls all the way through to this ordinary per-tag dispatch
        # instead. `<br>` isn't in `_USUALLY_INLINE_TAGS` (there's no safe
        # tag-based signal for "trust domonic's raw computed inline
        # default" the way there is for `<span>`/`<a>`/..., and none is
        # needed -- `<br>` never behaves like an ordinary inline anyway),
        # so without this it fell through as a plain, untrusted element:
        # an ordinary block, `width:auto` filling the full container and
        # `height:auto` collapsing to `0` with no content of its own.
        # Sized here to a single line's own strut instead -- zero width,
        # one line-height tall, from its own (inherited) font/line-height
        # -- the same font-metrics math `_empty_inline_strut_run` uses for
        # a real empty inline. Found on `wpt/css/CSS2/mpc/padding-top-
        # 036.xht`: a bare `<br />` between two block `<div>`s measured
        # `784x0` instead of Chrome's `0x18`, losing a whole line box's
        # height from the page and every following element's own `y`.
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
        # The break's own inline box (what Chrome reports as its client
        # rect when it shares a line with floats -- see the `<br>` branch
        # of `_fix_float_flow_after_block_sibling`).
        element.__dict__["_chromonic_br_glyph_height"] = ascent + descent
        element.__dict__.pop("_chromonic_br_flow_bottom", None)  # stale from an earlier pass
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    elif tag_name in ("img", "canvas", "svg", "svg:svg", "iframe"):
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
    elif tag_name == "select":
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
    elif tag_name in ("input", "textarea"):
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
    else:
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
