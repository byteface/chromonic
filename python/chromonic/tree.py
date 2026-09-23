"""Walk a live domonic DOM, build a mirroring Taffy tree, run layout, and
write geometry back onto the domonic elements via `element.set_layout_box(...)`.

No dirty-bit tracking: `layout()` is meant to be called again, in full,
after any mutation (see PLAN.md).

Style resolution, not Taffy itself, dominates relayout cost, so `_describe`
below builds exactly one `ComputedStyleDeclaration` per element per pass and
shares it across style-dict-building and `paint.py`'s paint-style extraction."""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
import math

import skia

from domonic import _fontmetrics
from domonic import bs4 as domonic_bs4
from domonic.dom import Element
from domonic.layout import AUTO, Edges, Keyword, LayoutBox, LayoutStyle, Length, _parse_length_or_percent
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from . import fonts, style_bridge, ua_style
from ._native import Tree, layout_text

_log = logging.getLogger(__name__)


def is_rust_panic(error: BaseException) -> bool:
    """Whether `error` is `pyo3_runtime.PanicException` -- what a genuine
    Rust-side panic (a real invariant violation inside Taffy/the `_native`
    extension, e.g. "invalid SlotMap key used") surfaces as in Python.
    Pyo3 deliberately derives it from `BaseException`, not `Exception`
    (its own docs compare it to `SystemExit`), specifically so an ordinary
    `except Exception:` can't accidentally swallow one -- which also means
    every "a bad page must not crash the whole browser" handler in this
    codebase, all written as `except Exception:`, has never actually been
    able to catch one; confirmed directly, a real Rust panic propagated
    straight through several of them. A caller that wants "a page tripped
    a recoverable bug" to include a Rust panic, not just an ordinary
    Python exception, catches `BaseException` and calls this to decide
    whether to handle it or re-raise. Matched by class name/module rather
    than importing `pyo3_runtime` directly, since pyo3 only creates that
    module lazily, the first time a panic actually happens -- it isn't
    reliably importable up front."""
    return type(error).__module__ == "pyo3_runtime" and type(error).__name__ == "PanicException"

# `Utils.case_kebab` backs every `ComputedStyleDeclaration` property read and
# is a pure string transform -- memoized process-wide since it was the
# single largest cost after `ComputedStyleDeclaration` construction itself
# under profiling.
Utils.case_kebab = staticmethod(functools.lru_cache(maxsize=2048)(Utils.case_kebab))

# Selector matchers are pure but re-parse selector text on every candidate
# element; cache them so each distinct selector is parsed once.
Element._parse_simple_selector = staticmethod(
    functools.lru_cache(maxsize=8192)(Element._parse_simple_selector)
)
domonic_bs4._split_simple_selector_chain = functools.lru_cache(maxsize=8192)(
    domonic_bs4._split_simple_selector_chain
)
domonic_bs4._strip_simple_pseudo = functools.lru_cache(maxsize=8192)(
    domonic_bs4._strip_simple_pseudo
)
domonic_bs4._parse_stripped_selector = functools.lru_cache(maxsize=8192)(
    domonic_bs4._parse_stripped_selector
)

ELEMENT_NODE = 1
TEXT_NODE = 3


class _AnonymousTextFragment:
    """Retained layout/paint projection for a direct DOM text node."""

    def __init__(self, source, parent):
        self.source = source
        self.parent = parent
        self.childNodes = []
        self.nodeType = TEXT_NODE
        self._chromonic_tag_name = "#text"
        self._chromonic_has_layout_children = False


class _AnonymousInlineRun(_AnonymousTextFragment):
    """Retained Taffy-only row for consecutive inline element children."""


# CSS 2.1 17.2.1 anonymous table boxes: what `_SyntheticComputed` answers
# for the properties an anonymous box does *not* inherit -- everything
# else (font, color, direction, `border-collapse`/`border-spacing`,
# `caption-side`, `text-align`, `white-space`, ...) is inherited and comes
# from the real parent's computed style.
_ANONYMOUS_COMPUTED_DEFAULTS = {
    "position": "static", "float": "none", "clear": "none",
    "overflowX": "visible", "overflowY": "visible", "verticalAlign": "baseline",
    "width": "auto", "height": "auto", "minWidth": "0px", "minHeight": "0px",
    "maxWidth": "none", "maxHeight": "none", "top": "auto", "right": "auto",
    "bottom": "auto", "left": "auto", "tableLayout": "auto", "boxSizing": "content-box",
    "zIndex": "auto", "opacity": "1", "backgroundColor": "rgba(0, 0, 0, 0)",
    "backgroundImage": "none", "marginTop": "0px", "marginRight": "0px",
    "marginBottom": "0px", "marginLeft": "0px", "paddingTop": "0px", "paddingRight": "0px",
    "paddingBottom": "0px", "paddingLeft": "0px", "borderTopWidth": "0px",
    "borderRightWidth": "0px", "borderBottomWidth": "0px", "borderLeftWidth": "0px",
    "borderTopStyle": "none", "borderRightStyle": "none", "borderBottomStyle": "none",
    "borderLeftStyle": "none", "flexGrow": "0", "flexShrink": "1", "flexBasis": "auto",
    "alignSelf": "auto", "order": "0", "transform": "none", "borderRadius": "0px",
}


class _SyntheticComputed:
    """A computed style for an anonymous table box: its own `display`,
    initial values for every non-inherited property, and the real parent's
    value (or helper method) for anything else."""

    def __init__(self, parent_computed, display: str):
        self.__dict__["_parent"] = parent_computed
        self.__dict__["_display"] = display

    def __getattr__(self, name):
        if name == "display":
            return self.__dict__["_display"]
        if name in _ANONYMOUS_COMPUTED_DEFAULTS:
            return _ANONYMOUS_COMPUTED_DEFAULTS[name]
        return getattr(self.__dict__["_parent"], name)

    def getPropertyValue(self, name):
        camel = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), name.strip().lower().lstrip("-"))
        return getattr(self, camel)


class _AnonymousTableBox:
    """A CSS 2.1 17.2.1 "missing" table box -- an anonymous `table`/
    `inline-table`, `table-row` or `table-cell` generated around
    misparented table content (a `display:table-cell` outside any row, a
    row outside any table, loose text or a plain block inside a row...).
    Not a DOM node: never in anyone's `childNodes` (a wrapped node's real
    `parentElement` is untouched -- `_layout_parent` follows
    `_chromonic_anonymous_parent` instead), reached only through
    `_normalized_child_nodes`. Carries the same `tagName` a real table
    part would so every tag-based check in `build()`/`_table_rows`/
    `_row_cells` treats it as one, and a synthetic style
    (`_chromonic_synthetic_style`, see `_describe`) instead of a cascade."""

    nodeType = ELEMENT_NODE
    _TAGS = {"table": "TABLE", "inline-table": "TABLE", "row": "TR", "cell": "TD", "block": "DIV"}
    _DISPLAYS = {"table": "table", "inline-table": "inline-table", "row": "table-row",
                 "cell": "table-cell", "block": "block"}

    def get_layout_box(self):
        return self.__dict__.get("_layout_box")

    def __init__(self, kind: str, parent):
        self.kind = kind
        self.tagName = self._TAGS[kind]
        self.parentElement = parent
        self.parentNode = parent
        self.childNodes: list = []

    @property
    def ownerDocument(self):
        return getattr(self.parentElement, "ownerDocument", None)

    @property
    def textContent(self):
        return "".join(getattr(node, "textContent", None) or "" for node in self.childNodes)

    def getAttribute(self, _name):
        return None

    def hasAttribute(self, _name):
        return False

    def __repr__(self):
        return f"<anonymous {self.kind} box: {len(self.childNodes)} nodes>"


class _InlineSpacer:
    """A Taffy-only leaf standing in, in the flex-row approximation of
    inline content, for the collapsed whitespace before an element item
    (one space wide, no height). Not a DOM node; never painted or reported."""

    nodeType = None

    def __init__(self, before):
        self.before = before

    def __repr__(self):
        return f"<inline spacer before {getattr(self.before, 'tagName', '?')}>"


class _RowspanPlaceholder:
    """A Taffy-only flex item holding a row's grid slot that a `rowspan`
    cell from an earlier row occupies (CSS 2.1 17.5.3) -- sized like a
    cell of that column, so the row's own cells land in their columns.
    Not a DOM node: never painted, never reported, never a parent."""

    nodeType = None

    def __init__(self, row, column: int):
        self.row = row
        self.column = column

    def __repr__(self):
        return f"<rowspan placeholder: column {self.column}>"


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
    a `_RowspanPlaceholder` leaf ahead of any cell whose grid column
    isn't the next one -- the gap is covered by a cell spanning down
    from an earlier row (table-height-algorithm-010.xht: a `rowspan=10`
    first cell, every later row's only cell sits in column 1). A
    placeholder carries its column's basis/grow/min so it shares the
    row's width exactly as the spanning cell does in its own row."""
    table = _layout_parent(row)
    while table is not None and not getattr(table, "_chromonic_is_table_root", False):
        table = _layout_parent(table)
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
            holder = holders[column] = _RowspanPlaceholder(row, column)
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
        style = _inline_text_style(row_style)
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


def _layout_parent(node):
    """`node`'s parent *box* -- its anonymous table wrapper when CSS 2.1
    17.2.1 generated one around it this pass, else its real DOM parent."""
    anonymous = node.__dict__.get("_chromonic_anonymous_parent") if hasattr(node, "__dict__") else None
    return anonymous if anonymous is not None else getattr(node, "parentElement", None)


_GRID_AREA_SPAN_RE = re.compile(r"^span\s+(\d+)$", re.I)
_GRID_AREA_LINE_RE = re.compile(r"^[+-]?\d+$")


def _parse_grid_area_token(token: str):
    """One `/`-separated `grid-area` component -> the same (line-number,
    `("span", n)`, or `None`-for-auto) shape `src/lib.rs`'s
    `parse_grid_placement` accepts. A named line/area (`<custom-ident>`,
    real but not modelled -- see PLAN.md) falls back to `None`/auto
    rather than guessing a line number."""
    token = token.strip()
    if not token or token.lower() == "auto":
        return None
    span = _GRID_AREA_SPAN_RE.match(token)
    if span:
        return ("span", int(span.group(1)))
    if _GRID_AREA_LINE_RE.match(token):
        return int(token)
    return None  # a named line/custom-ident -- not modelled, falls back to auto


def _parse_grid_area(area: str):
    """CSS Grid 1 §8.3.1 `grid-area: <row-start> [/ <column-start> [/
    <row-end> [/ <column-end>]]]` -- omitted trailing components are
    `auto` (the named-line "inherit the previous component's ident"
    special case doesn't apply here: every component this function
    resolves to a real line number/span is already a plain integer/`span
    N`, never a `<custom-ident>`). Returns `((row_start, row_end),
    (col_start, col_end))`, each component already in `_grid_line()`'s
    output shape."""
    parts = [p.strip() for p in area.split("/")]
    parts += ["auto"] * (4 - len(parts))
    row_start, col_start, row_end, col_end = (_parse_grid_area_token(p) for p in parts[:4])
    return (row_start, row_end), (col_start, col_end)


def _css_order(computed) -> int:
    """The computed `order` (CSS Flexbox 5.4) as an int; 0 when unset,
    unparsable, or for an anonymous item with no computed style."""
    try:
        return int(float(getattr(computed, "order", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _is_flex_or_grid_item(element) -> bool:
    """Whether `element`'s parent box is a real author flex or grid
    container -- then `width: auto` on this block is a flex/grid item's
    content-sized (then flexed/stretched by Taffy) width, never CSS 2.1
    10.3.3's fill-the-containing-block (`align-items-baseline-row-horz.
    html`: `<div>line1<br>line2</div>` items were handed to Taffy as
    `width: 100%` and shrank proportionally instead of sitting at their
    max-content widths)."""
    parent = _layout_parent(element)
    if parent is None or not hasattr(parent, "__dict__"):
        return False
    resolved = parent.__dict__.get("_chromonic_resolved_style")
    if resolved is None:
        return False
    display = getattr(resolved[1].display, "value", "")
    return display in _FLEX_DISPLAYS or display in ("grid", "inline-grid")


class _PseudoElement:
    """A `::before`/`::after` generated box -- not a real DOM node, just
    enough surface for `build()`/`paint.py` to treat it like a childless
    element. Never appears in real `childNodes` -- reached only via
    `_inline_mixed_content`'s synthesized items and `_chromonic_inline_fragments`."""

    def __init__(self, owner, which):
        self.owner = owner
        self.which = which
        self.parentElement = owner
        self.childNodes = ()
        self.text = ""

    @property
    def ownerDocument(self):
        return getattr(self.owner, "ownerDocument", None)

    @property
    def tagName(self):
        return "::" + self.which

    @property
    def textContent(self):
        return self.text

    def getAttribute(self, _name):
        return None


def _get_pseudo_object(element, which: str) -> "_PseudoElement":
    cache = element.__dict__.setdefault("_chromonic_pseudo_objs", {})
    obj = cache.get(which)
    if obj is None:
        obj = cache[which] = _PseudoElement(element, which)
    return obj


class _InlineFormattingPlan:
    """Measured shared line boxes for one block's mixed inline contents."""

    def __init__(self, element, runs, parent_style, owner_display):
        self.element = element
        self.runs = runs
        self.parent_style = parent_style
        self.owner_display = owner_display
        self.fragments = []
        self.owner_boxes = {}
        self.height = 0.0
        # CSS 2.1 9.10: a `direction: rtl` block's own line boxes start from
        # its *right* edge -- glyph order within a same-direction run (plain
        # Latin text here; full bidi reordering across mixed-direction runs
        # is out of scope) stays untouched, only each line's *position*
        # mirrors. `element` is the block establishing this inline
        # formatting context (never a nested wrapper's own `direction` --
        # that would need a real embedding, `unicode-bidi: embed/isolate`,
        # not implemented), so its own computed `direction` governs every
        # plan built for it, split or not.
        computed = getattr(element, "_chromonic_computed_style", None)
        self.rtl = _element_direction(element, computed) == "rtl"
        # `text-align`/`text-align-last` inherit normally through domonic's
        # own cascade -- `element` here is the block establishing this
        # formatting context (the split wrapper, or the plan's own element
        # for a non-split plan), so its computed value already reflects
        # whatever an ancestor (e.g. the real containing block a split
        # wrapper's own anonymous-block pieces belong to) declared.
        self.text_align = (getattr(computed, "textAlign", "") or "start").strip().lower()
        self.text_align_last = (getattr(computed, "textAlignLast", "") or "auto").strip().lower()
        # CSS 2.1 16.1: `text-indent` inherits normally and, same as
        # `text-align`, applies to *this* plan's own first formatted line
        # -- each CSS 2.1 9.2.1.1 split segment is its own anonymous block
        # box, so its own first line gets indented independently, not just
        # the wrapper's overall first one.
        self.text_indent = _resolve_text_indent(computed)

    def measure(self, available_width, _available_height, _known_width=None, _known_height=None):
        width = float(available_width or 0.0)
        if width < 0:
            # `src/lib.rs`'s `MinContent` sentinel: lay out at (almost)
            # zero width so every break opportunity is taken and the
            # reported `content_width` is the widest unbreakable piece.
            width = 1.0
        elif width <= 0 or width > 1_000_000:
            width = sum(run.get("intrinsic_width", 0.0) for run in self.runs)
        self._measured_width = width  # `publish()` needs this to mirror a `<br>`'s own box for RTL
        base_height = _resolved_line_height(self.parent_style["line_height"])
        base_font = _fontmetrics.parse_length(self.parent_style["font_size"], default=16.0)
        base_ascent, base_descent, normal = fonts.text_metrics(
            self.parent_style["font_family"], base_font,
            _parse_font_weight(self.parent_style["font_weight"]) >= 600,
            fonts.is_italic(self.parent_style["font_style"]))
        base_height = base_height or normal
        base_above = base_ascent + math.floor((base_height - base_ascent - base_descent) / 2)
        base_below = base_height - base_above
        # CSS 2.1 16.1: `text-indent` only ever offsets the block's own
        # first formatted line -- every later line (after a wrap or a
        # `<br>`) resets `x` to `0.0` already, unaffected.
        x = self.text_indent
        # A `<br>`'s own reported position sits right after the preceding
        # text's advance, never including that run's own trailing border/
        # padding/margin-end -- confirmed directly against `left-rtl-
        # ref.xht`'s real geometry: a `direction:rtl` first fragment's own
        # *end*-side package (`padding-right`/`margin-right`) genuinely
        # inflates that fragment's own box width, but a `<br>` immediately
        # following the text is positioned before that decoration, not
        # after it (the decoration renders past the wrap point instead).
        # Tracked separately from `x` since `x` itself must still carry the
        # trailing/margin-end forward for whatever follows on the same line.
        x_pre_trailing = x
        y = 0.0
        line_margin_start = 0.0  # how much of the current line's `x` is `margin_start`, not content
        line_leading_total = 0.0  # and how much is a leading border/padding edge
        above, below = base_above, base_below
        self._line_baselines = {}
        self._line_belows = {}
        self._line_has_content = {}
        line_has_content = False
        # run index -> (x, y, line height, line's own margin_start, line's own leading edge)
        self._break_positions = {}
        # For a `direction:rtl` plan, a `<br>` mirrors to the *start* (post-
        # mirror box `x`, not the un-mirrored text-end point) of whichever
        # real run immediately preceded it -- tracked here so it can be
        # resolved once `placed` itself has been mirrored, below.
        break_precedes_run: dict = {}
        last_real_run = None
        # id(escapee element) -> (x, y): an out-of-flow `top/left:auto`
        # absolutely-positioned element mixed into inline content still has
        # a real CSS 2.1 10.3.7/10.6.4 "static position" wherever it falls
        # in the surrounding text; `publish()` turns this into a page position.
        self._escapee_positions = {}
        placed = []
        for run_index, run in enumerate(self.runs):
            if run.get("escapee"):
                # Doesn't occupy space -- record where the cursor already
                # was and move on, unlike a forced line-break above. A
                # block-level escapee (CSS 2.1 10.3.7: its static position
                # is where a block would have started -- on the line
                # after the current one, at the line's start) goes below
                # the line in progress (abspos-007.xht: `<div class="test">`
                # between text and a block, at x 8 / y 26, not at the text's
                # end); an inline-level one sits where the text is.
                tag = (getattr(run["element"], "tagName", "") or "").lower()
                if tag in _USUALLY_INLINE_TAGS:
                    self._escapee_positions[id(run["element"])] = (x, y)
                else:
                    self._escapee_positions[id(run["element"])] = (
                        0.0, y + (above + below) if last_real_run is not None else y)
                continue
            if run.get("break"):
                # Forced line-break: flush the current line and move to the next.
                self._line_baselines[y] = above
                self._line_belows[y] = below
                self._line_has_content[y] = line_has_content
                self._break_positions[run_index] = (x_pre_trailing, y, above + below, line_margin_start, line_leading_total)
                break_precedes_run[run_index] = last_real_run
                y += above + below
                x = 0.0
                x_pre_trailing = 0.0
                last_real_run = None
                line_margin_start = 0.0
                line_leading_total = 0.0
                above, below = base_above, base_below
                line_has_content = False
                continue
            tokens = run["tokens"]
            for index, (text, token_width) in enumerate(tokens):
                leading = run["leading"] if index == 0 else 0.0
                trailing = run["trailing"] if index == len(tokens) - 1 else 0.0
                line_leading_total += leading
                # margin-start shifts the whole run right on the first token
                # only — not carried onto subsequent lines after a <br>.
                if index == 0:
                    x += run.get("margin_start", 0.0)
                    line_margin_start += run.get("margin_start", 0.0)
                advance_width = (max(token_width, run["atomic_width"])
                                 if len(tokens) == 1 else token_width)
                total = leading + advance_width + trailing
                fit_total = total - (run["space_width"] if text[-1:].isspace() else 0.0)
                following_space = 0.0
                if index == len(tokens) - 1 and run["owner"] is not self.element:
                    for later in self.runs[run_index + 1:]:
                        if later.get("break"):
                            break
                        if later.get("escapee"):
                            # Out of flow -- contributes no text/tokens of
                            # its own, so it can't be "the next token" for
                            # trailing-space purposes; skip past it to
                            # whatever real run actually follows.
                            continue
                        if later["tokens"]:
                            if not later["tokens"][0][0].strip():
                                following_space = later["tokens"][0][1]
                            break
                if x and x + fit_total + following_space > width and text.strip():
                    self._line_baselines[y] = above
                    self._line_belows[y] = below
                    self._line_has_content[y] = line_has_content
                    y += above + below
                    x = 0.0
                    x_pre_trailing = 0.0
                    line_margin_start = 0.0
                    line_leading_total = 0.0
                    above, below = base_above, base_below
                    line_has_content = False
                token_height = run["box_height"]
                above = max(above, run["above"])
                below = max(below, run["below"])
                # A CSS 2.1 9.2.1.1 split segment's own decoration-only
                # marker (`_empty_decoration_only_run`) never counts as
                # real content by itself -- only an actual text/strut run
                # sharing its line does, which is what `publish()` uses to
                # decide whether the marker should inherit that line's real
                # geometry instead of staying an isolated 0x0.
                if not (run.get("empty_strut") and run["ascent"] == 0.0
                        and run["above"] == 0.0 and run["below"] == 0.0):
                    line_has_content = True
                placed.append((run, text, x + leading, y, token_width, token_height,
                               leading, trailing, advance_width))
                x_pre_trailing = x + leading + advance_width
                last_real_run = run
                x += total
                if index == len(tokens) - 1:
                    # A non-replaced inline's horizontal margins are real,
                    # non-collapsing spacing (CSS 2.1 10.3.1/10.3.3) --
                    # `margin_start` already shifted the cursor before this
                    # run's first token; this is margin-end, added once
                    # after the last, kept out of the run's own width.
                    x += run.get("margin_end", 0.0)
        self._line_baselines[y] = above
        self._line_belows[y] = below
        self._line_has_content[y] = line_has_content
        # CSS 2.1 9.4.2: a line box collapses to zero height when every run
        # on it is a zero-edge empty strut (no text, no border/padding/
        # margin) -- any real content alongside one keeps normal height.
        is_all_zero_edge_empty = placed and all(
            run.get("empty_strut") and run["leading"] == 0.0 and run["trailing"] == 0.0
            and run["box_height"] <= run["glyph_height"] + 1e-6 and run.get("margin_start", 0.0) == 0.0
            for run in self.runs if not run.get("break") and not run.get("escapee")
        )
        if last_real_run is None and any(run.get("break") for run in self.runs):
            # Nothing placed after the final forced break: the line it
            # opened holds no content and collapses (CSS 2.1 9.4.2) --
            # `a<br>` is one line, `a<br><br>` two, and a `<br>` alone
            # still one (the break sits on the line it ends). Chrome on
            # table-height-algorithm-004.xht: ten `X<br />` lines make a
            # 200px cell, not 220.
            self.height = y
        else:
            self.height = 0.0 if is_all_zero_edge_empty else (y + above + below if placed else 0.0)
        if is_all_zero_edge_empty:
            # Each such strut's own fragment reports zero height too, not
            # its font-metrics `box_height` -- the line it sits on doesn't exist.
            placed = [
                (run, text, x, y, token_width, 0.0, leading, trailing, advance_width)
                for run, text, x, y, token_width, _token_height, leading, trailing, advance_width in placed
            ]
        content_width = min(width, max(
            (px + advance for _r, _t, px, _y, _pw, _h, _l, _tr, advance in placed), default=0.0))
        # An explicit physical `text-align:left` overrides `direction:rtl`'s
        # own default right-mirroring below -- CSS 2.1 9.10's own initial
        # `start` value is what resolves to "right" for rtl, not `left`.
        rtl_mirror_suppressed = self.text_align == "left"
        if self.rtl and not rtl_mirror_suppressed and placed:
            # `direction:rtl` mirrors each line, as a rigid group, against
            # the same `width` it was placed within. Each fragment's own
            # `margin_end` (physical-right margin, already attached to
            # whichever fragment actually owns it -- see the direct-child
            # bidi-box-model edge-swap in `_make_inline_formatting_plan`)
            # shifts its mirrored box left by that amount, same as it would
            # shift a plain LTR box's cursor rightward before mirroring --
            # confirmed directly against `right-rtl-ref.xht`'s real
            # geometry: only subtracting on the whole line's last placed
            # entry (the pre-mixed-direction-support original here) is
            # wrong whenever that entry isn't the one actually carrying the
            # margin (e.g. a bidi-box-model split's first, not last,
            # fragment owns the trailing package for `direction:rtl`).
            # A CSS 2.1 9.2.1.1 block-in-inline split (`_split_wrapping_
            # inline_element`) never threads its own trailing margin through
            # a run's `margin_end` field at all (only `margin_start`, on its
            # first segment) -- so for that case, the wrapper's own overall
            # last placed fragment still needs its raw CSS margin-right
            # applied directly here, same as before mixed-direction support
            # existed. Skipped whenever `margin_end` is already nonzero
            # (this plan's own bidi-box-model build already threaded it
            # correctly there -- see `_make_inline_formatting_plan`), to
            # avoid double-counting it.
            is_final_segment = getattr(self, "_chromonic_final_split_fragment", True)
            last_index_for_owner: dict = {}
            for i, entry in enumerate(placed):
                last_index_for_owner[id(entry[0].get("owner"))] = i
            mirrored = []
            for index, (run, text, px, y, token_width, token_height, leading, trailing, advance) in enumerate(placed):
                margin_start = run.get("margin_start", 0.0)
                margin_end = run.get("margin_end", 0.0)
                if (not margin_end and not run.get("_bidi_margin_resolved") and is_final_segment
                        and index == last_index_for_owner.get(id(run.get("owner")))):
                    owner_style = getattr(run.get("owner"), "_chromonic_native_style", None) or {}
                    owner_margin = owner_style.get("margin") or (0.0, 0.0, 0.0, 0.0)
                    margin_end = _numeric_edge(owner_margin[1])
                outer_left = px - leading - margin_start
                outer_width = leading + advance + trailing
                new_px = (width - outer_left - outer_width - margin_end) + leading
                mirrored.append((run, text, new_px, y, token_width, token_height, leading, trailing, advance))
            placed = mirrored
            # A `<br>`'s own position mirrors to the *text* start (the
            # mirrored `px`, not the box's own outer `x`) of whichever real
            # run immediately preceded it -- confirmed against both `left-
            # rtl-ref.xht` (a zero-leading fragment, where box `x` and text
            # `x` coincide) and `right-ltr-ref.xht` (a nonzero-leading
            # nested `ltr` fragment inside a `rtl` base, where only the text
            # position -- not the box's outer edge including its own
            # leading decoration -- matches real Chrome).
            run_text_x = {id(run): new_px for run, _t, new_px, _y, _tw, _th, _l, _tr, _a in placed}
            for run_index, preceding_run in break_precedes_run.items():
                if preceding_run is not None and id(preceding_run) in run_text_x:
                    old = self._break_positions[run_index]
                    self._break_positions[run_index] = (run_text_x[id(preceding_run)],) + old[1:]
        elif not self.rtl and placed:
            # This plan's own base direction is `ltr` (no plan-wide mirror
            # applies). A *nested* `direction:rtl` element's own start/end
            # edge assignment was already resolved at build time (see
            # `_build_text_runs_from_nodes`'s nested-element branch, which
            # attaches the nested element's border/padding/margin package to
            # the physically-correct side per its own direction) -- each
            # fragment's build position is therefore already correct, and no
            # position-mirroring step is needed here at all for a nested
            # override; only ordinary physical `text-align` applies.
            placed = self._apply_text_align(placed, width)
        self._placed = placed
        return (content_width, self.height)

    def _apply_text_align(self, placed, width):
        """CSS Text 3 `text-align`/`text-align-last`: shift each line's
        placed tokens to reflect the block's own alignment, physical
        `left`/`right`/`center` only (no `direction`-aware `start`/`end`
        remapping -- not needed for LTR, and `direction:rtl` is handled
        separately above, via the mirror). `justify` distributes leftover
        space as extra spacing at each token boundary that ends in real
        whitespace, on every line but the last -- CSS's own line box is
        never justified unless `text-align-last:justify` says otherwise;
        each split segment is its own anonymous block box (CSS 2.1
        9.2.1.1), so its own last physical line gets this treatment
        independently of any other segment's."""
        self._justified_lines = set()
        if not placed:
            return placed
        text_align = "left" if self.text_align in ("start", "") else (
            "right" if self.text_align == "end" else self.text_align)
        text_align_last = self.text_align_last
        if text_align_last in ("auto", ""):
            # CSS Text 3: `auto` means "ordinary `text-align`", except a
            # `justify` block's own last line is never force-justified by
            # this default -- it aligns `start` (left) instead.
            text_align_last = "left" if text_align == "justify" else text_align
        text_align_last = "left" if text_align_last in ("start", "") else (
            "right" if text_align_last == "end" else text_align_last)
        if text_align == "left" and text_align_last == "left":
            return placed
        lines: list = []
        current: list = []
        current_y = None
        for entry in placed:
            if current_y is None or abs(entry[3] - current_y) > 0.01:
                if current:
                    lines.append(current)
                current = []
                current_y = entry[3]
            current.append(entry)
        if current:
            lines.append(current)
        result: list = []
        for line_index, line_entries in enumerate(lines):
            align = text_align_last if line_index == len(lines) - 1 else text_align
            if align == "left":
                result.extend(line_entries)
                continue
            line_start = min(e[2] - e[6] for e in line_entries)
            line_end = max(e[2] + e[8] + e[7] for e in line_entries)
            slack = width - (line_end - line_start)
            if align == "justify":
                gap_after = [i for i, e in enumerate(line_entries[:-1]) if e[1][-1:].isspace()]
                if not gap_after or slack <= 0:
                    result.extend(line_entries)
                    continue
                extra_per_gap = slack / len(gap_after)
                gap_set = set(gap_after)
                cumulative = 0.0
                # `publish()`'s own trailing-whitespace collapse (correct
                # for an ordinary, un-justified line) would otherwise trim
                # this same slack right back off the last token's reported
                # width -- indistinguishable there from a line's ordinary
                # trailing space once distributed. This line's own real,
                # filled extent needs recording so `publish()` can skip it.
                self._justified_lines.add(line_entries[0][3])
                for index, (run, text, px, y, token_width, token_height, leading, trailing, advance) in enumerate(line_entries):
                    result.append((run, text, px + cumulative, y, token_width, token_height, leading, trailing, advance))
                    if index in gap_set:
                        cumulative += extra_per_gap
                continue
            shift = max(0.0, slack) if align == "right" else max(0.0, slack) / 2.0
            result.extend(
                (run, text, px + shift, y, token_width, token_height, leading, trailing, advance)
                for run, text, px, y, token_width, token_height, leading, trailing, advance in line_entries
            )
        return result

    def publish(self, box, padding, owner_accum, element_fragments_accum):
        origin_x = box.x + box.border_left + padding[3]
        origin_y = box.y + box.border_top + padding[0]
        escapee_positions = getattr(self, "_escapee_positions", None)
        if escapee_positions:
            # A later pass applies each escapee's real page-coordinate
            # static position once every element's box has been written.
            for run in self.runs:
                if run.get("escapee"):
                    position = escapee_positions.get(id(run["element"]))
                    if position is not None:
                        run["element"]._chromonic_static_position = (
                            origin_x + position[0], origin_y + position[1])
        self.fragments = []
        owner_rects = {}
        grouped = {}
        placed = getattr(self, "_placed", ())
        for placed_index, (run, text, x, y, width, token_height, leading, trailing, advance) in enumerate(placed):
            ends_line = placed_index + 1 == len(placed) or placed[placed_index + 1][3] != y
            # A `text-align:justify` line's own trailing space was already
            # redistributed into real, visible inter-word gaps -- nothing
            # natural is left over there to collapse (see `_apply_text_
            # align`'s own `_justified_lines`).
            collapsed_space = (run["space_width"]
                               if text[-1:].isspace() and ends_line
                               and y not in getattr(self, "_justified_lines", ()) else 0.0)
            visual_width = max(0.0, width - collapsed_space)
            visual_advance = max(0.0, advance - collapsed_space)
            font_size = run["font_size"]
            glyph_height = min(token_height, run["glyph_height"])
            glyph_y = y + self._line_baselines[y] - run["ascent"]
            key = (id(run["source"]), y)
            entry = grouped.get(key)
            # A genuinely empty inline's own strut run (`_empty_inline_
            # strut_run`, one `("", 0.0)` token) places a real element
            # rect (via `owner_rects` below) but is never a text-range
            # fragment -- there's no source text node at all, and real
            # Chrome's own `getClientRects()` for such an element reports
            # zero *text* fragments (only the element/box ones). Grouping
            # it here anyway would synthesize a spurious empty-string
            # entry in `_chromonic_owned_fragments`.
            if entry is None and text == "":
                pass
            elif entry is None:
                fragment = _AnonymousTextFragment(run["source"], self.element)
                fragment.owner = run["owner"]
                fragment._chromonic_paint_style = run["paint_style"]
                fragment._chromonic_text_lines = [text]
                fragment._chromonic_text_line_widths = [visual_width]
                fragment._chromonic_line_height = glyph_height
                # CSS 2.1 9.4.3: a `position: relative` inline (or inline
                # ancestor) moves its fragments by its offsets, layout
                # otherwise untouched (position-relative-002.xht: a
                # `top: 25px` span's text sits 25px below its line).
                rel_dx, rel_dy = _inline_relative_offset(run["owner"], self.element, box)
                fragment._layout_box = LayoutBox(
                    x=origin_x + x + rel_dx, y=origin_y + glyph_y + rel_dy,
                    width=visual_width, height=glyph_height,
                    client_width=visual_width, client_height=glyph_height,
                )
                grouped[key] = fragment
                self.fragments.append(fragment)
            else:
                entry._chromonic_text_lines[0] += text
                old = entry._layout_box
                rel_dx, _rel_dy = _inline_relative_offset(run["owner"], self.element, box)
                combined_width = origin_x + x + rel_dx + visual_width - old.x
                entry._chromonic_text_line_widths[0] = combined_width
                entry._layout_box = LayoutBox(
                    x=old.x, y=old.y, width=combined_width, height=old.height,
                    client_width=combined_width, client_height=old.client_height,
                )
            owner = run["owner"]
            # A CSS 2.1 9.2.1.1 split segment's own decoration-only marker
            # run (`_empty_decoration_only_run`: no font/line-height
            # contribution of its own -- see its docstring) normally sits
            # alone on its own dedicated zero-height line. But when real
            # sibling content (e.g. the text pending before a leading
            # segment) shares that same line, the marker doesn't get a
            # second, independent line of its own -- it's simply nowhere
            # (no ascent/above/below of its own to place a baseline
            # against), and belongs at the *top* of the real line, sized to
            # that line's own real height, not its own zero one.
            is_decoration_only_marker = (
                run.get("empty_strut") and run["ascent"] == 0.0
                and run["above"] == 0.0 and run["below"] == 0.0
            )
            if is_decoration_only_marker:
                # No ascent of its own to place a baseline against --
                # always the top of whatever line it's on, whether that's
                # a real shared line (sized to that line's own height) or
                # its own isolated, genuinely-empty one (0x0, unchanged).
                owner_y = origin_y + y
                marker_height = (self._line_baselines[y] + self._line_belows[y]
                                  if self._line_has_content.get(y) else token_height)
            else:
                owner_y = origin_y + (y if run["atomic_width"] else glyph_y - run["top_edge"])
                marker_height = token_height
            rel_dx, rel_dy = _inline_relative_offset(owner, self.element, box)
            rect = (origin_x + x - leading + rel_dx, owner_y + rel_dy,
                    visual_advance + leading + trailing, marker_height)
            # Split/document-order segment index (`None` for a non-split
            # owner), so `_finalize_inline_owner_boxes` can place
            # interruption-marker rects logically, not via a geometric sort.
            owner_rects.setdefault(owner, []).append((rect, run.get("split_group")))
        # `inline-block`/block owners keep their atomic Taffy box; a real
        # `display:inline` owner's rects come from `owner_rects[self.element]`
        # instead, merged per-line then unioned for getBoundingClientRect().
        #
        # Accumulated into `owner_accum`, not finalized here: a split owner
        # (CSS 2.1 9.2.1.1) publishes from multiple independent plans, one
        # per segment -- `_finalize_inline_owner_boxes` unions them all once,
        # after every plan sharing `owner_accum` has run, instead of the
        # last plan to publish overwriting the earlier ones.
        owner_is_inline = self.owner_display == "inline"
        for owner, rect_group_pairs in owner_rects.items():
            if owner is self.element and not owner_is_inline:
                continue
            entry = owner_accum.get(id(owner))
            if entry is None:
                entry = owner_accum[id(owner)] = (owner, {}, [])
            groups = entry[1]
            for rect, group in rect_group_pairs:
                groups.setdefault(group, []).append(rect)
            entry[2].extend(fragment for fragment in self.fragments if fragment.owner is owner)
        # Publish layout boxes for <br> elements sized to the line-box height
        # so the harness reports the correct height (Chrome: 18px, not 0).
        for run_index, run in enumerate(self.runs):
            if run.get("break"):
                br_x, br_y, line_h, _line_margin_start, _line_leading_total = self._break_positions.get(
                    run_index, (0.0, 0.0, 0.0, 0.0, 0.0))
                # For `self.rtl`, `measure()` itself already resolved `br_x`
                # to its mirrored position (the preceding real run's own
                # post-mirror box start) -- no further adjustment needed here.
                # The box is the `<br>`'s own inline box -- its font's
                # glyph height (ascent + descent), sat on the line's
                # baseline -- not the line box: Chrome reports a `<br>` in
                # `font: 20px/1 serif` as 23px tall starting 2px above its
                # 20px line (separated-border-model-004a.xht).
                br_paint = getattr(run["element"], "_chromonic_paint_style", None) or self.element._chromonic_paint_style
                br_size = _fontmetrics.parse_length(br_paint.get("font_size"), default=16.0)
                br_family = "" if br_paint.get("font_family") in (None, "none") else br_paint.get("font_family")
                br_ascent, br_descent, _normal = fonts.text_metrics(
                    br_family, br_size, _parse_font_weight(br_paint.get("font_weight")) >= 600,
                    fonts.is_italic(br_paint.get("font_style")))
                br_height = br_ascent + br_descent
                br_top = br_y + self._line_baselines.get(br_y, br_ascent) - br_ascent
                run["element"].__dict__["_layout_box"] = LayoutBox(
                    x=origin_x + br_x, y=origin_y + br_top,
                    width=0.0, height=br_height,
                    client_width=0.0, client_height=br_height,
                )
                run["element"]._chromonic_has_layout_children = False
        # Same accumulate-not-overwrite reasoning as `owner_accum` above,
        # for `self.element`'s own painted fragments.
        elem_entry = element_fragments_accum.get(id(self.element))
        if elem_entry is None:
            elem_entry = element_fragments_accum[id(self.element)] = (self.element, [])
        elem_entry[1].extend(self.fragments)

# Metadata/logic tags a real browser hardcodes as never painting a box;
# domonic's cascade gives these no such default on its own. `colgroup`/`col`
# (CSS 2.1 17.2.1: `display:table-column-group`/`table-column` "are not
# rendered" -- they exist purely as column-styling/-sizing metadata, never a
# box) belong here too: without it, they fell through to ordinary block
# treatment, taking a real flow position and height between a table's
# `<caption>` and its row-groups -- confirmed directly on a `<colgroup>` +
# `<thead>`/`<tbody>`/`<tfoot>` table, where the extra slot corrupted the
# header/body/footer reordering (CSS 2.1 17.5.3) badly enough that `<tbody>`
# ended up overlapping `<colgroup>`'s claimed position and `<thead>` landed
# *above* the caption instead of below it.
_NON_RENDERING_TAGS = frozenset({
    "script", "style", "head", "title", "meta", "link", "noscript", "template",
    "colgroup", "col",
})


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _child_nodes(element):
    """Iterate children without constructing Domonic's live NodeList.

    Domonic's authoritative Python child collection is ``args`` (its own
    ``__iter__`` delegates straight to it). ``childNodes`` constructs a fresh
    live-list wrapper and copies that tuple on every iteration, which is
    needlessly expensive in layout's repeated whole-tree walks. Chromonic's
    synthetic boxes are not Domonic nodes, so retain their small
    ``childNodes`` list as a fallback.
    """
    if isinstance(element, Element):
        return element.args
    return getattr(element, "childNodes", None) or ()


def _element_direction(element, computed=None) -> str:
    """CSS `direction`'s real used value for `element`. An explicit author
    CSS declaration (inherited normally -- domonic's own cascade already
    handles that correctly) always wins. But domonic's cascade has no
    UA-stylesheet mapping for HTML's own `dir` attribute at all (a real UA
    rule every browser has, `[dir=rtl] { direction: rtl }`), and no
    attribute-selector support to add one via `ua_style.py` either
    (verified directly: `[dir="rtl"] {...}` never matches in domonic) --
    so a plain `<section dir="rtl">`, with no CSS `direction` anywhere,
    resolves `ltr` here, silently wrong. Falls back to walking the DOM for
    the attribute directly (nearest ancestor-or-self, mirroring its own
    real inheritance) only when the cascade resolved plain `ltr` -- which
    it only ever does when no CSS rule set `direction` anywhere up the
    chain (proven by the explicit-CSS case correctly resolving `rtl`
    through this same inheritance), so this can't shadow a real author
    declaration except the rare, self-contradictory case of *also* writing
    literal `direction: ltr` on top of a `dir="rtl"` attribute."""
    if computed is None:
        computed = getattr(element, "_chromonic_computed_style", None)
    resolved = (getattr(computed, "direction", "ltr") or "ltr").strip().lower() if computed is not None else "ltr"
    if resolved == "rtl":
        return "rtl"
    node = element
    while node is not None:
        if _is_element(node) and hasattr(node, "getAttribute"):
            dir_attr = (node.getAttribute("dir") or "").strip().lower()
            if dir_attr in ("rtl", "ltr"):
                return dir_attr
        node = getattr(node, "parentElement", None)
    return resolved


def _resolve_text_indent(computed) -> float:
    """`text-indent` in real px -- `ComputedStyleDeclaration.getPropertyValue`
    has no used-length conversion for it (unlike `width`), so a `ch`/`em`
    value would otherwise reach here as the literal, unresolved CSS text.
    `domonic.layout`'s own length parser (already relied on for `width`
    etc. via `LayoutStyle`) resolves it the same way real `width:10ch`
    layout already does elsewhere in this file -- percentages aren't
    supported (need the containing block, layout's job, not this early)."""
    if computed is None:
        return 0.0
    raw = (getattr(computed, "textIndent", "") or "0").strip()
    if not raw or raw == "0":
        return 0.0
    value = _parse_length_or_percent(raw, computed, allow_percent=False, allow_auto=False)
    return value.px if isinstance(value, Length) else 0.0


def _extract_paint_style(computed) -> dict:
    """The handful of paint-only properties `paint.py` needs, read out of
    `computed` exactly once here and cached as plain strings -- a
    `ComputedStyleDeclaration` attribute access re-resolves from the
    underlying style text on every access, so without this every repaint
    (not just relayout) re-parses every element's colours/fonts from scratch."""
    raw = computed._resolved.get

    def raw_or_computed(name: str) -> str:
        value = raw(name)
        # Custom properties still need element-specific expansion.
        return computed.getPropertyValue(name) if value and "var(" in value else value

    return {
        "background_color": computed.getPropertyValue("background-color"),
        "background_image": computed.getPropertyValue("background-image"),
        "background_size": computed.getPropertyValue("background-size"),
        "background_position": computed.getPropertyValue("background-position"),
        "background_repeat": computed.getPropertyValue("background-repeat"),
        "overflow_x": computed.getPropertyValue("overflow-x"),
        "overflow_y": computed.getPropertyValue("overflow-y"),
        "border_top_color": computed.getPropertyValue("border-top-color"),
        "color": computed.getPropertyValue("color"),
        "font_size": computed.getPropertyValue("font-size"),
        # These three have no used-value conversion in getPropertyValue;
        # _ResolvedView already supplies inheritance and initial values.
        "font_weight": raw_or_computed("font-weight"),
        "font_style": raw_or_computed("font-style"),
        "font_family": raw_or_computed("font-family"),
        # not read by paint.py itself -- included so _make_measure can work
        # entirely from this one already-extracted dict (see its docstring)
        # rather than touching `computed` again on a `reuse_styles=True` pass.
        "letter_spacing": computed.getPropertyValue("letter-spacing"),
        "word_spacing": computed.getPropertyValue("word-spacing"),
        "line_height": computed.getPropertyValue("line-height"),
        "white_space": computed.getPropertyValue("white-space"),
        "word_break": computed.getPropertyValue("word-break") or "normal",
        "overflow_wrap": computed.getPropertyValue("overflow-wrap") or "normal",
        "text_align": computed.getPropertyValue("text-align"),
        # Domonic's generated IDL getter supplies this initial value while
        # getPropertyValue() currently returns an empty string when unset.
        "text_align_last": computed.getPropertyValue("text-align-last") or "auto",
        "text_transform": computed.getPropertyValue("text-transform"),
        "direction": computed.getPropertyValue("direction") or "ltr",
    }


def _css_generated_content_text(value: "str | None") -> str:
    """Decode the simple string form of CSS generated `content`.

    This deliberately handles only the common, layout-relevant case:
    quoted strings, including CSS escapes such as Font Awesome's "\\f03e".
    Keywords like `none`, `normal`, counters, images, and attributes remain
    out of scope for now.
    """
    if not value:
        return ""
    text = str(value).strip()
    if text in ("none", "normal", "initial", "inherit"):
        return ""
    if len(text) < 2 or text[0] not in ("'", '"') or text[-1] != text[0]:
        return ""
    inner = text[1:-1]
    inner = re.sub(r"\\\\(?=[0-9a-fA-F]{1,6}(?:\s|$))", r"\\", inner)

    def replace_escape(match):
        escaped = match.group(1)
        if not escaped:
            return ""
        if re.fullmatch(r"[0-9a-fA-F]{1,6}\s?", escaped):
            return chr(int(escaped.strip(), 16))
        return escaped[-1]

    return re.sub(r"\\([0-9a-fA-F]{1,6}\s?|.)", replace_escape, inner)


def _pseudo_generates_box(raw_content: "str | None") -> bool:
    """Whether a `::before`/`::after` rule's raw `content` generates a box
    at all, distinct from resolving to empty text -- `content: ""` still
    generates a real (icon-only) box; no matching rule, `none`, or the
    initial `normal` does not. domonic's own unset value is `"none"`."""
    text = (raw_content or "").strip().lower()
    return text not in ("", "none", "normal", "initial", "inherit")


def _extract_generated_content(element, computed_cache):
    """`(before_text, after_text, before_info, after_info)` -- the text
    pair is the plain generated-content strings; the info pair is `None`
    or `(pseudo_computed, text)` when the pseudo-element should become a
    real box (`_pseudo_generates_box`), consulted by `_inline_mixed_content`."""
    document = getattr(element, "ownerDocument", None)

    # No document/stylesheets -> no authored pseudo-element content possible;
    # skip the cascade resolutions entirely.
    if document is None or not getattr(document, "styleSheets", None):
        return "", "", None, None

    chain_cache = computed_cache.setdefault("_chromonic_chain_cache", {})

    # domonic's own rule index (built the moment any element's *real*
    # `ComputedStyleDeclaration` resolves on this `chain_cache` -- always
    # true here, since `_describe()` resolves `element`'s own style right
    # before calling this) tracks which pseudo-element names any selector
    # in the document's stylesheets targets at all (see `_build_rule_index`
    # in domonic/style.py). Most pages never author a `::before`/`::after`
    # rule, so when neither name is in that set, skip building *two* full
    # `ComputedStyleDeclaration`s (each its own cascade resolution) just to
    # learn `content` is the initial `normal` on both. Safe for the rest of
    # this layout pass: `chain_cache` is fresh per pass and nothing mutates
    # the DOM/stylesheets mid-pass (see the module docstring), so once this
    # entry is populated its pseudo-name set can't go stale before the next
    # `layout()` call builds a new `chain_cache` from scratch. If the entry
    # isn't populated yet (rare -- would need this to be a per-element style
    # cache hit that skipped the cascade, and no earlier element this pass
    # to have populated it either), fall through to the full resolution
    # below; that's always correct, just not the fast path.
    rule_index_entry = chain_cache.get("__rule_index__")
    if rule_index_entry is not None:
        pseudo_names = rule_index_entry[2]
        if "before" not in pseudo_names and "after" not in pseudo_names:
            return "", "", None, None

    before = ComputedStyleDeclaration(
        element,
        "::before",
        _chain_cache=chain_cache,
    )
    after = ComputedStyleDeclaration(
        element,
        "::after",
        _chain_cache=chain_cache,
    )

    before_text = _css_generated_content_text(before.content)
    after_text = _css_generated_content_text(after.content)
    before_info = (before, before_text) if _pseudo_generates_box(before.content) else None
    after_info = (after, after_text) if _pseudo_generates_box(after.content) else None
    return before_text, after_text, before_info, after_info


def _describe(element, computed_cache=None, *, reuse_styles=False):
    """`(ComputedStyleDeclaration, LayoutStyle)` for `element`. Stashes the
    `ComputedStyleDeclaration` and its extracted paint style on the element
    itself so `paint.py`'s later walk, and a repaint with no relayout in
    between, don't re-resolve either.

    `reuse_styles=True` skips resolving CSS entirely if a prior resolution
    (`element._chromonic_resolved_style`) exists, reusing it as-is -- CSS
    resolution, not Taffy, dominates relayout cost (domonic re-parses every
    selector from scratch, uncached), so a relayout triggered by something
    that can't have changed any element's class/inline-style/stylesheets
    (an image arriving, a same-bucket resize) passes this to skip it.

    `computed_cache` holds our own `(computed, style_obj)` tuples keyed by
    `id(element)`, kept separate from domonic's own `_chain_cache` (which
    expects bare `ComputedStyleDeclaration` values) under a nested dict."""
    cache = {} if computed_cache is None else computed_cache
    cached = cache.get(id(element))
    if cached is not None:
        return cached

    synthetic = element.__dict__.get("_chromonic_synthetic_style") if hasattr(element, "__dict__") else None
    if synthetic is not None:
        # An anonymous table box (`_AnonymousTableBox`): no cascade to run,
        # its style was synthesized from its parent's when it was generated.
        element._chromonic_computed_style = synthetic[0]
        element._chromonic_resolved_style = synthetic
        cache[id(element)] = synthetic
        return synthetic

    if reuse_styles:
        prior = getattr(element, "_chromonic_resolved_style", None)
        if prior is not None:
            cache[id(element)] = prior
            return prior

    # Share ancestor resolution across this layout pass, not across frames --
    # domonic's default cache only spans one element's ancestor walk.
    chain_cache = cache.setdefault("_chromonic_chain_cache", {})
    computed = ComputedStyleDeclaration(element, _chain_cache=chain_cache)
    # domonic's `_parent_computed()` only reads `chain_cache`, never
    # registers itself in it -- without this, a child resolved right after
    # `element` would build a second, separate `ComputedStyleDeclaration` for it.
    chain_cache[id(element)] = computed
    style_obj = LayoutStyle.from_computed(computed)
    element._chromonic_computed_style = computed
    element._chromonic_paint_style = _extract_paint_style(computed)
    (element._chromonic_before_text, element._chromonic_after_text,
     element._chromonic_before_pseudo, element._chromonic_after_pseudo) = _extract_generated_content(element, cache)
    from . import webfonts
    webfonts.resolve_style(element, element._chromonic_paint_style)
    # Give Parley and Skia the same platform choice for CSS monospace.
    family = element._chromonic_paint_style["font_family"]
    if family and family.strip().lower() in ("monospace", "ui-monospace"):
        element._chromonic_paint_style["font_family"] = fonts._GENERIC_FAMILIES[family.strip().lower()]
    result = (computed, style_obj)
    element._chromonic_resolved_style = result
    cache[id(element)] = result
    return result


#: CSS 2.1 17.2.1: `table-column`/`table-column-group` "are not rendered"
#: -- no box at all, same as `display:none` for box-generation purposes,
#: whether reached via a literal `<colgroup>`/`<col>` tag (already excluded
#: earlier, by tag, in `_NON_RENDERING_TAGS`) or an arbitrary element
#: authored with one of these two `display` values directly (`_renders`
#: is the computed-style-driven check for exactly that latter case, since
#: `_NON_RENDERING_TAGS`'s tag-based check runs *before* any style is even
#: resolved and so can't see it).
_NON_RENDERING_DISPLAYS = frozenset({"table-column", "table-column-group"})


def _renders(style_obj) -> bool:
    """Whether an element already known to be an ordinary rendering tag (see
    `_NON_RENDERING_TAGS`, checked by the caller before this) should still be
    walked into the Taffy tree -- false for anything the cascade resolved to
    `display: none` (a real browser's "don't lay this out, don't paint it,
    don't hit-test it" is exactly `display: none`), or to `table-column`/
    `table-column-group` (see `_NON_RENDERING_DISPLAYS`), *unless* this box
    is absolutely positioned. CSS 2.1 9.7 blockifies `display` for any
    `position:absolute`/`fixed` box regardless of its specified value (with
    `none` the only exception, already handled above) -- an author-written
    `display: table-column-group` on an absolutely positioned element
    computes to `block`, a perfectly ordinary rendered box, not the
    17.2.1 "not rendered" rule this function otherwise applies to a literal
    (in-flow) `table-column`/`table-column-group`. Confirmed directly on
    `bottom-applies-to-005.xht`: unfixed, such an element was dropped
    entirely (never even reaching Taffy), instead of rendering as an
    ordinary absolutely positioned block. The `float`-blockified case CSS
    2.1 9.7 also covers isn't handled here -- would need `computed` (for
    `float`), not just `style_obj`, threaded through every call site."""
    display = style_obj.display
    value = getattr(display, "value", display)
    if value == "none":
        return False
    if value in _NON_RENDERING_DISPLAYS:
        return _is_absolutely_positioned(style_obj)
    return True


_TABLE_PART_DISPLAYS = {
    "table": "table", "inline-table": "table", "table-row-group": "row-group",
    "table-header-group": "row-group", "table-footer-group": "row-group",
    "table-row": "row", "table-cell": "cell", "table-caption": "caption",
    "table-column": "column", "table-column-group": "column-group",
}
_TABLE_PART_TAGS = {
    "table": "table", "thead": "row-group", "tbody": "row-group", "tfoot": "row-group",
    "tr": "row", "td": "cell", "th": "cell", "caption": "caption", "col": "column",
    "colgroup": "column-group",
}
_TABLE_INTERNAL_KINDS = frozenset({"row-group", "row", "cell", "caption", "column", "column-group"})


def _table_part_kind(node, computed_cache) -> "str | None":
    """Which CSS 2.1 17.2.1 table box `node` generates, if any: `"table"`,
    `"row-group"`, `"row"`, `"cell"`, `"caption"`, `"column"`,
    `"column-group"` -- or `None` for a text node or any other box. An
    absolutely/fixed positioned element blockifies (CSS 2.1 9.7) and is
    never a table part."""
    if not _is_element(node):
        return None
    if isinstance(node, _AnonymousTableBox):
        return {"table": "table", "inline-table": "table", "row": "row", "cell": "cell"}.get(node.kind)
    tag = (getattr(node, "tagName", "") or "").lower()
    if tag in _NON_RENDERING_TAGS and tag not in ("col", "colgroup"):
        return None
    computed, style_obj = _describe(node, computed_cache)
    display = (getattr(computed, "display", "") or "").strip().lower()
    kind = _TABLE_PART_DISPLAYS.get(display) or _TABLE_PART_TAGS.get(tag)
    if _is_absolutely_positioned(style_obj):
        # CSS 2.1 9.7 blockifies `display`: `table`/`inline-table` stay a
        # table (top-applies-to-013.xht: an absolutely positioned table
        # keeps its rows); every internal part becomes a plain block.
        return "table" if kind == "table" else None
    return kind


def _synthesize_anonymous_style(box: "_AnonymousTableBox", parent, computed_cache) -> None:
    parent_computed, parent_style = _describe(parent, computed_cache)
    display = _AnonymousTableBox._DISPLAYS[box.kind]
    zero = Edges(Length(0.0), Length(0.0), Length(0.0), Length(0.0))
    style_obj = dataclasses.replace(
        parent_style, display=Keyword(display), position=Keyword("static"),
        boxSizing=Keyword("content-box"), overflowX=Keyword("visible"), overflowY=Keyword("visible"),
        inset=Edges(AUTO, AUTO, AUTO, AUTO), width=AUTO, height=AUTO, minWidth=AUTO, minHeight=AUTO,
        maxWidth=AUTO, maxHeight=AUTO, margin=zero, padding=zero, borderWidth=zero,
        flexGrow=0.0, flexShrink=1.0, flexBasis=AUTO, alignSelf=Keyword("auto"),
    )
    box.__dict__["_chromonic_synthetic_style"] = (_SyntheticComputed(parent_computed, display), style_obj)
    # Inherited paint properties (font, color, ...) come from the parent;
    # nothing an anonymous box paints of its own -- so the parent's own
    # (non-inherited) background/border must not come along, or the box
    # would repaint them over its area.
    paint_style = dict(getattr(parent, "_chromonic_paint_style", None) or {})
    for name in ("background_color", "border_top_color", "border_right_color",
                 "border_bottom_color", "border_left_color"):
        if name in paint_style:
            paint_style[name] = "transparent"
    if "background_image" in paint_style:
        paint_style["background_image"] = "none"
    box.__dict__["_chromonic_paint_style"] = paint_style
    box.__dict__["_chromonic_before_text"] = ""
    box.__dict__["_chromonic_after_text"] = ""
    box.__dict__["_chromonic_before_pseudo"] = None
    box.__dict__["_chromonic_after_pseudo"] = None


def _wrap_missing_table_boxes(element, computed_cache) -> list:
    """`element.childNodes`, with CSS 2.1 17.2.1's missing anonymous table
    boxes generated (`_AnonymousTableBox`, cached on `element` by kind and
    first wrapped node, so a retained `LayoutProjection` sees the same box
    -- and Taffy node -- across passes):

    - inside a table: any child that isn't a row group, row, caption or
      column (a bare cell, a block, loose text) gets an anonymous row --
      whose own normalization then wraps non-cells in an anonymous cell;
    - inside a row group: non-rows get an anonymous row;
    - inside a row: non-cells (an inline element, loose text) get an
      anonymous cell, consecutive ones sharing it;
    - anywhere else: a run of table-internal boxes (cells, rows, row
      groups, captions, columns) with no table to live in gets an
      anonymous table (`inline-table` inside an inline parent), whose own
      normalization supplies the row around bare cells.

    Whitespace-only text between table parts generates nothing. The vast
    majority of elements need no wrapping and get `childNodes` back as-is
    after one cheap classification pass."""
    nodes = list(_child_nodes(element))
    if not nodes:
        return nodes
    parent_kind = _table_part_kind(element, computed_cache)
    if parent_kind in ("caption", "cell", "column", "column-group"):
        parent_kind = None  # a caption/cell is an ordinary block container for this purpose
    if not isinstance(element, _AnonymousTableBox):
        # Clear last pass's wrappers; this pass re-links whatever it wraps
        # below. An anonymous box's own children are exactly the nodes it
        # wraps -- their link to it must stay (a nested wrapper generated
        # below overrides it for its own run).
        for node in nodes:
            if hasattr(node, "__dict__"):
                node.__dict__.pop("_chromonic_anonymous_parent", None)

    def is_blank_text(node) -> bool:
        return getattr(node, "nodeType", None) == TEXT_NODE and not _collapsed_text_node(node).strip()

    def renders(node) -> bool:
        if not _is_element(node):
            return getattr(node, "nodeType", None) == TEXT_NODE
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag in _NON_RENDERING_TAGS:
            return False
        return _renders(_describe(node, computed_cache)[1])

    def needs_wrap(node) -> bool:
        kind = _table_part_kind(node, computed_cache)
        if parent_kind == "table":
            return kind not in ("row-group", "row", "caption", "column", "column-group")
        if parent_kind == "column-group":
            return False  # CSS 2.1 17.2.1 rule 1.2: anything but a column here is `display: none`
        if parent_kind == "row-group":
            return kind != "row"
        if parent_kind == "row":
            return kind != "cell"
        return kind in _TABLE_INTERNAL_KINDS

    # A column or column group generates no box, but a misparented one
    # (inside a row, a row group, a cell, an ordinary block) still takes
    # the anonymous boxes CSS 2.1 17.2.1 gives any proper table child
    # there -- a `display: table-column` div inside a row becomes an
    # anonymous cell holding an anonymous table with that one column
    # (empty-cells-applies-to-012.xht: the row's text cell sits in
    # column 1, and the column reports a 16px-wide box).
    def out_of_flow(node) -> bool:
        # CSS 2.1 9.7: an absolutely positioned child of a table part
        # blockifies and leaves the table's flow -- never a table part,
        # never wrapped in an anonymous cell (top-applies-to-001.xht: a
        # `position: absolute; top: 0` row group is a block at the page
        # top, its own row becoming an anonymous table inside it).
        return (_is_element(node) and not isinstance(node, _AnonymousTableBox)
                and _is_absolutely_positioned(_describe(node, computed_cache)[1]))

    rendering = [node for node in nodes
                 if not out_of_flow(node)
                 and (renders(node) or _table_part_kind(node, computed_cache) in ("column", "column-group"))]
    if not any(needs_wrap(node) for node in rendering if not is_blank_text(node)):
        return nodes
    if parent_kind == "table":
        wrap = "row"
    elif parent_kind == "row-group":
        wrap = "row"
    elif parent_kind == "row":
        wrap = "cell"
    else:
        parent_style = _describe(element, computed_cache)[1] if not isinstance(element, _AnonymousTableBox) else None
        wrap = "inline-table" if (parent_style is not None and _is_inline_level(element, parent_style)) else "table"
    cache = element.__dict__.setdefault("_chromonic_anonymous_table_boxes", {})
    result: list = []
    run: list = []

    def flush():
        while run and is_blank_text(run[-1]):
            run.pop()
        if not run:
            run.clear()
            return
        key = (wrap, id(run[0]))
        box = cache.get(key)
        if box is None:
            box = cache[key] = _AnonymousTableBox(wrap, element)
        box.childNodes = list(run)
        for node in run:
            if hasattr(node, "__dict__"):
                node.__dict__["_chromonic_anonymous_parent"] = box
        _synthesize_anonymous_style(box, element, computed_cache)
        result.append(box)
        run.clear()

    for node in nodes:
        # A column/column group generates no box of its own (`_renders`
        # says no) but still belongs *inside* the table its columns
        # describe -- it has to travel into the anonymous table with the
        # rows it sits among, or that table has no columns at all.
        if out_of_flow(node) or (
                not renders(node) and _table_part_kind(node, computed_cache) not in ("column", "column-group")):
            result.append(node)
            continue
        if is_blank_text(node):
            # Whitespace that is a direct child of a table, row group or
            # row is dropped outright (CSS 2.1 17.2.1) -- even between two
            # inline spans that end up sharing one anonymous cell, Chrome
            # renders them with no space at all (table-anonymous-objects-
            # 085.xht). Inside an ordinary container it just stays part of
            # whatever run it sits in.
            if parent_kind is not None:
                continue
            if run:
                run.append(node)
            else:
                result.append(node)
            continue
        if needs_wrap(node):
            run.append(node)
        else:
            flush()
            result.append(node)
    flush()
    return result


def _wrap_inline_runs(element, nodes, computed_cache) -> list:
    """CSS 2.1 9.2.1.1 anonymous block boxes: when a block container holds
    both block-level children and inline content (loose text, inline
    elements), each maximal run of that inline content is wrapped in an
    anonymous block box so it gets its own line boxes between the real
    blocks -- `<div>Hello <p>para</p> world</div>` is three stacked
    blocks. Previously the loose text was silently dropped (confirmed
    directly: that `Hello`/`world` never got a box at all), which is also
    why `wpt/css/CSS2/tables/table-anonymous-objects-093.xht`'s leading
    body text pushed nothing down. Out-of-flow children (floats,
    absolutely positioned boxes, `<br>`) stay inside the run they sit in.

    A flex/grid container wraps only runs of *text* (CSS Flexbox 4:
    each element child is already its own item; a text run becomes an
    anonymous item). An inline element is left alone entirely -- an
    in-flow block inside an inline is CSS 2.1 9.2.1.1's *other* rule,
    `_split_inline_flow_around_blocks`'s job."""
    if not nodes or isinstance(element, _AnonymousTableBox) and element.kind != "cell":
        return nodes
    if _table_part_kind(element, computed_cache) in ("table", "row-group", "row"):
        return nodes
    computed, style_obj = _describe(element, computed_cache)
    if _is_inline_level(element, style_obj):
        return nodes
    display = (getattr(computed, "display", "") or "").strip().lower()
    flex_or_grid = display in ("flex", "inline-flex", "grid", "inline-grid")

    def classify(node) -> str:
        if not _is_element(node):
            if getattr(node, "nodeType", None) != TEXT_NODE:
                return "skip"
            # Only CSS white space is "blank": Python's `strip()` also eats
            # U+00A0, but a `&nbsp;` text node between flex items is a real
            # anonymous item (css-box-justify-content.html: four 4px items
            # Chrome lays out between the `DIV1..5` boxes).
            if flex_or_grid:
                # CSS Flexbox 4: a text run that is *purely* white space
                # never becomes an anonymous flex item, even under
                # `white-space: pre` (flexbox-whitespace-handling-001a.xhtml).
                raw = getattr(node, "textContent", None) or getattr(node, "data", "") or ""
                return "text" if raw.strip(_CSS_WHITESPACE_STRIP_CHARS) else "blank"
            return "text" if _collapsed_text_node(node).strip(_CSS_WHITESPACE_STRIP_CHARS) else "blank"
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag in _NON_RENDERING_TAGS:
            return "skip"
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            return "skip"
        if flex_or_grid:
            return "block"
        if (tag == "br" or _is_inline_level(node, child_style) or _is_absolutely_positioned(child_style)
                or _is_floated(child_computed)):
            return "inline"
        return "block"

    kinds = [classify(node) for node in nodes]
    if flex_or_grid:
        if "text" not in kinds:
            return nodes
        wrappable = {"text"}
    else:
        if "block" not in kinds or not ({"text", "inline"} & set(kinds)):
            return nodes
        wrappable = {"text", "inline"}
    cache = element.__dict__.setdefault("_chromonic_anonymous_table_boxes", {})
    result: list = []
    run: list = []

    def flush():
        while run and classify(run[-1]) == "blank":
            run.pop()
        if not run:
            run.clear()
            return
        key = ("block", id(run[0]))
        box = cache.get(key)
        if box is None:
            box = cache[key] = _AnonymousTableBox("block", element)
        box.childNodes = list(run)
        for node in run:
            if hasattr(node, "__dict__"):
                node.__dict__["_chromonic_anonymous_parent"] = box
        _synthesize_anonymous_style(box, element, computed_cache)
        result.append(box)
        run.clear()

    for node, kind in zip(nodes, kinds):
        if kind == "skip":
            result.append(node)
        elif kind in wrappable:
            run.append(node)
        elif kind == "blank":
            (run if run else result).append(node)
        else:
            flush()
            result.append(node)
    flush()
    return result


def _normalized_child_nodes(element, computed_cache, *, reuse_styles=False) -> list:
    """`element.childNodes` as the layout tree actually sees them: CSS 2.1
    17.2.1's anonymous table boxes (`_wrap_missing_table_boxes`) and then
    9.2.1.1's anonymous block boxes (`_wrap_inline_runs`) generated around
    the nodes that need them. Remembered on the element for
    `_inline_mixed_content`, which walks the same list."""
    # `reuse_styles=True` is reserved for passes where neither DOM structure
    # nor CSS can have changed (currently image-intrinsic relayouts). The
    # anonymous table/block projection is therefore identical too. Reusing
    # the prior list avoids reclassifying every child and resolving each
    # child's display several times on an otherwise unchanged large DOM.
    if reuse_styles and hasattr(element, "__dict__"):
        cached = element.__dict__.get("_chromonic_normalized_children")
        if cached is not None:
            return cached
    nodes = _wrap_inline_runs(element, _wrap_missing_table_boxes(element, computed_cache), computed_cache)
    if hasattr(element, "__dict__"):
        element.__dict__["_chromonic_normalized_children"] = nodes
    return nodes


def _child_elements(element, computed_cache=None, *, reuse_styles=False) -> list:
    """`[(child, computed, style_obj), ...]` for children that should
    render -- each child's style computed exactly once here, then handed
    straight to the recursive `build()` call below instead of being
    recomputed there. Anonymous table/block boxes (CSS 2.1 17.2.1/9.2.1.1)
    appear here in place of the nodes they wrap -- see
    `_normalized_child_nodes`."""
    result = []
    if computed_cache is None:
        computed_cache = {}
    for child in _normalized_child_nodes(
        element, computed_cache, reuse_styles=reuse_styles,
    ):
        if not _is_element(child):
            continue
        if (getattr(child, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
            continue
        computed, style_obj = _describe(child, computed_cache, reuse_styles=reuse_styles)
        if _renders(style_obj):
            result.append((child, computed, style_obj))
        else:
            _clear_stale_layout_geometry(child)
    return result


def _clear_stale_layout_geometry(element) -> None:
    """`element` just resolved to `display:none` -- clear its (and its
    subtree's) `_layout_box`/`_chromonic_inline_fragments` rather than
    leaving them from whenever it last rendered, or `paint_tree`/hit-
    testing/`getBoundingClientRect()` (which all walk the real DOM, not
    `node_map`) keep reading it as still having a box."""
    element.__dict__.pop("_layout_box", None)
    element.__dict__.pop("_chromonic_inline_fragments", None)
    for child in _child_nodes(element):
        if _is_element(child):
            _clear_stale_layout_geometry(child)


# CSS 2.1 16.6.1 whitespace collapsing only touches ASCII space/tab/newline/
# CR/form-feed, never U+00A0 (nbsp) -- unlike Python's own `str.strip()`/`\s`,
# which treats nbsp as whitespace too and would collapse an nbsp-only node away.
_CSS_COLLAPSIBLE_WHITESPACE_RE = re.compile(r"[ \t\n\r\f]+")
_CSS_WHITESPACE_STRIP_CHARS = " \t\n\r\f"


def _apply_text_transform(text: str, transform: str | None) -> str:
    transform = (transform or "none").strip().lower()
    if transform == "uppercase":
        return text.upper()
    if transform == "lowercase":
        return text.lower()
    if transform == "capitalize":
        return re.sub(r"\b(\w)", lambda match: match.group(1).upper(), text)
    return text


def _own_text(element) -> str:
    # Mixed content (text alongside child elements) is out of scope -- only
    # a childless element's own text is measured. `_rendering_text_content`,
    # not raw `.textContent`, since a "childless" element can still have a
    # `<style>`/`<script>` descendant whose raw source text isn't real prose.
    text = (
        getattr(element, "_chromonic_before_text", "")
        + _rendering_text_content(element)
        + getattr(element, "_chromonic_after_text", "")
    )
    style = element.__dict__.get("_chromonic_paint_style", {})
    text = _apply_text_transform(text, style.get("text_transform"))
    if style.get("white_space") in ("pre", "pre-wrap", "break-spaces"):
        return text
    return _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", text).strip(_CSS_WHITESPACE_STRIP_CHARS)


def _collapsed_text_node(node) -> str:
    raw = getattr(node, "textContent", None)
    if raw is None:
        raw = getattr(node, "data", "")
    if not raw:
        return ""
    return _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw).strip(_CSS_WHITESPACE_STRIP_CHARS)


def _inline_mixed_content(element, children, element_is_inline=False):
    """Return DOM-order inline items when a block contains direct text or
    when all children are inline-level elements (spans, links, etc.).

    Block children opt out, except when `element` is itself a genuine
    `display:inline` element -- CSS 2.1 9.2.1.1: an in-flow block child of
    an inline forces that inline to split around it, which needs the block
    to reach `_split_inline_flow_around_blocks`/`_contains_in_flow_block`
    as an ordinary item here rather than being rejected up front. An
    ordinary (non-inline) block `element` still opts a block child out
    entirely -- no anonymous-block implementation for that case."""
    # The normalized view (`_normalized_child_nodes`): text a CSS 2.1
    # 9.2.1.1 anonymous block took over is no longer this element's own,
    # and an anonymous inline-table (17.2.1) generated around loose cells
    # stands in for them as one inline-level item.
    child_nodes = (element.__dict__.get("_chromonic_normalized_children")
                   if hasattr(element, "__dict__") else None)
    if child_nodes is None:
        child_nodes = _child_nodes(element)
    has_direct_text = any(
        getattr(node, "nodeType", None) == TEXT_NODE and _collapsed_text_node(node).strip()
        for node in child_nodes
    )
    # Also qualifies when all children are inline-level (or <br>), even with
    # no text anywhere -- CSS 2.1 9.2.1.1/10.8: a genuinely empty inline
    # still contributes its own line-box height/baseline, width 0.
    def _child_qualifies(child, style_obj) -> bool:
        return (
            _is_inline_level(child, style_obj)
            or _is_absolutely_positioned(style_obj)
            or (getattr(child, "tagName", "") or "").lower() == "br"
            # A direct in-flow block child of an inline `element` is the
            # CSS 2.1 9.2.1.1 split trigger, not grounds to reject it.
            or element_is_inline
        )

    has_inline_only_children = (
        not has_direct_text
        and bool(children)
        and all(_child_qualifies(child, style_obj) for child, _computed, style_obj in children)
    )
    before_info = getattr(element, "_chromonic_before_pseudo", None)
    after_info = getattr(element, "_chromonic_after_pseudo", None)
    if not has_direct_text and not has_inline_only_children and before_info is None and after_info is None:
        return None
    by_id = {id(child): (child, computed, style_obj) for child, computed, style_obj in children}
    # Out-of-flow positioned children do not break an inline formatting run.
    # Keep them in the retained projection so Taffy can anchor them, while the
    # surrounding direct text still gets its own measurable fragment.
    # <br> elements are handled as forced line-breaks and are always permitted.
    # CSS 2.1 9.5: a float doesn't break an inline formatting run either --
    # like an absolutely positioned child, it's out of flow -- but *only*
    # once real direct text already earned this container a plan above
    # (`_is_floated`, unlike the other `_child_qualifies` cases, is never
    # consulted for `has_inline_only_children`: a *pure*-float sibling
    # group with no text at all must keep going through the older,
    # more capable `elif children:` -> `_approximate_inline_flow` ->
    # `_fix_float_flow_after_block_sibling` block-packing path exactly as
    # before -- confirmed directly on `zero-available-space-float-
    # positioning.html`, two floats and no text, regressed by this
    # exact function accepting them here too). With real text present,
    # though, rejecting the container outright loses every text sibling
    # to that same fallback, which has no notion of direct text at all --
    # found on any `<p>text <img style="float:left"> more text</p>`-
    # shaped fixture throughout `CSS2/floats`: the *entire paragraph's*
    # text silently vanished, not just the float's own position.
    if any(not (_child_qualifies(child, style_obj) or (has_direct_text and _is_floated(computed)))
           for child, computed, style_obj in children):
        return None
    items = []
    pending_space = False
    previous_was_element = False
    # CSS 2.1 16.6.1: whitespace at the very *start* of a block's content
    # collapses to nothing, same as at a line's start -- only *interior*
    # whitespace (between two real pieces of content) collapses to a
    # single space. `pending_space` must therefore never fire until real
    # content has actually been emitted (`has_content`); otherwise a
    # purely-formatting leading newline/indentation before this
    # container's first child (`<div>\n  <span>...`, extremely common in
    # any hand-formatted or templated HTML) would wrongly manufacture a
    # visible leading space on that first child. Confirmed directly on
    # `position-relative-003.xht`: `<div>\n<span>Filler Text</span>\n
    # </div>` measured the span 4px too wide, a spurious leading space
    # baked into its own first (and only) run.
    has_content = False
    for node in child_nodes:
        if getattr(node, "nodeType", None) == TEXT_NODE:
            text = _collapsed_text_node(node)
            if text:
                fragment = getattr(node, "_chromonic_fragment", None)
                if fragment is None:
                    fragment = _AnonymousTextFragment(node, element)
                    node._chromonic_fragment = fragment
                # Only a real (collapsed-away) whitespace node earns the
                # leading space -- a text node butted right up against
                # the preceding inline element has none (CSS 2.1 16.6.1;
                # column-visibility-004.xht's `<span>F</span>P` is "FP",
                # 200px in Ahem, not "F P").
                fragment._chromonic_leading_collapsed_space = has_content and pending_space
                items.append(("text", fragment, text, None, None))
                pending_space = False
                previous_was_element = False
                has_content = True
            elif (getattr(node, "textContent", "") or ""):
                pending_space = True
        elif id(node) in by_id:
            child, computed, style_obj = by_id[id(node)]
            if (getattr(child, "tagName", "") or "").lower() == "br":
                items.append(("break", child, None, computed, style_obj))
                pending_space = False
                previous_was_element = False
                # A forced line break starts a fresh line -- whitespace
                # right after it collapses to nothing too, not a real
                # space, the same as at this container's own start.
                has_content = False
            else:
                # Mirrors the text-fragment case just above: a whitespace-
                # only text node collapses to nothing of its own (no run
                # ever represents it), so the fact a space belongs *before*
                # this element has to be carried forward the same way, or
                # it's lost entirely once this element flattens its own
                # content into runs below. Confirmed directly: `<a>Blue
                # stein</a> <a>1 hour ago</a>` (a plain space between two
                # adjacent elements) rendered as "Bluestein1 hour ago",
                # the space silently dropped.
                if _is_absolutely_positioned(style_obj):
                    # Out of flow: invisible to whitespace collapsing --
                    # neither content that makes a following space real
                    # nor something a pending space attaches to (height-
                    # width-inline-table-001.xht: an inline-table after an
                    # absolutely positioned div and a newline starts at
                    # the line's start, no 4px space).
                    child._chromonic_leading_collapsed_space = False
                    items.append(("element", child, None, computed, style_obj))
                    continue
                child._chromonic_leading_collapsed_space = has_content and pending_space
                items.append(("element", child, None, computed, style_obj))
                pending_space = False
                previous_was_element = True
                has_content = True

    def pseudo_item(which, info):
        pseudo_computed, text = info
        pseudo = _get_pseudo_object(element, which)
        pseudo.text = text
        pseudo_style_obj = LayoutStyle.from_computed(pseudo_computed)
        # A synthetic pseudo never goes through `_describe()` (no real DOM
        # node to resolve a `ComputedStyleDeclaration` for), so its paint
        # style must be set explicitly here, `@font-face` substitution included.
        pseudo._chromonic_paint_style = _extract_paint_style(pseudo_computed)
        pseudo._chromonic_computed_style = pseudo_computed
        from . import webfonts
        webfonts.resolve_style(element, pseudo._chromonic_paint_style)
        return ("element", pseudo, None, pseudo_computed, pseudo_style_obj)

    if before_info is not None:
        items.insert(0, pseudo_item("before", before_info))
    if after_info is not None:
        items.append(pseudo_item("after", after_info))
    return items


def _inline_text_style(parent_style):
    style = dict(parent_style)
    style.update({
        "display": "block", "position": "relative", "width": "auto", "height": "auto",
        "inset": ["auto", "auto", "auto", "auto"],
        "min_width": "auto", "min_height": "auto", "max_width": "auto", "max_height": "auto",
        "margin": [0.0, 0.0, 0.0, 0.0], "padding": [0.0, 0.0, 0.0, 0.0],
        "border": [0.0, 0.0, 0.0, 0.0], "flex_grow": 0.0, "flex_shrink": 1.0,
        # Never the parent's own basis: a table cell's is its whole
        # column width, and a text fragment inheriting it took a full
        # line to itself, stacking a cell's `<img/>B<img/>Y...` content
        # one item per line (border-conflict-element-001d.xht).
        "flex_basis": "auto",
    })
    return style


def _numeric_edge(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _collapse_margin_set(margins: list) -> float:
    """CSS 2.1 8.3.1: adjoining margins collapse into one -- the largest
    positive value plus the largest-magnitude negative one. An empty list
    collapses to no margin at all."""
    positive = max((m for m in margins if m > 0), default=0.0)
    negative = min((m for m in margins if m < 0), default=0.0)
    return positive + negative


def _block_margins_collapse_through(child, child_box) -> bool:
    """CSS 2.1 8.3.1: an empty in-flow block (no border/padding, `auto`
    height, no content) doesn't stop its own top/bottom margins from
    collapsing through it. Zero height alone isn't enough -- an explicit
    `height: 0` still stops collapse-through; only `auto` qualifies."""
    if child_box.height != 0.0:
        return False
    native = getattr(child, "_chromonic_native_style", None) or {}
    if native.get("height") != "auto":
        return False
    if any(_numeric_edge(v) != 0.0 for name in ("border", "padding") for v in native.get(name, ())):
        return False
    return True


def _empty_inline_strut_run(owner, leading_edge, trailing_edge, top_edge_val, extra_height, margin_start):
    """CSS 2.1 9.2.1.1/10.8: a genuinely empty, non-replaced inline still
    generates one zero-width inline box participating in the line --
    contributing its own font/line-height to the line's height/baseline
    like a real text run (`above`/`below`), while its own vertical
    padding/border/margin only grow its own box (`box_height`), never the
    line's. One empty `("", 0.0)` token so `measure()` places it without
    treating it as wrappable content."""
    paint_style = owner._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    # An explicit `line-height: 0` is a real, valid (if unusual) authored
    # value, not "unset" -- `_resolved_line_height` already returns `None`
    # for the genuinely-unset/`normal` case, so a `... or normal_height`
    # here would wrongly treat the *value* `0.0` the same way, falling
    # back to the font's own metrics-based line-height instead of the
    # explicit zero the author asked for.
    resolved_line_height = _resolved_line_height(paint_style["line_height"])
    used_line_height = resolved_line_height if resolved_line_height is not None else normal_height
    above = ascent + math.floor((used_line_height - glyph_height) / 2)
    below = used_line_height - above
    return {
        "source": owner, "owner": owner, "paint_style": paint_style,
        "font_size": font_size, "tokens": [("", 0.0)],
        "leading": leading_edge, "trailing": trailing_edge,
        "box_height": glyph_height + extra_height,
        "glyph_height": glyph_height, "ascent": ascent,
        "above": above, "below": below, "top_edge": top_edge_val,
        "space_width": 0.0, "atomic_width": 0.0, "margin_start": margin_start,
        "intrinsic_width": 0.0, "empty_strut": True,
    }


def _empty_decoration_only_run(owner, leading_edge, trailing_edge, top_edge_val, extra_height, margin_start):
    """A split-inline leading/trailing segment (CSS 2.1 9.2.1.1) with no
    real text still needs a fragment for its own border/padding decoration,
    but unlike `_empty_inline_strut_run` it's never a real inline-flow
    participant (it's on its own dedicated block-flow line) -- so it gets
    no font/line-height contribution at all, just its own border/padding."""
    return {
        "source": owner, "owner": owner, "paint_style": owner._chromonic_paint_style,
        "font_size": 0.0, "tokens": [("", 0.0)],
        "leading": leading_edge, "trailing": trailing_edge,
        "box_height": extra_height,
        "glyph_height": 0.0, "ascent": 0.0,
        "above": 0.0, "below": 0.0, "top_edge": top_edge_val,
        "space_width": 0.0, "atomic_width": 0.0, "margin_start": margin_start,
        "intrinsic_width": 0.0, "empty_strut": True,
    }


def _make_collapsed_space_run(paint_style, owner, top_edge_val: float, extra_height: float) -> dict:
    """A standalone run for a whitespace-only text node that collapsed to
    nothing of its own but still needs to occupy a real (wrappable,
    collapsible-at-line-end) slot between two elements -- built the same
    way an ordinary text run is, just for a single space token, and kept
    as its *own* run rather than merged into a neighboring element's own
    run. Merging it into the neighbor's first token was tried first and
    is exactly as wide, but confirmed wrong directly on `inline-
    formatting-context-013.xht`: when that neighbor's own content didn't
    fit on the space's line and had to wrap, the merged token pair wrapped
    as a unit strangely, leaving a stray zero-width space fragment
    dangling on the *previous* line real Chrome never reports at all. A
    standalone run collapses at a wrap boundary the same well-tested way
    any other adjacent whitespace-only run already does elsewhere in this
    file, instead of taking on a new, untested code path."""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    resolved_lh = _resolved_line_height(paint_style["line_height"])
    used_lh = resolved_lh if resolved_lh is not None else normal_height
    above = ascent + math.floor((used_lh - glyph_height) / 2)
    below = used_lh - above
    one_w = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
    spaced_w = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
    space_width = max(0.0, spaced_w - 2.0 * one_w)
    return {
        "source": owner, "owner": owner, "paint_style": paint_style,
        "font_size": font_size, "tokens": [(" ", space_width)],
        "leading": 0.0, "trailing": 0.0, "box_height": glyph_height + extra_height,
        "glyph_height": glyph_height, "ascent": ascent,
        "above": above, "below": below, "top_edge": top_edge_val,
        "space_width": space_width, "atomic_width": 0.0,
        "margin_start": 0.0, "margin_end": 0.0, "intrinsic_width": space_width,
    }


def _build_text_runs_from_nodes(child_nodes, paint_style, owner, *,
                                 leading_edge=0.0, trailing_edge=0.0,
                                 top_edge_val=0.0, extra_height=0.0,
                                 margin_start=0.0, margin_end=0.0, computed_cache=None):
    """Build inline-formatting-plan `runs` entries for the text/`<br>`
    content of `child_nodes` (DOM order): `leading_edge`/`trailing_edge`
    (border+padding) go on the first/last text run, `top_edge_val`/
    `extra_height` on every run's box height, `margin_start`/`margin_end`
    applied once each around the whole sequence. `[]` if no non-empty text.

    Shared by `_make_inline_formatting_plan` and `_split_wrapping_inline_
    element` (CSS 2.1 9.2.1.1's split fragments, each passing 0 for
    whichever edge it doesn't own).

    `computed_cache`, when given, also handles two node kinds a split
    segment can carry: a *simple* nested inline (text/`<br>` only, no
    further nesting) is flattened in place via a recursive call with its
    own paint style/edges; an absolutely-positioned element becomes an
    "escapee" run -- no width/height of its own, but its position in
    `runs` marks its CSS 2.1 10.3.7/10.6.4 static position for later use.
    Anything deeper is silently skipped."""

    def is_text_bearing(node) -> bool:
        node_type = getattr(node, "nodeType", None)
        if node_type == TEXT_NODE:
            return bool(_collapsed_text_node(node).strip())
        if not _is_element(node):
            return False
        if (getattr(node, "tagName", "") or "").lower() == "br":
            return False
        if computed_cache is not None:
            _node_computed, node_style_obj = _describe(node, computed_cache)
            if _is_absolutely_positioned(node_style_obj):
                return False  # an escapee -- out of flow, no text-bearing slot
        return bool((getattr(node, "textContent", "") or "").strip())

    text_node_indices = [i for i, n in enumerate(child_nodes) if is_text_bearing(n)]
    if not text_node_indices:
        return []
    runs = []
    # Whitespace collapses across sibling-node boundaries the same way it
    # does across run boundaries in `_make_inline_formatting_plan`'s own
    # top-level text handling (`raw[:1]`/`raw[-1:]`) -- but unlike that
    # one, every text run built below unconditionally strips *both* ends
    # of its own text (`t.strip(...)`), with no equivalent restoration.
    # `pending_space` carries a boundary space forward (from a text node
    # that collapsed to nothing of its own, or a real node's own trailing
    # whitespace) to whatever the *next* run turns out to be -- a plain
    # text token, or a nested element's own first run. Confirmed directly
    # rendering `news.ycombinator.com`: a subtext line's own `<span class=
    # "subline">` wrapper (nested one level inside `<td>`, so its content
    # is flattened here, not via that other, already-correct top-level
    # path) rendered `Bluestein2 hours ago` -- an `<a>` immediately
    # followed by a `<span>` with no space token anywhere between them.
    pending_space = False
    # Same CSS 2.1 16.6.1 reasoning as `_inline_mixed_content`'s own
    # `has_content` guard: whitespace collapses to nothing at the very
    # *start* of this content (never to a real space), so neither a prior
    # sibling's trailing whitespace nor this node's own leading whitespace
    # should manufacture a leading-space token until something real has
    # already been emitted.
    has_content = False
    for node_index, child_node in enumerate(child_nodes):
        node_tag = (getattr(child_node, "tagName", "") or "").lower()
        if getattr(child_node, "nodeType", None) == TEXT_NODE:
            node_raw = getattr(child_node, "textContent", None)
            if node_raw is None:
                node_raw = getattr(child_node, "data", "") or ""
            raw_text = _collapsed_text_node(child_node)
            if not raw_text:
                if node_raw:
                    pending_space = True
                continue
            has_leading_space = has_content and (pending_space or bool(
                node_raw[:1] and node_raw[:1] in _CSS_WHITESPACE_STRIP_CHARS))
            has_trailing_space = bool(node_raw[-1:] and node_raw[-1:] in _CSS_WHITESPACE_STRIP_CHARS)
            pending_space = False
            has_content = True
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            run_leading = leading_edge if is_first_text else 0.0
            run_trailing = trailing_edge if is_last_text else 0.0
            t = _apply_text_transform(
                _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw_text),
                paint_style.get("text_transform"),
            )
            if not t.strip(_CSS_WHITESPACE_STRIP_CHARS):
                continue
            t = ((" " if has_leading_space else "")
                 + t.strip(_CSS_WHITESPACE_STRIP_CHARS)
                 + (" " if has_trailing_space else ""))
            font_size_i = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
            family_i = ("" if paint_style["font_family"] == "none"
                        else paint_style["font_family"])
            weight_i = _parse_font_weight(paint_style["font_weight"])
            italic_i = fonts.is_italic(paint_style["font_style"])
            ascent_i, descent_i, normal_i = fonts.text_metrics(
                family_i, font_size_i, weight_i >= 600, italic_i)
            glyph_h_i = ascent_i + descent_i
            # An explicit `line-height: 0` must not be treated as unset.
            resolved_lh_i = _resolved_line_height(paint_style["line_height"])
            used_lh_i = resolved_lh_i if resolved_lh_i is not None else normal_i
            above_i = ascent_i + math.floor((used_lh_i - glyph_h_i) / 2)
            below_i = used_lh_i - above_i
            box_h_i = glyph_h_i + extra_height
            one_w = layout_text("a", family_i, font_size_i,
                                font_weight=weight_i, italic=italic_i)[0]
            spaced_w = layout_text("a a", family_i, font_size_i,
                                   font_weight=weight_i, italic=italic_i)[0]
            space_w_i = max(0.0, spaced_w - 2.0 * one_w)
            tokens_i = []
            for tok in re.findall(r"\S+\s*|\s+", t):
                m, _h, _ls = layout_text(
                    tok, family_i, font_size_i,
                    font_weight=weight_i, italic=italic_i,
                    letter_spacing=_fontmetrics.parse_length(
                        paint_style["letter_spacing"], default=0.0),
                    word_spacing=_fontmetrics.parse_length(
                        paint_style["word_spacing"], default=0.0),
                )
                tokens_i.append((tok, sum(l[1] for l in _ls)))
            runs.append({
                "source": child_node, "owner": owner,
                "paint_style": paint_style,
                "font_size": font_size_i, "tokens": tokens_i,
                "leading": run_leading, "trailing": run_trailing,
                "box_height": box_h_i,
                "glyph_height": glyph_h_i, "ascent": ascent_i,
                "above": above_i, "below": below_i,
                "top_edge": top_edge_val,
                "space_width": space_w_i,
                "atomic_width": 0.0,
                "margin_start": margin_start if is_first_text else 0.0,
                "margin_end": margin_end if is_last_text else 0.0,
                "intrinsic_width": (
                    (margin_start if is_first_text else 0.0)
                    + run_leading + sum(w for _t, w in tokens_i)
                    + run_trailing
                    + (margin_end if is_last_text else 0.0)
                ),
            })
        elif node_tag == "br":
            pending_space = False
            # A forced line break starts a fresh line the same way the
            # whole container's own start does -- whitespace right after
            # it collapses to nothing too, not to a real space.
            has_content = False
            runs.append({"break": True, "element": child_node})
        elif _is_element(child_node) and computed_cache is not None:
            child_computed, child_style = _describe(child_node, computed_cache)
            if _is_absolutely_positioned(child_style):
                # Out of flow -- doesn't occupy an inline-content slot of
                # its own, so any space pending before it isn't consumed
                # here; it still belongs before whatever real content
                # comes next.
                runs.append({"escapee": True, "element": child_node,
                             "computed": child_computed, "style": child_style})
                continue
            non_br_element_children = [
                node for node in _child_nodes(child_node)
                if _is_element(node) and (getattr(node, "tagName", "") or "").lower() != "br"
            ]
            if non_br_element_children and not _is_genuine_inline_wrapper(child_node, child_style):
                # A nested block, `inline-block`, or replaced descendant
                # needs its own dedicated treatment (the CSS 2.1 9.2.1.1
                # split, or an atomic box with real intrinsic sizing) --
                # this function only ever flattens plain text into runs,
                # so it has no way to represent one and silently drops it,
                # same as it already did for a one-level-nested one (a bare
                # replaced/block element directly here, with no element
                # children of its own, always recursed into a trivially-
                # empty call and vanished the same way). Same reasoning as
                # the escapee case just above: dropped, not consumed, so a
                # pending space still belongs before whatever comes next.
                continue
            needs_leading_space = has_content and pending_space
            pending_space = False
            has_content = True
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            nested_native = style_bridge.to_dict(child_style)
            nested_left = _numeric_edge(nested_native["padding"][3]) + _numeric_edge(nested_native["border"][3])
            nested_right = _numeric_edge(nested_native["padding"][1]) + _numeric_edge(nested_native["border"][1])
            nested_top = _numeric_edge(nested_native["padding"][0]) + _numeric_edge(nested_native["border"][0])
            nested_extra = (nested_top + _numeric_edge(nested_native["padding"][2])
                             + _numeric_edge(nested_native["border"][2]))
            nested_margin_left = _numeric_edge(nested_native["margin"][3])
            nested_margin_right = _numeric_edge(nested_native["margin"][1])
            child_node.__dict__["_chromonic_flattened_inline"] = True  # see `_is_flattened_inline`
            nested_runs = _build_text_runs_from_nodes(
                list(_child_nodes(child_node)), child_node._chromonic_paint_style, child_node,
                leading_edge=leading_edge if is_first_text else 0.0,
                trailing_edge=trailing_edge if is_last_text else 0.0,
                top_edge_val=top_edge_val + nested_top,
                extra_height=extra_height + nested_extra,
                margin_start=margin_start if is_first_text else 0.0,
                margin_end=margin_end if is_last_text else 0.0,
                computed_cache=computed_cache,
            )
            if needs_leading_space and nested_runs:
                runs.append(_make_collapsed_space_run(paint_style, owner, top_edge_val, extra_height))
            # CSS 2.1's bidi box model (box.html#bidi-box-model, confirmed
            # directly against `left-rtl-ref.xht`'s own reference markup:
            # `<span style="border-left-style:none; padding-right:10px;
            # margin-right:60px">One</span>` for the DOM-first fragment of a
            # `direction:rtl` box split by a line break) attaches this
            # element's own *start*-side package (border+padding+margin
            # together, on one physical side, nothing on the other) to the
            # DOM-first non-break run and the *end*-side package to the
            # DOM-last one -- start=left/end=right for `ltr`, start=right/
            # end=left for `rtl`. `leading_edge`/`trailing_edge`/
            # `margin_start`/`margin_end` above carry only outer-ancestor
            # contributions (always physical, unswapped -- an ancestor's own
            # side was already resolved at its own level); this element's
            # own package is added directly onto the correct physical field
            # of the correct run here, after the recursive call returns,
            # since the recursive call's own `leading_edge`/`trailing_edge`
            # params are structurally physical-left/physical-right and can't
            # express "this element's start edge is physically on the right".
            non_break_runs = [r for r in nested_runs if not r.get("break")]
            if non_break_runs:
                first_run, last_run = non_break_runs[0], non_break_runs[-1]
                is_rtl_nested = _element_direction(child_node, child_computed) == "rtl"
                # Tells `_InlineFormattingPlan.measure()`'s whole-line RTL
                # mirror not to fall back to the owner's raw CSS margin-right
                # on this fragment even when its own `margin_end` is zero --
                # zero is a real, direction-aware answer here (an `rtl`
                # box's end fragment legitimately owns margin-*left*
                # instead), not a sign the field was never threaded (which
                # is what that fallback exists for, elsewhere).
                first_run["_bidi_margin_resolved"] = True
                last_run["_bidi_margin_resolved"] = True
                if first_run is last_run:
                    # Unsplit -- the sole run owns both edges, physically,
                    # regardless of direction (a box's own border/padding
                    # doesn't change because its content is `rtl`; only a
                    # line-break split invokes the start/end swap at all).
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_right
                                                      + nested_margin_left + nested_margin_right)
                elif is_rtl_nested:
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_right + nested_margin_right)
                    last_run["leading"] = last_run.get("leading", 0.0) + nested_left
                    last_run["margin_start"] = last_run.get("margin_start", 0.0) + nested_margin_left
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_left + nested_margin_left)
                else:
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_margin_left)
                    last_run["trailing"] = last_run.get("trailing", 0.0) + nested_right
                    last_run["margin_end"] = last_run.get("margin_end", 0.0) + nested_margin_right
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_right + nested_margin_right)
            runs.extend(nested_runs)
    return runs


def _is_genuine_inline_wrapper(node, style_obj) -> bool:
    """Whether `node` is a genuinely `display:inline` wrapper, as opposed
    to an atomic inline-level box (`inline-block`, replaced) that merely
    happens to also be inline-level. Only a genuine wrapper's content is
    reachable "through" it for CSS 2.1 9.2.1.1 (`_contains_in_flow_block`)
    -- an `inline-block` owns its own BFC/descendants entirely, so a block
    child inside one must never look like a direct block child of whatever
    merely contains that inline-block."""
    tag_name = (getattr(node, "tagName", "") or "").lower()
    return (
        tag_name not in _REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and _trusts_computed_inline(node, tag_name)
    )


def _contains_in_flow_block(element, computed_cache) -> bool:
    """Whether `element`'s subtree contains a genuine in-flow, block-level
    descendant reachable by walking only inline-level elements -- the CSS
    2.1 9.2.1.1 "anonymous block box" split trigger.

    `select`/`svg` are never walked into, matching `build()`'s own
    treatment of their real children as not real layout content.

    Walks the *normalized* children: loose cells inside this inline are
    already wrapped in one anonymous inline-table (CSS 2.1 17.2.1), an
    atomic inline-level box -- not the block-level cells themselves,
    which read as a split trigger and broke the inline around each one
    (table-anonymous-objects-177.xht)."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
        return False
    for node in _normalized_child_nodes(element, computed_cache):
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return True
        if _is_genuine_inline_wrapper(node, child_style) and _contains_in_flow_block(node, computed_cache):
            return True
    return False


def _first_reachable_in_flow_block(element, computed_cache):
    """Like `_contains_in_flow_block`, but returns the first descendant
    node itself rather than a bool -- used so a *nested* wrapper's own
    marker fragment (`_finalize_inline_owner_boxes`) has a real box to
    read geometry from. `None` if nothing qualifies."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
        return None
    for node in _normalized_child_nodes(element, computed_cache):
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return node
        if _is_genuine_inline_wrapper(node, child_style):
            found = _first_reachable_in_flow_block(node, computed_cache)
            if found is not None:
                return found
    return None


def _has_direct_in_flow_block_child(element, computed_cache) -> bool:
    """Like `_contains_in_flow_block`, but shallow -- true only for a
    block that's `element`'s own immediate child, not one further nested.
    Distinguishes CSS 2.1 9.2.1.1's two shapes: `element` itself directly
    parenting a block (splits itself) vs. a nested wrapper doing so (only
    that wrapper splits; `element` stays an ordinary block container)."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
        return False
    for node in _normalized_child_nodes(element, computed_cache):
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return True
    return False


def _split_wrapping_inline_element(wrapper, computed_cache, container):
    """CSS 2.1 9.2.1.1: `wrapper`, an inline element containing an in-flow
    block, splits into a sequence of fragments around each such block --
    yields `("run", runs)` or `("block", child, child_computed, child_style)`
    in DOM order. Only the first run gets `wrapper`'s own left border/
    padding/margin; only the last gets its right border/padding; an
    interior fragment gets neither. `wrapper` itself is never built as a
    Taffy node -- the split exists only in the layout projection.

    `container` (the real ancestor whose block-flow children the split
    pieces become) is stashed on `wrapper` for two post-layout corrections
    needing its finished geometry: the block-interruption marker rect
    (`_finalize_inline_owner_boxes`) and the percentage `top`/`left` basis
    (`_fix_split_inline_relative_offset`)."""
    wrapper._chromonic_split_container = container
    wrapper_computed, wrapper_style_obj = _describe(wrapper, computed_cache)
    native = style_bridge.to_dict(wrapper_style_obj)
    wrapper._chromonic_native_style = native
    left_edge = _numeric_edge(native["padding"][3]) + _numeric_edge(native["border"][3])
    right_edge = _numeric_edge(native["padding"][1]) + _numeric_edge(native["border"][1])
    # The split's leading/trailing fragments carry the wrapper's *logical*
    # start/end edge, not always its physical left/right -- in
    # `direction:rtl`, the first-generated fragment owns the right
    # border/padding/margin and the last owns the left, swapped from `ltr`.
    # `left_edge`/`right_edge`/`margin_left`/`margin_right` themselves stay
    # physical below (unswapped); which segment (first vs last) each gets
    # routed to is decided per-segment further down instead, since a plain
    # value-swap here would just move the *magnitude* without moving it to
    # the physically-correct side (`_build_text_runs_from_nodes`'s own
    # `leading_edge`/`trailing_edge` params are always physical-left/
    # physical-right, tied structurally to first/last segment respectively).
    is_rtl = _element_direction(wrapper, wrapper_computed) == "rtl"
    top_edge_val = _numeric_edge(native["padding"][0]) + _numeric_edge(native["border"][0])
    extra_height = (top_edge_val + _numeric_edge(native["padding"][2])
                    + _numeric_edge(native["border"][2]))
    margin_left = _numeric_edge(native["margin"][3])
    margin_right = _numeric_edge(native["margin"][1])
    if wrapper is container:
        # The direct-child shape: `wrapper` is a real Taffy node, so Taffy
        # already physically shifted its text-leaf children by this same
        # border/padding/margin -- zero them here to avoid double-counting
        # horizontal position; `top_edge_val` stays (one-way addition, safe),
        # its own vertical position corrected via `_chromonic_split_self_edges`
        # in `_finalize_inline_owner_boxes` instead.
        wrapper._chromonic_split_self_edges = (
            (right_edge if is_rtl else left_edge), (left_edge if is_rtl else right_edge), top_edge_val)
        left_edge = right_edge = margin_left = margin_right = 0.0
    else:
        wrapper.__dict__.pop("_chromonic_split_self_edges", None)
    paint_style = wrapper._chromonic_paint_style

    segments: list = [[]]
    blocks: list = []
    # A child that's itself an inline wrapper reaching a block only through
    # further nesting (not directly) must not fall through to `segments[-1].
    # append(node)` -- that treats it as ordinary content for
    # `_build_text_runs_from_nodes`, which has no notion of a nested block
    # and drops it. Recorded by segment index instead; resolved to a real
    # block for the marker rect below, and split via recursive delegation
    # at yield time rather than being flattened into `blocks` directly.
    nested_wrapper_at: dict = {}
    for node in _child_nodes(wrapper):
        if _is_element(node):
            tag = (getattr(node, "tagName", "") or "").lower()
            if tag in _NON_RENDERING_TAGS:
                continue
            if tag != "br":
                child_computed, child_style = _describe(node, computed_cache)
                if not _renders(child_style):
                    continue
                # CSS 2.1 9.2.1.1 only ever applies to an *in-flow* block
                # child -- a `float:left`/`right` one is out of flow (still
                # computed block-level, per 9.7's blockification, but never
                # forces the wrapper to split around it): it stays ordinary
                # segment content instead, built as its own atomic subtree
                # below, the same way an `inline-block` already is.
                if (not _is_absolutely_positioned(child_style) and not _is_floated(child_computed)
                        and not _is_inline_level(node, child_style)):
                    # `_fix_block_in_inline_rtl_position` needs the same
                    # real containing block `wrapper` itself tracks --
                    # CSS 2.1 9.2.1.1's split doesn't change this block's
                    # own normal-flow containing block or positioning
                    # rules, `direction:rtl` included.
                    node._chromonic_split_container = container
                    blocks.append((node, child_computed, child_style))
                    segments.append([])
                    continue
                if (_is_genuine_inline_wrapper(node, child_style)
                        and _contains_in_flow_block(node, computed_cache)):
                    nested_wrapper_at[len(blocks)] = node
                    blocks.append((None, child_computed, child_style))
                    segments.append([])
                    continue
        segments[-1].append(node)

    # `getClientRects()`/`getBoundingClientRect()`: Chrome exposes one extra,
    # zero-height rect per interruption -- positioned exactly where the
    # interrupting block sits -- alongside the real leading/trailing
    # fragment rects (confirmed: a 2-fragment split's `element.getClientRects
    # ()` returns *3* rects in real Chrome, not 2). `_finalize_inline_owner_
    # boxes` adds these once boxes are final; record which blocks to use here
    # (overwritten fresh on every relayout that reaches this branch) rather
    # than recomputing the split there, where only `owner_accum`'s already-
    # merged rects are visible. A nested-wrapper interruption reports the
    # first *real* block reachable through it -- the same element its own
    # recursive split (below) will itself report a marker for -- so every
    # ancestor level between the real block and the line gets its own
    # marker rect at that same position, matching real Chrome.
    wrapper._chromonic_interruption_blocks = [
        block if block is not None else _first_reachable_in_flow_block(nested_wrapper_at[index], computed_cache)
        for index, (block, _computed, _style) in enumerate(blocks)
    ]
    wrapper._chromonic_atomic_segment_elements = {}
    wrapper._chromonic_split_edge_flow_height = {}
    # `wrapper` itself is never built as a real Taffy node at all when it's
    # a *nested* wrapper (see this function's own docstring) -- it never
    # ends up in `node_map`, so nothing that only ever walks `node_map.
    # values()` (e.g. `_fix_nested_split_flow_extent`) can discover it
    # directly. Each real interruption block *is* a genuine node, though,
    # so a back-reference on it is a reliable way back to its wrapper.
    for block, _computed, _style in blocks:
        if block is not None:
            block.__dict__["_chromonic_split_wrapper_ref"] = wrapper

    for index, seg_nodes in enumerate(segments):
        is_first, is_last = index == 0, index == len(segments) - 1
        # `direction:rtl`: the first segment owns the *start* (physical-
        # right) package, the last owns the *end* (physical-left) one --
        # swapped from `ltr`'s first=left/last=right (CSS 2.1's own bidi box
        # model, confirmed against `left-rtl-ref.xht`'s real geometry).
        seg_leading = (left_edge if is_last else 0.0) if is_rtl else (left_edge if is_first else 0.0)
        seg_trailing = (right_edge if is_first else 0.0) if is_rtl else (right_edge if is_last else 0.0)
        seg_margin_start = (margin_left if is_last else 0.0) if is_rtl else (margin_left if is_first else 0.0)
        seg_margin_end = (margin_right if is_first else 0.0) if is_rtl else 0.0
        runs = _build_text_runs_from_nodes(
            seg_nodes, paint_style, wrapper,
            leading_edge=seg_leading,
            trailing_edge=seg_trailing,
            top_edge_val=top_edge_val, extra_height=extra_height,
            margin_start=seg_margin_start, margin_end=seg_margin_end,
            computed_cache=computed_cache,
        )
        if not runs and (seg_leading or seg_trailing):
            # A leading/trailing segment with no real text still needs a
            # fragment when it has real inline extent (own padding making
            # it non-zero-width) -- CSS 2.1 9.2.1.1's split generates an
            # anonymous inline box there even with no text, at the font's
            # line-height plus the wrapper's vertical border/padding. A
            # segment with *no* inline extent is genuinely `0x0` instead.
            runs = [_empty_inline_strut_run(
                wrapper, seg_leading, seg_trailing, top_edge_val, extra_height, seg_margin_start,
            )]
            # Ancestor auto-height must read only the real line box
            # (`above`+`below`), not this fragment's visual height (which
            # includes the wrapper's vertical border/padding) -- stashed
            # per edge for `_fix_nested_split_flow_extent` to correct with.
            wrapper.__dict__.setdefault("_chromonic_split_edge_flow_height", {})[
                "leading" if is_first else "trailing"
            ] = runs[0]["above"] + runs[0]["below"]
        if not runs:
            # A segment can also come up empty because it holds one or
            # more atomic inline-level elements (`inline-block`/replaced)
            # with no text -- `_build_text_runs_from_nodes` can't build a
            # subtree for those, so represent each as its own real,
            # recursively-built block-flow piece instead.
            atomic_candidates = [
                (node,) + _describe(node, computed_cache)
                for node in seg_nodes if _is_element(node)
            ]
            if atomic_candidates and all(
                (_is_inline_level(node, node_style) and not _is_genuine_inline_wrapper(node, node_style))
                or _is_floated(node_computed)
                for node, node_computed, node_style in atomic_candidates
            ):
                wrapper._chromonic_atomic_segment_elements[index] = [node for node, _c, _s in atomic_candidates]
                for node, node_computed, node_style in atomic_candidates:
                    yield ("block", node, node_computed, node_style)
                # A zero-edge marker run, not a real fragment -- every
                # segment needs at least one run tagged to it or
                # `_finalize_inline_owner_boxes` skips its marker rect too.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
            else:
                # Genuinely nothing on this side -- still an explicit `0x0`
                # fragment (Chrome reports one), but no height/font/flow
                # contribution of its own.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
        if runs:
            # Tags each run with its segment index (document order), so
            # `_finalize_inline_owner_boxes` places interruption markers
            # logically rather than via a geometric sort.
            for run in runs:
                if not run.get("break"):
                    run["split_group"] = index
                    if is_rtl and (is_first or is_last):
                        # A zero `margin_end`/`margin_start` on this segment
                        # is a real, direction-aware answer here (see the
                        # identical tag elsewhere) -- suppress `measure()`'s
                        # owner-raw-margin fallback, which exists only for
                        # the untagged, non-`rtl` case above where that
                        # fallback is genuinely needed (margin-right is
                        # never threaded there at all).
                        run["_bidi_margin_resolved"] = True
            yield ("run", runs)
        if index < len(blocks):
            if index in nested_wrapper_at:
                # Not a real block child -- delegate to this nested
                # wrapper's own split (one anonymous-block level per
                # genuine inline ancestor, CSS 2.1 9.2.1.1 applied recursively).
                yield from _split_wrapping_inline_element(
                    nested_wrapper_at[index], computed_cache, container)
            else:
                yield ("block",) + blocks[index]


def _split_inline_flow_around_blocks(element, inline_items, style, css_display, computed_cache):
    """`inline_items` contains, at some depth reachable only through
    inline-level elements, a genuine in-flow block -- CSS 2.1 9.2.1.1's
    "anonymous block box" case (`<span>One<div/>Two</span>`: the block
    forces `span` to split into a "One" fragment, the block, and a "Two"
    fragment, siblings in normal block flow, not one flex row or a single
    measured text leaf).

    Returns an ordered list of pieces -- `("plan", _InlineFormattingPlan)`
    or `("block", child, child_computed, child_style)` -- ready to become
    `element`'s ordinary block-stack Taffy children. `None` if nothing
    needs splitting -- caller falls back to its existing handling.

    Two shapes reach this function (`_has_direct_in_flow_block_child`
    distinguishes them): `element` itself directly parenting the block
    (splits itself, dispatched immediately below), or a plain block
    `element` merely containing a nested inline item that wraps a block
    deeper (only that nested item splits; `element` stays an ordinary
    container, handled by the per-item loop below)."""
    if _has_direct_in_flow_block_child(element, computed_cache):
        pieces: list = []
        runs_acc: list = []
        for sub in _split_wrapping_inline_element(element, computed_cache, element):
            if sub[0] == "run":
                runs_acc.extend(sub[1])
            else:
                if runs_acc:
                    plan = _InlineFormattingPlan(
                        element, runs_acc, element._chromonic_paint_style, css_display)
                    plan._chromonic_final_split_fragment = False
                    pieces.append(("plan", plan))
                    runs_acc = []
                pieces.append(sub)
        if runs_acc:
            plan = _InlineFormattingPlan(
                element, runs_acc, element._chromonic_paint_style, css_display)
            plan._chromonic_final_split_fragment = True
            pieces.append(("plan", plan))
        return pieces
    element.__dict__.pop("_chromonic_interruption_blocks", None)
    element.__dict__.pop("_chromonic_split_container", None)
    pieces: list = []
    pending: list = []
    found_split = False

    def flush_pending():
        if pending:
            plan = _make_inline_formatting_plan(element, list(pending), style, css_display, computed_cache,
                                                allow_escapees=True)
            if plan is not None:
                pieces.append(("plan", plan))
            pending.clear()

    for kind, item, text, child_computed, child_style in inline_items:
        if (kind == "element" and not _is_absolutely_positioned(child_style)
                and _is_genuine_inline_wrapper(item, child_style)
                and _contains_in_flow_block(item, computed_cache)):
            found_split = True
            # CSS 2.1 9.2.1.1: the wrapper's own leading fragment isn't
            # itself a line break -- any text already pending before it
            # belongs on the *same* line box, right up until the first
            # real block interruption actually forces a split. Seed
            # `runs_acc` from `pending`'s own runs instead of flushing it
            # as an independent, separately-positioned plan (which would
            # otherwise stack it as its own zero-height row above the
            # wrapper's leading segment rather than sharing its line).
            runs_acc: list = []
            if pending:
                pending_plan = _make_inline_formatting_plan(element, list(pending), style, css_display, computed_cache)
                if pending_plan is not None:
                    runs_acc.extend(pending_plan.runs)
                pending.clear()
            for sub in _split_wrapping_inline_element(item, computed_cache, element):
                if sub[0] == "run":
                    runs_acc.extend(sub[1])
                else:
                    if runs_acc:
                        plan = _InlineFormattingPlan(
                            element, runs_acc, element._chromonic_paint_style, css_display)
                        # A block interruption follows -- this plan is a
                        # *leading* or *interior* segment, never the
                        # wrapper's true trailing one, regardless of
                        # whether it happens to be the only (and so,
                        # locally, "last") entry in its own `placed` list.
                        # `measure()`'s RTL mirror must not apply the
                        # wrapper's margin-right to it on that false
                        # signal -- margin-right belongs only to the one
                        # segment that comes after every interruption.
                        plan._chromonic_final_split_fragment = False
                        pieces.append(("plan", plan))
                        runs_acc = []
                    pieces.append(sub)
            if runs_acc:
                # Nothing follows this plan for `item` -- the real trailing
                # segment, where `measure()` should apply the wrapper's
                # margin-right normally (the default when this attribute is
                # absent, as for every ordinary non-split plan).
                plan = _InlineFormattingPlan(
                    element, runs_acc, element._chromonic_paint_style, css_display)
                plan._chromonic_final_split_fragment = True
                pieces.append(("plan", plan))
        else:
            pending.append((kind, item, text, child_computed, child_style))
    flush_pending()
    return pieces if found_split else None


def _make_inline_formatting_plan(element, inline_items, style, css_display, computed_cache=None,
                                 allow_escapees: bool = False):
    """Build styled text runs for a shared inline formatting context.
    `allow_escapees`: a top-level absolutely positioned item becomes an
    "escapee" marker run (its static position) instead of making this
    function bail to the flex-row fallback -- the CSS 2.1 9.2.1.1 split
    path has no such fallback for a segment (abspos-029.html: an abs div
    alone between two blocks inside an inline span was simply dropped)."""
    if any(kind == "element" and (
            (_is_absolutely_positioned(child_style) and not allow_escapees)
            # CSS 2.1 9.5: a float, like an abs-pos item, is out of flow --
            # but unlike abs-pos this plan has no escapee/static-position
            # handling for one (real float positioning needs the
            # block-level packer, `_fix_float_flow_after_block_sibling`-
            # style logic, not a text-flow static position) -- always
            # bails to the `elif inline_items:` flex-row fallback, which
            # `build()` gives its own float handling
            # (`_chromonic_inline_floats`/`_fix_inline_float_position`).
            or _is_floated(child_computed)
            or isinstance(item, _PseudoElement)
            # A *real* nested element with only text children (no further
            # element nesting) would otherwise be absorbed straight into
            # this plan as flattened text runs (see the "element" branch
            # below, `_build_text_runs_from_nodes`) -- which never calls
            # `build()` on it at all, so its *own* `::before`/`::after`
            # (one level deeper than this function ever looks) would
            # silently never be considered. Bail so the flex-row fallback's
            # real recursive `build()` call on it runs instead, exactly
            # like an absolutely-positioned or pseudo item already does.
            or getattr(item, "_chromonic_before_pseudo", None) is not None
            or getattr(item, "_chromonic_after_pseudo", None) is not None
            # `inline-block` (CSS 2.1 10.3.10) is always an atomic box with
            # its own formatting context, own explicit/auto width+height,
            # never flattened text runs sharing this plan's line metrics --
            # the "element" branch below only ever tries to flatten a
            # nested element's own *text* into runs, which for a genuinely
            # empty `inline-block` (no text, no children at all) produces
            # zero runs and *no fallback strut either* (that fallback is
            # deliberately `display:inline`-only, since only a plain empty
            # inline collapses to nothing -- an empty inline-block still
            # keeps its own explicit box, CSS 2.1 9.2.1.1/10.8) -- silently
            # dropping the element from the plan's own `runs` entirely, and
            # therefore from `node_map`, painted or not. Confirmed on `wpt/
            # css/CSS2/visudet/inline-block-baseline-011.xht`: an empty
            # `<span style="display:inline-block">` between two text runs
            # never got a Taffy node at all. Bailing here routes it through
            # the flex-row fallback instead, which already builds every
            # element item as its own real recursive `build()` subtree.
            or getattr(child_style.display, "value", "") in (
                "inline-block", "inline-flex", "inline-grid", "-webkit-inline-flex")
            # An `inline-table` (CSS 2.1 17.4) -- a real element's, or a
            # CSS 2.1 17.2.1 anonymous one generated around loose cells
            # inside this inline (`_AnonymousTableBox`) -- is exactly as
            # atomic: one box, laid out by its own table algorithm.
            # Flattened as if it were a plain inline, its cells (never
            # inline-level themselves) read as in-flow blocks that split
            # this inline around them, one full-width row per cell
            # (table-anonymous-objects-177.xht).
            or getattr(child_style.display, "value", "") == "inline-table"
            # A replaced element (`<img>`, `<canvas>`, `<svg>`, `<iframe>`,
            # a form control) is exactly as atomic as `inline-block` above
            # -- its own box is a real Taffy leaf sized from its own
            # intrinsic/authored width+height, never flattened text runs --
            # and one is typically a void element with no `childNodes` of
            # its own at all, so it hits the exact same silent-drop gap:
            # zero runs, no empty-strut fallback (also deliberately
            # excluded for these tags, since they don't collapse to
            # nothing like a plain empty inline can). Confirmed directly on
            # `wpt/css/CSS2/visudet/replaced-elements-width-40.html`: every
            # `<img>` meant to flow inline with the comma-separated text
            # between them vanished from layout entirely once that text
            # started actually being recognised as inline-mixed content
            # (see `_USUALLY_INLINE_TAGS` picking up `img`/`canvas`/`svg`/
            # `iframe`).
            or (getattr(item, "tagName", "") or "").lower() in _REPLACED_OR_CONTROL_TAGS)
           for kind, item, _text, child_computed, child_style in inline_items):
        # A generated-content pseudo-element needs its own real box (own
        # font, own position, possibly absolute) -- this shared-plan path
        # only ever measures *text runs* sharing one Taffy leaf, with no
        # way to represent a distinct nested box at all, let alone one an
        # empty-`content` pseudo (an icon-only box with no text of its own,
        # e.g. `csszengarden.com`'s `h1::before`) needs just to exist. The
        # flex-row fallback below already builds every "element" item as
        # its own real recursive `build()` subtree -- exactly what a
        # pseudo-element needs -- so route it there unconditionally,
        # same as an absolutely-positioned item already does.
        return None
    runs = []
    for kind, item, collapsed, child_computed, child_style in inline_items:
        if kind == "break":
            # Forced line-break: store a sentinel run so measure() can end the line.
            runs.append({"break": True, "element": item})
            continue
        if kind == "element" and _is_absolutely_positioned(child_style):
            runs.append({"escapee": True, "element": item, "computed": child_computed, "style": child_style})
            continue
        if kind == "text":
            source = item.source
            owner = element
            paint_style = element._chromonic_paint_style
            native = None
        else:
            # Nested markup is flattened into this plan when it contains
            # text and `<br>` forced line-breaks -- and, recursively, any
            # further plain (`_is_genuine_inline_wrapper`) inline nesting
            # too (`_build_text_runs_from_nodes`, called below, recurses
            # into each non-`<br>` element child the same way). A nested
            # block, `inline-block`, or replaced descendant still can't be
            # represented as a flattened text run, so it's silently
            # dropped rather than bailing this whole plan to the flex-row
            # fallback -- confirmed directly that fallback mismeasures
            # mixed text + nested-inline content (real Chrome vs chromonic
            # on `news.ycombinator.com`'s subtext line, `<span class="age">
            # <a>1 hour ago</a></span>` sitting among plain text and other
            # `<a>`s: the whole line was scrambled -- the nested `<a>`'s
            # own text wrapped internally into two lines and landed out of
            # DOM order relative to its siblings).
            # `item` is on the ordinary (non-split) path this layout --
            # clear any stale interruption-block bookkeeping a previous
            # layout's split may have left on it.
            item.__dict__.pop("_chromonic_interruption_blocks", None)
            item.__dict__.pop("_chromonic_split_container", None)
            # Walk childNodes to collect text segments and <br> breaks,
            # producing runs for each and decorating them with the child
            # element's border+padding edges (CSS 2.1: first fragment gets
            # left edge, last fragment gets right edge). See
            # `_build_text_runs_from_nodes` -- also reused, with a real
            # split, by `_split_wrapping_inline_element`.
            native = style_bridge.to_dict(child_style)
            item._chromonic_native_style = native
            left_edge = (_numeric_edge(native["padding"][3])
                         + _numeric_edge(native["border"][3]))
            right_edge = (_numeric_edge(native["padding"][1])
                          + _numeric_edge(native["border"][1]))
            top_edge_val = (_numeric_edge(native["padding"][0])
                            + _numeric_edge(native["border"][0]))
            extra_height = (top_edge_val
                            + _numeric_edge(native["padding"][2])
                            + _numeric_edge(native["border"][2]))
            # margin-left/-right apply before the first/after the last LTR
            # fragment only; CSS 2.1 10.3.1/10.3.3: real spacing, but never
            # part of either fragment's own rect, and -- unlike a block's
            # vertical margins -- never collapses with an adjoining
            # element's own margin (`_InlineFormattingPlan.measure()`'s own
            # `margin_end` handling adds both sides independently).
            margin_start = _numeric_edge(native["margin"][3])
            margin_end = _numeric_edge(native["margin"][1])
            # CSS 2.1's bidi box model (box.html#bidi-box-model, confirmed
            # against `left-rtl-ref.xht`'s own reference markup) attaches
            # the *start*-side package (border+padding+margin together) to
            # the DOM-first fragment and the *end*-side package to the
            # DOM-last one -- start=left/end=right for `ltr`, start=right/
            # end=left for `rtl`. `_build_text_runs_from_nodes`'s own
            # `leading_edge`/`trailing_edge` params are always physical-left/
            # physical-right, so for a `direction:rtl` item the two edge
            # packages are built unswapped here and then swapped onto the
            # correct fragment below, once the actual (possibly `<br>`-
            # split) fragments are known.
            is_rtl_item = _element_direction(item, child_computed) == "rtl"
            # Flattened into this plan: no Taffy box of its own this pass
            # (`build()` clears the mark when it does build the element).
            item.__dict__["_chromonic_flattened_inline"] = True
            child_runs = _build_text_runs_from_nodes(
                list(_child_nodes(item)), item._chromonic_paint_style, item,
                leading_edge=0.0 if is_rtl_item else left_edge,
                trailing_edge=0.0 if is_rtl_item else right_edge,
                top_edge_val=top_edge_val, extra_height=extra_height,
                margin_start=0.0 if is_rtl_item else margin_start,
                margin_end=0.0 if is_rtl_item else margin_end,
                computed_cache=computed_cache,
            )
            leading_space_run = None
            if getattr(item, "_chromonic_leading_collapsed_space", False) and child_runs:
                # A whitespace-only text node right before this element
                # collapsed to nothing of its own (see where this flag is
                # set, in `_inline_mixed_content`) -- restore it as its
                # own standalone run, owned by this shared plan's own
                # `element` (the same as any other collapsed-whitespace
                # text node would be), the same collapsed space a plain
                # text item's own `inferred_leading` (just below) restores
                # for itself. Kept as a *separate* run rather than merged
                # into this element's own first token -- confirmed wrong
                # directly on `inline-formatting-context-013.xht`: merged,
                # a wrap point landing between the space and this
                # element's own content left a stray zero-width space
                # fragment on the previous line real Chrome never reports.
                leading_space_run = _make_collapsed_space_run(
                    element._chromonic_paint_style, element, top_edge_val, extra_height)
            if is_rtl_item:
                non_break_runs = [r for r in child_runs if not r.get("break")]
                if non_break_runs:
                    first_run, last_run = non_break_runs[0], non_break_runs[-1]
                    # See the identical tag in `_build_text_runs_from_nodes`'s
                    # nested-element branch: a zero `margin_end` here is a
                    # real, direction-aware answer, not an unthreaded field.
                    first_run["_bidi_margin_resolved"] = True
                    last_run["_bidi_margin_resolved"] = True
                    if first_run is last_run:
                        # Unsplit -- the sole run owns both edges, physically,
                        # regardless of direction.
                        first_run["leading"] = first_run.get("leading", 0.0) + left_edge
                        first_run["trailing"] = first_run.get("trailing", 0.0) + right_edge
                        first_run["margin_start"] = first_run.get("margin_start", 0.0) + margin_start
                        first_run["margin_end"] = first_run.get("margin_end", 0.0) + margin_end
                        first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                          + left_edge + right_edge
                                                          + margin_start + margin_end)
                    else:
                        first_run["trailing"] = first_run.get("trailing", 0.0) + right_edge
                        first_run["margin_end"] = first_run.get("margin_end", 0.0) + margin_end
                        first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                          + right_edge + margin_end)
                        last_run["leading"] = last_run.get("leading", 0.0) + left_edge
                        last_run["margin_start"] = last_run.get("margin_start", 0.0) + margin_start
                        last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                         + left_edge + margin_start)
            item_display = (getattr(child_computed, "display", "") or "").strip().lower()
            item_tag = (getattr(item, "tagName", "") or "").lower()
            if (not child_runs and not _child_nodes(item)
                    and item_display == "inline" and item_tag not in _REPLACED_OR_CONTROL_TAGS):
                # CSS 2.1 9.2.1.1/10.8's empty-inline strut applies only to
                # a plain, non-replaced `display:inline` -- an `inline-
                # block`/replaced element keeps its own explicit width/
                # height even with no content (CSS 2.1 10.3.10).
                child_runs = [_empty_inline_strut_run(
                    item, left_edge, right_edge, top_edge_val, extra_height, margin_start,
                )]
            if leading_space_run is not None:
                runs.append(leading_space_run)
            runs.extend(child_runs)
            continue
        raw = getattr(source, "textContent", "") or collapsed or ""
        text = _apply_text_transform(_CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw), paint_style.get("text_transform"))
        if not text.strip(_CSS_WHITESPACE_STRIP_CHARS):
            continue
        # Whitespace collapses across run boundaries. Keep a single leading
        # or trailing space only when the source actually contains one.
        inferred_leading = (kind == "text" and
                            getattr(item, "_chromonic_leading_collapsed_space", False))
        text = ((" " if (raw[:1] and raw[:1] in _CSS_WHITESPACE_STRIP_CHARS) or inferred_leading else "")
                + text.strip(_CSS_WHITESPACE_STRIP_CHARS)
                + (" " if raw[-1:] and raw[-1:] in _CSS_WHITESPACE_STRIP_CHARS else ""))
        font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
        family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
        weight = _parse_font_weight(paint_style["font_weight"])
        italic = fonts.is_italic(paint_style["font_style"])
        ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
        glyph_height = ascent + descent
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style["line_height"])
        used_line_height = resolved_line_height if resolved_line_height is not None else normal_height
        above = ascent + math.floor((used_line_height - glyph_height) / 2)
        below = used_line_height - above
        nowrap = bool(kind == "element" and child_computed.whiteSpace == "nowrap")
        token_texts = [text] if nowrap else re.findall(r"\S+\s*|\s+", text)
        one_width = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
        spaced_width = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
        space_width = max(0.0, spaced_width - 2.0 * one_width)
        tokens = []
        for token in token_texts:
            measured, _height, _lines = layout_text(
                token, family, font_size, font_weight=weight, italic=italic,
                letter_spacing=_fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0),
                word_spacing=_fontmetrics.parse_length(paint_style["word_spacing"], default=0.0),
            )
            measured = sum(line[1] for line in _lines)
            tokens.append((token, measured))
        leading = trailing = 0.0
        box_height = glyph_height
        top_edge = 0.0
        if native is not None:
            top_edge = _numeric_edge(native["padding"][0]) + _numeric_edge(native["border"][0])
            leading = _numeric_edge(native["padding"][3]) + _numeric_edge(native["border"][3])
            trailing = _numeric_edge(native["padding"][1]) + _numeric_edge(native["border"][1])
            box_height += (_numeric_edge(native["padding"][0]) + _numeric_edge(native["padding"][2])
                           + _numeric_edge(native["border"][0]) + _numeric_edge(native["border"][2]))
            if isinstance(native["height"], (int, float)):
                box_height = max(box_height, float(native["height"]))
        atomic_width = (float(native["width"])
                        if native is not None and isinstance(native["width"], (int, float)) else 0.0)
        runs.append({
            "source": source, "owner": owner, "paint_style": paint_style,
            "font_size": font_size, "tokens": tokens, "leading": leading,
            "trailing": trailing, "box_height": box_height,
            "glyph_height": glyph_height, "ascent": ascent,
            "above": above, "below": below, "top_edge": top_edge,
            "space_width": space_width,
            "atomic_width": atomic_width,
            "intrinsic_width": leading + max(
                atomic_width, sum(width for _text, width in tokens)
            ) + trailing,
        })
    # DOM boundaries with identical shaping properties are not kerning
    # boundaries. Preserve the pair adjustment across adjacent text owners.
    shaping_keys = ("font_family", "font_size", "font_weight", "font_style",
                    "letter_spacing", "word_spacing")
    # CSS 2.1 16.6.1: collapsible spaces collapse across element
    # boundaries too -- a run ending in a space followed by one starting
    # with a space keeps just one (abspos-inline-001.xht: `<span>...text.
    # </span>\n<span> The test...`, one 7.8px space in Chrome, not two).
    previous = None
    for run in runs:
        if run.get("break") or run.get("escapee"):
            previous = run if run.get("break") else previous
            continue
        tokens = run["tokens"]
        if (previous is not None and not previous.get("break") and tokens and previous["tokens"]
                and previous["tokens"][-1][0][-1:] in _CSS_WHITESPACE_STRIP_CHARS
                and tokens[0][0] and not tokens[0][0].strip(_CSS_WHITESPACE_STRIP_CHARS)):
            run["tokens"] = tokens[1:] or [("", 0.0)]
            run["intrinsic_width"] = max(0.0, run.get("intrinsic_width", 0.0) - tokens[0][1])
        previous = run
    for left, right in zip(runs, runs[1:]):
        if left.get("break") or right.get("break") or left.get("escapee") or right.get("escapee"):
            continue  # a forced break or an out-of-flow escapee has no glyphs to kern against
        if left["trailing"] or right["leading"] or left["atomic_width"] or right["atomic_width"]:
            continue
        if any(left["paint_style"][key] != right["paint_style"][key] for key in shaping_keys):
            continue
        ls = left["paint_style"]
        a = ''.join(token for token, _width in left["tokens"])
        b = ''.join(token for token, _width in right["tokens"])
        def advance(value):
            return sum(line[1] for line in layout_text(
                value, ls["font_family"], left["font_size"],
                font_weight=_parse_font_weight(ls["font_weight"]), italic=fonts.is_italic(ls["font_style"]),
                letter_spacing=_fontmetrics.parse_length(ls["letter_spacing"], default=0.0),
                word_spacing=_fontmetrics.parse_length(ls["word_spacing"], default=0.0))[2])
        adjustment = advance(a + b) - advance(a) - advance(b)
        token, old_width = left["tokens"][-1]
        left["tokens"][-1] = (token, old_width + adjustment)
        left["intrinsic_width"] += adjustment
    return _InlineFormattingPlan(element, runs, element._chromonic_paint_style, css_display) if runs else None


def _needs_inline_flow_grouping(child, child_style) -> bool:
    """Whether `child` still needs `_group_inline_element_runs`'s flex-row
    simulation of real inline flow. An inline-level *tag* (`_is_inline_
    level`) that CSS 2.1 9.2.1.1 has already split around an in-flow block
    child (`_split_wrapping_inline_element` marks this on the element
    itself via `_chromonic_split_container`, set every time it runs) no
    longer needs it: `build()` already represents that element as an
    ordinary block-flow Taffy node (ordinary sibling block pieces, not
    real horizontal inline content), so wrapping it in a flex row here
    would be both redundant and actively wrong -- CSS margins never
    collapse across a flex formatting context, so a plain block sibling
    after it could never collapse through the wrapper with the split's
    own trailing margin, even though nothing here is really "inline"
    content anymore. Confirmed directly: `<div class="container"><span>
    <div class="first"></div></span><div class="second"></div></div>`
    only collapsed margin-bottom/margin-top additively (70px) instead of
    the correct `max()` (40px) until this exemption was added -- the
    anonymous flex wrapper `_group_inline_element_runs` built around the
    already-dissolved `<span>` was the collapse barrier."""
    return _is_inline_level(child, child_style) and getattr(child, "_chromonic_split_container", None) is None


def _group_inline_element_runs(tree, parent, entries, parent_style, node_map, projection):
    """Wrap consecutive inline siblings in retained anonymous flex rows."""
    if parent_style["display"] != "block":
        return [node_id for _child, _style, node_id in entries]
    output, run_index, index = [], 0, 0
    cache = parent.__dict__.setdefault("_chromonic_inline_runs", {})
    while index < len(entries):
        child, child_style, node_id = entries[index]
        if not _needs_inline_flow_grouping(child, child_style):
            output.append(node_id)
            index += 1
            continue
        run = []
        run_elements = []
        while index < len(entries) and _needs_inline_flow_grouping(entries[index][0], entries[index][1]):
            run.append(entries[index][2])
            run_elements.append(entries[index][0])
            index += 1
        wrapper = cache.get(run_index)
        if wrapper is None:
            wrapper = cache[run_index] = _AnonymousInlineRun(None, parent)
        run_index += 1
        wrapper_style = _inline_text_style(parent_style)
        # A real inline formatting context's default cross-axis alignment
        # is the text baseline, not flex's own initial "normal"/stretch.
        wrapper_style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap",
                              "align_items": "baseline"})
        wrapper._chromonic_native_style = wrapper_style
        # `_fix_flex_row_baseline_alignment` only corrects Taffy's own
        # (sometimes wrong -- see that function's docstring) baseline
        # cross-axis placement for a row it can find via this attribute;
        # never set here before, so a run of consecutive real inline-level
        # element siblings (not the separate mixed-text-and-elements `elif
        # inline_items:` approximation, which already sets it) got no such
        # correction at all. Confirmed on `wpt/css/CSS2/visudet/content-
        # height-001.html`: three sibling `display:inline-block` divs with
        # different `line-height`s (so genuinely different heights) landed
        # at the wrong `y` relative to each other, silently unfixed.
        wrapper._chromonic_flex_row_members = run_elements
        wrapper_id = (projection.upsert(wrapper, wrapper_style, run, None, None)
                      if projection else tree.new_with_children(wrapper_style, run))
        node_map[wrapper_id] = wrapper
        output.append(wrapper_id)
    for stale in [key for key in cache if key >= run_index]:
        cache.pop(stale)
    return output


def _parse_font_weight(value) -> float:
    """CSS `font-weight`'s computed value -- domonic normalises it to a
    plain numeric string ("400"/"700") for anything but a genuinely unknown
    input, but named fallbacks are handled here too rather than assumed."""
    if not value:
        return 400.0
    text = str(value).strip().lower()
    named = {"normal": 400.0, "bold": 700.0, "bolder": 700.0, "lighter": 300.0}
    if text in named:
        return named[text]
    try:
        return float(text)
    except ValueError:
        return 400.0


def _resolved_line_height(value) -> "float | None":
    """`None` means "normal" -- let Parley use the font's own metrics-based
    line height (its own default), rather than guessing one ourselves.
    domonic already resolves an explicit `line-height` (unitless or not) to
    a plain `"Npx"` string, so this is just `_fontmetrics.parse_length`
    guarded against the unset/"normal" case."""
    if not value or value == "normal":
        return None
    return _fontmetrics.parse_length(value, default=None)


def _make_measure(paint_style: dict, text: str, element):
    """Real text layout via Parley (`chromonic._native.layout_text`) -- font
    matching, shaping, and genuine Unicode line-breaking. `font_family` is
    read from `paint_style` (already extracted by `_describe`), not a
    fresh `computed.fontFamily` access, to avoid re-resolving it. Parley's
    job is strictly layout (line breaks, space needed), not painting --
    `chromonic.fonts` separately resolves the actual Skia typeface."""
    font_family = paint_style["font_family"]
    if font_family == "none":
        font_family = ""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    font_weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    letter_spacing = _fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0)
    word_spacing = _fontmetrics.parse_length(paint_style["word_spacing"], default=0.0)
    line_height = _resolved_line_height(paint_style["line_height"])
    word_break = (paint_style.get("word_break") or "normal").strip().lower()
    overflow_wrap = (paint_style.get("overflow_wrap") or "normal").strip().lower()
    # CSS Sizing 3's min-content carve-out: `overflow-wrap: break-word`
    # (unlike `anywhere`) must *not* shrink the min-content contribution
    # below "widest whole word" -- it only breaks a word that would
    # otherwise overflow its line, which by definition never happens at
    # a min-content query's own (infinitely narrow) constraint. `word-
    # break: break-all` has no such carve-out; it always may break
    # anywhere. Matches Parley's own `OverflowWrap::BreakWord` doc note
    # ("treated differently for min-content sizing"); handled here
    # rather than assumed inside Parley itself, since the `max_width:
    # 1.0` call below is this function's own proxy for "give me min-
    # content", not a dedicated min-content request Parley can key off.
    breaks_within_words_at_min_content = word_break == "break-all" or overflow_wrap == "anywhere"
    ascent, descent, normal_height = fonts.text_metrics(font_family, font_size, font_weight >= 600, italic)

    def measure(available_width, available_height, _known_width=None, _known_height=None):
        # `-1.0` is `src/lib.rs`'s sentinel for Taffy's `MinContent`
        # request: wrap at every opportunity, so the reported width is
        # the widest unbreakable piece (`white-space: nowrap`/`pre` text
        # has no break opportunities -- its min-content is its max-content).
        min_content = available_width is not None and available_width < 0
        if min_content:
            available_width = None
        width, height, lines = layout_text(
            text, font_family, font_size,
            font_weight=font_weight, italic=italic,
            max_width=(None if paint_style.get("white_space") in ("pre", "nowrap")
                       else 1.0 if min_content else available_width),
            letter_spacing=letter_spacing, word_spacing=word_spacing, line_height=line_height,
            word_break=word_break, overflow_wrap=overflow_wrap,
        )
        if (min_content and paint_style.get("white_space") not in ("pre", "nowrap")
                and not breaks_within_words_at_min_content):
            # A wrapped line's width from Parley keeps its trailing space
            # (`"IT "` = 150px in 50px Ahem); the min-content width is the
            # widest *word* (`"IT"` = 100px, Chrome's answer). Skipped
            # above when `word-break`/`overflow-wrap` allow breaking
            # *within* a word -- there, `width` already reflects that
            # (the `max_width: 1.0` call above forces every break Parley
            # will take), and re-measuring whole whitespace-delimited
            # words here would silently ignore it, overstating min-content.
            words = [word for word in re.split(r"[ \t\n\r\f]+", text) if word]
            if words:
                width = max(layout_text(word, font_family, font_size, font_weight=font_weight, italic=italic,
                                        letter_spacing=letter_spacing, word_spacing=word_spacing,
                                        line_height=line_height)[0] for word in words)
        if line_height is None and lines:
            # Chrome exposes integral line-box heights for platform fonts
            # while Parley's raw metrics are fractional -- normalize the
            # implicit `normal` line box before it accumulates down a page.
            lines = [(line_text, line_width, normal_height)
                     for line_text, line_width, _height in lines]
            height = normal_height * len(lines)
        # stashed for paint.py -- the *only* record of how this text wrapped
        # (and, now, its real per-line height); paint draws exactly these
        # lines rather than re-wrapping or re-measuring itself.
        element._chromonic_text_lines = [line_text for line_text, _line_width, _line_height in lines]
        line_widths = [line_width for _line_text, line_width, _line_height in lines]
        # CSS Text 3 `text-align: justify`: every line but the last (unless
        # `text-align-last` says otherwise) stretches to fill the line box
        # -- paint.py doesn't yet re-space individual words to visually
        # match (a real leaf here has no per-word gap bookkeeping the way
        # `_InlineFormattingPlan` does), but the *reported* line width
        # (hit-testing/`getClientRects()`) must still reflect the real,
        # filled extent, not each line's own natural shrink-to-fit one.
        if (len(line_widths) > 1 and (paint_style.get("text_align") or "").strip().lower() == "justify"
                and available_width and 0 < available_width < 1_000_000):
            text_align_last = (paint_style.get("text_align_last") or "auto").strip().lower()
            stretch_last = text_align_last not in ("auto", "left", "start", "")
            last_index = len(line_widths) - 1
            line_widths = [
                available_width if (index != last_index or stretch_last) else line_width
                for index, line_width in enumerate(line_widths)
            ]
        element._chromonic_text_line_widths = line_widths
        element._chromonic_line_height = lines[0][2] if lines else font_size * 1.2
        # `_fix_flex_row_baseline_alignment` needs this to re-assert the
        # real measured content height onto a member of an `align-items:
        # baseline` row -- Taffy's own cross-axis sizing for a custom
        # `MeasureFunc` leaf doesn't reliably keep this method's own
        # returned `height` once that alignment mode is in play (the same
        # quirk `_InlineFormattingPlan.measure()`'s own leaves have,
        # confirmed on `wpt/css/CSS2/visudet/inline-block-baseline-001.xht`:
        # a `display:inline-block` `<span>`'s `line-height:5`-driven `75px`
        # height came back `17px` -- this method itself, called directly
        # with the same arguments, correctly returns `75`).
        element._chromonic_measured_height = height
        return (width, height)

    return measure


def _form_control_display_text(element) -> str:
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name == "textarea":
        return getattr(element, "value", "") or element.textContent or ""
    input_type = (element.getAttribute("type") or "text").lower()
    if input_type in {"checkbox", "radio", "button", "submit", "reset", "file", "hidden"}:
        return ""
    value = getattr(element, "value", "") or ""
    if value and input_type == "password":
        text = "•" * len(str(value))
    else:
        text = str(value) if value else element.getAttribute("placeholder") or ""
    style = element.__dict__.get("_chromonic_paint_style", {})
    return _apply_text_transform(text, style.get("text_transform"))


def _select_display_text(element) -> str:
    """`<select>` is a native, closed dropdown -- shows only the selected
    option's text, not every `<option>` stacked as visible content. Picks
    the first `<option selected>`, falling back to the first option, or
    `""` if it has none."""
    options = element.getElementsByTagName("option")
    if not options:
        return ""
    for opt in options:
        if opt.hasAttribute("selected"):
            return " ".join((opt.textContent or "").split())
    return " ".join((options[0].textContent or "").split())


def _apply_image_intrinsic_size(style: dict, element) -> None:
    """`<img>` is a replaced element: an `auto` width/height is sized from
    its own intrinsic width/height -- built here since Taffy has no concept
    of an image's intrinsic properties (only the plain rectangle `width`/
    `height`/`aspect_ratio` this function bakes into `style` before Taffy
    ever sees the tree, as a block child's own width isn't `measure`'s to
    give). An explicit CSS size on either axis is always left alone.

    A raster image (PNG/JPEG/GIF/...) always has a complete intrinsic size.
    An SVG only has whichever of `width`/`height`/`viewBox` its root
    element actually declares (`browser_images.natural_size`), each
    independently possibly absent -- CSS 2.1 10.3.2 leaves a replaced
    element's sizing genuinely undefined for every case *except* a
    complete intrinsic width+height pair (this fixture's own `<meta
    name="flags" content="should">` notes exactly that), and real browsers
    resolve the undefined cases by falling back to the CSS default object
    size (300x150, the same UA default `<canvas>`/`<iframe>` already use)
    outright -- confirmed directly against Chrome on `wpt/css/CSS2/
    visudet/replaced-elements-width-40.html`'s own seven SVGs: an
    intrinsic ratio alone (from `viewBox`, with or without one matching
    dimension) is *not* used to scale an explicit CSS dimension the way
    CSS Images 3 5.2's idealised algorithm would suggest -- only a
    complete width+height pair ever drives sizing; every partial case
    (ratio-only, one dimension only, or nothing at all) gets the default
    150 height/300 width regardless."""
    from . import browser_images

    # Undo the auto-width block stretch this function itself synthesizes
    # below for a still-loading image, if the *previous* call here (on
    # this same element) applied one -- otherwise, once applied, it looks
    # identical to a real, explicit `width`, and everything below keeps
    # treating it that way even on a later call where the image actually
    # has arrived. `style` isn't necessarily freshly built each call: a
    # `reuse_styles=True` layout pass hands back the very same cached
    # dict this function already mutated last time, not a fresh one from
    # `style_bridge.to_dict()`. Confirmed directly: an `<img>` that starts
    # still-loading (this branch fires, `width` becomes `("pct", 1.0)`)
    # and then arrives on a later `reuse_styles=True` pass kept that
    # loading-time override -- `width_auto` read `False` below, skipping
    # the real intrinsic-size branch entirely and using the *stretched*
    # 300px width with the image's aspect ratio instead of its own real
    # (much smaller) intrinsic size.
    if getattr(element, "_chromonic_image_loading_width_stretch", False):
        style["width"] = "auto"
        element._chromonic_image_loading_width_stretch = False

    src = element.getAttribute("src") or ""
    image = browser_images.load_image(src)
    if image is None:
        # No image to size from yet -- nothing to reserve height for, but
        # width:auto still needs its ordinary block behavior (stretch to
        # fill the containing block), same as any other block-level box.
        # An `<img>` is always built as a bare Taffy *leaf* (`tree.new_leaf`,
        # just below this function's own call site), never a container with
        # children Taffy's own block algorithm sizes the normal way -- a
        # leaf has no measure function to fall back on while `width` is
        # still `"auto"`, so it silently collapses to `0` instead. Confirmed
        # directly: an otherwise-identical empty `<div>` (an ordinary
        # container node, not a leaf) correctly stretches to its 300px
        # parent; a still-loading `<img>` in the same spot measured `0`.
        if style["display"] == "block" and style["width"] == "auto":
            style["width"] = ("pct", 1.0)
            element._chromonic_image_loading_width_stretch = True
        return
    intrinsic_width, intrinsic_height, _ratio = browser_images.natural_size(src)
    has_complete_pair = intrinsic_width is not None and intrinsic_height is not None
    intrinsic_ratio = intrinsic_width / intrinsic_height if has_complete_pair and intrinsic_height else None
    # CSS Sizing 4: a declared `aspect-ratio` (a bare `<ratio>`, not the
    # `auto <ratio>` form -- domonic's own parser doesn't keep the two
    # distinguishable, see `style_bridge._aspect_ratio`) always wins over
    # the image's own natural ratio for sizing purposes, so it's swapped
    # in for `intrinsic_ratio` here, once, and the rest of this function's
    # existing ratio-driven sizing logic (already correct for the
    # natural-ratio case) applies to it unchanged.
    declared_ratio = style.get("aspect_ratio")
    if isinstance(declared_ratio, (int, float)):
        intrinsic_ratio = declared_ratio
    # CSS Images 3 5.2's own fallback when nothing intrinsic is known at
    # all on the needed axis -- 300x150, the same UA default `<canvas>`/
    # `<iframe>` already use elsewhere in this file.
    default_width, default_height = 300.0, 150.0
    if isinstance(style["height"], tuple) and style.get("position") not in ("absolute", "fixed"):
        # CSS 2.1 10.5: a percentage `height` whose containing block has
        # no definite height computes to `auto` -- and then, for a
        # replaced element, comes from the width and intrinsic ratio
        # (flex-aspect-ratio-img-column-004.html: `width: 100%; height:
        # 100%` in a `min-height: 500px` column is 100x50, not 0 tall).
        parent = _layout_parent(element)
        parent_native = getattr(parent, "_chromonic_native_style", None) if parent is not None else None
        if parent_native is not None and not isinstance(parent_native.get("height"), (int, float)):
            style["height"] = "auto"
    width_auto = style["width"] == "auto"
    height_auto = style["height"] == "auto"
    if width_auto and height_auto and intrinsic_ratio and _stretched_replaced_flex_item(element, style):
        # CSS Flexbox 9.2.3 rule C / 9.4: a replaced flex item stretched
        # across a row container with a definite height takes that
        # stretched cross size, and its main size then follows its own
        # ratio (flex-cross-size-border-box-001.html: a 1x1 image in a
        # 180px-tall row is 180x180). Taffy resolves both from the ratio
        # once the sizes are left `auto`.
        style["aspect_ratio"] = intrinsic_ratio
        return
    element.__dict__.pop("_chromonic_img_measure", None)
    if width_auto and height_auto:
        if has_complete_pair:
            width, height = intrinsic_width, intrinsic_height
            if False and intrinsic_ratio and style.get("flex_basis") == "auto" and _is_flex_or_grid_item(element):
                # Disabled: Taffy sizes a measured leaf's cross axis from
                # its style, never from the flexed main size, so this
                # bought nothing over the explicit sizes below and lost
                # the min/max ratio transfer (image-as-flexitem-size-001).
                # An auto-sized image flex item: its main size flexes
                # (`flex: 1`, image-as-flexitem-size-005.html) and the
                # cross size then follows the ratio from the *flexed*
                # size -- only Taffy knows that size, so the image is
                # built as a measured leaf (see `build()`): intrinsic
                # size unconstrained, ratio-derived once one side is known.
                iw, ih, ratio = intrinsic_width, intrinsic_height, intrinsic_ratio

                def measure(_available_width, _available_height, known_width=None, known_height=None,
                            iw=iw, ih=ih, ratio=ratio):
                    # Only a *known* size (the flexed main size, a
                    # stretched cross size) drives the ratio -- the
                    # available space is merely offered (a 16x16 image in
                    # a 40px box stays 16x16).
                    if known_width is not None:
                        return (known_width, known_height if known_height is not None else known_width / ratio)
                    if known_height is not None:
                        return (known_height * ratio, known_height)
                    return (iw, ih)

                element.__dict__["_chromonic_img_measure"] = (measure, ("img-measure", src, iw, ih))
                style["aspect_ratio"] = intrinsic_ratio
                return
            if (intrinsic_ratio and isinstance(style.get("flex_basis"), (int, float))
                    and _is_flex_or_grid_item(element)):
                # A numeric `flex-basis` is the image's main size; the
                # cross size follows the ratio from that used size
                # (image-as-flexitem-size-001.html: `flex-basis: 30px` on
                # a 16x16 image is 30x30). Taffy's own `aspect_ratio` only
                # ever reads the style width, never the flexed size, so
                # both are resolved here (flex-grow/shrink not modelled).
                parent_native = (_layout_parent(element).__dict__.get("_chromonic_native_style") or {})
                if (parent_native.get("flex_direction") or "row").startswith("row"):
                    width = float(style["flex_basis"])
                    height = width / intrinsic_ratio
                else:
                    height = float(style["flex_basis"])
                    width = height * intrinsic_ratio
            if intrinsic_ratio:
                # CSS 2.1 10.4: a min/max constraint on one axis of an
                # auto-sized replaced element transfers to the other
                # through the intrinsic ratio (image-as-flexitem-size-
                # 001.html: `min-width: 34px` on a 16x16 image is 34x34).
                for key, pick, axis in (("max_width", min, "w"), ("max_height", min, "h"),
                                        ("min_width", max, "w"), ("min_height", max, "h")):
                    bound = style.get(key)
                    if not isinstance(bound, (int, float)):
                        continue
                    if axis == "w" and pick(width, bound) != width:
                        width = bound
                        height = width / intrinsic_ratio
                    elif axis == "h" and pick(height, bound) != height:
                        height = bound
                        width = height * intrinsic_ratio
            style["width"], style["height"] = width, height
        else:
            style["width"], style["height"] = default_width, default_height
    elif height_auto and isinstance(style["width"], (int, float)) and _stretched_replaced_flex_item(element, style):
        # An explicit-width image stretched across a definite-height flex
        # row keeps that width and takes the row's height (flexbox-
        # whitespace-handling-001a.xhtml: `img { width: 40px }` items in
        # a 100px row are 40x100) -- height left `auto` for Taffy.
        pass
    elif height_auto and isinstance(style["width"], (int, float)):
        # CSS 2.1 10.4: the height comes from the width *after* its own
        # `min-width`/`max-width` clamp (flex-aspect-ratio-img-column-
        # 005.html: `width: 500px; max-width: 100%` in a 100px column is
        # 100x100, not 100x500 -- Taffy's own `aspect_ratio` applies the
        # ratio before clamping, so the clamp is resolved here, against
        # the parent's definite width for a percentage).
        clamped = style["width"]
        parent = _layout_parent(element)
        parent_native = getattr(parent, "_chromonic_native_style", None) if parent is not None else None
        parent_width = parent_native.get("width") if parent_native is not None else None
        for key, pick in (("max_width", min), ("min_width", max)):
            bound = style.get(key)
            if isinstance(bound, tuple) and isinstance(parent_width, (int, float)):
                bound = bound[1] * parent_width
            if isinstance(bound, (int, float)):
                clamped = pick(clamped, bound)
        style["height"] = clamped * (1.0 / intrinsic_ratio) if intrinsic_ratio else default_height
    elif width_auto and isinstance(style["height"], (int, float)):
        style["width"] = style["height"] * intrinsic_ratio if intrinsic_ratio else default_width
    elif (height_auto or width_auto) and intrinsic_ratio:
        # `width`/`height` isn't a plain pixel length on either side (most
        # commonly a percentage, e.g. `width:100%` on a responsive `<img>`)
        # -- its resolved pixel value isn't known until Taffy itself lays
        # out the box, so this function (which only ever runs once, before
        # Taffy sees the tree at all) can't precompute the scaled auto side
        # the way the plain-pixel branches above do. Taffy's own native
        # `aspect_ratio` support (added to its `Style` for exactly this)
        # picks it up after resolving whichever side has a real value, on
        # its own. Confirmed directly on bbc.com: every `<img
        # style="width:100%">` measured a real width and `height:auto` but
        # a literal `0` height -- nothing here had ever handled a
        # percentage width/height at all, so the image simply never
        # painted (zero-height box), despite genuinely finishing loading.
        style["aspect_ratio"] = intrinsic_ratio
    if isinstance(style["width"], (int, float)):
        # A resolved replaced-element width (intrinsic, ratio-derived, or
        # the 300px UA default) is never subject to flex-shrink -- it's
        # not a genuine flex item, just an atomic run inside whatever
        # flex-row this project's own inline-formatting approximation
        # wraps it in alongside surrounding text. Flexbox's plain default
        # `flex-shrink:1` would otherwise squeeze it down to fit that
        # row's available width instead of overflowing/wrapping as a
        # whole unit the way a real inline-replaced element does.
        # Confirmed directly on replaced-elements-min-width-40.html: six
        # SVGs with no complete intrinsic size (falling back to the
        # 300x150 UA default here) were squeezed down to 200px -- the
        # 200px-wide containing `<div>`'s own width, not theirs -- every
        # one of them, instead of overflowing it at their real 300px.
        style["flex_shrink"] = 0.0


def _stretched_replaced_flex_item(element, style: dict) -> bool:
    """Whether `element` is an in-flow item of a row flex container with a
    definite height whose effective `align-self` is `stretch`/`normal`
    (so its cross size is the container's, per Flexbox 9.4)."""
    if style.get("position") in ("absolute", "fixed") or not _is_flex_or_grid_item(element):
        return False
    parent = _layout_parent(element)
    parent_native = getattr(parent, "_chromonic_native_style", None) or {}
    parent_inset = parent_native.get("inset") or ("auto",) * 4
    parent_definite_height = (
        isinstance(parent_native.get("height"), (int, float))
        # An absolutely positioned container with `top` and `bottom` set
        # is as definite (flex-abspos-inset-nested-001.html).
        or (parent_native.get("position") == "absolute"
            and parent_inset[0] != "auto" and parent_inset[2] != "auto"))
    if (parent_native.get("display") != "flex"
            or not (parent_native.get("flex_direction") or "row").startswith("row")
            or not parent_definite_height):
        return False
    resolved = getattr(element, "_chromonic_resolved_style", None)
    align, _safe = _alignment_parts(getattr(resolved[0], "alignSelf", "auto") if resolved else "auto")
    if align == "auto":
        parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
        align, _safe = _alignment_parts(getattr(parent_resolved[0], "alignItems", "normal")
                                        if parent_resolved else "normal")
    return align in ("stretch", "normal")


def _resolve_replaced_percent_height(style: dict, element, height_attr: str) -> "float | None":
    """CSS 2.1 10.6.2: a replaced element's own percentage intrinsic
    height (an HTML `height="N%"` attribute, e.g. on `<svg>`/`<iframe>`)
    resolves against its containing block's height -- but only when that
    containing block's own height is itself definite (an explicit
    length, not `auto`); otherwise the percentage "is treated as '0'" (no
    intrinsic height at all, same as never having one), never resolved
    circularly against the containing block's own (still-undetermined)
    auto height."""
    containing_block = _find_containing_block_ancestor(element) if (
        style.get("position") in ("absolute", "fixed")) else getattr(element, "parentElement", None)
    cb_native = (getattr(containing_block, "_chromonic_native_style", None)
                 if containing_block is not None else None)
    cb_height = cb_native.get("height") if cb_native is not None else None
    if not isinstance(cb_height, (int, float)):
        return None
    try:
        return cb_height * float(height_attr[:-1]) / 100.0
    except ValueError:
        return None


def _apply_iframe_intrinsic_size(style: dict, element) -> None:
    """`<iframe>` is a replaced element with no intrinsic ratio -- CSS 2.1
    10.3.2/10.6.2's fallback for that case is a UA-defined default,
    300 x 150 in every real browser, same as `<canvas>`'s own default
    bitmap (an explicit `width`/`height` HTML attribute, or CSS size,
    still wins over it as always)."""
    # `style["width"]`/`["height"]` can already be the UA stylesheet's own
    # `300`/`150` default rather than literal `"auto"` (`ua_style.py` gives
    # every `<iframe>` an explicit fallback size, CSS 2.1 10.3.2/10.6.2's
    # own UA-defined default) -- an HTML `width`/`height` attribute is a
    # real, if legacy, presentational hint that should still win over that
    # UA default (though never over genuine author CSS, indistinguishable
    # here from that same UA default -- accepted as a rare edge case).
    if style["width"] in ("auto", 300.0):
        width_attr = (element.getAttribute("width") or "").strip()
        if width_attr and not width_attr.endswith("%"):
            try:
                style["width"] = float(width_attr)
            except ValueError:
                pass
        elif style["width"] == "auto":
            style["width"] = 300.0
    if style["height"] in ("auto", 150.0):
        height_attr = (element.getAttribute("height") or "").strip()
        if height_attr.endswith("%"):
            resolved = _resolve_replaced_percent_height(style, element, height_attr)
            if resolved is None and style.get("position") in ("absolute", "fixed"):
                # CSS 2.1 10.5: an absolutely positioned box's containing
                # block always has a resolvable height -- left as a
                # percentage for Taffy to resolve against it (absolute-
                # replaced-height-007.xht: `height="50%"` of a 0px-tall
                # relative div is 0, not the 150px default).
                try:
                    style["height"] = ("pct", float(height_attr[:-1]) / 100.0)
                    resolved = style["height"]
                except ValueError:
                    pass
            if not isinstance(resolved, tuple):
                style["height"] = resolved if resolved is not None else (
                    style["height"] if style["height"] != "auto" else 150.0)
        elif height_attr:
            try:
                style["height"] = float(height_attr)
            except ValueError:
                pass
        elif style["height"] == "auto":
            style["height"] = 150.0


def _apply_canvas_intrinsic_size(style: dict, element) -> None:
    """Canvas is a replaced element with a 300 x 150 default bitmap."""
    intrinsic_width = float(element.getAttribute("width") or 300)
    intrinsic_height = float(element.getAttribute("height") or 150)
    if (style["width"] == "auto" and style["height"] == "auto" and intrinsic_height
            and _stretched_replaced_flex_item(element, style)):
        # Stretched across a definite-height flex row (Flexbox 9.4 and
        # 9.2.3 rule C; flexbox-flex-basis-content-001a.html: a 20x150
        # canvas in a 50px row is as tall as the row and its width
        # follows the ratio) -- both left `auto` with the ratio for Taffy.
        style["aspect_ratio"] = intrinsic_width / intrinsic_height
        return
    if style["width"] == "auto":
        style["width"] = intrinsic_width
    if style["height"] == "auto":
        style["height"] = intrinsic_height


def _apply_svg_intrinsic_size(style: dict, element) -> None:
    """Treat an outer SVG viewport as one replaced element for HTML layout."""
    width_attr = (element.getAttribute("width") or "").strip()
    height_attr = (element.getAttribute("height") or "").strip()
    width = (_fontmetrics.parse_length(width_attr, default=None)
             if width_attr and not width_attr.endswith("%") else None)
    height = (_fontmetrics.parse_length(height_attr, default=None)
              if height_attr and not height_attr.endswith("%") else None)
    if height is None and height_attr.endswith("%"):
        height = _resolve_replaced_percent_height(style, element, height_attr)
    view_box = (element.getAttribute("viewBox") or "").replace(",", " ").split()
    ratio = None
    if len(view_box) == 4:
        try:
            view_width, view_height = float(view_box[2]), float(view_box[3])
            ratio = view_width / view_height if view_height else None
        except ValueError:
            pass
    if style["width"] == "auto":
        if width is not None:
            style["width"] = float(width)
        elif height is not None and ratio is not None:
            style["width"] = float(height * ratio)
    if style["height"] == "auto":
        if height is not None:
            style["height"] = float(height)
        elif width is not None and ratio is not None:
            style["height"] = float(width / ratio)
    # CSS 2.1 10.3.2/10.6.2: with no intrinsic width/height/ratio to fall
    # back on at all (no `width`/`height` attribute, no `viewBox`), a
    # replaced element still isn't sized like an ordinary block -- it
    # gets the same UA-defined 300 x 150 default `<iframe>`/`<canvas>`
    # already do, even where that means overflowing a narrower
    # containing block (confirmed directly against Chrome: a
    # `width:auto` SVG with only `height="50"` and no `viewBox` comes out
    # `300px` wide inside a `288px` container, not shrunk to fit it).
    if style["width"] == "auto":
        style["width"] = 300.0
    if style["height"] == "auto":
        style["height"] = 150.0


_INTRINSIC_WIDTH_KEYWORDS = ("min-content", "max-content", "fit-content")
_measuring_intrinsic_depth = 0


def _numeric_or_zero(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _resolve_intrinsic_width_keyword(element, computed, style_obj, computed_cache) -> "float | None":
    """CSS Sizing 3: `width: min-content | max-content | fit-content` (and
    the `-webkit-`/`-moz-` spellings) on a block-level box, resolved to
    a Taffy-usable content-box width before the box is built -- Taffy's
    `Dimension` has no intrinsic keywords (`style_bridge._len` dropped
    them to `auto`, so `inline-size: min-content` on `align-items-
    baseline-row-horz.html`'s flex container filled the whole body).
    `max-content` (and, approximated, `fit-content`) is the scratch-tree
    measurement `_measure_intrinsic_width` already does for table cells;
    `min-content` is the longest unbreakable token, or for a single-line
    flex row the sum of its items' own min-content margin boxes. `None`
    leaves the width alone. Re-entrancy from the scratch measurement is
    guarded so the measured copy lays out as plain `auto`."""
    global _measuring_intrinsic_depth
    if _measuring_intrinsic_depth:
        return None
    raw = (getattr(computed, "width", "") or "").strip().lower()
    for prefix in ("-webkit-", "-moz-"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    if raw not in _INTRINSIC_WIDTH_KEYWORDS:
        return None
    display = getattr(style_obj.display, "value", "")
    if display == "inline" or not _renders(style_obj):
        return None
    _measuring_intrinsic_depth += 1
    try:
        if raw == "min-content":
            content = _min_content_width(element, computed_cache)
            if content is None:
                return None
            border_box = None
        else:
            border_box = _measure_intrinsic_width(element, computed_cache)
            if border_box is None:
                return None
            content = None
    finally:
        _measuring_intrinsic_depth -= 1
    native = style_bridge.to_dict(style_obj)
    padding = native.get("padding") or (0.0,) * 4
    border = native.get("border") or (0.0,) * 4
    horizontal = (_numeric_or_zero(padding[1]) + _numeric_or_zero(padding[3])
                  + _numeric_or_zero(border[1]) + _numeric_or_zero(border[3]))
    if content is None:
        content = max(0.0, border_box - horizontal)
    return content + horizontal if native.get("box_sizing") == "border-box" else content


def _min_content_width(element, computed_cache) -> "float | None":
    computed, style_obj = _describe(element, computed_cache)
    direction = (getattr(computed, "flexDirection", "row") or "row").strip().lower()
    wrap = (getattr(computed, "flexWrap", "nowrap") or "nowrap").strip().lower()
    if (getattr(style_obj.display, "value", "") in _FLEX_DISPLAYS
            and direction in ("row", "row-reverse") and wrap == "nowrap"):
        total = 0.0
        for child in _child_nodes(element):
            if not _is_element(child):
                continue  # an anonymous text item: not measured here (rare in a sized row)
            child_computed, child_style = _describe(child, computed_cache)
            if not _renders(child_style) or _is_absolutely_positioned(child_style):
                continue
            native = style_bridge.to_dict(child_style)
            padding = native.get("padding") or (0.0,) * 4
            border = native.get("border") or (0.0,) * 4
            margin = native.get("margin") or (0.0,) * 4
            edges = (_numeric_or_zero(padding[1]) + _numeric_or_zero(padding[3])
                     + _numeric_or_zero(border[1]) + _numeric_or_zero(border[3]))
            width = native.get("width")
            if isinstance(width, (int, float)):
                outer = float(width) + (0.0 if native.get("box_sizing") == "border-box" else edges)
            else:
                outer = (_min_content_width(child, computed_cache) or 0.0) + edges
            total += outer + _numeric_or_zero(margin[1]) + _numeric_or_zero(margin[3])
        return total
    return _measure_min_content_width(element, computed_cache)


def _measure_intrinsic_width(element, computed_cache) -> "float | None":
    """The natural (max-content) width `element` would take with no line
    wrapping -- computed in a disposable Taffy tree so real layout does the
    measuring rather than a hand-rolled approximation. `None` on any
    failure; caller falls back to today's behaviour."""
    scratch = Tree()
    try:
        root_id = build(scratch, element, {}, computed_cache=computed_cache, reuse_styles=False)
        boxes = scratch.compute(root_id, None, None)
        box = boxes.get(root_id)
        return float(box[2]) if box is not None else None
    except Exception:
        return None


def _rendering_text_content(element) -> str:
    """`element.textContent`, but skipping any descendant subtree rooted at
    a `_NON_RENDERING_TAGS` tag -- plain `.textContent` includes their raw
    source text verbatim, which is never actually rendered.

    No `childNodes` at all (e.g. a `_PseudoElement`'s generated-content
    text) falls back to `element.textContent` directly."""
    child_nodes = getattr(element, "childNodes", None)
    if not child_nodes:
        return getattr(element, "textContent", None) or ""
    parts = []

    def walk(node):
        # A plain-string child (domonic's programmatic constructors never
        # wrap one in a Text node) has no `nodeType` at all, so it must be
        # checked for explicitly or it's silently dropped.
        if isinstance(node, str):
            if node:
                parts.append(node)
            return
        node_type = getattr(node, "nodeType", None)
        if node_type == TEXT_NODE:
            text = getattr(node, "textContent", None) or getattr(node, "data", "")
            if text:
                parts.append(text)
            return
        if node_type == ELEMENT_NODE:
            if (getattr(node, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
                return
            for child in _child_nodes(node):
                walk(child)

    for child in child_nodes:
        walk(child)
    return "".join(parts)


def _measure_min_content_width(element, computed_cache) -> "float | None":
    """The width of `element`'s own longest unbreakable token (its longest
    whitespace-separated word, measured in its own font) -- CSS 2.1
    17.5.2.2's real "minimum content width" for auto table-layout column
    sizing: the smallest a column can be made without literally breaking a
    word mid-token. Deliberately *not* `_measure_intrinsic_width`'s
    max-content (the width if the content never wrapped at all) -- that's
    the right "requirement" for a short, rarely-wrapping label cell, but
    wildly too wide a floor for a colspan'd cell holding a whole wrapping
    sentence or list. Found on `en.wikipedia.org`'s Python-article infobox:
    a colspan'd "Influenced by" cell listing dozens of comma-separated
    language names measured over 1200px unwrapped -- using that as the
    column's required width forced it absurdly wide instead of letting it
    wrap across several lines the way Chrome renders it.

    A plain per-token font-metrics measurement (not a real Taffy layout
    pass, unlike `_measure_intrinsic_width`) -- deliberately minimal, and
    good enough for ordinary prose/lists: it doesn't account for a nested
    element's own different font, only `element`'s own (that nested
    element's *content* still counts, via `_rendering_text_content`, just
    measured in the outer font)."""
    text = _rendering_text_content(element).strip()
    if not text:
        return None
    _describe(element, computed_cache)
    widest = 0.0

    def measure(owner, token: str) -> float:
        paint_style = owner._chromonic_paint_style
        font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
        family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
        weight = _parse_font_weight(paint_style["font_weight"])
        italic = fonts.is_italic(paint_style["font_style"])
        return layout_text(token, family, font_size, font_weight=weight, italic=italic)[0]

    # Only CSS white space separates tokens -- `str.split()` would also
    # break at U+00A0, which never is a break opportunity (caption-side-
    # 001.xht: a `Filler&nbsp;Text` caption is one 66.9px word, and the
    # table under it is that wide in Chrome). Each text node is measured
    # in its own element's font (table-margin-003.xht: a `font-size:
    # 0.9em` span's `_PASS!__` is the cell's widest word at 56.4px, not
    # the 62.6px it measures in the cell's own font); a word running
    # across an element boundary is read as two, which only ever
    # under-measures slightly.
    # A word runs on across text-node and element boundaries (the XHTML
    # parser hands `Filler&nbsp;Text` over as three text nodes; `<b>bo</b>ld`
    # is one word) and only ends at CSS white space or a `<br>`: its width
    # is the sum of its pieces, each measured in its own font.
    word: list = []

    def flush():
        nonlocal widest
        if word:
            widest = max(widest, sum(measure(owner, piece) for owner, piece in word))
            word.clear()

    def walk(node, owner):
        node_type = getattr(node, "nodeType", None)
        if node_type == TEXT_NODE:
            raw = getattr(node, "textContent", None) or getattr(node, "data", "") or ""
            for part in re.split(r"([ \t\n\r\f]+)", raw):
                if not part:
                    continue
                if part[0] in " \t\n\r\f":
                    flush()
                else:
                    word.append((owner, part))
            return
        if node_type != ELEMENT_NODE:
            return
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag in _NON_RENDERING_TAGS:
            return
        if tag == "br":
            flush()
            return
        try:
            _describe(node, computed_cache)
            child_owner = node if getattr(node, "_chromonic_paint_style", None) else owner
        except Exception:
            child_owner = owner
        for child in _child_nodes(node):
            walk(child, child_owner)

    child_nodes = getattr(element, "childNodes", None)
    if not child_nodes:
        for token in re.split(r"[ \t\n\r\f]+", text):
            if token:
                widest = max(widest, measure(element, token))
        return widest
    for child in child_nodes:
        walk(child, element)
    flush()
    return widest


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

    CSS 2.1 17.5.3: however many header/body/footer row-groups appear,
    and in whatever source order, they're always *displayed* in a fixed
    header-groups, then body-groups, then footer-groups order -- a
    `<tfoot>` authored first (a common pattern, so its totals row can
    reach the network before the body finishes loading) still renders
    last. A row with no row-group ancestor at all counts as an (implicit)
    body row, same as a `table-row-group`. Row order *within* each bucket,
    and row-group order within `header`/`footer` (multiple of either is
    non-conforming markup, but not fatal here), stays DOM order."""
    buckets: dict[str, list] = {"header": [], "body": [], "footer": []}
    # Only the *first* header group and the *first* footer group get their
    # special placement -- any further `thead`/`tfoot` (or `table-header-
    # group`/`table-footer-group` element) is laid out as an ordinary body
    # group in source order, as Chrome does. Confirmed on border-spacing-
    # applies-to-010.xht: two `display: table-footer-group` siblings
    # rendered in source order in Chrome, not both hoisted to the end.
    claimed: set = set()

    def walk(node, kind: str):
        for child in _normalized_child_nodes(node, computed_cache):
            if not _is_element(child):
                continue
            tag = (getattr(child, "tagName", "") or "").lower()
            if tag in _NON_RENDERING_TAGS:
                continue
            child_computed, child_style = _describe(child, computed_cache)
            if not _renders(child_style):
                continue
            if _is_absolutely_positioned(child_style):
                # CSS 2.1 9.7: an absolutely/fixed positioned element's
                # `display` blockifies regardless of its specified value
                # (`table-row`/`table-row-group`/etc. included) -- pulled
                # entirely out of the table's own row/row-group structure,
                # not even transparently walked into for nested rows the
                # way an ordinary non-table wrapper is below. Confirmed
                # directly on `top-applies-to-001.xht`: an absolutely
                # positioned `display:table-row-group` element was still
                # being placed as a real row-group here, landing it inside
                # the table's normal flow (y=178) instead of at its own
                # `top:0` offset (y=0).
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
    for child in _normalized_child_nodes(row_element, computed_cache):
        if not _is_element(child):
            continue
        tag = (getattr(child, "tagName", "") or "").lower()
        if tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(child, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style):
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
    groups = [id(_layout_parent(row)) for row in rows]
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

    for child in _child_nodes(table_element):
        if not _is_element(child):
            continue
        tag = (getattr(child, "tagName", "") or "").lower()
        child_computed, child_style = _describe(child, computed_cache)
        if _is_absolutely_positioned(child_style):
            continue  # CSS 2.1 9.7: blockified, no longer a column (top-applies-to-005.xht)
        display = (getattr(child_computed, "display", "") or "").strip().lower()
        if tag == "colgroup" or display == "table-column-group":
            cols = [
                node for node in _child_nodes(child)
                if _is_element(node) and (
                    (getattr(node, "tagName", "") or "").lower() == "col"
                    or (getattr(_describe(node, computed_cache)[0], "display", "") or "").strip().lower()
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
        computed = _describe(cell, computed_cache)[0]
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
        ancestor = _layout_parent(row)
        group = None
        while ancestor is not None and ancestor is not table_element:
            tag = (getattr(ancestor, "tagName", "") or "").lower()
            if _row_group_kind(tag, _describe(ancestor, computed_cache)[0]) is not None:
                group = ancestor
                break
            ancestor = _layout_parent(ancestor)
        groups.append(group)
    for r, row in enumerate(rows):
        computed = _describe(row, computed_cache)[0]
        for cc in range(column_count):
            horizontal[r][cc].append(_border_candidate(computed, "top", "row"))
            horizontal[r + 1][cc].append(_border_candidate(computed, "bottom", "row"))
        vertical[r][left_line(0, column_count)].append(_border_candidate(computed, "left", "row"))
        vertical[r][right_line(0, column_count)].append(_border_candidate(computed, "right", "row"))
        group = groups[r]
        if group is None:
            continue
        computed = _describe(group, computed_cache)[0]
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
            computed = _describe(column, computed_cache)[0]
            for rr in range(row_count):
                vertical[rr][left_line(c, c + 1)].append(_border_candidate(computed, "left", "column"))
                vertical[rr][right_line(c, c + 1)].append(_border_candidate(computed, "right", "column"))
            horizontal[0][c].append(_border_candidate(computed, "top", "column"))
            horizontal[row_count][c].append(_border_candidate(computed, "bottom", "column"))
        if group is not None:
            computed = _describe(group, computed_cache)[0]
            if c == 0 or columns[c - 1][1] is not group:
                # First column of this group: contribute the group's own
                # left/right borders once, at the physical edges of its
                # whole logical span.
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
    if _rendering_text_content(cell).strip():
        return True
    return bool(_child_elements(cell, computed_cache))


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
            width = _measure_intrinsic_width(cell, computed_cache)
            single_cells[id(cell)] = col_index
            if width is not None:
                per_column[col_index] = max(per_column.get(col_index, 0.0), width)
            # CSS 2.1 17.5.2.2's other half: a column can never be made
            # narrower than its widest cell's *minimum* content width (its
            # longest unbreakable word) plus that cell's own padding and
            # borders -- the floor a table sits on when its container is
            # too narrow (it overflows rather than squeezing cells below
            # it, confirmed on collapsing-border-model-005.xht: a 34px
            # min-content table in a 32px div is 34px wide in Chrome).
            # `_measure_intrinsic_width` just built this cell in a scratch
            # tree, so `_chromonic_native_style` carries its real resolved
            # padding/borders (collapsed halves included).
            native = getattr(cell, "_chromonic_native_style", None) or {}
            edges = list(native.get("padding") or (0.0,) * 4) + list(native.get("border") or (0.0,) * 4)
            horizontal = _numeric_edge(edges[1]) + _numeric_edge(edges[3]) + _numeric_edge(edges[5]) + _numeric_edge(edges[7])
            minimum = (_measure_min_content_width(cell, computed_cache) or 0.0) + horizontal
            if width is not None:
                # Never past the real (laid-out) max-content: the token
                # measure knows nothing of a child's negative margin
                # (table-height-algorithm-026.xht: a `margin-left: -10px`
                # div's one word is 320px, the cell's content 310).
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
        needed = _measure_min_content_width(cell, computed_cache)
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
    # A percentage (on a column or a first-row cell) resolves against the
    # space the columns actually share: the table's content width less
    # every inter-column gap (fixed-table-layout-017.xht: `40%` of a
    # 422px table with 12px of borders and 2px spacing over 4 columns is
    # 160px, i.e. 40% of 400).
    percentage_base = max(0.0, content_width - spacing_h * max(0, column_count - 1))
    for c, (column, _group) in enumerate(columns):
        # Only a `<col>`'s own width -- a column *group*'s is ignored in
        # fixed layout (fixed-table-layout-013.xht/-014.xht).
        if column is None:
            continue
        width = style_bridge._len(_describe(column, computed_cache)[1].width)
        if isinstance(width, (int, float)):
            widths[c] = float(width)
        elif isinstance(width, tuple) and width[0] == "pct":
            widths[c] = width[1] * percentage_base
    collapsed = getattr(table_element, "_chromonic_collapsed_cell_borders", None) or {}
    for cell, row_index, c, _rowspan, colspan in cells:
        if row_index != 0:
            continue
        computed, style_obj = _describe(cell, computed_cache)
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


def _apply_button_intrinsic_width(style: dict, element) -> None:
    if style["width"] != "auto":
        return
    text = _own_text(element)
    paint_style = element._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=13.3333)
    width, _height, _lines = layout_text(
        text, paint_style["font_family"], font_size,
        font_weight=_parse_font_weight(paint_style["font_weight"]),
        italic=fonts.is_italic(paint_style["font_style"]),
    )
    horizontal = sum(float(value) for value in (style["padding"][1], style["padding"][3],
                                                 style["border"][1], style["border"][3])
                     if not isinstance(value, tuple) and value != "auto")
    style["width"] = width + horizontal


# Fallback tag guess for a raw DOM tree built without `browser.load()`
# (skipping the UA stylesheet), where every tag's un-cascaded default is
# "inline" and tag-name is the only signal left.
_USUALLY_INLINE_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "small", "code", "label", "abbr",
    "cite", "mark", "sub", "sup", "time", "kbd", "samp", "var", "q", "u", "s",
    "button", "input", "select", "textarea",
    # Every real HTML UA stylesheet gives these `display:inline` by default
    # too, same as the form controls just above -- missing here left `img`/
    # `canvas`/`svg`/`iframe` untrusted by `_trusts_computed_inline`, so
    # `_is_inline_level` always said `False` for them regardless of their
    # own genuinely-inline computed style, which made `_inline_mixed_
    # content`'s `_child_qualifies` reject any container mixing one with
    # real text -- the whole container fell out of inline flow entirely
    # (`_make_inline_formatting_plan` *and* the flex-row fallback both
    # bail together, since neither ever got a chance to run at all),
    # putting each image on its own block-level line instead of flowing
    # with its surrounding text. Confirmed directly on `wpt/css/CSS2/
    # visudet/replaced-elements-width-40.html`: seven `<img>`s meant to
    # flow with comma-separated text between them each landed alone on
    # its own row.
    "img", "canvas", "svg", "svg:svg", "iframe",
})

# Replaced elements and form controls size themselves from authored
# `width`/`height` even at `display:inline` -- unlike an ordinary inline
# element, whose box is purely a function of its content.
_REPLACED_OR_CONTROL_TAGS = frozenset({
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select", "button", "iframe",
})

# CSS 2.1 17.4/CSS Tables 3: computed `display` keywords for the internal
# table boxes margin never applies to, regardless of what tag carries the
# value -- `display:table`/`inline-table` (the outer table box itself,
# where margin still applies normally) are deliberately not in this set.
_TABLE_INTERNAL_DISPLAYS = frozenset({
    "table-row-group", "table-header-group", "table-footer-group",
    "table-row", "table-cell", "table-column-group", "table-column",
})

# Tags with their own dedicated `build()` branch that must always run --
# see the `has_pseudo` check that uses this, right before `inline_items` is
# computed.
_NO_GENERATED_CONTENT_TAGS = frozenset({
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select",
})


def _ua_stylesheet_applied(element) -> bool:
    """Whether `ua_style.apply()` ran on `element`'s document -- cached per
    document (this is checked once per element, on the hot `build()` path)."""
    document = getattr(element, "ownerDocument", None)
    if document is None:
        return False
    cached = getattr(document, "_chromonic_ua_applied_cache", None)
    if cached is None:
        cached = document.querySelector("style[data-chromonic-ua]") is not None
        document._chromonic_ua_applied_cache = cached
    return cached


def _trusts_computed_inline(element, tag_name: str) -> bool:
    """Whether a computed `display: inline`/`inline-block` on `element`
    can be trusted as real author intent rather than domonic's un-cascaded
    default -- true for a tag assumed usually-inline, or one `ua_style.py`
    gives an explicit `block` default when that stylesheet actually ran."""
    if tag_name in _USUALLY_INLINE_TAGS:
        return True
    return tag_name in ua_style.BLOCK_DEFAULT_TAGS and _ua_stylesheet_applied(element)


def _is_inline_level(element, style_obj) -> bool:
    if isinstance(element, _AnonymousTableBox):
        # A synthetic box's display is authoritative -- an anonymous
        # `inline-table` generated inside an inline parent (CSS 2.1
        # 17.2.1) flows with that parent's text.
        return element.kind == "inline-table"
    display = style_obj.display
    value = getattr(display, "value", display)
    if isinstance(value, str):
        match = style_bridge._SIMPLE_VAR_FALLBACK.match(value.strip())
        if match:
            value = match.group(1).strip()
    if value in ("inline-flex", "inline-grid", "-webkit-inline-flex"):
        # An atomic inline-level flex/grid container (flex-inline.html:
        # `display: inline-flex` sat in its line as a block-level box,
        # 784px wide). No tag gate: no UA default ever computes to these,
        # so the value is unambiguous author intent.
        return True
    if value not in ("inline", "inline-block", "inline-table"):
        return False
    tag_name = (getattr(element, "tagName", "") or "").lower()
    return _trusts_computed_inline(element, tag_name)


def _is_floated(child_computed) -> bool:
    """Whether an author explicitly gave this element `float: left`/`right`
    -- unlike `display`, `float`'s initial value is always `none` regardless
    of tag, so any non-`none` value is unambiguous author intent, no tag
    gate needed (contrast `_is_inline_level`)."""
    float_value = getattr(child_computed, "float", None)
    return isinstance(float_value, str) and float_value.strip().lower() in ("left", "right")


def _wants_horizontal_flow(element, computed, style_obj) -> bool:
    return _is_inline_level(element, style_obj) or _is_floated(computed)


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
    computed as `inline`/`inline-block` (`_is_inline_level`), or an
    explicit `float: left`/`right` (`_is_floated`) -- the *other* real
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
    please</p>` -- already out of scope, see `_own_text`'s own docstring),
    does not merge adjacent text runs, does not clear floats or let
    non-floated content flow around them the way real float layout would,
    and does not implement inline-level *text* wrapping around floated/
    inline boxes -- only whole elements wrapping onto new rows, via
    ordinary flex-wrap."""
    element.__dict__.pop("_chromonic_float_flow_children", None)
    element.__dict__.pop("_chromonic_float_flow_qualifies", None)
    for child in child_elements:
        child.__dict__.pop("_chromonic_force_full_row_width", None)
        child.__dict__.pop("_chromonic_float_no_shrink", None)
    if style["display"] != "block":
        return  # already flex/grid/none -- a real, explicit layout mode wins, no guessing over it
    if len(child_elements) < 2 and not any(_is_floated(cc) for cc in child_computeds):
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
    if not any(_is_floated(cc) for cc in child_computeds) and sum(qualifies) < len(child_elements) * 0.8:
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
    for child, ok, child_computed in zip(child_elements, qualifies, child_computeds):
        if not ok:
            child._chromonic_force_full_row_width = True
        elif _is_floated(child_computed):
            # A real float with an explicit (non-`auto`) width is never
            # shrink-to-fit -- CSS 2.1 10.3.5 uses the specified width
            # outright, and it's free to overflow past its containing
            # block rather than shrink to stay inside it (the same "no
            # min-width:auto floor" fact `_chromonic_force_full_row_width`
            # already relies on, but the opposite problem: here it's
            # `flex_shrink`, not a min-width floor, doing the unwanted
            # shrinking -- flex's plain default `flex-shrink:1` lets this
            # row-packed item give up space to fit the flex line, which an
            # explicit-width float must never do). Confirmed directly on
            # floats-rule3-outside-right-001.xht: a lone `float:right`
            # child with `width:425px` inside a 400px-wide flex-wrap
            # container was shrunk to fit at 400px instead of staying
            # 425px and overflowing past the container's own left edge.
            child._chromonic_float_no_shrink = True
    inline_tag_qualifies = any(
        _is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
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


def _is_absolutely_positioned(style_obj) -> bool:
    position = style_obj.position
    return getattr(position, "value", position) in ("absolute", "fixed")


def _establishes_bfc(computed) -> bool:
    """CSS 2.1 9.4.1: whether this box establishes its own new block
    formatting context -- float/absolute/fixed positioning/`flow-root`/
    `inline-block`/table-cell/table-caption, or any non-`visible`
    overflow. Plain `overflow:visible` must NOT establish one -- only a
    real BFC box avoids a float; an ordinary block's border box may
    extend behind one."""
    if computed is None:
        return False
    display = (getattr(computed, "display", "") or "").strip().lower()
    if display in (
        "flow-root", "inline-block", "table-cell", "table-caption",
        "flex", "inline-flex", "grid", "inline-grid", "table", "inline-table",
    ):
        return True
    float_value = (getattr(computed, "float", None) or "none").strip().lower()
    if float_value != "none":
        return True
    position = (getattr(computed, "position", None) or "static").strip().lower()
    if position in ("absolute", "fixed"):
        return True
    overflow_x = (getattr(computed, "overflowX", "visible") or "visible").strip().lower()
    overflow_y = (getattr(computed, "overflowY", "visible") or "visible").strip().lower()
    return overflow_x != "visible" or overflow_y != "visible"


def _cleared_y(computed, active_floats, current_y: float) -> float:
    """The minimum y `computed`'s own `clear` property requires, given
    `active_floats` (`_fix_float_flow_after_block_sibling`'s own running
    list of `{side, edge, top, bottom}` for every float already packed in
    this same container) -- CSS 2.1 9.5.2: a cleared box's top border edge
    must be at or below the bottom outer edge of every earlier float, on
    the cleared side(s), still in this block formatting context. Applies
    to a clearing float exactly as much as a clearing ordinary block (CSS
    2.1 9.5.2 doesn't exempt one), so both of `_fix_float_flow_after_
    block_sibling`'s packing branches call this. Returns `current_y`
    unchanged when there's nothing to clear -- no `clear`, or no float on
    the relevant side yet."""
    if computed is None:
        return current_y
    clear_value = (getattr(computed, "clear", None) or "none").strip().lower()
    if clear_value not in ("left", "right", "both"):
        return current_y
    required = current_y
    for active in active_floats:
        if clear_value == "both" or active["side"] == clear_value:
            required = max(required, active["bottom"])
    return required


def _establishes_containing_block(style_obj) -> bool:
    """Whether `position` makes this element a valid containing block for
    `position:absolute`/`fixed` descendants -- anything but `static`."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"


def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created. `computed`/`style_obj`, if given, are `element`'s already-
    computed style (the caller's `_child_elements` call already needed
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
        computed, style_obj = _describe(element, computed_cache, reuse_styles=reuse_styles)
    style = getattr(element, "_chromonic_native_style", None) if reuse_styles else None
    if style is None:
        # Measured *before* this element's own style is published: the
        # scratch-tree measurement re-runs `build()` on this very element
        # and overwrites its per-pass attributes, which the real pass
        # below then rewrites anyway.
        intrinsic_width = _resolve_intrinsic_width_keyword(element, computed, style_obj, computed_cache)
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
                row, col = _parse_grid_area(area)
                if row != (None, None):
                    style["grid_row"] = row
                if col != (None, None):
                    style["grid_column"] = col
        element._chromonic_native_style = style
    if False and is_grid_item and style["min_width"] == "auto":
        # Disabled: this predates `_is_flex_or_grid_item`'s exclusion of
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
            and _is_flex_or_grid_item(element)):
        # CSS Flexbox 7.2.3 `flex-basis: content`: the base size is the
        # item's content size, whatever its main-axis `width`/`height`
        # says (flexbox-flex-basis-content-001a.html: `width: 0px` items
        # still size to their text). Taffy has no `content` keyword; the
        # main-axis size is cleared so its `auto` basis measures content.
        parent_native = (_layout_parent(element).__dict__.get("_chromonic_native_style") or {})
        if parent_native.get("display") == "flex":
            main = "height" if (parent_native.get("flex_direction") or "row").startswith("column") else "width"
            style["flex_basis"] = "auto"
            style[main] = "auto"
    if isinstance(style.get("flex_basis"), tuple) and _is_flex_or_grid_item(element):
        # CSS Flexbox 9.2.3 B: a percentage `flex-basis` against an
        # *indefinite* main size (a column container with `height: auto`)
        # is treated as `content`, and the item's own `height` is then
        # ignored for its base size (flex-basis-010.html: `flex: 0 0 0%;
        # height: 500px` holding a 100px child is 100px tall). Taffy
        # resolves the percentage against nothing and falls back to the
        # `height` instead.
        parent_native = (_layout_parent(element).__dict__.get("_chromonic_native_style") or {})
        if (parent_native.get("display") == "flex"
                and (parent_native.get("flex_direction") or "row").startswith("column")
                and parent_native.get("height") == "auto"):
            style["flex_basis"] = "auto"
            style["height"] = "auto"
    own_escapees = [] if is_containing_block else escapees
    tag_name = (getattr(element, "tagName", "") or "").lower()
    element._chromonic_tag_name = tag_name
    is_genuinely_inline = (
        tag_name not in _REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and _trusts_computed_inline(element, tag_name)
        # CSS Flexbox 4 / Grid 6.1: a flex/grid item's `display` is
        # blockified -- an inline `<span>` item keeps its width/height/
        # vertical margins like any block.
        and not _is_flex_or_grid_item(element)
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
        "" if _is_absolutely_positioned(style_obj) else getattr(style_obj.display, "value", "")
    )
    if table_internal_display in _TABLE_INTERNAL_DISPLAYS:
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
            and _trusts_computed_inline(element, tag_name))
            or getattr(style_obj.display, "value", "") in ("inline-flex", "inline-grid")):
        # `inline-block` establishes its own BFC (CSS 2.1 9.2.1), so an
        # in-flow child's margin must not collapse through it -- signalled
        # to Taffy the same way as `overflow`, via `Contain::PAINT`.
        style["establishes_bfc"] = True
    is_table_root = tag_name == "table" or _is_table_root_display(computed)
    is_table_row = not _is_absolutely_positioned(style_obj) and (
        tag_name == "tr" or _is_table_row_display(computed))
    is_table_cell = not _is_absolutely_positioned(style_obj) and (
        tag_name in ("td", "th") or _is_table_cell_display(computed))
    if is_table_root:
        element._chromonic_is_table_root = True
        element.__dict__.pop("_chromonic_table_growth_propagated", None)
        # The previous pass's column resolution must not leak into this
        # one: `_compute_table_column_widths` measures every cell in a
        # scratch tree, and the cell branch (`_layout_parent` reaches this
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
        # every cell's grid position (`_table_grid` -- rowspan/colspan
        # occupancy included).
        rows = _table_rows(element, computed_cache)
        cells, column_count = _table_grid(rows, computed_cache)
        # Declared columns past the cells' last one still exist -- a table
        # of nothing but a `width: 5em` column is 80px wide in Chrome
        # (table-column-rendering-001.xht).
        column_count = max(column_count, len(_table_columns(element, computed_cache, None)))
        element._chromonic_table_rows = rows
        element._chromonic_table_grid_cells = cells
        element._chromonic_table_columns = _table_columns(element, computed_cache, column_count)
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
                    visibility = (getattr(_describe(owner, computed_cache)[0], "visibility", "") or "")
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
        element._chromonic_table_rtl = _element_direction(element, computed) == "rtl"
        row_cells: dict = {}
        content_rows: set = set()
        for cell, row_index, _col, rowspan, _colspan in cells:
            row_cells.setdefault(id(rows[row_index]), []).append(cell)
            # A row a content-bearing cell spans down into counts as
            # having content too: table-height-algorithm-018.xht's
            # `height: 200px` table splits its surplus equally between
            # its two rows although the second row's only own cell is
            # empty -- the `rowspan=2` "Filler Text" cell covers it.
            if _table_cell_has_content(cell, computed_cache):
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
                    visibility = (getattr(_describe(node, computed_cache)[0], "visibility", "") or "")
                except Exception:
                    visibility = ""
                if visibility.strip().lower() == "collapse":
                    collapsed = True
                    break
                node = _layout_parent(node)
            row._chromonic_row_collapsed = collapsed
        if element._chromonic_border_collapse:
            # CSS 2.1 17.6.2: a collapsed border straddles the grid edge,
            # so each box either side of it only ever includes *half* the
            # winning width -- the table's own box included, which is
            # why "the width of the table includes half the table border".
            # `_resolve_collapsed_table_borders` runs the real 17.6.2.1
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
            cell_borders, perimeter = _resolve_collapsed_table_borders(
                element, computed, rows, cells, column_count, computed_cache,
                rtl=element._chromonic_table_rtl)
            element._chromonic_collapsed_cell_borders = cell_borders
            own = [_numeric_edge(value) for value in style["border"]]
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
        # column cell agrees. See `_compute_table_column_widths`. Applies
        # equally to a literal `<table>` and any `display:table`/`inline-
        # table` arbitrary element -- `_table_rows`/`_row_cells` (which
        # this calls) recognise a table-row/-cell by computed `display`
        # too, not just tag name.
        # A `table-layout: fixed` table with `width: auto` uses the auto
        # algorithm (CSS 2.1 17.5.2.1 only defines fixed layout for a
        # non-auto width; Chrome does the same): empty-cells-applies-to-
        # 014.xht's `width: 1em` cell still takes its column's 57.78px.
        column_widths = (_compute_table_column_widths(cells, computed_cache)
                         if computed.tableLayout != "fixed" or style["width"] == "auto"
                         else {"cells": {}, "cells_min": {}, "columns": [], "columns_min": []})
        # CSS 2.1 17.5.2.2: a column element's `width` is that column's
        # minimum width (column-width-001.xht: a `width: 1in` column over
        # a `width: 0.5in` cell makes a 96px column, cell and table).
        columns_list, columns_min_list = column_widths["columns"], column_widths["columns_min"]
        # Auto layout only: fixed layout resolves column elements itself
        # (`_compute_fixed_column_widths`, or `_enforce_fixed_column_boxes`
        # for a percentage-width table, which must find these lists empty
        # -- fixed-table-layout-023.xht).
        auto_layout = computed.tableLayout != "fixed" or style["width"] == "auto"
        for c, (column, group) in enumerate(element._chromonic_table_columns if auto_layout else ()):
            specified = None
            for owner in (column, group):
                if owner is None:
                    continue
                try:
                    value = style_bridge._len(_describe(owner, computed_cache)[1].width)
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
            padding = [_numeric_edge(v) for v in style["padding"]]
            border = [_numeric_edge(v) for v in style["border"]]
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
                    from . import domonic_ex_unit_patch
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
            # CSS 2.1 17.5.2.1 -- see `_compute_fixed_column_widths`. Needs
            # the table's real content width, so only a definite pixel
            # `width` gets the exact algorithm here; a percentage-width
            # fixed table keeps the flex approximation (specified cells
            # rigid, the rest sharing the remainder equally -- see the
            # cell branch). `columns_min` stays empty: a fixed layout has
            # no min-content floor, content simply overflows.
            spacing_h = element._chromonic_border_spacing[0]
            horizontal = sum(_numeric_edge(v) for v in style["padding"][1::2]) + sum(
                _numeric_edge(v) for v in style["border"][1::2])
            content_width = style["width"] - (horizontal if style["box_sizing"] == "border-box" else 0.0)
            fixed = _compute_fixed_column_widths(
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
        edges = [_numeric_edge(v) for v in style["padding"]] + [_numeric_edge(v) for v in style["border"]]
        horizontal = edges[1] + edges[3] + edges[5] + edges[7]
        outer = horizontal if style["box_sizing"] == "border-box" else 0.0
        element._chromonic_table_max_content_width = (sum(columns_max) + gaps + horizontal) if columns_max else None
        floor = sum(columns_min) + gaps + outer
        if columns_min and floor > 0.0 and style["min_width"] in ("auto", 0.0):
            style["min_width"] = floor
        elif columns_min and isinstance(style["min_width"], (int, float)):
            style["min_width"] = max(style["min_width"], floor)
    if is_table_row or (not _is_absolutely_positioned(style_obj)
                        and _row_group_kind(tag_name, computed) is not None):
        # CSS 2.1 17.6.1: in the separated border model rows, row groups,
        # columns and column groups "cannot have borders" -- a `tr {
        # border: ... }` is simply ignored; in the collapsing model their
        # borders do count, but only as contenders for the shared grid
        # lines (`_resolve_collapsed_table_borders`, folded into the
        # cells' own halves), never as a box border of their own. Either
        # way the row/row-group box itself carries none.
        style["border"] = [0.0, 0.0, 0.0, 0.0]
        # CSS 2.1 17.4: nor a margin (`_TABLE_INTERNAL_DISPLAYS` above only
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
        row_table = _layout_parent(element)
        while row_table is not None and not getattr(row_table, "_chromonic_is_table_root", False):
            row_table = _layout_parent(row_table)
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
        ancestor = _layout_parent(element)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = _layout_parent(ancestor)
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
        # `<td>` computes `inline`, so `_TABLE_INTERNAL_DISPLAYS` above
        # missed it -- `td { margin: 50px }` in table-visual-layout-002.xht).
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        ancestor = _layout_parent(element)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = _layout_parent(ancestor)
        if ancestor is not None and getattr(ancestor, "_chromonic_border_collapse", False):
            # CSS 2.1 17.6.2: this cell's box includes half of each of
            # its four collapsed grid lines' *winning* widths -- resolved
            # once for the whole table (`_resolve_collapsed_table_borders`,
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
                    collapse = _collapse_amounts(span, hit, uncollapsed, spacing_h)
        element._chromonic_cell_collapse = collapse
        if column_width is not None or style["width"] == "auto":
            if column_width is not None:
                # CSS 2.1 17.5.2.2: a cell's own `width` is only a *minimum*
                # for its column -- the cell's box is always the column's
                # width, which another cell in the column (wider content,
                # or the same content plus wider collapsed borders) can
                # push past it. `_compute_table_column_widths` measured
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
                    basis = (column_width - sum(_numeric_edge(v) for v in horizontal)
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
                    column_min = (column_min - sum(_numeric_edge(v) for v in horizontal_edges)
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
    is_table_caption = not _is_absolutely_positioned(style_obj) and (
        tag_name == "caption"
        or (getattr(computed, "display", "") or "").strip().lower() == "table-caption")
    parent = _layout_parent(element)
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
        bt, br, bb, bl = (_numeric_edge(v) for v in parent_style.get("border", (0.0,) * 4))
        pt, pr, pb, pl = (_numeric_edge(v) for v in parent_style.get("padding", (0.0,) * 4))
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
    if getattr(element, "_chromonic_float_no_shrink", False):
        # Set by `_approximate_inline_flow` for a real float with an
        # explicit width -- flexbox's plain default `flex-shrink:1` would
        # otherwise let this row-packed item give up its specified width
        # to fit the flex line, which CSS 2.1 10.3.5 never does for a
        # float (it overflows past its containing block instead).
        style["flex_shrink"] = 0.0
    # `<select>`'s `<option>`s and `<iframe>`'s light-DOM children are never
    # real layout content -- treated as childless regardless of markup.
    children = [] if tag_name in ("select", "svg", "svg:svg", "iframe") else _child_elements(
        element, computed_cache, reuse_styles=reuse_styles
    )
    if style["display"] in ("flex", "grid") and len(children) > 1:
        # CSS Flexbox 5.4 / Grid: `order` reorders the items (stable, so
        # equal orders keep DOM order) -- Taffy lays children out in the
        # order given, so the reordering happens here (flex-order.html;
        # `flexbox-anonymous-items-001.html`'s anonymous items are 0).
        # Absolutely positioned children are handed on unsorted after the
        # in-flow ones; their static position doesn't follow `order`.
        if any(_css_order(child_computed) for _child, child_computed, _style in children):
            children = sorted(children, key=lambda entry: _css_order(entry[1]))
    if is_table_root and children:
        # CSS 2.1 17.5.3: row-groups always *display* in header/body/
        # footer order regardless of source order (a `<tfoot>` authored
        # first, so its totals reach the network before the body
        # finishes loading, is common markup) -- `_table_rows` already
        # reorders for column-width *measurement*; this reorders the
        # table's own direct Taffy children the same way so the visual
        # stacking (built from ordinary DOM-order block-child handling,
        # same as any other element) matches. A non-row-group direct
        # child (a bare `<tr>`, or anything else) counts as an implicit
        # body row/group, same as `_table_rows`'s own default.
        # CSS 2.1 17.4: a `<caption>`/`display:table-caption` sits outside
        # the row groups entirely, above them (`caption-side: top`, the
        # default) or below every one of them (`bottom`) -- never sorted
        # among the body groups the way a bare `<tr>` is.
        captions_top, captions_bottom, header, body, footer = [], [], [], [], []
        for entry in children:
            child_tag = (getattr(entry[0], "tagName", "") or "").lower()
            child_display = (getattr(entry[1], "display", "") or "").strip().lower()
            if _is_absolutely_positioned(entry[2]):
                # CSS 2.1 9.7: blockified and out of flow -- neither a
                # caption nor a row group, and nothing for the table to
                # size around (top-applies-to-015.xht).
                body.append(entry)
                continue
            if child_tag == "caption" or child_display == "table-caption":
                side = (getattr(entry[1], "captionSide", "") or "top").strip().lower()
                (captions_bottom if side == "bottom" else captions_top).append(entry)
                continue
            kind = _row_group_kind(child_tag, entry[1]) or "body"
            # Same first-header/first-footer-only rule as `_table_rows`.
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
                caption_min = max(caption_min, _measure_min_content_width(caption, computed_cache) or 0.0)
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
        _apply_button_intrinsic_width(style, element)
    # Replaced/control elements always run their own dedicated branch below
    # -- CSS generated content doesn't apply to them, so a stray
    # `::before`/`::after` rule must not divert them into inline formatting.
    has_pseudo = tag_name not in _NO_GENERATED_CONTENT_TAGS and (
        getattr(element, "_chromonic_before_pseudo", None) is not None
        or getattr(element, "_chromonic_after_pseudo", None) is not None
    )
    # A table, row group or row never formats inline content of its own:
    # CSS 2.1 17.2.1 wraps any loose text/inline child in an anonymous
    # cell first (`_normalized_child_nodes`), which is where that content
    # is then laid out.
    is_table_container = is_table_root or is_table_row or (
        not _is_absolutely_positioned(style_obj) and _row_group_kind(tag_name, computed) is not None)
    # CSS Flexbox 4 / Grid 6.1: every in-flow child of a flex or grid
    # container is a (blockified) flex/grid item, and whitespace-only text
    # is dropped -- the container never formats inline content of its own
    # (real text got its anonymous item from `_wrap_inline_runs`). A
    # container of `<span>`/inline-block children (`flex-direction-
    # column.html`, and every real-site nav bar) previously fell into the
    # inline-formatting path here and laid them out as one text line.
    is_flex_or_grid_container = style["display"] in ("flex", "grid")
    inline_items = (_inline_mixed_content(element, children, element_is_inline=is_genuinely_inline)
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
    split_pieces = (_split_inline_flow_around_blocks(
                         element, inline_items, style, css_display_value, computed_cache)
                     if inline_items else None)
    inline_plan = (_make_inline_formatting_plan(element, inline_items, style, css_display_value, computed_cache)
                   if inline_items and split_pieces is None else None)

    if split_pieces is not None:
        # CSS 2.1 9.2.1.1: an inline element split around an in-flow block
        # child -- see `_split_inline_flow_around_blocks`. Each piece
        # becomes its own ordinary block-flow child of `element` (never a
        # flex row): a "plan" piece is one measured text leaf (same
        # machinery as the single-inline-plan case below, just built once
        # per piece instead of once for the whole element), a "block" piece
        # is that child's own real, recursively-built subtree.
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if (style["width"] == "auto" and element._chromonic_tag_name != "body"
                and not _is_flex_or_grid_item(element)):
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
                    owner = owner_cache[plan_index] = _AnonymousInlineRun(None, element)
                plan_index += 1
                plan_style = _inline_text_style(style)
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
                        escapee_is_cb = _establishes_containing_block(run["style"])
                        escapee_id = build(
                            tree, escapee_child, node_map, computed=run["computed"], style_obj=run["style"],
                            computed_cache=computed_cache, is_containing_block=escapee_is_cb,
                            escapees=escapees if not is_containing_block else own_escapees,
                            reuse_styles=reuse_styles, projection=projection,
                        )
                        (own_escapees if is_containing_block else escapees).append(escapee_id)
            else:
                child, child_computed, child_style = payload
                child_is_cb = _establishes_containing_block(child_style)
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
        if css_display_value == "block" and style["width"] == "auto" and not _is_flex_or_grid_item(element):
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
                    is_containing_block=_establishes_containing_block(run["style"]),
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
        if _element_direction(element, computed) == "rtl":
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
        weight = _parse_font_weight(paint_style["font_weight"])
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
                # `_inline_mixed_content` regardless of its real display,
                # so it can land in this flex-row fallback too -- must
                # still escape to its real containing-block ancestor when
                # `element` (its literal DOM parent) isn't one, same as
                # the `elif children:` branch below already does.
                child_is_cb = _establishes_containing_block(child_style)
                if _is_absolutely_positioned(child_style) and not is_containing_block:
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
                            spacer = spacers[id(item)] = _InlineSpacer(item)
                        spacer_style = _inline_text_style(style)
                        spacer_style.update({"width": space_width, "height": 0.0, "flex_shrink": 0.0})
                        spacer_id = (projection.upsert(spacer, spacer_style, [], None, None)
                                     if projection else tree.new_leaf(spacer_style))
                        node_map[spacer_id] = spacer
                        normal_child_ids.append(spacer_id)
                    normal_child_ids.append(build(
                        tree, item, node_map, computed=child_computed, style_obj=child_style,
                        computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                        reuse_styles=reuse_styles, projection=projection,
                    ))
                    if _is_floated(child_computed):
                        # Never a baseline participant (CSS 2.1 10.8.1
                        # baseline alignment only ever considers in-flow
                        # boxes) -- `_fix_inline_float_position` positions
                        # it afterward.
                        inline_floats.append(item)
                    else:
                        row_members.append(item)
                if isinstance(item, _PseudoElement):
                    # Not a real DOM child -- reaches paint only via this
                    # side-channel list, same as retained text fragments.
                    fragments.append(item)
                continue
            fragment_style = _inline_text_style(style)
            raw = getattr(getattr(item, "source", None), "textContent", "") or ""
            leading = space_width if (raw[:1].isspace() or
                                      getattr(item, "_chromonic_leading_collapsed_space", False)) else 0.0
            has_later_in_flow_item = any(
                later_kind == "text" or not _is_absolutely_positioned(later_style)
                for later_kind, _later_item, _later_text, _later_computed, later_style
                in inline_items[item_index + 1:]
            )
            trailing = space_width if raw[-1:].isspace() and has_later_in_flow_item else 0.0
            fragment_style["margin"] = [0.0, trailing, 0.0, leading]
            item._chromonic_native_style = fragment_style
            item._chromonic_paint_style = element._chromonic_paint_style
            measure_key = _measure_key(item._chromonic_paint_style, text)
            measure = (_make_measure(item._chromonic_paint_style, text, item)
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
            child_is_cb = _establishes_containing_block(child_style)
            if _is_absolutely_positioned(child_style) and not is_containing_block:
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
            normal_child_ids = _row_child_ids_with_rowspan_placeholders(
                tree, element, normal_entries, style, node_map, projection)
        elif normal_entries:
            normal_child_ids = _group_inline_element_runs(
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
        # machinery at all (`_inline_mixed_content`'s own gate correctly
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
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
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
            _apply_image_intrinsic_size(style, element)
        elif tag_name == "canvas":
            _apply_canvas_intrinsic_size(style, element)
        elif tag_name == "iframe":
            _apply_iframe_intrinsic_size(style, element)
        else:
            _apply_svg_intrinsic_size(style, element)
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
        text = _select_display_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
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
        text = _form_control_display_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
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
        text = _own_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
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


class LayoutProjection:
    """A retained Taffy projection of an authoritative live Domonic tree.

    Domonic remains the source of structure, styles and text. Each reconcile
    walks that tree, reuses native nodes by element identity, and only mutates
    native style, child or measure state whose snapshot changed.
    """

    def __init__(self):
        self.tree = Tree()
        self.nodes = {}
        self.state = {}
        self.node_map = {}
        self._seen = set()

    def begin(self):
        self._seen.clear()
        # Deliberately *not* `self.node_map = {}` -- `node_map` is this
        # projection's only strong Python reference to each tracked
        # element (`self.nodes`/`self.state` key by `id(element)`, a bare
        # memory address). Clearing it here, before `build()` walks the
        # new tree and before `finish()` prunes the Taffy side, would drop
        # that reference for every element that's about to turn out stale
        # this same pass -- and since domonic's DOM elements hold
        # parent/child back-references (a reference cycle), losing the
        # last *strong* ref doesn't free one immediately; it just becomes
        # eligible for Python's cyclic GC, which can run at any
        # allocation-heavy moment, including mid-`build()` while this same
        # pass is allocating a large new tree (a full page navigation, a
        # big DOM). If a brand-new element's address then lands on a
        # just-collected stale element's address, `upsert()`'s `self.nodes
        # .get(id(element))` aliases onto the stale entry, corrupting the
        # bookkeeping until `finish()` later removes an already-invalid or
        # misattributed Taffy node -- a real, reproduced "invalid SlotMap
        # key used" panic. Leaving old entries in place here keeps every
        # still-tracked element referenced (and thus its address
        # unreusable) right up until `finish()` explicitly removes it --
        # see `finish()`'s own `self.node_map.pop(node, None)`.

    def measure_changed(self, element, measure_key):
        previous = self.state.get(id(element))
        return previous is None or previous[2] != measure_key

    def upsert(self, element, style, children, measure, measure_key):
        key = id(element)
        self._seen.add(key)
        children = tuple(children)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        if node is None:
            if children:
                node = self.tree.new_with_children(style, list(children))
            elif measure is not None:
                node = self.tree.new_text_leaf(style, measure)
            else:
                node = self.tree.new_leaf(style)
            self.nodes[key] = node
        else:
            old_style, old_children, old_measure_key = previous
            if old_style != style:
                self.tree.set_style(node, style)
            if old_children != children:
                self.tree.set_children(node, list(children))
            if old_measure_key != measure_key:
                self.tree.set_measure(node, measure)
        # Keep an independent value snapshot. Image intrinsic sizing
        # mutates its cached dictionary in place; retaining that object would
        # make the next dirty comparison miss the change.
        self.state[key] = (_snapshot_style(style), children, measure_key)
        self.node_map[node] = element
        return node

    def finish(self):
        stale = set(self.nodes) - self._seen
        for key in stale:
            node = self.nodes.pop(key)
            try:
                self.tree.remove(node)
            except BaseException as error:
                if not is_rust_panic(error):
                    raise
                # Some path still not fully understood leaves `node`
                # already invalid in the Rust tree by the time this runs
                # (the `begin()`/`finish()` fix for the GC-timing
                # id(element) reuse race this class is otherwise exposed
                # to -- see `begin()` -- closes one way to reach this,
                # evidently not the only one). Whatever the exact trigger,
                # the *intent* of this call is just "make sure Taffy
                # doesn't still have this node" -- an already-invalid key
                # means that's already true, so this is safe to treat as a
                # no-op rather than letting one stale bookkeeping entry
                # take the entire browser process down; every Python-side
                # structure below is still cleaned up either way. Logged
                # so a recurrence leaves a trail toward whatever the
                # remaining cause turns out to be.
                _log.exception(
                    "chromonic: LayoutProjection.finish() could not remove "
                    "an already-stale Taffy node (id=%r, tag=%r) -- treating "
                    "it as already gone",
                    key, getattr(self.node_map.get(node), "_chromonic_tag_name", None),
                )
            self.state.pop(key, None)
            # Drops this stale element's last strong reference -- see
            # `begin()` for why that must not happen any earlier than
            # this, right alongside the matching Taffy-side removal above.
            self.node_map.pop(node, None)

    def patch_style(self, element, **changes):
        """Apply known layout-field changes after their Domonic mutation.

        This is an explicit incremental bridge for callers that know exactly
        which translated Taffy fields their authoritative DOM write changed.
        Unknown CSS mutations must use ``layout()`` to reconcile normally.
        """
        key = id(element)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        style = getattr(element, "_chromonic_native_style", None)
        if node is None or previous is None or style is None:
            raise KeyError("element is not present in this layout projection")
        style = dict(style)
        style.update(changes)
        element._chromonic_native_style = style
        self.tree.set_style(node, style)
        _old_style, children, measure_key = previous
        self.state[key] = (_snapshot_style(style), children, measure_key)

    def patch_insets(self, updates):
        """Batch known ``(element, top, right, bottom, left)`` changes."""
        native_updates = []
        for element, top, right, bottom, left in updates:
            key = id(element)
            node = self.nodes.get(key)
            previous = self.state.get(key)
            style = getattr(element, "_chromonic_native_style", None)
            if node is None or previous is None or style is None:
                raise KeyError("element is not present in this layout projection")
            inset = [float(top), float(right), float(bottom), float(left)]
            native_updates.append((node, *inset))
            # Cached native style and its retained snapshot are independent;
            # update only the one changed field in each instead of copying a
            # roughly 50-property dictionary per animated element.
            style["inset"] = inset
            snapshot, _children, _measure_key = previous
            snapshot["inset"] = list(inset)
        self.tree.set_insets(native_updates)

    def compute(self, root_element, *, width, height=None, viewport_height=None):
        """Compute and publish geometry after explicit projection patches.
        `viewport_height`: see `layout()`'s own parameter of the same name."""
        root_id = self.nodes[id(root_element)]
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        _write_boxes(boxes, self.node_map)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )

    def layout(self, root_element, *, width, height=None, reuse_styles=False, viewport_height=None):
        """See the module-level `layout()` function for what every
        parameter here means -- this is the same operation, just against a
        retained projection that reuses native nodes by element identity
        instead of rebuilding the whole Taffy tree from scratch."""
        from . import webfonts
        if webfonts.prepare_layout(root_element):
            reuse_styles = False
        self.begin()
        with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
            root_id = build(
                self.tree, root_element, self.node_map,
                reuse_styles=reuse_styles, projection=self,
            )
        self.finish()
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        _write_boxes(boxes, self.node_map)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )


def _snapshot_style(style):
    # style_bridge emits primitives/tuples and top-level lists of those. Copy
    # list values so later intrinsic-image mutation cannot alias the snapshot;
    # dict equality then stays in optimized Python/C code during reconciliation.
    return {key: list(value) if isinstance(value, list) else value
            for key, value in style.items()}


def _root_compute_height(root_element, height, viewport_height):
    if height is not None or viewport_height is None:
        return height
    style = getattr(root_element, "_chromonic_native_style", {})
    root_height = style.get("height")
    if isinstance(root_height, tuple) and root_height == ("pct", 1.0):
        return viewport_height
    return height


def _document_element_box_edges(root_element):
    """`(left, right, top, bottom)` margin+border+padding from `<html>`'s
    own computed style, or `None` if `root_element` isn't `<body>` with a
    real `<html>` parent, or `<html>` has none of the three set at all.

    `<html>` is never built into the Taffy tree -- chromonic hands Taffy
    `<body>` as its root instead, so `<html>`'s own box-model edges were
    never read anywhere. Summed together rather than kept separate --
    nothing downstream needs to tell them apart."""
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return None
    # `.parentElement` is broken for `<body>` in domonic (`.parentNode`
    # works); that object's `nodeType` is `DOCUMENT_NODE`, not
    # `ELEMENT_NODE` (domonic's `<html>` and `Document` are the same
    # underlying object), so `tagName` is the only reliable signal.
    html_element = getattr(root_element, "parentNode", None)
    if (html_element is None
            or (getattr(html_element, "tagName", "") or "").lower() != "html"):
        return None
    from domonic.style import ComputedStyleDeclaration
    computed = ComputedStyleDeclaration(html_element)

    def edge_px(name: str) -> float:
        raw = str(getattr(computed, name, "") or "0px")
        try:
            return float(raw[:-2]) if raw.endswith("px") else 0.0
        except ValueError:
            return 0.0

    left = edge_px("marginLeft") + edge_px("paddingLeft") + edge_px("borderLeftWidth")
    right = edge_px("marginRight") + edge_px("paddingRight") + edge_px("borderRightWidth")
    top = edge_px("marginTop") + edge_px("paddingTop") + edge_px("borderTopWidth")
    bottom = edge_px("marginBottom") + edge_px("paddingBottom") + edge_px("borderBottomWidth")
    if left == 0.0 and right == 0.0 and top == 0.0 and bottom == 0.0:
        return None
    return (left, right, top, bottom)


def _constrain_root_to_document_element(tree_obj, root_element, root_id, width: float) -> float:
    """Corrects `root_element` (`<body>`)'s own Taffy style for `<html>`'s
    box-model edges (`_document_element_box_edges`) before `compute()`
    runs, and returns the available width `<body>` must be computed
    against (`width` minus `<html>`'s horizontal edges).

    `<body>`'s own `width:auto` is always forced to a definite content-box
    number (`<html>`'s edges and `<body>`'s own margin subtracted from
    `width`) rather than left for Taffy to resolve -- a root node with
    only out-of-flow children would otherwise shrink-to-fit to `0`.
    `box-sizing: border-box` is left alone, since there `width` already
    means the border-box total."""
    edges = _document_element_box_edges(root_element)
    html_left, html_right = edges[0:2] if edges is not None else (0.0, 0.0)
    style = root_element.__dict__.get("_chromonic_native_style")
    body_margin = style.get("margin") if style is not None else None
    body_margin_left = _resolve_inset((body_margin or (0.0,) * 4)[3], width) or 0.0
    body_margin_right = _resolve_inset((body_margin or (0.0,) * 4)[1], width) or 0.0
    available_width = max(0.0, width - html_left - html_right)
    if style is not None and style.get("width") == "auto":
        outer_width = max(0.0, available_width - body_margin_left - body_margin_right)
        if style.get("box_sizing") != "border-box":
            padding = style.get("padding") or (0.0,) * 4
            border = style.get("border") or (0.0,) * 4
            outer_width = max(0.0, outer_width
                               - _numeric_edge(padding[1]) - _numeric_edge(padding[3])
                               - _numeric_edge(border[1]) - _numeric_edge(border[3]))
        style["width"] = outer_width
        tree_obj.set_style(root_id, style)
    return available_width


def warm_text_layout() -> None:
    """Pay Parley's one-time `FontContext` setup cost (~100ms, font
    enumeration) now, not during the first real page's first text --
    subsequent calls reuse the process-lifetime context and are near-free."""
    layout_text("warm", "sans-serif", 16.0)


def _write_boxes(boxes, node_map):
    """Publish native geometry back onto the authoritative Domonic nodes."""
    for node_id, box in boxes.items():
        x, y, w, h, bt, br, bb, bl, pt, pr, pb, pl = box
        element = node_map[node_id]
        state = element.__dict__
        # Same private-state assignment domonic's set_layout_box wrappers
        # ultimately do -- done directly here to skip them per node.
        state["_layout_box"] = LayoutBox(
            x=x, y=y, width=w, height=h,
            client_width=w - bl - br,
            client_height=h - bt - bb,
            border_top=bt, border_left=bl,
        )
        state["_chromonic_padding"] = (pt, pr, pb, pl)
        # Fresh Taffy geometry undoes any row heights `_settle_table`
        # distributed inside this table -- it must run again.
        state.pop("_chromonic_table_settled", None)


def _fix_float_shrink_to_fit_width(tree_obj, node_map: dict) -> bool:
    """CSS 2.1 10.3.5/10.3.6: a floated box with `width:auto` is sized by
    shrink-to-fit, not stretched to fill its containing block -- chromonic
    has no real float implementation, so a floated element reaches this
    point laid out as an ordinary full-width block first.

    Re-runs Taffy's `compute()` for just this element at `available_width=
    None` (max-content), re-laying-out the real subtree so descendants
    reflow into the narrower width too, then shifts the whole subtree to
    its real page position. Only ever shrinks -- nothing to correct if the
    intrinsic width isn't already smaller.

    Returns whether any subtree was actually shifted -- the caller uses
    this to skip a redundant `_publish_inline_formatting` republish (an
    O(node count) pass) on the, in practice, large majority of layouts
    that have no floats needing this correction at all."""
    shifted = False
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None or not _is_floated(resolved[0]):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("width") != "auto":
            continue
        node_id = by_id.get(id(element))
        if node_id is None:
            continue
        boxes = tree_obj.compute(node_id, None, None)
        own = boxes.get(node_id)
        if own is None:
            continue
        new_width = own[2]
        if new_width >= box.width:
            continue  # shrink-to-fit never grows a box past its available width
        float_value = getattr(resolved[0], "float", None)
        float_value = (float_value or "").strip().lower()
        target_x = (box.x + box.width - new_width) if float_value == "right" else box.x
        _write_boxes(boxes, node_map)
        dx = target_x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_recomputed_subtree(element, dx, dy, boxes, node_map)
            shifted = True
    return shifted


def _fix_table_shrink_to_fit_width(tree_obj, node_map: dict) -> bool:
    """CSS 2.1 17.5.2: an outer `display:table`/`inline-table` box with
    `width:auto` is sized by shrink-to-fit (summed column widths), the
    same as a float or `inline-block` -- not stretched to fill its
    containing block the way an ordinary block is. Chromonic's table
    root has no dedicated Taffy display mode of its own (plain "block",
    same as any other box; only its `<tr>`-equivalent children switch to
    a flex-row simulation -- see `build()`'s `is_table_row` handling), so
    it reaches this point laid out full-width first, same starting point
    `_fix_float_shrink_to_fit_width` corrects for floats -- reuses the
    identical technique (a fresh, max-content `tree.compute()` for just
    this subtree).

    Returns whether any subtree was actually shifted -- see `_fix_float_
    shrink_to_fit_width`'s return value for why the caller needs this."""
    shifted = False
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    # Reversed: `node_map` is in Taffy node-creation order, which `build()`
    # produces depth-first with children before their parent -- so a table
    # nested inside another came first here, got shrunk, and was then
    # overwritten by the *outer* table's own recompute (`_write_boxes`
    # covers the whole subtree). Outer first, inner last, keeps both.
    for element in reversed(list(node_map.values())):
        if not _is_element(element):
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
            # The real CSS 2.1 17.5.2.2 max-content width, from the
            # resolved columns (see the `is_table_root` branch of
            # `build()`), laid out as a *definite* width so the row's
            # cells land exactly on their bases -- captions never widen
            # it (a wide caption wraps to the grid, caption-side-example-
            # 001.xht). `available_width` is the containing-block width
            # Taffy positions this root's own margins within.
            new_width = min(box.width, max_content)
            grow = False
            if new_width >= box.width - 1e-6:
                # Shrink-to-fit never grows a box past its available
                # width -- but an `inline-table`, a flex item of the
                # inline-content approximation, can come out of Taffy
                # *narrower* than its columns, sized from its text alone
                # (inline-table-001.xht: the table around a `width: 1in`
                # cell at 66.6px, its text's width, not 96). With room in
                # the containing block it takes its full max-content width.
                parent = _layout_parent(element)
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
            available = new_width + _numeric_edge(margin[1]) + _numeric_edge(margin[3])
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
        _write_boxes(boxes, node_map)
        # Recomputed from scratch: every correction made inside this
        # subtree before now is gone with it (the caller re-runs them).
        shifted = True
        dx = box.x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_recomputed_subtree(element, dx, dy, boxes, node_map)
    return shifted


def _is_inline_table_box(element) -> bool:
    """An `inline-table` -- a real element's computed display, or a CSS
    2.1 17.2.1 anonymous one generated inside inline content."""
    if isinstance(element, _AnonymousTableBox):
        return element.kind == "inline-table"
    computed = getattr(element, "_chromonic_computed_style", None)
    return (getattr(computed, "display", "") or "").strip().lower() == "inline-table" if computed is not None else False


def _grow_box_height(element, delta: float) -> None:
    box = element.__dict__.get("_layout_box")
    if box is not None and delta:
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, height=box.height + delta, client_height=box.client_height + delta)


def _distribute_table_extra_height_in(table) -> None:
    """CSS 2.1 17.5.3: when a table's specified height (a minimum -- see
    `build()`'s table-root `min_height` handling) leaves surplus space
    below its rows, that surplus is handed out to the rows, not left as
    empty space inside the table box the way an ordinary block's `height`
    would leave it. Taffy lays the rows out at their own content heights
    inside the (already correctly tall) table box, so this stretches them
    to fill it afterwards: each grown row's cells grow with it (a cell
    always spans its row's full height), every later row and the row
    groups' own boxes move/grow to match.

    Which rows get the surplus follows Chrome: rows that have any real
    content share it in proportion to their heights; only when every row
    is empty is it split evenly between them all. Confirmed directly on
    border-conflict-element-001.xht (`table { height: 2in }`, three rows
    of empty bordered cells: each row 62.3px tall in Chrome, 5px here
    before this pass). Cell *content* stays where Taffy put it -- top-
    aligned; `vertical-align: middle` (Chrome's UA default for cells) is
    a separate piece not yet built."""
    for element in (table,):  # one table per call; `continue` below means "done"
        box = element.__dict__.get("_layout_box")
        rows = [row for row in (getattr(element, "_chromonic_table_rows", None) or ())
                if row.__dict__.get("_layout_box") is not None]
        if box is None:
            continue
        if not rows:
            # No rows at all, but a specified height: the (empty) grid is
            # still that tall, below any captions (table-caption-margins-
            # 001.xht: a 15px `display: table` holding only a caption is
            # caption box plus 15px).
            specified = getattr(element, "_chromonic_table_specified_height", None)
            if specified is None:
                continue
            captions = 0.0
            for caption in (list(getattr(element, "_chromonic_table_captions", None) or ())):
                caption_box = caption.__dict__.get("_layout_box")
                if caption_box is not None:
                    margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                    captions += caption_box.height + _numeric_edge(margin[0]) + _numeric_edge(margin[2])
            native = getattr(element, "_chromonic_native_style", None) or {}
            padding_top, _pr, padding_bottom, _pl = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
            border_bottom = box.height - box.client_height - box.border_top
            chrome = box.border_top + padding_top + border_bottom + padding_bottom
            grid = specified if native.get("box_sizing") == "border-box" else specified + chrome
            growth = (captions + grid) - box.height
            if growth > 0.5:
                _grow_box_height(element, growth)
            continue
        padding_top, _pr, padding_bottom, _pl = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        border_bottom = box.height - box.client_height - box.border_top
        inner_bottom = box.y + box.height - border_bottom - padding_bottom
        captions_height = 0.0
        for caption in getattr(element, "_chromonic_table_captions", None) or ():
            caption_box = caption.__dict__.get("_layout_box")
            if caption_box is not None:
                margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                captions_height += caption_box.height + _numeric_edge(margin[0]) + _numeric_edge(margin[2])
        for caption in getattr(element, "_chromonic_table_bottom_captions", None) or ():
            caption_box = caption.__dict__.get("_layout_box")
            if caption_box is not None:
                # Margins included: the table box is a BFC, so a bottom
                # caption's `margin-bottom: 10em` sits inside it
                # (table-anonymous-block-012.xht).
                margin = (getattr(caption, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
                inner_bottom -= caption_box.height + _numeric_edge(margin[0]) + _numeric_edge(margin[2])
        first_box = rows[0].__dict__["_layout_box"]
        last_box = rows[-1].__dict__["_layout_box"]
        rows_extent = (last_box.y + last_box.height) - first_box.y
        chrome_height = box.border_top + padding_top + border_bottom + padding_bottom
        # Surplus already inside the box (a `min_height` Taffy honoured with
        # nothing but caption-free rows to fill it) ...
        extra = inner_bottom - (last_box.y + last_box.height)
        # ... or, with captions in the box, the specified height applies to
        # the *grid* alone (CSS 2.1 17.4: captions sit outside the table
        # box, in the wrapper) -- so the grid may need to grow past what
        # the box currently holds, and the box with it. Confirmed on
        # border-collapse-applies-to-015.xht: a 100px `display:table` with
        # a 100px caption and one 10px row is 210px tall in Chrome (row
        # stretched to the full 100), not 120.
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
            _grow_box_height(element, growth)
        # CSS 2.1 17.5.3: a row (or any of its own cells) with a real
        # specified height is not a candidate for the surplus at all --
        # only rows left to their own auto/content height take a share
        # of it. Found on `wpt/css/css-grid/grid-model/display-grid.html`'s
        # own reference `<table>`: a `height:100%` table with one row's
        # `td`s given an explicit `height:30px` and the other row left
        # auto split the surplus 37.5/62.5 (proportional to *both* rows'
        # current heights) instead of leaving the explicit row at its own
        # 30px and handing the auto row the entire remainder (70).
        def _has_specified_height(row) -> bool:
            # A row's or cell's own declared `height` is converted to
            # `min_height` (with `height` itself reset to `auto`) back in
            # `build()`'s `is_table_row`/`is_table_cell` branches, per CSS
            # 2.1 17.5.3's "specified height is a minimum" -- so the
            # signal to read here is `min_height`, not `height` (which is
            # always `"auto"` on a table row/cell by this point).
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
            # A row group's (or any wrapper's) own box starts where its
            # first row does: moved by the shift accumulated before that
            # row, grown by everything its rows gain.
            ancestor = _layout_parent(row)
            while ancestor is not None and ancestor is not element:
                if id(ancestor) not in seen_ancestors:
                    seen_ancestors.add(id(ancestor))
                    if shift:
                        _shift_box(ancestor, 0.0, shift)
                ancestor = _layout_parent(ancestor)
            if shift:
                _shift_subtree(row, 0.0, shift)
            delta = deltas.get(id(row), 0.0)
            if delta:
                _grow_box_height(row, delta)
                for cell in getattr(row, "_chromonic_table_cells", None) or ():
                    _grow_box_height(cell, delta)
                ancestor = _layout_parent(row)
                while ancestor is not None and ancestor is not element:
                    group_growth[id(ancestor)] = (ancestor, group_growth.get(id(ancestor), (ancestor, 0.0))[1] + delta)
                    ancestor = _layout_parent(ancestor)
                shift += delta
        for ancestor, growth in group_growth.values():
            _grow_box_height(ancestor, growth)


def _grow_and_reflow(element, delta: float, *, stop_at=None, grow_self: bool = True) -> None:
    """`element` just needed `delta` more height than Taffy gave it: grow
    its box, move every later in-flow sibling down, and carry the same
    growth up through each auto-height ancestor (whose own box Taffy sized
    from the old height) with its later siblings likewise -- stopping at
    the first ancestor with a non-`auto` height, which doesn't grow.
    `stop_at` names an ancestor that still grows but propagates no
    further (a table settling its own rows: `_settle_table` hands the
    table's net growth on, exactly once). `grow_self=False` propagates a
    growth already applied to `element`'s own box."""
    if grow_self:
        _grow_box_height(element, delta)
    _shift_later_siblings_for_height_delta(element, delta)
    child = element
    ancestor = _layout_parent(element)
    while ancestor is not None and _is_element(ancestor):
        native = getattr(ancestor, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            break
        if ancestor is stop_at:
            _grow_box_height(ancestor, delta)
            break
        # An ancestor grows by what its flow now needs, not blindly by
        # `delta`: when the grown box is the last in flow and the
        # ancestor was already taller (sized by a taller sibling sharing
        # the same line -- table-vertical-align-baseline-008.xht's 100px
        # inline-block beside an inline-table Taffy first laid out 0px
        # tall), only the part of the new bottom edge that overflows
        # counts, which may be nothing.
        growth = _needed_ancestor_growth(ancestor, child, delta)
        if growth <= 0.01:
            break
        _grow_box_height(ancestor, growth)
        # A table row grown this way (a nested table inside one of its
        # cells got taller) keeps every cell as tall as the row.
        for cell in getattr(ancestor, "_chromonic_table_cells", None) or ():
            if cell is not child:
                _grow_box_height(cell, growth)
        _shift_later_siblings_for_height_delta(ancestor, growth)
        child, delta = ancestor, growth
        ancestor = _layout_parent(ancestor)


def _needed_ancestor_growth(ancestor, child, delta: float) -> float:
    """How much `ancestor`'s `height:auto` box must grow now that its
    in-flow `child` is `delta` taller (later siblings already shifted by
    that much). `delta` when anything follows the child in flow; else the
    part of the child's new bottom margin edge below the ancestor's
    content edge, capped at `delta`."""
    ancestor_box = ancestor.__dict__.get("_layout_box")
    child_box = child.__dict__.get("_layout_box")
    if ancestor_box is None or child_box is None:
        return delta
    seen_self = False
    for sibling in _child_nodes(ancestor):
        if sibling is child:
            seen_self = True
            continue
        if not seen_self or not _is_element(sibling) or sibling.__dict__.get("_layout_box") is None:
            continue
        sibling_style = getattr(sibling, "_chromonic_native_style", None) or {}
        if sibling_style.get("position") in ("absolute", "fixed"):
            continue
        return delta
    margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
    child_bottom = child_box.y + child_box.height + _numeric_edge(margin[2])
    padding = ancestor.__dict__.get("_chromonic_padding", (0.0,) * 4)
    content_bottom = ancestor_box.y + ancestor_box.border_top + ancestor_box.client_height - padding[2]
    return max(0.0, min(delta, child_bottom - content_bottom))


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
        if not _is_element(child):
            continue
        box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
        child_bottom = box.y + box.height + _numeric_edge(margin[2])
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
        weight = _parse_font_weight(paint_style.get("font_weight"))
        _ascent, _descent, normal = fonts.text_metrics(family, font_size, weight >= 600,
                                                       fonts.is_italic(paint_style.get("font_style")))
        resolved = _resolved_line_height(paint_style.get("line_height"))
        content = max(content, resolved if resolved is not None else normal)
    return content


def _layout_children(element):
    """`element`'s child *boxes* as laid out: the CSS 2.1 17.2.1/9.2.1.1
    anonymous boxes generated around its children this pass where there
    are any (`_normalized_child_nodes`), else its DOM children."""
    normalized = element.__dict__.get("_chromonic_normalized_children") if hasattr(element, "__dict__") else None
    return normalized if normalized is not None else _child_nodes(element)


def _first_baseline(element) -> "float | None":
    """CSS 2.1 17.5.3: the baseline of a cell (or any block) is the baseline
    of its first in-flow line box, reached through its first in-flow
    child that has one; a replaced element's is its bottom edge. `None`
    when there's no line box at all (an empty cell)."""
    box = element.__dict__.get("_layout_box")
    if box is None:
        return None
    if getattr(element, "_chromonic_is_table_root", False):
        # CSS 2.1 17.5.3/10.8.1: a table's baseline is its first row's.
        # A caption sits outside the table box (17.4) and never counts:
        # table-height-algorithm-031.xht aligns a nested captioned
        # table's first cell text, not its caption, with the sibling
        # cell's text. The rows must be settled first (see `_settle_table`).
        _settle_table(element)
        for row in getattr(element, "_chromonic_table_rows", None) or ():
            row_box = row.__dict__.get("_layout_box")
            if row_box is not None:
                return _table_row_baseline(row, row_box)
        return None
    def line_baseline(owner, top, line_height):
        paint = getattr(owner, "_chromonic_paint_style", None) or getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint.get("font_size"), default=16.0)
        family = paint.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint.get("font_weight"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600,
                                                     fonts.is_italic(paint.get("font_style")))
        line_height = line_height or normal
        return top + math.floor((line_height - (ascent + descent)) / 2) + ascent

    tag = getattr(element, "_chromonic_tag_name", None) or (getattr(element, "tagName", "") or "").lower()
    padding = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
    if tag in _REPLACED_OR_CONTROL_TAGS:
        if tag == "button" and (getattr(element, "_chromonic_text_lines", None) or []):
            # A button's baseline is its label's (table-height-algorithm-
            # 026.xht: a 64px `<button>` and a 64px `<div>` of the same
            # text share one baseline in Chrome), not its bottom edge.
            return line_baseline(element, box.y + box.border_top + padding[0],
                                 float(getattr(element, "_chromonic_line_height", 0.0) or 0.0))
        return box.y + box.height

    plan = getattr(element, "_chromonic_inline_plan", None)
    if plan is not None:
        # An element laying out its own inline formatting context (text
        # mixed with inline children -- `align-self-006.html`'s `<div><a>
        # aaa</a></div>` flex items): its first line box with content.
        baselines = getattr(plan, "_line_baselines", None) or {}
        has_content = getattr(plan, "_line_has_content", None)
        for y in sorted(baselines):
            if has_content is not None and not has_content.get(y):
                continue
            return box.y + y + baselines[y]
    computed = getattr(element, "_chromonic_computed_style", None)
    display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
    if not getattr(element, "_chromonic_has_layout_children", False):
        if not (getattr(element, "_chromonic_text_lines", None) or []):
            if display == "list-item":
                # An empty list item still has its marker's line box, and
                # that line's baseline (empty-cells-applies-to-003.xht: a
                # 1em `display: list-item` beside a text cell lines the
                # marker up with the text, 5px down).
                return line_baseline(element, box.y + box.border_top + padding[0],
                                     float(getattr(element, "_chromonic_line_height", 0.0) or 0.0))
            return None
        offset = float(element.__dict__.get("_chromonic_content_offset_y", 0.0) or 0.0)
        return line_baseline(element, box.y + box.border_top + padding[0] + offset,
                             float(getattr(element, "_chromonic_line_height", 0.0) or 0.0))
    for fragment in element.__dict__.get("_chromonic_inline_fragments") or ():
        fragment_box = fragment.__dict__.get("_layout_box")
        if fragment_box is not None and (getattr(fragment, "_chromonic_text_lines", None) or []):
            return line_baseline(fragment, fragment_box.y,
                                 float(getattr(fragment, "_chromonic_line_height", 0.0) or 0.0))
    children = element.__dict__.get("_chromonic_normalized_children") or getattr(element, "childNodes", None) or ()
    for child in children:
        if not _is_element(child):
            continue
        native = getattr(child, "_chromonic_native_style", None) or {}
        if native.get("position") in ("absolute", "fixed"):
            continue
        baseline = _first_baseline(child)
        if baseline is not None:
            return baseline
    return None


def _table_cell_baseline(cell, box, padding) -> "float | None":
    """CSS 2.1 17.5.3: a cell's baseline is that of its first in-flow line
    box (or first in-flow row); with neither, it's synthesized from the
    bottom of the cell's content -- the content's own extent, not the
    cell box Taffy already stretched to its row (empty-cells-applies-to-
    008.xht: a cell holding a 16px rowless table beside an 18px text
    cell puts the row's baseline at 16, making the row 20px, not 22).
    `None` for a cell with nothing in it at all: it has no baseline and
    takes no part in the row's alignment (table-vertical-align-baseline-
    008.xht: an inline-table whose one cell is empty aligns on its
    bottom edge, not on that cell's top)."""
    baseline = _first_baseline(cell)
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
    # A row with no cells at all: its baseline is its top, as Chrome has
    # it (empty-cells-applies-to-011.xht: a 16px cell-less `table-row`
    # wrapped into an anonymous cell sits with its top on the row's
    # baseline, 14px down, and the row is 30px tall).
    return row_box.y


def _align_table_cell_baselines_in(table) -> None:
    """CSS 2.1 17.5.4: every cell in a row whose `vertical-align` is
    `baseline` -- or any other value but `top`/`middle`/`bottom` (`sub`,
    `super`, `text-top`, a length...), which all mean `baseline` for a
    cell -- has its content pushed down so its first baseline meets the
    row's baseline, the lowest of theirs; a cell with no line box
    contributes its bottom content edge. A cell pushed past its row's
    height makes the row (and its table) taller. Runs before the
    `middle`/`bottom` alignment and the surplus-height distribution, both
    of which need the rows' final heights. Confirmed on table-vertical-
    align-baseline-001.xht (three baseline cells with 40/20/0px top
    padding: their text lines share one baseline in Chrome) and
    table-height-algorithm-019.xht (`vertical-align: sub` on three cells
    of 10/20/30pt text)."""
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
                        if _is_element(child):
                            _shift_subtree(child, 0.0, shift)
                else:
                    cell._chromonic_content_offset_y = shift
                if content_height is not None:
                    inner_height = box.client_height - padding[0] - padding[2]
                    growth = max(growth, shift + content_height - inner_height)
            if growth > 0.5:
                _grow_and_reflow(row, growth, stop_at=element)
                for cell in getattr(row, "_chromonic_table_cells", None) or ():
                    _grow_box_height(cell, growth)


def _settle_table(table) -> None:
    """Settle `table`'s vertical geometry -- baseline-align its cells,
    hand any specified-height surplus to its rows, then place `middle`/
    `bottom` cell content -- once per Taffy result (`_write_boxes` resets
    the mark when it rewrites the table), and only then carry the
    table's net growth to what follows it, exactly once per layout pass:
    a second settle after a shrink-to-fit recompute finds the ancestors
    and later siblings already moved. Called from the table pipeline for
    every table, innermost first, and on demand by `_first_baseline`:
    an `inline-table`'s baseline is read by the flex-row alignment
    before the table pipeline runs, and must see settled rows
    (table-vertical-align-baseline-008.xht: its one empty cell is 0px
    tall until the table's 100px height is distributed)."""
    if table.__dict__.get("_chromonic_table_settled"):
        return
    table.__dict__["_chromonic_table_settled"] = True
    before = table.__dict__.get("_layout_box")
    # Rowspans first, collapsing after: a `visibility: collapse` row is
    # laid out like any other -- it takes its share of a spanning cell's
    # height (row-visibility-003.xht: a two-line `rowspan=2` cell over a
    # visible and a collapsed row leaves the visible row one line tall,
    # the second line vanishing with the collapsed row) -- and only then
    # is flattened to nothing.
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
        _grow_and_reflow(table, pending, grow_self=False)
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
    _grow_box_height(row, delta)
    for cell in getattr(row, "_chromonic_table_cells", None) or ():
        if id(cell) not in exclude:
            _grow_box_height(cell, delta)
    _shift_later_siblings_for_height_delta(row, delta)
    ancestor = _layout_parent(row)
    while ancestor is not None and ancestor is not table:
        _grow_box_height(ancestor, delta)
        _shift_later_siblings_for_height_delta(ancestor, delta)
        ancestor = _layout_parent(ancestor)
    if ancestor is table:
        _grow_box_height(table, delta)


def _settle_collapsed_cells_in(table) -> None:
    """CSS 2.1 17.5.5, after layout: every row item laid out at a
    `visibility: collapse` column's real width (a cell, a colspan across
    one, a rowspan placeholder -- see `build()`'s cell branch) is
    narrowed by the collapsed width, and everything after it in the row
    moved up by that plus the lost border-spacing gap. The items were
    built with `flex-shrink: 0`, so their Taffy positions are exactly the
    uncollapsed ones this works from."""
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
            # with it: the item moves up by it (column-visibility-003.xht:
            # the collapsed cell sits flush against its neighbour at
            # 268px, not 270).
            moved += pre_move
            if moved:
                dx = moved if rtl else -moved
                if is_cell:
                    _shift_subtree(item, dx, 0.0)
                else:
                    _shift_box(item, dx, 0.0)
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
    # The rows, their groups and the table box (an auto-width one) give
    # up the same width (column-visibility-004.xht: four 100px columns,
    # one collapsed, make a 308px table -- three columns and four gaps).

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
        ancestor = _layout_parent(row)
        while ancestor is not None and ancestor is not table:
            if id(ancestor) not in seen:
                seen.add(id(ancestor))
                narrow(ancestor, moved)
            ancestor = _layout_parent(ancestor)
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
    """CSS 2.1 17.5.3 for `rowspan`: a spanning cell's height is spread
    over the rows it spans, not loaded onto the first. Taffy, seeing it
    as one item of its first row, stretched that row (and every cell in
    it) to the spanning cell's whole content height -- so first the row
    is brought back to what its *other* cells and its own `height` need,
    then, where the spanned rows together still can't hold the cell,
    the shortfall is dealt out to them in proportion to their heights
    (equally when they're all empty). The cell's own box is fitted to
    its rows last, by `_cover_spanned_rows_in`, once every row height is
    final. Confirmed on table-height-algorithm-010.xht (a ten-line
    `rowspan=10` cell over ten `height: 1em` rows: the table is exactly
    ten rows tall) and -018.xht (a `height: 200px` table's two rows,
    97px each, under a spanning cell)."""
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
        parent = _layout_parent(element)
        while parent is not None:
            count += 1
            parent = _layout_parent(parent)
        return count

    tables.sort(key=depth, reverse=True)
    for table in tables:
        _settle_table(table)


def _align_table_cell_content_in(table) -> None:
    """CSS 2.1 17.5.4: a cell's `vertical-align` positions its *content*
    within the cell box, whose height is always the full row height --
    `middle` centres it, `bottom` sinks it to the bottom; `top` and
    `baseline` (the latter approximated as top: every cell in these
    fixtures' rows shares one font, so their first baselines already
    line up) leave it where Taffy put it. Chrome's UA stylesheet makes
    `middle` the default for every `<td>`/`<th>` (`ua_style.py`), so this
    fires for essentially every real table whose rows are taller than
    some cell's own content -- confirmed on border-conflict-style-001.xht
    (`height: 3em` cells, one line of text each: Chrome's text sits 15px
    lower than the content-box top).

    Runs after `_distribute_table_extra_height`, once every row's height
    is final. A text-only cell gets `_chromonic_content_offset_y`, which
    `paint.py`/the harness add when placing its lines; a cell holding
    block children has each child's subtree shifted for real. A cell with
    mixed inline content (its own `_InlineFormattingPlan` fragments,
    shared between it and its inline descendants) is left top-aligned for
    now -- shifting those shared fragment lists safely needs the same
    re-publish machinery the shrink-to-fit passes use."""
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
                        if _is_element(child):
                            _shift_subtree(child, 0.0, offset)
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
            # A percentage-width fixed table: its content width was
            # unknown at build time, so the exact CSS 2.1 17.5.2.1 column
            # algorithm runs here instead, against the box Taffy gave it
            # (fixed-table-layout-023.xht: `width: 80%`). Styles were all
            # resolved during the build -- reused, not recomputed.
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
                    _shift_subtree(cell, dx, 0.0)
                    box = cell.__dict__["_layout_box"]
                if abs(box.width - width) > 1e-6:
                    cell.__dict__["_layout_box"] = dataclasses.replace(
                        box, width=width, client_width=max(0.0, width - (box.width - box.client_width)))


def _column_elements(table_element) -> list:
    """Every `<col>`/`<colgroup>` (or `table-column`/`table-column-group`)
    element directly under `table_element`, groups' columns included, in
    DOM order -- `_table_columns` without the per-column expansion."""
    found: list = []
    for child in _child_nodes(table_element):
        if not _is_element(child):
            continue
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and _is_absolutely_positioned(resolved[1]):
            continue  # CSS 2.1 9.7: blockified, a real box of its own (top-applies-to-005.xht)
        tag = (getattr(child, "tagName", "") or "").lower()
        computed = getattr(child, "_chromonic_computed_style", None)
        display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
        if display == "":
            try:
                display = (getattr(_describe(child, {})[0], "display", "") or "").strip().lower()
            except Exception:
                display = ""
        if tag == "colgroup" or display == "table-column-group":
            found.append(child)
            for node in _child_nodes(child):
                if _is_element(node):
                    node_tag = (getattr(node, "tagName", "") or "").lower()
                    if node_tag == "col":
                        found.append(node)
                    else:
                        try:
                            if (getattr(_describe(node, {})[0], "display", "") or "").strip().lower() == "table-column":
                                found.append(node)
                        except Exception:
                            pass
        elif tag == "col" or display == "table-column":
            found.append(child)
    return found


def _publish_svg_shape_boxes(node_map: dict) -> None:
    """An `<svg>`'s own content isn't laid out here (the root is one
    replaced box), but Chrome still answers `getBoundingClientRect()` for
    a shape inside it: a `<rect>` reports its `x`/`y`/`width`/`height`
    offset from the svg root's box, unclipped, unscaled when the svg has
    no `viewBox` (absolute-replaced-width-002.xht: a 200x100 rect in a
    300x50 svg). Published purely so the element reports that rect."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        tag = (getattr(element, "tagName", "") or "").lower()
        if tag not in ("svg", "svg:svg"):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None or element.getAttribute("viewBox") is not None:
            continue

        def length(node, name, default=0.0) -> float:
            raw = node.getAttribute(name)
            try:
                return float(str(raw).strip().rstrip("px")) if raw not in (None, "") else default
            except ValueError:
                return default

        for child in _child_nodes(element):
            if not _is_element(child):
                continue
            child_tag = (getattr(child, "tagName", "") or "").lower()
            if child_tag not in ("rect", "svg:rect"):
                continue
            width, height = length(child, "width"), length(child, "height")
            child.__dict__["_layout_box"] = LayoutBox(
                x=box.x + box.border_left + length(child, "x"), y=box.y + box.border_top + length(child, "y"),
                width=width, height=height, client_width=width, client_height=height)


def _publish_table_column_boxes(node_map: dict) -> None:
    """CSS 2.1 17.2.1 says a `table-column`/`table-column-group` box "is
    not rendered" -- it has no box of its own in the visual tree (and no
    Taffy node here; `_NON_RENDERING_TAGS`). Chrome still answers
    `getBoundingClientRect()` for a `<col>`/`<colgroup>` with the grid
    area its columns cover: the union of those columns' cells across
    every row (confirmed on basic-css-table-001.xht: a two-column group
    reports the two columns' full width and all three rows' height).
    Published here, after every row/cell box is final, purely so the
    element reports that same rect -- nothing paints it."""
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
            # A column with no cell in it at all: Chrome reports its own
            # specified width (if any) and no height, at the table box's
            # origin (separated-border-model-006.xht: two cell-less
            # `<col>`s in a spaced table sit at the table's own corner;
            # empty-cells-applies-to-012.xht: a `width: 1em` column in a
            # rowless anonymous table reports 16px wide).
            width = 0.0
            try:
                specified = style_bridge._len(_describe(owner, {})[1].width)
            except Exception:
                specified = None
            if isinstance(specified, (int, float)):
                width = max(0.0, float(specified))
            owner.__dict__["_layout_box"] = LayoutBox(
                x=table_box.x, y=table_box.y, width=width, height=0.0,
                client_width=width, client_height=0.0, border_top=0.0, border_left=0.0)

        # Column elements past the grid's last column (separated-border-
        # model-006.xht: four `<col>`s over two columns of cells) have no
        # column of their own at all -- reported empty, like any cell-less
        # column.
        for owner in _column_elements(element):
            if id(owner) not in ranges:
                publish_empty(owner)
        if not columns or not cells or not rows:
            for _first, _last, owner in ranges.values():
                publish_empty(owner)
            continue
        top = min(row.__dict__["_layout_box"].y for row in rows)
        bottom = max(row.__dict__["_layout_box"].y + row.__dict__["_layout_box"].height for row in rows)
        # Column edges from every cell edge that lands on a grid line: a
        # column's left edge is where some cell starts in it, or failing
        # that just past the previous column's right edge (a column under
        # the middle of a colspan has no cell of its own starting there);
        # likewise its right edge.
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
            # Chrome reports a zero-width column as an entirely empty
            # rect -- no height either (fixed-table-layout-014.xht), and
            # at the table box's own top (column-visibility-003.xht's
            # collapsed column: y 50, the table's, not the rows' 52).
            height = max(0.0, bottom - top) if width > 0.0 else 0.0
            owner.__dict__["_layout_box"] = LayoutBox(
                x=left, y=top if width > 0.0 else table_box.y, width=width, height=height,
                client_width=width, client_height=height,
                border_top=0.0, border_left=0.0,
            )


def _fix_float_flow_after_block_sibling(node_map: dict) -> None:
    """CSS 2.1 9.5: a float starts at or below the current block-flow
    position, at the containing block's edge, never wherever a previous
    sibling's box happened to end horizontally. `_approximate_inline_flow`
    stands in for real float layout with plain `flex-wrap`, which has no
    notion of this -- a row only wraps on width overflow, so a paragraph
    followed by floats packed them onto its own row instead of below it.

    Runs after Taffy's flex-wrap layout, using the qualifying split
    `_approximate_inline_flow` recorded on `element`. Narrow on purpose:
    only applies when every *qualifying* child is a real float, not merely
    inline-level -- a group with even one qualifying-but-not-floated
    (inline-tag) child leaves Taffy's own flex-wrap result alone entirely
    (real inline-flow approximation, e.g. a nav bar of plain `<a>`s, relies
    on that result's own gap/wrap handling, not this simplified packer).
    Does *not* also require at least one ordinary (non-qualifying) block
    sibling -- a pure all-float sibling group needs this same real
    left/right packing just as much (confirmed directly: two floats with
    no other sibling packed side-by-side, both flush-left, via Taffy's own
    flex-wrap row layout, `float:right` never actually consulted for
    positioning at all). When it applies, every child's position is
    recomputed by simple left-to-right block/float packing."""
    for element in list(node_map.values()):
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None:
            continue
        def out_of_flow(child) -> bool:
            resolved = getattr(child, "_chromonic_resolved_style", None)
            return resolved is not None and _is_absolutely_positioned(resolved[1])

        if any(is_flow and not out_of_flow(child) and not _is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        # An absolutely positioned child is no sibling in this flow at
        # all (position-absolute-007.xht: an abs box before a float
        # pushed the float 96px down and lost its own `top`).
        children, qualifies = zip(*[(child, is_flow) for child, is_flow in zip(children, qualifies)
                                    if not out_of_flow(child)]) if any(
            not out_of_flow(child) for child in children) else ((), ())
        if not children:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = box.x + box.border_left + pl
        content_right = content_left + (box.client_width - pl - pr)
        cursor_x = content_left
        right_cursor_x = content_right
        cursor_y = box.y + box.border_top + pt
        row_bottom = cursor_y
        # CSS 2.1 9.5: every still-uncleared float narrows the line box of
        # every row it overlaps, not just the one it first packed onto --
        # tracked here so a later block's `margin:auto` resolves against
        # the narrowed band, not the full content width.
        active_floats: list = []
        # `_adjust_body_collapsed_margins` may have already folded
        # `element`'s own top margin together with this first child's
        # (CSS 2.1 8.3.1 adjoining-margins collapse) -- treated as an
        # already-resolved `0`, not `mt`, here.
        first_margin_collapsed = getattr(element, "_chromonic_margin_collapsed", False)
        # CSS 2.1 8.3.1: adjoining margins collapse into one; a float
        # between two blocks doesn't break the adjoining chain.
        # `pending_margins` accumulates the current chain (an empty block
        # joins both its own margins without resolving anything); a real
        # block resolves the whole set via `_collapse_margin_set`.
        pending_margins: list = []
        block_bottom = cursor_y
        for index, (child, is_flow) in enumerate(zip(children, qualifies)):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            mt, mr, mb, ml = (_numeric_edge(v) for v in margin)
            if not is_flow and (getattr(child, "tagName", "") or "").lower() == "br":
                # CSS 2.1 9.2.2/9.5.2: a `<br>` among floats is a forced
                # line break, not a block -- its (empty) line box sits at
                # the current flow position *beside* the floats, narrowed
                # by them like any line box, and counts one line-height
                # in flow; its own `clear` (the `br { clear: both }` idiom
                # separating rows of floated test containers throughout
                # `css-flexbox/abspos/`) then applies clearance to what
                # *follows* the break, never to the break's own line.
                # Previously handled as an ordinary cleared block below
                # the floats, a full extra line lower than Chrome.
                line_top = block_bottom + _collapse_margin_set(pending_margins)
                line_height = child_box.height
                left = content_left
                for active in active_floats:
                    if (active["side"] == "left" and active["top"] < line_top + line_height
                            and active["bottom"] > line_top):
                        left = max(left, active["edge"])
                glyph_height = child.__dict__.get("_chromonic_br_glyph_height")
                new_y, new_height = line_top, line_height
                if glyph_height is not None and glyph_height < line_height:
                    # Chrome reports the break's own inline box (the
                    # font's content area, centred in the line), not the
                    # whole line box.
                    new_y = line_top + math.floor((line_height - glyph_height) / 2)
                    new_height = glyph_height
                child.__dict__["_layout_box"] = LayoutBox(
                    x=left, y=new_y, width=0.0, height=new_height,
                    client_width=0.0, client_height=new_height, border_top=0.0, border_left=0.0)
                block_bottom = line_top + line_height
                child_computed = (getattr(child, "_chromonic_resolved_style", None) or (None,))[0]
                block_bottom = _cleared_y(child_computed, active_floats, block_bottom)
                # The clearance is part of the container's flow extent
                # (`_fix_float_flow_container_auto_height`: Chrome's
                # `.big` wrapper ends at the cleared position, 1px past
                # the break's own line).
                # Stored relative to the break's own box: a later pass
                # may shift the whole container (an earlier sibling's
                # auto height changing), and an absolute y would go stale.
                child.__dict__["_chromonic_br_flow_bottom"] = block_bottom - new_y
                pending_margins = []
                row_bottom = cursor_y = block_bottom
                cursor_x = content_left
                right_cursor_x = content_right
                continue
            if not is_flow:
                if index == 0 and first_margin_collapsed:
                    mt = 0.0
                pending_margins.append(mt)
                if _block_margins_collapse_through(child, child_box):
                    # Own top/bottom margin joins the same adjoining set --
                    # nothing resolves yet, so this empty block's zero-size
                    # position is only a best-effort placement.
                    pending_margins.append(mb)
                    new_x = content_left + ml
                    new_y = block_bottom + _collapse_margin_set(pending_margins)
                    dx, dy = new_x - child_box.x, new_y - child_box.y
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        _shift_subtree(child, dx, dy)
                    cursor_x = content_left
                    continue
                # An ordinary in-flow block: own row, at the containing
                # block's edge, below everything placed so far -- narrowed
                # by a still-active float (CSS 2.1 9.5) only if this child
                # establishes its own BFC (9.4.1); an ordinary block's
                # border box may extend behind one otherwise.
                collapsed = _collapse_margin_set(pending_margins)
                new_y = block_bottom + collapsed
                narrowed_left = content_left
                narrowed_right = content_right
                child_computed = (getattr(child, "_chromonic_resolved_style", None) or (None,))[0]
                new_y = _cleared_y(child_computed, active_floats, new_y)
                child_native_style = getattr(child, "_chromonic_native_style", None) or {}
                child_has_explicit_width = child_native_style.get("width") != "auto"
                if _establishes_bfc(child_computed):
                    # CSS 2.1 9.5: a box establishing its own BFC must not
                    # overlap any float still active at its top -- narrowing
                    # alone (as before) stops there, but an *explicit*-width
                    # box too wide for what's left between the active
                    # floats at this `new_y` needs to drop further, past
                    # whichever of them is blocking it, and be renarrowed
                    # there -- repeated since dropping past one float can
                    # still leave another (or the same one, still) in the
                    # way. Confirmed directly on floats-wrap-top-below-bfc-
                    # 002l.xht: a 200px-wide new-BFC box between a 150px
                    # left float and a 300px right float (leaving negative
                    # room) previously just sat at its unnarrowed `new_y`,
                    # overlapping both, instead of dropping below the
                    # lower of the two.
                    #
                    # `width:auto` never needs this push-down check at all
                    # -- narrowing alone already gives it the right answer,
                    # since (unlike a fixed width) it just *fills* whatever
                    # narrowed space is left rather than needing to fit an
                    # already-decided size into it. Using this box's own
                    # (still full-row, not yet narrowed) `child_box.width`
                    # as the "does it fit" check here, as the fixed-width
                    # case does, was wrong for auto-width boxes: confirmed
                    # directly on floats-wrap-bfc-001-left-overflow.xht, an
                    # `overflow:hidden` (`width:auto`) div only 150px worth
                    # of actual content wide but still full-row (300px) at
                    # this point in the pipeline -- checking that 300
                    # against the 200px narrowed by an adjacent float
                    # wrongly looked like an overflow and pushed the whole
                    # box below the float instead of correctly narrowing
                    # beside it.
                    while True:
                        narrowed_left = content_left
                        narrowed_right = content_right
                        # A real interval overlap, not just "hasn't ended
                        # yet" -- a float whose own top is still below this
                        # box's `new_y` hasn't started yet either, and
                        # mustn't narrow a box placed above it (confirmed
                        # directly: a right float starting well below this
                        # row's top was otherwise still treated as
                        # "blocking" a same-row box that starts and ends
                        # entirely above it).
                        blocking = [
                            a for a in active_floats
                            if a["top"] < new_y + child_box.height and a["bottom"] > new_y
                        ]
                        for active in blocking:
                            if active["side"] == "left":
                                narrowed_left = max(narrowed_left, active["edge"])
                            else:
                                narrowed_right = min(narrowed_right, active["edge"])
                        if (not child_has_explicit_width or not blocking
                                or child_box.width <= narrowed_right - narrowed_left):
                            break
                        new_y = min(active["bottom"] for active in blocking)
                ml_auto = margin[3] == "auto"
                mr_auto = margin[1] == "auto"
                if ml_auto or mr_auto:
                    available = max(0.0, narrowed_right - narrowed_left)
                    remaining = available - child_box.width
                    if ml_auto and mr_auto:
                        ml = mr = remaining / 2.0
                    elif ml_auto:
                        ml = remaining - mr
                    else:
                        mr = remaining - ml
                if narrowed_left > content_left + 1e-6:
                    # CSS 2.1 9.5: a BFC box's *border* box must clear the
                    # float; its own margin may run under the float
                    # (flexbox_fbfc2.html: `margin-left: -200px` beside a
                    # 200px float still starts at the float's edge).
                    new_x = max(narrowed_left, content_left + ml)
                else:
                    new_x = narrowed_left + ml
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    _shift_subtree(child, dx, dy)
                block_bottom = new_y + child_box.height
                pending_margins = [mb]
                row_bottom = cursor_y = block_bottom
                cursor_x = content_left
                right_cursor_x = content_right
                continue
            if pending_margins:
                # A float never participates in margin collapsing itself
                # (CSS 2.1 8.3.1 only ever adjoins in-flow block boxes),
                # but it still starts *below* whatever vertical space a
                # still-pending collapsed margin resolves to -- resolved
                # here, once, the first time anything (this float) is
                # actually placed at that flow position; a later ordinary
                # block starts its own fresh chain from `block_bottom`
                # exactly as if this float were never there, matching the
                # float being out of flow for collapsing purposes.
                block_bottom = block_bottom + _collapse_margin_set(pending_margins)
                cursor_y = row_bottom = block_bottom
                pending_margins = []
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            child_computed = child_resolved[0] if child_resolved is not None else None
            float_side = "left"
            if child_computed is not None:
                float_value = (getattr(child_computed, "float", None) or "").strip().lower()
                if float_value == "right":
                    float_side = "right"
            # CSS 2.1 9.5.2: `clear` applies to a floated box exactly as
            # much as an ordinary block -- pushes its own top down (and
            # therefore `cursor_y`/`row_bottom`, both derived from it
            # below) past whatever it's clearing, before this float's own
            # placement is computed.
            cleared_y = _cleared_y(child_computed, active_floats, cursor_y)
            if cleared_y > cursor_y:
                cursor_y = row_bottom = cleared_y
                cursor_x = content_left
                right_cursor_x = content_right
            if float_side == "right":
                # `float:right` packs flush to the containing block's right
                # content edge, not the left-to-right packing below (CSS
                # 2.1 9.5.1).
                start_x = right_cursor_x - mr - child_box.width
                # CSS 2.1 9.5.1 rule 7: a float's outer top may not be
                # higher than any earlier float's it would otherwise
                # overlap. Triggered by the overlap itself (`start_x <
                # cursor_x`, i.e. this position collides with whatever's
                # already packed on the left) -- an earlier version also
                # required `right_cursor_x < content_right` (an existing
                # right float having already narrowed this row), which
                # incorrectly left the *first* right float on a row
                # unpushed even when it collided with an earlier *left*
                # float (confirmed directly on floats-wrap-top-below-bfc-
                # 002l.xht: a 300px right float that can't fit beside a
                # 150px left float in a 400px container needs to drop
                # below it, but only ever did when a second right float
                # was involved).
                if start_x < cursor_x:
                    cursor_y = row_bottom
                    right_cursor_x = content_right
                    start_x = right_cursor_x - mr - child_box.width
                new_x, new_y = start_x, cursor_y + mt
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    _shift_subtree(child, dx, dy)
                right_cursor_x = new_x - ml
                bottom = new_y + child_box.height + mb
                row_bottom = max(row_bottom, bottom)
                active_floats.append({"side": "right", "edge": new_x - ml, "top": new_y, "bottom": bottom})
                continue
            start_x = cursor_x + ml
            # Symmetric with the right-float branch above -- the overlap
            # itself is the trigger, not whether this happens to be the
            # first item packed so far.
            if start_x + child_box.width + mr > right_cursor_x:
                cursor_x = content_left
                cursor_y = row_bottom
                start_x = cursor_x + ml
            new_x, new_y = start_x, cursor_y + mt
            dx, dy = new_x - child_box.x, new_y - child_box.y
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                _shift_subtree(child, dx, dy)
            cursor_x = new_x + child_box.width + mr
            bottom = new_y + child_box.height + mb
            row_bottom = max(row_bottom, bottom)
            active_floats.append({"side": "left", "edge": cursor_x, "top": new_y, "bottom": bottom})


def _shift_later_siblings_for_height_delta(element, delta: float) -> None:
    """When `element`'s own height just changed by `delta` (a post-hoc
    correction, after Taffy already stacked its siblings using the old
    value), every later DOM sibling sharing its parent's ordinary block
    flow needs the same vertical shift -- Taffy positioned each one
    immediately after the previous sibling's own (now-stale) box.
    Absolutely/fixed-positioned siblings are excluded: their own position
    doesn't derive from preceding-sibling flow at all. A `display:none`
    sibling is excluded too, by `_shift_subtree` itself -- see its
    docstring."""
    parent = getattr(element, "parentNode", None)
    if parent is None or not _is_element(parent):
        return
    parent_native = getattr(parent, "_chromonic_native_style", None) or {}
    if parent_native.get("display") == "flex" and parent_native.get("flex_direction") in ("row", "row-reverse"):
        # Siblings laid out side by side (a table row's cells, the
        # inline-content approximation's items) don't follow `element`
        # vertically -- nothing to move (table-height-algorithm-026.xht:
        # a grown button cell pushed the neighbouring cell down 4px).
        return
    seen_self = False
    for sibling in _child_nodes(parent):
        if sibling is element:
            seen_self = True
            continue
        if not seen_self or not _is_element(sibling):
            continue
        sibling_style = getattr(sibling, "_chromonic_native_style", None) or {}
        if sibling_style.get("position") in ("absolute", "fixed"):
            continue
        if sibling.__dict__.get("_layout_box") is None:
            continue
        _shift_subtree(sibling, 0.0, delta)


def _bfc_descendant_float_bottom(element, floor: float) -> float:
    """The deepest bottom-margin-edge of any float inside `element`'s own
    BFC (CSS 2.1 10.6.7) -- descends through non-BFC-establishing
    descendants (an ordinary wrapper isn't a float's containing block;
    the nearest real BFC ancestor still owns it), stopping at any
    descendant that establishes its own BFC."""
    best = floor
    for child in _child_nodes(element):
        if not _is_element(child):
            continue
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is None:
            continue
        computed, style_obj = resolved
        if _is_absolutely_positioned(style_obj):
            continue
        child_box = child.__dict__.get("_layout_box")
        if child_box is None:
            continue
        if _is_floated(computed):
            native = getattr(child, "_chromonic_native_style", None) or {}
            margin = native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            best = max(best, child_box.y + child_box.height + _numeric_edge(margin[2]))
            continue
        if _establishes_bfc(computed):
            continue
        best = max(best, _bfc_descendant_float_bottom(child, floor))
    return best


def _has_ratio_derived_height(native: dict) -> bool:
    """CSS Sizing 4 `aspect-ratio`: when `height` is `auto` but `width` is
    definite and a ratio was declared, the *used* height comes from the
    ratio (Taffy's own `aspect_ratio` field already resolves it inside
    Taffy's layout), not from summed content -- so any pass that would
    otherwise recompute a `height:auto` element's height from its
    children's own extent must leave this one alone. Confirmed directly
    on `css-sizing/aspect-ratio/block-aspect-ratio-010.html`: a
    `width:100px; aspect-ratio:1/1; overflow:hidden` block holding a
    500px-tall child was recomputed to `600px` (the summed children,
    completely ignoring the ratio) instead of staying the ratio's own
    `100px`. Narrow on purpose: only the "definite width, auto height"
    case -- `min-height` clamping past the ratio (needing the *bigger*
    of the two) is a real, separate CSS Sizing 4 rule this doesn't
    attempt, and an *indefinite* width leaves the ratio unresolved,
    where content-based sizing is still exactly right."""
    return (isinstance(native.get("aspect_ratio"), (int, float))
            and isinstance(native.get("width"), (int, float))
            and native.get("height") == "auto")


def _fix_nested_bfc_float_auto_height(node_map: dict) -> None:
    """The same CSS 2.1 10.6.3/10.6.7 rule `_fix_float_flow_container_
    auto_height` applies (a `height:auto` box never counts a float unless
    it establishes a BFC) but for the cases that heuristic doesn't reach:
    a lone float (the only child of an ordinary wrapper div) reaches Taffy
    as a plain in-flow block, its full height counted toward the wrapper's
    auto-height like any other child -- Taffy has no notion it should be
    excluded, only that its margin might collapse through.

    A BFC-establishing ancestor further up needs the opposite correction,
    recursing past that same non-BFC wrapper to find the float, since its
    own auto-height counts every descendant float in its formatting
    context, not just direct children."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        if getattr(element, "_chromonic_float_flow_children", None) is not None:
            continue  # already handled by _fix_float_flow_container_auto_height
        if getattr(element, "_chromonic_tag_name", None) == "body":
            continue  # _adjust_body_collapsed_margins owns body
        if getattr(element, "_chromonic_is_table_root", False):
            # A table box (a BFC too) is sized by the table pipeline
            # (`_settle_table`) from its rows and captions -- often
            # anonymous boxes with no DOM children to read here at all
            # (caption-side-applies-to-017.xht; table-margin-004.xht's
            # `<p style="display: table">Test</p>` came out 0px tall).
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        if _has_ratio_derived_height(native):
            continue
        if native.get("display") in ("flex", "grid"):
            # `float` always computes to `none` on a flex/grid item, so
            # such a container can never actually have a floated child --
            # this function's whole premise never applies to one, even
            # though `_establishes_bfc` (correctly, as a separate CSS
            # fact) says it establishes a BFC. Its block-flow-style
            # recompute isn't equivalent to Taffy's own flex/grid sizing
            # (cross-axis extent, not the lowest child's bottom edge), so
            # it must never touch one -- Taffy's own number is already correct.
            continue
        if not getattr(element, "_chromonic_has_layout_children", False):
            # A genuine leaf (no element children) was never sized by
            # summing child contributions -- its `height:auto` is already
            # a real, correctly-measured text/line-box result, not
            # something to recompute from `childNodes` here.
            continue
        if getattr(element, "_chromonic_inline_plan", None) is not None:
            # `_chromonic_has_layout_children` is set `True` for one of
            # these too (a different purpose -- see `build()`'s own
            # comment there, stopping `paint.py` from drawing raw
            # `textContent` a second time), but it's still a genuine,
            # single Taffy leaf measured whole by `plan.measure()` -- its
            # own inline children (a `<span>`, say) were flattened into the
            # plan's runs, never built as real Taffy nodes of their own, so
            # they have no real `_layout_box` this function's "sum child
            # bottoms" logic could read. Recomputing its height from
            # `childNodes` here silently discarded the plan's own already-
            # correct measured height instead (confirmed on `wpt/css/CSS2/
            # visudet/content-height-001.html`: a `line-height:200px`
            # `display:inline-block` div, which also establishes a BFC,
            # measured `200px` correctly and then got overwritten to `129px`
            # by this exact function, right here).
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = _establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        has_float_child = False
        for child in _child_nodes(element):
            if not _is_element(child):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            if child_resolved is None:
                continue
            child_computed, child_style_obj = child_resolved
            if _is_absolutely_positioned(child_style_obj):
                continue
            if _is_floated(child_computed):
                has_float_child = True
                continue
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            normal_bottom = max(normal_bottom, child_box.y + child_box.height + _numeric_edge(margin[2]))
        if not has_float_child and not establishes_bfc:
            continue  # nothing this pass would change -- leave Taffy's own result alone
        content_bottom = normal_bottom
        if establishes_bfc:
            content_bottom = max(content_bottom, _bfc_descendant_float_bottom(element, content_top))
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            _shift_later_siblings_for_height_delta(element, delta)


def _fix_float_flow_container_auto_height(node_map: dict) -> None:
    """CSS 2.1 10.6.3/10.6.7: an element's own `height:auto` is the max
    extent of its in-flow content's bottom margin edge -- a float
    contributes too, but only if the element establishes a BFC (9.4.1).
    Taffy's own flex-wrap row-summing (`_approximate_inline_flow`'s
    stand-in for real float layout) instead *adds* each wrapped row's
    height together, double-counting a float row and a later normal-flow
    row that both start from the same content top.

    Runs after `_fix_float_flow_after_block_sibling` has placed every
    child at its real, float-aware position -- recomputes the container's
    height from those final positions instead."""
    for element in node_map.values():
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None:
            continue
        def out_of_flow(child) -> bool:
            resolved = getattr(child, "_chromonic_resolved_style", None)
            return resolved is not None and _is_absolutely_positioned(resolved[1])

        if any(is_flow and not out_of_flow(child) and not _is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        if getattr(element, "_chromonic_tag_name", None) == "body":
            # `_adjust_body_collapsed_margins` already owns body's own
            # auto-height with extra precision this generic version
            # doesn't replicate -- recomputing it here risks regressing it.
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        if _has_ratio_derived_height(native):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = _establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        float_bottom = content_top
        for child, is_flow in zip(children, qualifies):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or out_of_flow(child):
                continue  # an absolutely positioned child never sizes its parent (abspos-008.xht)
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            bottom = child_box.y + child_box.height + _numeric_edge(margin[2])
            br_flow_bottom = child.__dict__.get("_chromonic_br_flow_bottom")
            if br_flow_bottom is not None and not is_flow:
                bottom = max(bottom, child_box.y + br_flow_bottom)  # a `<br clear>`'s clearance
            if is_flow:
                float_bottom = max(float_bottom, bottom)
            else:
                normal_bottom = max(normal_bottom, bottom)
        content_bottom = max(normal_bottom, float_bottom) if establishes_bfc else normal_bottom
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            # Taffy already stacked every later sibling using this
            # element's stale, pre-fix height -- must be propagated.
            _shift_later_siblings_for_height_delta(element, delta)


def _apply_linebox_strut_height(node_map: dict) -> None:
    """CSS 2.1 10.8: a line box's height always includes its own "strut"
    (an invisible zero-width inline box using the line's font/line-height)
    even when the only real content is a single atomic inline-level box
    with no text. Taffy has no concept of a line box, so such a block's
    `height:auto` comes out exactly as tall as its tallest child.

    Deliberately conservative: only a `height:auto` block whose in-flow
    children are all atomic inline-level boxes at default `vertical-align:
    baseline`, with no text of their own, is corrected."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        if getattr(element, "_chromonic_inline_plan", None) is not None:
            continue  # real text already measured a correct line box
        native = getattr(element, "_chromonic_native_style", None)
        if native is None:
            continue
        # A fixed-height container still places its atomics on each line's
        # baseline (flex-wrap-002.html: 0px-tall inline-blocks in a 100px
        # box sit 15px down, on the 20px line's baseline); only the
        # container's own growth below is reserved for `height: auto`.
        height_auto = native.get("height") == "auto"
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is not None and getattr(resolved[1].display, "value", "") in (
                _FLEX_DISPLAYS + ("grid", "inline-grid")):
            # A real flex/grid container has no line box: its inline-block
            # children are flex/grid items (flex-direction-column.html:
            # four stacked inline-block items were pulled back onto one
            # "baseline" at the container's top).
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        child_nodes = _child_nodes(element)
        if any(getattr(node, "nodeType", None) == TEXT_NODE
               and _collapsed_text_node(node).strip() for node in child_nodes):
            continue  # real text present -- not this function's scope
        children = [node for node in child_nodes if _is_element(node)]
        if not children:
            continue
        atomic_children = []
        for child in children:
            computed = getattr(child, "_chromonic_computed_style", None)
            child_box = child.__dict__.get("_layout_box")
            if computed is None or child_box is None:
                atomic_children = None
                break
            display = (getattr(computed, "display", "") or "").strip().lower()
            tag_name = (getattr(child, "tagName", "") or "").lower()
            if display != "inline-block" and tag_name not in _REPLACED_OR_CONTROL_TAGS:
                atomic_children = None
                break
            if tag_name in _REPLACED_OR_CONTROL_TAGS and display in (
                    "block", "flex", "grid", "table", "list-item", "flow-root"):
                # A replaced element made block-level (`img { display:
                # block }`, empty-cells-007.xht) is a block box: no line
                # box, no strut, nothing to sit on a baseline.
                atomic_children = None
                break
            vertical_align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower()
            if vertical_align not in ("baseline", ""):
                atomic_children = None
                break
            atomic_children.append((child, child_box))
        if not atomic_children:
            continue
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        half_leading = (line_height - (ascent + descent)) / 2.0
        strut_above = ascent + half_leading
        strut_below = descent + half_leading
        aboves: dict = {}
        belows: dict = {}
        for child, child_box in atomic_children:
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            # `vertical-align:baseline` on an atomic box aligns its own
            # baseline to the line's (CSS 2.1 10.8.1): a replaced box's or
            # an empty inline-block's is its bottom margin edge; an
            # inline-block with text sits on its last line's baseline
            # (absolute-non-replaced-width-017.xht: a 120px inline-block
            # of 30px/4 text makes a 120px line, not 171).
            own = _element_own_baseline(child)
            if own is None:
                child_above = child_box.height + _numeric_edge(margin[0]) + _numeric_edge(margin[2])
                child_below = 0.0
            else:
                child_above = own + _numeric_edge(margin[0])
                child_below = child_box.height - own + _numeric_edge(margin[2])
            aboves[id(child)] = child_above
            belows[id(child)] = child_below
        # The atomics wrap into line boxes (the flex-row approximation's
        # own `flex-wrap: wrap` rows): a new line starts where x turns
        # back, or fails to advance while y moves on. Each line is at
        # least one strut tall and stacks under the previous one -- the
        # old single-line reading pulled every wrapped row onto the first
        # baseline (flex-wrap-002.html: five 25px inline-blocks in a 50px
        # box are three lines, 20px apart).
        lines: list = []
        current: list = []
        prev_x = prev_bottom = None
        reversed_row = native.get("flex_direction") == "row-reverse"  # an rtl line advances leftwards
        for child, child_box in atomic_children:
            turned = prev_x is not None and (
                (child_box.x > prev_x + 0.01) if reversed_row else (child_box.x < prev_x - 0.01))
            if prev_x is not None and (
                    turned
                    or (abs(child_box.x - prev_x) <= 0.01 and child_box.y >= prev_bottom - 0.01)):
                lines.append(current)
                current = []
            current.append((child, child_box))
            prev_x = child_box.x
            prev_bottom = child_box.y + child_box.height
        if current:
            lines.append(current)
        # Each atomic box sits with its bottom margin edge on its line's
        # baseline, `max_above` below the line top (CSS 2.1 10.8.1) --
        # Taffy's own baseline placement only knows the boxes, not the
        # strut (empty-cells-008.xht: a 0x0 `<img>` in an otherwise empty
        # cell reports its top at the baseline, 14px down, not centred).
        content_top = box.y + box.border_top + element.__dict__.get("_chromonic_padding", (0.0,) * 4)[0]
        line_top = content_top
        for line in lines:
            max_above = max([strut_above] + [aboves[id(child)] for child, _b in line])
            max_below = max([strut_below] + [belows[id(child)] for child, _b in line])
            for child, child_box in line:
                child_native = getattr(child, "_chromonic_native_style", None) or {}
                margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
                target = line_top + max_above - aboves[id(child)] + _numeric_edge(margin[0])
                if abs(target - child_box.y) > 0.01:
                    _shift_subtree(child, 0.0, target - child_box.y)
            line_top += max_above + max_below
        needed_height = line_top - content_top
        if not height_auto or needed_height <= box.height + 0.01:
            continue
        delta = needed_height - box.height
        # Grown through `_grow_and_reflow`: whatever follows moves down and
        # every auto-height ancestor grows with it (empty-cells-008.xht: a
        # cell holding only a 0x0 image is one strut tall, and so are its
        # row and table).
        _grow_and_reflow(element, delta)


def _apply_empty_inline_block_min_height(node_map: dict) -> None:
    """A genuinely empty `display:inline-block` box still measures
    `height:auto` as one line's worth of its own font/line-height, not
    zero -- unlike a plain non-replaced `display:inline`, whose *shared*
    ancestor line can legitimately collapse to zero when empty (CSS 2.1
    9.4.2), an inline-block always establishes its own self-contained
    formatting context, whose line box exists even with nothing in it.
    `build()`'s childless fallback gives it no such machinery on its own."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        computed = getattr(element, "_chromonic_computed_style", None)
        display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
        if display != "inline-block":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        # CSS 2.1 10.6.1/10.6.7: a block container with *no* line boxes is
        # zero tall -- a genuinely empty inline-block (no child node with
        # any content: `css-flexbox/flex-wrap-002.html`'s `<div style=
        # "width: 25px; display: inline-block"></div>` is 25x0 in Chrome)
        # has no line box to be one line tall. Only an inline-block with
        # some content, laid out shorter than a line, is corrected here.
        if not any(
                (getattr(node, "nodeType", None) == TEXT_NODE
                 and _collapsed_text_node(node).strip(_CSS_WHITESPACE_STRIP_CHARS))
                or _is_element(node)
                for node in _child_nodes(element)):
            continue
        # Deliberately not gated on `_chromonic_has_layout_children` --
        # that flag can be set for degenerate content too. What matters is
        # only whether the box ended up shorter than one line, checked
        # below against `needed_height`.
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        _ascent, _descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        if line_height <= 0.0:
            continue
        border = native.get("border") or (0.0,) * 4
        padding = native.get("padding") or (0.0,) * 4
        vertical_edges = (_numeric_edge(border[0]) + _numeric_edge(border[2])
                          + _numeric_edge(padding[0]) + _numeric_edge(padding[2]))
        needed_height = line_height + vertical_edges
        if needed_height <= box.height + 0.01:
            continue
        delta = needed_height - box.height
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, height=box.height + delta, client_height=box.client_height + delta,
        )


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


def _resync_interruption_marker_heights(node_map: dict) -> None:
    """`_finalize_inline_owner_boxes` builds each CSS 2.1 9.2.1.1
    interruption marker's rect from its block's `_layout_box` height at
    that point in the pipeline -- before the float-auto-height
    corrections (`_fix_float_flow_container_auto_height`/`_fix_nested_bfc_
    float_auto_height`, both run after `_publish_inline_formatting`) have
    had a chance to zero out a float-only block's own contribution. A
    block whose in-flow content is nothing but a float (CSS 2.1 9.5: a
    float doesn't contribute to its containing block's auto-height) still
    got its pre-correction, float-inflated height baked into the marker,
    and so did the owner's own bounding box built from it.

    Patches each marker rect's height back in sync with the block's real,
    final height now that it's known, and re-derives the owner's own
    bounding box from the corrected rects the same way `_finalize_inline_
    owner_boxes` first did -- idempotent, so a block whose height didn't
    actually change after all is simply a no-op. A *nested* split wrapper
    (CSS 2.1 9.2.1.1, see `_split_wrapping_inline_element`'s docstring)
    never appears in `node_map` itself -- reached instead the same way
    `_fix_nested_split_flow_extent` reaches it, via each interruption
    block's own `_chromonic_split_wrapper_ref` back-reference."""
    seen_ids: set = set()
    for node in list(node_map.values()) + [
        node.__dict__.get("_chromonic_split_wrapper_ref")
        for node in node_map.values()
        if _is_element(node) and node.__dict__.get("_chromonic_split_wrapper_ref") is not None
    ]:
        if not _is_element(node) or id(node) in seen_ids:
            continue
        seen_ids.add(id(node))
        owner = node
        marker_positions = owner.__dict__.get("_chromonic_marker_positions")
        rects = owner.__dict__.get("_chromonic_inline_boxes")
        if not marker_positions or rects is None:
            continue
        rects = list(rects)
        changed = False
        for index, blocks in marker_positions:
            if index >= len(rects):
                continue
            boxes = [block.__dict__.get("_layout_box") for block in blocks]
            if any(box is None for box in boxes):
                continue
            total_height = sum(box.height for box in boxes)
            old = rects[index]
            if abs(old[3] - total_height) > 0.01:
                rects[index] = (old[0], old[1], old[2], total_height)
                changed = True
        if not changed:
            continue
        owner.__dict__["_chromonic_inline_boxes"] = rects
        # Same all-degenerate fallback as `_finalize_inline_owner_boxes`
        # (see its own comment) -- kept in sync here since this can be the
        # pass that *makes* every rect degenerate (a float-only marker
        # zeroing out).
        bounding_rects = [r for r in rects if r[2] != 0.0 and r[3] != 0.0] or rects[-1:]
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right - left, height=bottom - top,
            client_width=right - left, client_height=bottom - top,
        )


def _fix_absolute_shrink_to_fit_extent(node_map: dict) -> None:
    """CSS 2.1 10.3.7 rule 3/5: an absolutely positioned box with
    `width: auto` and `left` or `right` auto is shrink-to-fit -- as wide
    as its content's preferred width, which for a child capped by its own
    `max-width` is that cap. Taffy sizes the box (the inline-content
    approximation's flex container) from the children's raw max-content
    instead (absolute-non-replaced-width-017..020.xht: a `max-width:
    4em` inline-block or float of 8em text made a 240px box, not 120).
    After layout the items' real extent is known: the box is narrowed to
    it when they ended up narrower than the box."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute" or style.get("width") != "auto":
            continue
        inset = style.get("inset") or ()
        if len(inset) != 4 or (inset[3] != "auto" and inset[1] != "auto"):
            continue
        members = (element.__dict__.get("_chromonic_flex_row_members")
                   or element.__dict__.get("_chromonic_float_flow_children") or [])
        if not members or style.get("display") != "flex":
            continue
        padding = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_left = box.x + box.border_left + padding[3]
        right_edge = None
        for member in members:
            member_box = member.__dict__.get("_layout_box") if hasattr(member, "__dict__") else None
            if member_box is None:
                continue
            member_native = member.__dict__.get("_chromonic_native_style") or {}
            if member_native.get("position") in ("absolute", "fixed"):
                continue
            margin = member_native.get("margin") or (0.0,) * 4
            edge = member_box.x + member_box.width + _numeric_edge(margin[1])
            right_edge = edge if right_edge is None else max(right_edge, edge)
        if right_edge is None:
            continue
        content_width = box.client_width - padding[1] - padding[3]
        extent = right_edge - content_left
        if extent >= content_width - 0.5 or extent < 0:
            continue
        new_width = box.width - (content_width - extent)
        dx = 0.0
        if inset[3] == "auto" and inset[1] != "auto":
            dx = box.width - new_width  # anchored on the right: the box's left edge moves in
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, x=box.x + dx, width=new_width, client_width=box.client_width - (content_width - extent))
        if dx:
            for member in members:
                if hasattr(member, "__dict__") and member.__dict__.get("_layout_box") is not None:
                    _shift_subtree(member, dx, 0.0) if _is_element(member) else _shift_box(member, dx, 0.0)


def _fix_relative_rtl_insets(node_map: dict) -> None:
    """CSS 2.1 9.4.3: a `position: relative` box with both `left` and
    `right` set is over-constrained -- `left` wins in an ltr containing
    block, `right` in an rtl one. Taffy always takes `left`; an rtl box
    is moved from `left` to `-right` here (position-relative-010.xht:
    `left: 1in; right: 1in` in an rtl div stays put; relpos-calcs-
    006.xht: `left: -50%; right: -50%` moves right by 50%)."""
    for element in list(node_map.values()):
        if not _is_element(element):
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
        parent = _layout_parent(element)
        parent_box = parent.__dict__.get("_layout_box") if parent is not None and hasattr(parent, "__dict__") else None
        if parent_box is None:
            continue
        if _element_direction(parent, getattr(parent, "_chromonic_computed_style", None)) != "rtl":
            continue
        # `_fix_rtl_block_positioning` leaves relative boxes alone, so
        # this one is still at Taffy's left-aligned spot plus Taffy's
        # `left`; CSS 2.1 10.3.3 puts an rtl block against the containing
        # block's right edge (its `margin-left` is the one recomputed)
        # before the relative offset applies.
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
            _shift_subtree(element, dx, 0.0)


def _inline_relative_offset(owner, stop, container_box) -> "tuple[float, float]":
    """The CSS 2.1 9.4.3 offset a fragment owned by `owner` carries from
    every `position: relative` inline between it and the plan element
    `stop` (exclusive): `left` (else `-right`) and `top` (else
    `-bottom`), each summed up the chain; a percentage resolves against
    the plan element's box (its containing block, near enough)."""
    dx = dy = 0.0
    node = owner
    while node is not None and node is not stop and _is_element(node):
        resolved = getattr(node, "_chromonic_resolved_style", None)
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
                rtl = _element_direction(node, resolved[0]) == "rtl"
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
    if not _is_element(element) or isinstance(element, _AnonymousTableBox):
        return False
    return bool(element.__dict__.get("_chromonic_flattened_inline"))


def _finalize_inline_owner_boxes(owner_accum) -> None:
    """Merge each inline owner's accumulated fragment rects -- gathered
    across every `_InlineFormattingPlan` that published fragments for it
    (CSS 2.1 9.2.1.1's split contributes from multiple independent plans
    belonging to the same owner) -- into its final `_chromonic_inline_
    boxes`/`_layout_box`/`_chromonic_owned_fragments`, once per owner per pass.

    Rects are grouped by split segment (`run["split_group"]`) rather than
    merged as one re-sorted list -- `getClientRects()` preserves document
    order, not a geometric sort.

    CSS 2.1 9.2.1: an inline's line-box fragments cover nested inline
    descendants' content too -- each owner's merged rects are folded into
    every tracked inline ancestor's, deepest owner first."""
    # An inline wrapper flattened into the plan with no text of its own
    # (`<span class="test"><span>FAILED</span></span>`) is never a
    # fragment owner, so it would get no box at all -- Chrome reports
    # its descendants' union (abspos-inline-003.xht, where that span is
    # also the containing block of an absolutely positioned child). It
    # borrows its descendants' rects here; `_chromonic_native_style` set
    # means the element has a real Taffy box already and stops the walk.
    for key, (owner, groups, _fragments) in list(owner_accum.items()):
        if not groups:
            continue
        parent = getattr(owner, "parentElement", None)
        while parent is not None and _is_flattened_inline(parent):
            entry = owner_accum.get(id(parent))
            if entry is None:
                # Only an ancestor with no fragments of its own: one that
                # has some folds its descendants into them below instead.
                entry = owner_accum[id(parent)] = (parent, {}, [])
                for group_key, rects in groups.items():
                    entry[1].setdefault(group_key, []).extend(rects)
            parent = getattr(parent, "parentElement", None)
    own_merged = {}
    for key, (owner, groups, _fragments) in owner_accum.items():
        if not groups:
            continue
        group_keys = sorted(groups, key=lambda key: (key is not None, key))
        merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
        own_merged[key] = [rect for group in merged_groups for rect in group]

    def _depth(owner) -> int:
        depth = 0
        node = getattr(owner, "parentElement", None)
        while node is not None:
            depth += 1
            node = getattr(node, "parentElement", None)
        return depth

    # `descendant_only[key]`: every rect contributed by `key`'s inline
    # descendants, excluding `key`'s own -- kept separate since only the
    # horizontal extent folds upward, never the vertical.
    descendant_only = {key: [] for key in own_merged}
    for key in sorted(own_merged, key=lambda key: _depth(owner_accum[key][0]), reverse=True):
        parent = getattr(owner_accum[key][0], "parentElement", None)
        while parent is not None:
            parent_key = id(parent)
            if parent_key in descendant_only:
                descendant_only[parent_key].extend(own_merged[key])
                descendant_only[parent_key].extend(descendant_only[key])
                break
            parent = getattr(parent, "parentElement", None)

    for key, (owner, groups, fragments) in owner_accum.items():
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
            # Vertically too: a `position: relative` descendant's shifted
            # box stretches its inline ancestor's rect (position-relative-
            # 032.xht: a span holding a `top: 25px` span is 43px tall).
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
        # `getClientRects()`: real Chrome exposes one extra rect per in-flow
        # block interruption (CSS 2.1 9.2.1.1) -- the anonymous block box
        # wrapping the real interrupting block, not the block's own
        # (possibly narrower) box. It's `width:auto`, 100% of `owner`'s
        # containing block, on top of (not merged with) the real leading/
        # trailing fragments, at its logical split position.
        interruption_blocks = getattr(owner, "_chromonic_interruption_blocks", None) or ()
        if interruption_blocks:
            container = getattr(owner, "_chromonic_split_container", None)
            container_box = container.__dict__.get("_layout_box") if container is not None else None
            cpt, cpr, cpb, cpl = (container.__dict__.get("_chromonic_padding", (0.0,) * 4)
                                  if container is not None else (0.0,) * 4)
            group_keys = sorted(groups, key=lambda key: (key is not None, key))
            merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
            self_edges = getattr(owner, "_chromonic_split_self_edges", None)
            self_left, self_right, self_top = self_edges or (0.0, 0.0, 0.0)
            if self_edges and merged_groups:
                # `owner` is a real Taffy node here (the direct-child split
                # shape), so *every* one of its text-leaf children -- not
                # just the leading segment's -- already sits physically
                # shifted right by `owner`'s own real border-left/padding-
                # left/top (Taffy applies that to every child alike).
                # Every rect in every group needs that same shift undone
                # first -- otherwise a trailing segment (which only ever
                # gains *width* below, never its own position correction)
                # stays off by that same amount, on both axes: `top_edge_
                # val` is never zeroed out of `box_height` (a one-way
                # addition, so it can't double-count there), but the
                # *position* it feeds into (`owner_y` in `publish()`)
                # cancels back to the leaf's own already-shifted position
                # exactly the way the horizontal one does. Only *after*
                # undoing the horizontal shift uniformly does the real
                # edge apply once more, correctly, to only the true
                # leading/trailing rects: left-widening the very first
                # rect of the first (leading) group, right-widening the
                # very last rect of the last (trailing) one -- exactly
                # which fragments a real inline box's own edges ever show
                # up on. The vertical shift has no such edge-widening
                # counterpart -- every segment's own `box_height` already
                # carries the *full* top+bottom edge unconditionally, so
                # only the position needs correcting, everywhere.
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
            atomic_segment_elements = getattr(owner, "_chromonic_atomic_segment_elements", None) or {}
            for index, group in enumerate(merged_groups):
                atomic_nodes = atomic_segment_elements.get(group_keys[index])
                # An *interior* segment (between two interruption blocks,
                # never the wrapper's own true leading/trailing one) with
                # no real content of its own -- no text, no atomic element,
                # nothing -- gets no fragment of its own in real Chrome:
                # nothing there ever generated an anonymous inline box to
                # begin with. The leading/trailing 0x0 case is different
                # (kept as-is) -- that one *is* a real, if empty, fragment
                # of the wrapper's own remaining content on that side.
                interior_empty = (
                    not atomic_nodes and 0 < index < len(merged_groups) - 1
                    and len(group) == 1 and group[0][2] == 0.0 and group[0][3] == 0.0
                )
                if atomic_nodes:
                    # This segment's "group" is a zero-sized marker run --
                    # its real content is one or more atomic elements built
                    # as real subtrees; use their already-final boxes instead.
                    for node in atomic_nodes:
                        node_box = node.__dict__.get("_layout_box")
                        if node_box is not None:
                            final_rects.append((node_box.x, node_box.y, node_box.width, node_box.height))
                elif not interior_empty:
                    final_rects.extend(group)
                if index < len(interruption_blocks):
                    block = interruption_blocks[index]
                    block_box = block.__dict__.get("_layout_box")
                    if block_box is not None and container_box is not None:
                        block_y = block_box.y
                        if container is owner:
                            # The direct-child shape: `container` is `owner`
                            # itself, forced to `width:100%` of its own
                            # containing block, so the marker uses the
                            # whole border box, uninset by `owner`'s own
                            # edges (never carried by the anonymous block
                            # box). `block_box.y` still needs the same
                            # border-top correction the text rects got.
                            marker_x = container_box.x
                            marker_width = container_box.width
                            block_y = block_y - self_top
                        else:
                            marker_x = container_box.x + container_box.border_left + cpl
                            marker_width = container_box.client_width - cpl - cpr
                        # CSS 2.1 9.2.1.1's anonymous block box wraps the
                        # real interrupting block, but the marker rect
                        # reports the block's own border box, not a
                        # margin-inflated union -- margin still participates
                        # in block-flow spacing, but isn't part of any
                        # box's own border-box geometry or hit region.
                        marker_rect = (marker_x, block_y, marker_width, block_box.height)
                        # Two markers with a skipped, genuinely-empty
                        # interior segment between them (just above) are
                        # visually contiguous -- nothing (no line box) ever
                        # separated them, so real Chrome reports them as
                        # one merged rect, not two back to back.
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
            owner.__dict__["_chromonic_inline_boxes"] = final_rects
            owner.__dict__["_chromonic_marker_positions"] = marker_positions
            all_merged = final_rects  # the block interruption also grows getBoundingClientRect()
        else:
            owner.__dict__["_chromonic_inline_boxes"] = all_merged
        # `getBoundingClientRect()` unions every `getClientRects()` rect
        # except zero-width/height ones -- `all_merged`/`final_rects`
        # themselves stay unfiltered (a zero-sized rect is a real fragment
        # there); only the union bounds here drop them. When *every* rect
        # is degenerate (e.g. a float-only interruption block whose marker
        # legitimately collapses to 0 height, CSS 2.1 9.5), real Chrome's
        # own bounding rect isn't a union of their differing positions --
        # confirmed empty (0x0) at the position of the *last* one, not a
        # box spanning from the first to the last.
        bounding_rects = [r for r in all_merged if r[2] != 0.0 and r[3] != 0.0] or all_merged[-1:]
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right-left, height=bottom-top,
            client_width=right-left, client_height=bottom-top,
        )
        owner._chromonic_has_layout_children = True
        # Text-range fragments stay scoped to this owner's own direct text,
        # never unioned across a nested element boundary.
        owner._chromonic_owned_fragments = fragments


def _publish_inline_formatting(node_map) -> None:
    """Project shared line fragments after parent boxes reach final positions."""
    seen = set()
    owner_accum: dict = {}
    element_fragments_accum: dict = {}
    for element in node_map.values():
        if id(element) in seen:
            continue
        seen.add(id(element))
        plan = getattr(element, "_chromonic_inline_plan", None)
        box = element.__dict__.get("_layout_box")
        if plan is not None and box is not None:
            plan.publish(box, element.__dict__.get("_chromonic_padding", (0.0,) * 4),
                         owner_accum, element_fragments_accum)
    _finalize_inline_owner_boxes(owner_accum)
    _fix_split_inline_relative_offset(owner for owner, _groups, _fragments in owner_accum.values())
    for element, fragments in element_fragments_accum.values():
        element._chromonic_inline_fragments = fragments


def _fix_split_inline_relative_offset(owners) -> None:
    """CSS 2.1 9.4.3: `position:relative`'s `top`/`left` offset shifts
    every box an element generates -- for a split inline (CSS 2.1
    9.2.1.1), that includes the real block child's own box too. `wrapper`
    (the split inline) is never built as a real Taffy node, so Taffy's own
    `position:relative` handling never sees it -- reapplied by hand here,
    after `_finalize_inline_owner_boxes` has published `wrapper`'s own
    fragment geometry.

    Takes the owners `_finalize_inline_owner_boxes` just published rather
    than walking `node_map` -- a nested split wrapper never appears there
    at all."""
    for owner in owners:
        interruption_blocks = getattr(owner, "_chromonic_interruption_blocks", None)
        if not interruption_blocks:
            continue
        container = getattr(owner, "_chromonic_split_container", None)
        native = getattr(owner, "_chromonic_native_style", None)
        container_box = container.__dict__.get("_layout_box") if container is not None else None
        if container_box is None or native is None:
            continue
        cpt, cpr, cpb, cpl = container.__dict__.get("_chromonic_padding", (0.0,) * 4)
        basis_width = container_box.client_width - cpl - cpr
        basis_height = container_box.client_height - cpt - cpb
        top, right, bottom, left = native.get("inset") or ("auto",) * 4
        top_v = _resolve_inset(top, basis_height)
        bottom_v = _resolve_inset(bottom, basis_height)
        left_v = _resolve_inset(left, basis_width)
        right_v = _resolve_inset(right, basis_width)
        dy = top_v if top_v is not None else (-bottom_v if bottom_v is not None else 0.0)
        dx = left_v if left_v is not None else (-right_v if right_v is not None else 0.0)
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        box = owner.__dict__.get("_layout_box")
        if box is not None:
            owner.__dict__["_layout_box"] = dataclasses.replace(box, x=box.x + dx, y=box.y + dy)
        inline_boxes = owner.__dict__.get("_chromonic_inline_boxes")
        if inline_boxes:
            owner.__dict__["_chromonic_inline_boxes"] = [
                (rx + dx, ry + dy, rw, rh) for rx, ry, rw, rh in inline_boxes
            ]
        for fragment in getattr(owner, "_chromonic_owned_fragments", None) or ():
            fbox = fragment.__dict__.get("_layout_box")
            if fbox is not None:
                fragment._layout_box = dataclasses.replace(fbox, x=fbox.x + dx, y=fbox.y + dy)
        for block in interruption_blocks:
            _shift_subtree(block, dx, dy)


def _fix_nested_split_flow_extent(node_map: dict) -> None:
    """A nested CSS 2.1 9.2.1.1 split wrapper (never a real Taffy node)
    still gets a `_layout_box` published for it -- the visual union of
    every generated fragment, border/padding decoration included, which
    can be taller than the real vertical space those fragments occupy in
    ordinary block flow (an edge fragment's border can overlap an
    adjoining one). An ancestor's auto-height must not read it directly.

    Computes a second, decoration-free box instead -- the real block-flow
    extent: the interruption blocks' own final top/bottom edges, extended
    by whichever edge fragments contributed real flow height. Ordinary
    sequential stacking, so it can't overlap; `_adjust_body_collapsed_
    margins` prefers this when present."""
    seen_wrappers: set = set()
    for node in node_map.values():
        if not _is_element(node):
            continue
        element = node.__dict__.get("_chromonic_split_wrapper_ref")
        if element is None or id(element) in seen_wrappers:
            continue
        seen_wrappers.add(id(element))
        container = getattr(element, "_chromonic_split_container", None)
        if container is None or container is element:
            continue  # the "wrapper is container" case already has a real, accurate Taffy box
        blocks = getattr(element, "_chromonic_interruption_blocks", None)
        if not blocks:
            continue
        first_box = blocks[0].__dict__.get("_layout_box")
        last_box = blocks[-1].__dict__.get("_layout_box")
        if first_box is None or last_box is None:
            continue
        edge_heights = getattr(element, "_chromonic_split_edge_flow_height", None) or {}
        flow_top = first_box.y - edge_heights.get("leading", 0.0)
        flow_bottom = last_box.y + last_box.height + edge_heights.get("trailing", 0.0)
        element._chromonic_flow_extent_box = LayoutBox(
            x=first_box.x, y=flow_top, width=first_box.width,
            height=max(0.0, flow_bottom - flow_top),
            client_width=first_box.width, client_height=max(0.0, flow_bottom - flow_top),
        )


def _adjust_body_collapsed_margins(root_element):
    """Publish Chrome-compatible body geometry for collapsed child margins.

    Taffy correctly positions block children with collapsed sibling margins,
    but a root node has no containing block into which its first/last margin
    struts can escape. HTML's body is special: Chrome's body rect excludes
    those escaped margins. Restrict this correction to the simple eligible
    body case; complex block formatting stays with Taffy.

    `_chromonic_scroll_extent` (set below, only once this correction
    applies) doubles as the signal `_apply_root_margin_offset` uses to
    know this pass already accounted for the root's own top margin, so it
    doesn't add that margin a second time -- cleared unconditionally up
    front so a stale value can never survive from an earlier pass.
    """
    root_element.__dict__.pop("_chromonic_scroll_extent", None)
    root_element.__dict__.pop("_chromonic_margin_collapsed", None)
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return
    style = getattr(root_element, "_chromonic_native_style", {})
    # `_approximate_inline_flow` may have turned `body` into a `flex-wrap`
    # row standing in for real float layout -- `_chromonic_float_flow_
    # children` marks this as chromonic's own approximation, where margin
    # collapsing still applies as it would to an ordinary block body. A
    # genuine author flexbox body (no such marker) is left alone: real
    # flex containers don't collapse margins with their children.
    if (style.get("display") != "block"
            and getattr(root_element, "_chromonic_float_flow_children", None) is None):
        return
    if any(value not in (0.0, "auto") for name in ("padding", "border")
           for value in style.get(name, ())):
        return
    # CSS 2.1 8.3.1: a block's own top margin collapses with its first
    # in-flow child's (letting the child's margin "escape" outward, which
    # is what this whole correction exists to publish) only when the
    # block doesn't establish a new block formatting context -- and any
    # `overflow` other than `visible` does exactly that, same as a real
    # border/padding already excluded above. Taffy has no such concept at
    # all -- its own raw child position already reflects the child's own
    # margin applied plainly (root nodes carry no margin of their own to
    # Taffy), so simply leaving it alone here and letting `_apply_root_
    # margin_offset` add body's own margin normally on top, unmodified,
    # already gives the correct, uncollapsed result.
    if False and any(value != "visible" for value in style.get("overflow", ("visible", "visible"))):
        # Disabled: CSS 2.1 8.3.1's adjoining-margins list for a block and
        # its first/last in-flow child doesn't actually name the block's
        # own `overflow` as a blocking condition (only border/padding
        # between them, or the child's own clearance) -- `overflow`
        # establishing a BFC stops a *grandchild*'s margin from escaping
        # past this element to things outside it, a different pairing
        # than this element's own margin against its direct child's.
        # Found on `css-grid/layout-algorithm/grid-as-flex-item-should-
        # not-shrink-to-fit-001.html`: `body { overflow: hidden }`'s
        # first-child `<p>`'s 16px margin still collapsed with body's own
        # 8px in Chrome (`y: 16`, `max(8, 16)`), not stacked (`y: 24`).
        return
    boxes = []
    visible_boxes = []
    float_top = None
    # The layout tree's own view of the children: a CSS 2.1 9.2.1.1
    # anonymous block around leading loose text is the real first in-flow
    # child here (table-anonymous-objects-093.xht: body text before a div).
    for child in (root_element.__dict__.get("_chromonic_normalized_children")
                  or _child_nodes(root_element)):
        if not _is_element(child):
            continue
        child_style = getattr(child, "_chromonic_native_style", {})
        if child_style.get("position") in ("absolute", "fixed"):
            continue
        # CSS 2.1 10.6.3: an ordinary block's auto height is the distance
        # to its last in-flow child's bottom margin edge -- floats are out
        # of flow for this (plain `<body>` never establishes a BFC, so
        # 10.6.7's float-inclusive algorithm doesn't apply). Taffy has no
        # `float` concept, so an un-flex-rowed floated child would
        # otherwise count fully toward `bottom` like real content.
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and _is_floated(resolved[0]):
            # A leading float sits at body's content top; the first in-flow
            # box may still be below it at this point (its `<br clear>`
            # line is only placed beside the float later, in `_fix_float_
            # flow_after_block_sibling`), so the float's own top bounds
            # body's (image-as-flexitem-size-001.html: body was pushed
            # 36px down to its first `<br>`).
            float_box = child.__dict__.get("_layout_box")
            if float_box is not None and not boxes:
                float_top = float_box.y if float_top is None else min(float_top, float_box.y)
            continue
        # A child that dissolved into a CSS 2.1 9.2.1.1 split reports its
        # `_chromonic_flow_extent_box` (the decoration-free flow extent,
        # authoritative and available earlier in the correction pipeline)
        # rather than its own `_layout_box` -- which, for such a child, is
        # not written until `_publish_inline_formatting` runs, several
        # passes after this function. Checking `_layout_box` for `None`
        # first (the previous order) meant this function saw no box at
        # all for a still-mid-split child on its first (pre-publish) call
        # this pass, silently skipped it, and returned without ever
        # setting `_chromonic_margin_collapsed` -- letting `_apply_root_
        # margin_offset` (which runs immediately after) wrongly add the
        # root's own margin a second time on top of Taffy's already-
        # correct collapsed position. Taffy's raw output was never wrong;
        # only this substitution order was. `_chromonic_flow_extent_box`
        # is checked first and preferred for exactly this reason -- only
        # a child with neither is skipped.
        box = getattr(child, "_chromonic_flow_extent_box", None)
        if box is None:
            box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        boxes.append(box)
        # A CSS-empty box (no border/padding/height, no in-flow content --
        # an absolutely-positioned-only wrapper still counts as empty)
        # doesn't stop a preceding margin from collapsing straight through
        # it; counting it toward `bottom` would double-count that margin
        # instead of letting it escape past this empty child. `box.height
        # == 0` alone isn't enough to conclude "no in-flow content" --
        # a negative child margin can legitimately pull a non-empty
        # wrapper's own auto-height back to zero too.
        has_in_flow_content = any(
            _is_element(node) and getattr(node, "_chromonic_resolved_style", None) is not None
            and not _is_absolutely_positioned(node._chromonic_resolved_style[1])
            and not _is_floated(node._chromonic_resolved_style[0])
            for node in _child_nodes(child)
        )
        if box.height == 0 and not has_in_flow_content and not any(
            value not in (0.0, "auto") for name in ("padding", "border")
            for value in child_style.get(name, ())
        ):
            continue
        visible_boxes.append(box)
    if not boxes:
        return
    if visible_boxes:
        boxes = visible_boxes
    # CSS 2.1 10.6.3: auto height is anchored to the *first* and *last*
    # in-flow child's own margin edges specifically -- not the extent of
    # whichever child happens to reach furthest. `boxes` is already in DOM
    # order, so those are literally the first/last entries here. This only
    # differs from a plain min/max when a negative margin makes an earlier
    # sibling's box visually stick out past a later one -- a negative
    # margin can pull an earlier child's own bottom edge past the real
    # last child's, but Chrome still tracks the real last child regardless.
    top = boxes[0].y if float_top is None else min(boxes[0].y, float_top)
    bottom = boxes[-1].y + boxes[-1].height
    old = root_element.__dict__.get("_chromonic_pristine_box") or root_element.__dict__.get("_layout_box")
    # `old` (the pristine, pre-offset box) is only right for the scroll-
    # extent baseline below -- on the second pass `_apply_root_margin_
    # offset` has since shifted the real box horizontally, and rebuilding
    # from the stale pristine `x` would silently discard that shift.
    current = root_element.__dict__.get("_layout_box") or old
    if old is not None:
        # The escaped final margin still contributes to the document's scroll
        # extent even though it is outside body.getBoundingClientRect() --
        # and unlike the rendered height above, the scrollable area *does*
        # need the true max over every child, first/last or not.
        explicit_height = style.get("height") != "auto"
        # Auto-height can go negative when a child's negative margin pulls
        # `bottom` back above `top` -- a used height is never negative
        # (CSS 2.1 8.1/10.5), so this clamps to `0` like Chrome does.
        corrected_height = old.height if explicit_height else max(0.0, bottom - top)
        corrected_client_height = old.client_height if explicit_height else corrected_height
        root_element.__dict__["_chromonic_scroll_extent"] = max(
            old.y + old.height, bottom, *(box.y + box.height for box in boxes)
        )
        if top > 0:
            # top > 0 means a child margin collapsed through the root, and
            # _adjust_body_collapsed_margins already accounts for it by
            # anchoring body's y to boxes[0].y.  Signal this so
            # _apply_root_margin_offset doesn't add the margin a second time.
            root_element.__dict__["_chromonic_margin_collapsed"] = True
        root_element.__dict__["_layout_box"] = LayoutBox(
            x=current.x, y=top, width=old.width, height=corrected_height,
            client_width=old.client_width, client_height=corrected_client_height,
            border_top=old.border_top, border_left=old.border_left,
        )


def _is_root_anchored(element) -> bool:
    """Whether `element` is `position:absolute`/`fixed` with no positioned
    ancestor -- its containing block is the viewport itself. Shared by
    `_fix_viewport_anchored_positioning` and `_apply_root_margin_offset`
    (which must not shift such elements)."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is None or not _is_absolutely_positioned(resolved[1]):
        return False
    return _find_containing_block_ancestor(element) is None


def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning, or `None` if none exists.

    CSS 2.1 10.1 rule 4: a `position:fixed` box's containing block is
    *always* the viewport (`None` here, so the caller falls back to the
    root/viewport-anchored fix-up) -- unlike `position:absolute`'s rule 3,
    no ancestor's own `position` can ever substitute for it, not even
    another positioned one. Confirmed directly on `position-absolute-
    005.xht`: a `position:fixed` box nested inside a `position:absolute`
    ancestor was walking up and anchoring to that ancestor instead,
    landing at its offset (296px, 418px) instead of the viewport's own
    top-left corner (0, 0) real Chrome puts it at."""
    # `element._chromonic_native_style["position"]` can't distinguish this
    # -- `style_bridge._position()` maps CSS `fixed` to the same Taffy-level
    # `"absolute"` string as CSS `absolute` (Taffy has no native `fixed`
    # concept). `_chromonic_resolved_style`'s `LayoutStyle.position` is the
    # pre-Taffy-mapping value straight from domonic's cascade, which still
    # keeps `fixed` distinct -- the same source `_is_absolutely_positioned`
    # already reads for exactly this reason.
    own_resolved = getattr(element, "_chromonic_resolved_style", None)
    if own_resolved is not None:
        own_position = own_resolved[1].position
        if getattr(own_position, "value", own_position) == "fixed":
            return None
    parent = getattr(element, "parentElement", None)
    while parent is not None:
        resolved = getattr(parent, "_chromonic_resolved_style", None)
        if resolved is not None and _establishes_containing_block(resolved[1]):
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
        # `top` alone (or neither) determines this element's position --
        # independent of the containing block's height. A root-anchored
        # *absolute* box Taffy already has at `top`; a `position: fixed`
        # box it attached to a positioned ancestor is at that ancestor's
        # offset instead (position-absolute-005.xht: `top: 0` fixed inside
        # an absolute inside a relative div sat at y 418), so `top` is
        # re-asserted against the viewport.
        return (top_v + mt if top_v is not None else None), None
    if top_v is None:
        # bottom-anchored, top:auto -- box.height is already right (an
        # explicit or intrinsic height never depends on the containing
        # block's own height), only the box's *position* needs correcting.
        return viewport_height - bottom_v - mb - box.height, None
    # Both top and bottom are definite. If height is also definite, this is
    # over-constrained -- top (+height) alone already fully determine the
    # box, same as the top-only case above, so leave it alone. If height is
    # auto, the box stretches to fill the gap between top and bottom, which
    # *does* depend on the containing block's height.
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
        # above -- also catches the synthetic `("pct", 1.0)` `build()`
        # assigns a width:auto block with inline content, which for a
        # root-anchored box must resolve against the true viewport width.
        new_width = width[1] * viewport_width
        if left_v is not None:
            return left_v + (ml or 0.0), new_width
        if right_v is not None:
            return viewport_width - right_v - (mr or 0.0) - new_width, new_width
        return None, new_width
    if right_v is None:
        # `left` alone determines position. Unlike the vertical
        # counterpart, this can't just be left as Taffy computed it --
        # `left_v` may be a percentage Taffy resolved against its own
        # (margin-shrunk) root box instead of the true viewport width.
        return (None if left_v is None else left_v + (ml or 0.0)), None
    if left_v is None:
        return viewport_width - right_v - (mr or 0.0) - box.width, None
    if width != "auto":
        return None, None
    # CSS 2.1 10.3.7 rule 5 (`left`/`right` definite, `width` auto): any
    # auto margin is 0 while solving for width. 10.4 then clamps that
    # solved width to `min-width`/`max-width`; once clamped, width is back
    # to being definite too, so an auto margin gets to re-absorb the slack
    # the clamp freed up (equal split, or the same zero-one-side exception
    # `_fix_absolute_horizontal_auto_margins` applies when that split would
    # go negative) -- same reasoning as `_fix_absolute_width_against_
    # containing_block`'s real-containing-block-ancestor case beside it.
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
            if _element_direction(element) == "rtl":
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
    """CSS 2.1 10.3.7: for a `position:absolute` box whose `left`/`width`/
    `right` are all definite, any `auto` margin absorbs the remaining
    slack of `left + margin-left + width + margin-right + right ==
    containing block width` -- split evenly if both are auto. Taffy's own
    absolute-positioning doesn't solve this, so this corrects the box's
    `x` (and everything inside it) after the fact.

    Only handled when a real containing-block ancestor exists -- a
    root-anchored box is corrected separately by `_fix_viewport_anchored_
    positioning`.

    Also handles exactly one of `left`/`right` being `auto` (CSS 2.1
    10.3.7 case 3/5): any auto margin resolves to `0`, and the missing
    inset is solved from the constraint equation -- auto margins only
    center the box in the fully-constrained case above.

    And handles the fully over-constrained case too (CSS 2.1 10.3.7 rule
    1 / 10.3.8): `left`/`width`/`right`/both margins *all* definite at
    once -- includes every replaced element whose intrinsic size makes
    `width` definite, not just an explicit non-auto `width`. Real Chrome
    drops the specified `right` (in `ltr`; `left` in `rtl`) and re-solves
    it, so `x` simply follows `left` + `margin-left` (mirrored for `rtl`)
    -- Taffy has no such rule and, confirmed directly on `absolute-
    replaced-width-071.xht`, instead positioned the box from `right`."""
    for element in list(node_map.values()):
        if not _is_element(element):
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
            if _element_direction(containing) == "rtl":
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
                    # CSS 2.1 10.3.7: splitting the negative slack evenly
                    # would give both margins a negative value -- instead
                    # the containing block's leading-edge margin (left in
                    # ltr, right in rtl) is pinned to 0 and the *other*
                    # margin absorbs all of it.
                    if _element_direction(containing) == "rtl":
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
            _shift_subtree(element, dx, 0.0)


def _fix_absolute_vertical_auto_margins(node_map: dict) -> None:
    """CSS 2.1 10.6.4: the vertical counterpart to `_fix_absolute_
    horizontal_auto_margins` -- for a `position:absolute` box whose
    `top`/`height`/`bottom` are all definite, any `auto` `margin-top`/
    `margin-bottom` absorbs the remaining slack of `top + margin-top +
    height + margin-bottom + bottom == containing block height`, split
    evenly if both are auto. Unlike the horizontal rule, 10.6.4 has no
    `direction`-based "pin one side to zero" exception for a negative
    split -- CSS 2.1 only ever attaches that exception to the *horizontal*
    margins (10.3.7), so an equal split here is applied unconditionally,
    even when it comes out negative. Taffy's own absolute positioning
    doesn't solve this equation, so this corrects the box's `y` (and
    everything inside it) after the fact.

    Only handled when a real containing-block ancestor exists -- a
    root-anchored box is corrected separately by `_fix_viewport_anchored_
    positioning`.

    Also handles exactly one of `top`/`bottom` being `auto` (CSS 2.1
    10.6.4 case 3/5): any auto margin resolves to `0`, and the missing
    inset is solved from the constraint equation -- auto margins only
    center the box in the fully-constrained case above. Confirmed
    directly on `absolute-non-replaced-height-003.xht` (`top: 0.5in;
    bottom: 0.5in; height: 1in; margin-top/margin-bottom: auto` inside a
    3in-tall `position:relative` containing block): unfixed, both auto
    margins stayed `0` (Taffy's own default) instead of splitting the
    96px of remaining slack 48px/48px, leaving the box flush against
    `top` instead of vertically centered."""
    for element in list(node_map.values()):
        if not _is_element(element):
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
            _shift_subtree(element, 0.0, dy)


def _fix_absolute_width_against_containing_block(node_map: dict) -> None:
    """CSS 2.1 10.3.7: a `position:absolute` box with `width:auto` and both
    `left`/`right` definite has its width *solved* from the constraint
    equation (`left + margin-left + width + margin-right + right ==
    containing block width`) -- the same equation `_resolve_viewport_
    anchored_box_x` already solves for a *root*-anchored box (no real
    containing-block ancestor, so it resolves against the viewport
    instead), generalized here for the ordinary case of a real containing-
    block ancestor, which that function doesn't cover at all. Taffy's own
    absolute positioning doesn't solve this equation either way, leaving
    `width:auto` at whatever the element's own content happened to
    measure (typically far too small, or `0` for an otherwise-empty box)
    instead. Confirmed directly on position-absolute-percentage-inherit-
    001.xht: a nested absolutely-positioned box with all four insets given
    and no explicit `width` measured `0` instead of the ~169px CSS 2.1
    10.3.7 actually solves for.

    Also applies CSS 2.1 10.4's min/max-width clamp to that solved width:
    unclamped, an auto margin is treated as `0` while solving the width
    equation (rule 5 above) -- but once clamping replaces the solved width
    with a fixed `min-width`/`max-width`, the box is back to the ordinary
    "all three of left/width/right are definite" case, so any auto margin
    gets to re-absorb the slack the clamp just freed up via the same
    equal-split (or, if that split would go negative, `_fix_absolute_
    horizontal_auto_margins`'s zero-one-side exception) rule -- otherwise
    a `max-width`-clamped, centered (`margin: auto`) absolutely positioned
    box would keep sitting flush against `left` instead of centering.
    Confirmed directly on `position-absolute-width-025.xht`
    (`left/right: 8px; width: auto; max-width: 100px; margin: 0 auto`):
    unclamped this solved `width: 784px` (the full containing block minus
    the two 8px insets) instead of the expected `100px`, centered box.

    Deliberately as narrow as `_fix_absolute_horizontal_auto_margins`
    beside it: only overwrites this box's own width/x (and its content-box
    accordingly) -- its children are shifted, like that function's box is,
    but not relaid-out to the new width, the same simplification
    `_fix_viewport_anchored_positioning` already accepts for the root-
    anchored case."""
    for element in list(node_map.values()):
        if not _is_element(element):
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
        # CSS 2.1 10.3.7 rule 5: with `left`/`right` both set and the
        # width solved, an `auto` margin is 0 and the box sits at `left`
        # -- whether or not Taffy already happened to solve the width
        # (absolute-non-replaced-width-015.xht: 100px wide already, but
        # placed from `right` at x 214 instead of `left`'s 111).
        new_x = cb_content_x + left_v + (ml or 0.0)
        if clamped:
            remaining = cb_width - left_v - new_width - right_v
            cml, cmr = ml, mr
            if cml is None and cmr is None:
                if remaining < 0:
                    if _element_direction(containing) == "rtl":
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
        # Resize first, at the box's *current* x -- `_shift_subtree` below
        # (not a second, redundant x assignment here) is what carries this
        # box and its descendants over to `new_x`, reading whatever x this
        # box has at the moment it runs.
        element.__dict__["_layout_box"] = LayoutBox(
            x=box.x, y=box.y, width=new_width, height=box.height,
            client_width=max(0.0, new_width - border_and_padding), client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
        dx = new_x - box.x
        if abs(dx) > 1e-6:
            _shift_subtree(element, dx, 0.0)


def _fix_absolute_height_against_containing_block(node_map: dict) -> None:
    """CSS 2.1 10.6.4: the vertical counterpart to `_fix_absolute_width_
    against_containing_block` -- a `position:absolute` box with
    `height:auto` and both `top`/`bottom` definite has its height *solved*
    from the constraint equation (`top + margin-top + height + margin-
    bottom + bottom == containing block height`), then clamped by CSS
    2.1 10.7's `min-height`/`max-height`, with any auto margin re-
    absorbing the slack that clamp frees up. Unlike the width version,
    the vertical margin split has no `direction`-based zero-one-side
    exception for a negative remainder (CSS 2.1 10.6.4, unlike 10.3.7,
    doesn't carve one out) -- always an equal split, matching `_fix_
    absolute_vertical_auto_margins` beside it. Taffy's own absolute
    positioning doesn't solve this equation, leaving `height:auto` at
    whatever the box's own content happened to measure instead.

    Deliberately as narrow as `_fix_absolute_width_against_containing_
    block`: only overwrites this box's own height/y (and its content-box
    accordingly) -- children are shifted, not relaid-out to the new
    height."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        if style.get("height") != "auto":
            continue
        inset = style.get("inset")
        if not inset:
            continue
        top, bottom = inset[0], inset[2]
        if top == "auto" or bottom == "auto":
            continue  # under-constrained differently -- not this equation
        containing = _find_containing_block_ancestor(element)
        if containing is None:
            continue  # root-anchored -- not handled here
        cb_box = containing.__dict__.get("_layout_box")
        if cb_box is None:
            continue
        cb_height = cb_box.client_height
        cb_content_y = cb_box.y + cb_box.border_top
        margin = style.get("margin") or (0.0, 0.0, 0.0, 0.0)
        margin_top_raw, margin_bottom_raw = margin[0], margin[2]
        mt = _resolve_inset(margin_top_raw, cb_height)
        mb = _resolve_inset(margin_bottom_raw, cb_height)
        top_v = _resolve_inset(top, cb_height) or 0.0
        bottom_v = _resolve_inset(bottom, cb_height) or 0.0
        # CSS Position 3 abspos-auto-size + CSS Sizing 4 aspect-ratio: when
        # both width and height are auto and every inset is definite (this
        # function's own left/right-auto guard would otherwise leave this
        # to the ordinary equation below -- narrowed here to exactly that
        # "all four insets given" case, since that's the only one where the
        # spec unambiguously names the block axis (height) as ratio-
        # dependent; the mirrored case -- only one inset auto, on the
        # *inline* axis -- makes width the ratio-dependent axis instead,
        # left to the existing insets-equation height below, which is
        # already correct for it). `_fix_absolute_width_against_containing_
        # block` runs first in the pipeline, so `box.width` here is already
        # the inset-stretched value to derive the ratio height from.
        ratio = style.get("aspect_ratio")
        ratio_derived = (isinstance(ratio, (int, float)) and ratio > 0
                         and inset[3] != "auto" and inset[1] != "auto")
        if ratio_derived:
            new_height = max(0.0, box.width / ratio)
        else:
            new_height = max(0.0, cb_height - top_v - (mt or 0.0) - bottom_v - (mb or 0.0))
        max_height_v = _resolve_inset(style.get("max_height"), cb_height)
        min_height_v = _resolve_inset(style.get("min_height"), cb_height)
        clamped = ratio_derived
        if max_height_v is not None and new_height > max_height_v:
            new_height, clamped = max_height_v, True
        elif min_height_v is not None and new_height < min_height_v:
            new_height, clamped = min_height_v, True
        new_y = box.y
        if clamped:
            remaining = cb_height - top_v - new_height - bottom_v
            cmt, cmb = mt, mb
            if cmt is None and cmb is None:
                cmt = cmb = remaining / 2.0
            elif cmt is None:
                cmt = remaining - cmb
            elif cmb is None:
                cmb = remaining - cmt
            new_y = cb_content_y + top_v + cmt
        if abs(new_height - box.height) <= 1e-6 and abs(new_y - box.y) <= 1e-6:
            continue
        border_and_padding = box.height - box.client_height
        element.__dict__["_layout_box"] = LayoutBox(
            x=box.x, y=box.y, width=box.width, height=new_height,
            client_width=box.client_width, client_height=max(0.0, new_height - border_and_padding),
            border_top=box.border_top, border_left=box.border_left,
        )
        dy = new_y - box.y
        if abs(dy) > 1e-6:
            _shift_subtree(element, 0.0, dy)


def _publish_used_horizontal_margins(node_map: dict) -> None:
    """CSS 2.1 10.3.3: an in-flow block's `auto` `margin-left`/`margin-
    right` resolves during layout, but Taffy never hands that resolved
    value back to Python (`LayoutBox` defaults both to `0`) -- domonic's
    own `getComputedStyle()` reads `_layout_box.margin_left`/`_right` for
    a declared `auto`, so this derives them from the box's own position
    relative to its parent's content-box edges.

    Narrow, matching CSS 2.1 10.3.3's scope: only ordinary in-flow block
    boxes, excluded when floated/out-of-flow or the parent is flex/grid
    (siblings share the row there). Pure reporting -- never moves a box."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None:
            continue
        computed, style_obj = resolved
        if _is_floated(computed) or _is_absolutely_positioned(style_obj):
            continue
        if _is_inline_level(element, style_obj):
            continue
        parent = getattr(element, "parentNode", None)
        if parent is None or not _is_element(parent):
            continue
        parent_box = parent.__dict__.get("_layout_box")
        parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
        if parent_box is None or parent_resolved is None:
            continue
        parent_display = parent_resolved[1].display
        parent_display = getattr(parent_display, "value", parent_display)
        if parent_display in ("flex", "inline-flex", "grid", "inline-grid"):
            continue
        parent_padding = parent.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = parent_box.x + parent_box.border_left + parent_padding[3]
        content_right = parent_box.x + parent_box.border_left + parent_box.client_width - parent_padding[1]
        margin_left = box.x - content_left
        margin_right = content_right - (box.x + box.width)
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, margin_left=margin_left, margin_right=margin_right,
        )


def _fix_absolute_static_position_fallback(node_map: dict) -> None:
    """CSS 2.1 10.3.7/10.6.4: an absolutely-positioned box with all-`auto`
    insets falls back to its static position -- where it would have
    landed as `position:static`. Taffy has no concept of this (an
    all-auto inset just resolves to `0`, landing the box at its
    containing block's origin).

    Only a reasonably common approximation, not full normal-flow layout:
    the static position is the literal DOM parent's content-box origin
    when there's no earlier in-flow sibling, or (approximating ordinary
    block stacking) directly
    below the last earlier in-flow sibling's own margin box otherwise. Real
    static-position resolution needs a full shadow layout pass computing
    where the box would land as if it were never taken out of flow at all
    -- a substantially bigger feature, not attempted here."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        inset = style.get("inset")
        if not inset:
            continue
        # CSS 2.1 10.3.7/10.6.4 resolve each axis independently -- `top:
        # 82px; left/right: auto` still needs the *horizontal* static-
        # position fallback even though the vertical position is already
        # correctly pinned by the explicit `top`.
        inset_top, inset_right, inset_bottom, inset_left = inset
        needs_x = inset_left == "auto" and inset_right == "auto"
        needs_y = inset_top == "auto" and inset_bottom == "auto"
        # CSS 2.1 10.1: a containing block formed by an *inline* ancestor
        # (a `position: relative` span flattened into its paragraph's
        # plan, with no Taffy box of its own) is that ancestor's first
        # inline box -- Taffy anchored the element to some outer box
        # instead (abspos-inline-003.xht: `top: 0; left: 0` inside a
        # relative span lands at the span's own corner, 602px in).
        inline_cb = None
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and _is_element(ancestor):
            resolved = getattr(ancestor, "_chromonic_resolved_style", None)
            if resolved is not None and _establishes_containing_block(resolved[1]):
                if _is_flattened_inline(ancestor):
                    inline_cb = ancestor
                break
            ancestor = getattr(ancestor, "parentElement", None)
        cb_rects = (inline_cb.__dict__.get("_chromonic_inline_boxes") or []) if inline_cb is not None else []
        if cb_rects and not (needs_x and needs_y):
            first, last = cb_rects[0], cb_rects[-1]
            new_x, new_y = box.x, box.y
            if not needs_x:
                new_x = (first[0] + _numeric_edge(inset_left) if inset_left != "auto"
                         else last[0] + last[2] - _numeric_edge(inset_right) - box.width)
            if not needs_y:
                new_y = (first[1] + _numeric_edge(inset_top) if inset_top != "auto"
                         else last[1] + last[3] - _numeric_edge(inset_bottom) - box.height)
            if abs(new_x - box.x) > 1e-6 or abs(new_y - box.y) > 1e-6:
                _shift_subtree(element, new_x - box.x, new_y - box.y)
                box = element.__dict__["_layout_box"]
        if not needs_x and not needs_y:
            continue
        # CSS 2.1 9.2.1.1/10.3.7: mixed into inline content (`_build_text_
        # runs_from_nodes`'s "escapee" runs, e.g. `wpt/css/CSS2/
        # positioning/abspos-007.xht`'s `<div class="test">` sitting
        # between plain text and a following in-flow block, all inside a
        # `display:inline` wrapper), `element`'s real static position is
        # wherever the surrounding text's own layout placed it -- not
        # simply "its literal DOM parent's content-box origin" (the
        # fallback below), which is also usually unusable here anyway: the
        # literal parent is commonly an inline wrapper never built as a
        # Taffy node at all (no `_layout_box`), unlike an ordinary block
        # parent this function already handles. `_InlineFormattingPlan.
        # measure()`/`.publish()` compute this directly (the only place
        # that actually knows the inline formatting context's own cursor
        # position) and stash it here.
        inline_static_position = getattr(element, "_chromonic_static_position", None)
        if inline_static_position is not None:
            static_x, static_y = inline_static_position
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
                    _shift_subtree(element, dx, dy)
                continue
            # The static position is where `element` would sit as an
            # ordinary `position:static` box -- pushed down by its own
            # margin-top (collapsing with a preceding sibling handled
            # separately below).
            own_margin = style.get("margin") or (0.0,) * 4
            own_margin_top = _numeric_edge(own_margin[0])
            own_margin_right = _numeric_edge(own_margin[1])
            own_margin_left = _numeric_edge(own_margin[3])
            # The static position is the parent's content-box origin, not
            # its border box.
            parent_pad_top, parent_pad_right, _parent_pad_bottom, parent_pad_left = (
                parent.__dict__.get("_chromonic_padding", (0.0,) * 4))
            # CSS 2.1 10.1: a block-level box's hypothetical static
            # position still stacks top-to-bottom the same regardless of
            # `direction` -- but *where* it would land horizontally, as
            # an ordinary in-flow block, follows the same `direction:rtl`
            # flush-right rule `_fix_rtl_block_positioning` already
            # applies to a real (non-absolute) sibling: flush against the
            # containing block's *right* content edge, not its left. Either
            # way this is still an ordinary block box's own margin box, so
            # its own margin-left/-right (mirroring `static_y`'s own
            # margin-top below) has to push it in from that edge same as
            # any other block -- previously omitted here, unlike the
            # vertical axis, so a `position:absolute` element with `left/
            # right:auto` (falling back to its static position) landed
            # flush against the containing block's padding edge with its
            # own declared margin silently dropped.
            if _element_direction(parent) == "rtl":
                static_x = (parent_box.x + parent_box.border_left
                            + parent_box.client_width - parent_pad_left - parent_pad_right
                            - box.width - own_margin_right)
            else:
                static_x = parent_box.x + parent_box.border_left + parent_pad_left + own_margin_left
            static_y = parent_box.y + parent_box.border_top + parent_pad_top + own_margin_top
            for sibling in _child_nodes(parent):
                if sibling is element:
                    break
                if not _is_element(sibling):
                    continue
                sibling_style = getattr(sibling, "_chromonic_native_style", None)
                sibling_box = sibling.__dict__.get("_layout_box")
                if sibling_style is None or sibling_box is None:
                    continue
                if sibling_style.get("position") in ("absolute", "fixed"):
                    continue  # out of flow -- doesn't move the static-position cursor
                sibling_resolved = getattr(sibling, "_chromonic_resolved_style", None)
                if sibling_resolved is not None and _is_floated(sibling_resolved[0]):
                    # A float doesn't move the block-flow position either
                    # (abspos-028.xht: an abs box after a 4em float has its
                    # static position at the container's top, `clear`
                    # notwithstanding -- it doesn't apply to abs boxes).
                    continue
                # `sibling_box` never includes margin, so the sibling's
                # trailing margin has to be added back explicitly, as the
                # larger of its own margin-bottom and this element's
                # margin-top (ordinary collapsing), not just added alone.
                #
                # A CSS-empty sibling is the one exception: Taffy already
                # resolves its own margin collapsing internally, so
                # `sibling_box.y` is already the fully-collapsed resting
                # position -- adding its raw margin-bottom on top would
                # double-count a margin Taffy already folded in.
                sibling_empty = sibling_box.height == 0 and not any(
                    value not in (0.0, "auto") for name in ("padding", "border")
                    for value in sibling_style.get(name, ())
                )
                if sibling_empty:
                    static_y = sibling_box.y + sibling_box.height + own_margin_top
                else:
                    sibling_margin_bottom = _numeric_edge((sibling_style.get("margin") or (0.0,) * 4)[2])
                    static_y = sibling_box.y + sibling_box.height + max(sibling_margin_bottom, own_margin_top)
        dx = (static_x - box.x) if needs_x else 0.0
        dy = (static_y - box.y) if needs_y else 0.0
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_subtree(element, dx, dy)


_FLEX_DISPLAYS = ("flex", "inline-flex", "-webkit-flex", "-webkit-inline-flex", "-ms-flexbox")


def _alignment_parts(value) -> "tuple[str, bool]":
    """A raw `align-*`/`justify-*` computed value as (keyword, safe) --
    `safe center` -> ("center", True), `last baseline` -> ("last-baseline",
    False), `unsafe end` -> ("end", False)."""
    parts = [part for part in (value or "").strip().lower().split()]
    safe = "safe" in parts
    parts = [part for part in parts if part not in ("safe", "unsafe")]
    return ("-".join(parts) or "normal", safe)


def _flex_container_static_position(parent, parent_box, element, box, style):
    """CSS Flexbox 4.1: the static position of an absolutely-positioned
    child of a flex container is where it would land as the *sole* flex
    item -- so the container's `justify-content` (main axis) and the
    child's `align-self` (cross axis, defaulting to the container's
    `align-items`) apply to it, using the child's own margin box against
    the container's content box (`css-flexbox/abspos/flex-abspos-staticpos-
    *.html`: `justify-content: center` centres the box, `align-self: safe
    end` bottom-aligns it unless it overflows, when it falls back to the
    start). Returns None for a parent that isn't a real CSS flex container
    (table rows and float wrappers are Taffy flex rows too, but their
    static position is ordinary block stacking)."""
    parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
    if parent_resolved is None:
        return None
    computed, layout_style = parent_resolved
    if getattr(layout_style.display, "value", "") not in _FLEX_DISPLAYS:
        return None
    direction = (getattr(computed, "flexDirection", "row") or "row").strip().lower()
    row = direction in ("row", "row-reverse")
    reverse = direction.endswith("-reverse")
    wrap_reverse = (getattr(computed, "flexWrap", "nowrap") or "nowrap").strip().lower() == "wrap-reverse"
    rtl = _element_direction(parent, computed) == "rtl"
    pad_top, pad_right, pad_bottom, pad_left = parent.__dict__.get("_chromonic_padding", (0.0,) * 4)
    content_x = parent_box.x + parent_box.border_left + pad_left
    content_y = parent_box.y + parent_box.border_top + pad_top
    content_w = parent_box.client_width - pad_left - pad_right
    content_h = parent_box.client_height - pad_top - pad_bottom
    margin = style.get("margin") or (0.0,) * 4
    mt, mr, mb, ml = (_numeric_edge(edge) for edge in margin)
    outer_w = box.width + ml + mr
    outer_h = box.height + mt + mb

    child_computed = (getattr(element, "_chromonic_resolved_style", None) or (None,))[0]
    child_rtl = _element_direction(element, child_computed) == "rtl"

    def place(keyword, safe, size, item, *, flex_flipped, start_flipped, self_flipped=None):
        # `flex_flipped`: `flex-start` is the axis's physical end (a
        # `-reverse` direction, or `wrap-reverse` on the cross axis);
        # `start_flipped`: writing-mode `start` is the physical end (rtl);
        # `self_flipped`: the same for `self-start`/`self-end`, judged by
        # the *item's* own direction (flex-abspos-staticpos-align-self-
        # rtl-004.html: an ltr child in an rtl column).
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
            # Baseline alignment's fallback is writing-mode `start`/`end`,
            # untouched by `wrap-reverse` (flex-abspos-staticpos-align-
            # self-002.html: `baseline` stays at the top, `last baseline`
            # at the bottom, while `stretch`/`flex-start` flip).
            at_end = (keyword == "last-baseline") != start_flipped
        else:  # flex-start, normal, stretch, space-between, auto...
            at_end = flex_flipped
        if safe and item > size:
            at_end = False
        return size - item if at_end else 0.0

    justify, justify_safe = _alignment_parts(getattr(computed, "justifyContent", "normal"))
    align, align_safe = _alignment_parts(getattr(computed, "alignSelf", "auto"))
    child_resolved = getattr(element, "_chromonic_resolved_style", None)
    if child_resolved is not None:
        align, align_safe = _alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
    if align == "auto":
        # Only `auto` defers to the container's `align-items`; `normal`
        # on the child itself behaves as `start` for an abs child.
        align, align_safe = _alignment_parts(getattr(computed, "alignItems", "normal"))
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


def _shift_box(node, dx: float, dy: float) -> None:
    box = node.__dict__.get("_layout_box")
    if box is not None:
        node.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=box.width, height=box.height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
    # An inline element's per-line rects (`_publish_inline_formatting`'s
    # `_chromonic_inline_boxes`, what it reports as its client rects) move
    # with it (column-visibility-004.xht: a span inside a cell the column
    # collapse shifted 2px up still reported its old x).
    rects = node.__dict__.get("_chromonic_inline_boxes")
    if rects:
        node.__dict__["_chromonic_inline_boxes"] = [
            (rect[0] + dx, rect[1] + dy) + tuple(rect[2:]) for rect in rects]


def _shift_recomputed_subtree(element, dx: float, dy: float, boxes, node_map: dict) -> None:
    """After a shrink-to-fit recompute of `element`'s subtree
    (`_write_boxes(boxes)`, positions relative to the subtree's own
    origin), move exactly what that recompute produced: an absolutely
    positioned descendant anchored to a containing block *outside* the
    subtree kept its real page position and must stay put (top-applies-
    to-001.xht: a `position: absolute; top: 0` row group anchored to the
    page was dragged down to its table's y). Elements with no Taffy node
    of their own (inline boxes published from fragments) are left to the
    caller's re-publish."""
    recomputed = {id(node_map[node_id]) for node_id in boxes if node_id in node_map}

    def walk(node):
        resolved = getattr(node, "_chromonic_resolved_style", None)
        if resolved is not None and not _renders(resolved[1]):
            return
        if id(node) not in recomputed:
            if resolved is not None and _is_absolutely_positioned(resolved[1]):
                return
        else:
            _shift_box(node, dx, dy)
        for fragment in getattr(node, "_chromonic_inline_fragments", None) or ():
            if id(fragment) in recomputed:
                _shift_box(fragment, dx, dy)
        for box in (node.__dict__.get("_chromonic_anonymous_table_boxes") or {}).values():
            if id(box) in recomputed:
                _shift_box(box, dx, dy)
            walk_anonymous_children(box)
        for child in _child_nodes(node):
            if _is_element(child):
                walk(child)

    def walk_anonymous_children(box):
        for inner in (box.__dict__.get("_chromonic_anonymous_table_boxes") or {}).values():
            if id(inner) in recomputed:
                _shift_box(inner, dx, dy)
            walk_anonymous_children(inner)

    walk(element)


def _shift_subtree(element, dx: float, dy: float) -> None:
    """Shift `element` and everything painted inside it by `(dx, dy)` --
    used to carry a corrected element's own position through to its
    descendants, whose boxes Taffy computed as offsets from `element`'s
    own (now-corrected) origin. A uniform shift preserves every internal
    relationship Taffy already got right.

    Skips `element` entirely when it's currently `display:none` -- it was
    never given a real Taffy node this pass, so its stale `_layout_box`
    must not keep being shifted on top of whatever was last published for
    it, or the correction compounds forever across relayouts."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is not None and not _renders(resolved[1]):
        return
    _shift_box(element, dx, dy)
    for fragment in getattr(element, "_chromonic_inline_fragments", None) or ():
        _shift_box(fragment, dx, dy)
    # Anonymous table boxes generated under `element` (CSS 2.1 17.2.1) are
    # not in `childNodes` -- their own boxes are shifted here; the real
    # nodes they wrap are still reached once, through the DOM walk below.
    _shift_anonymous_boxes(element, dx, dy)
    for child in _child_nodes(element):
        if _is_element(child):
            _shift_subtree(child, dx, dy)


def _shift_anonymous_boxes(element, dx: float, dy: float) -> None:
    for box in (element.__dict__.get("_chromonic_anonymous_table_boxes") or {}).values():
        _shift_box(box, dx, dy)
        _shift_anonymous_boxes(box, dx, dy)


def _fix_viewport_anchored_positioning(node_map: dict, viewport_height: float,
                                        viewport_width: "float | None" = None) -> None:
    """Correct the vertical position (and, when stretched, height) Taffy
    computed for any `position:absolute`/`fixed` element whose containing
    block is the document root itself.

    Taffy resolves such an element's `top`/`bottom`/`height` insets against
    the root's own Taffy box -- the full document height for a scrollable
    page, not the viewport. CSS's real rule is that such an element
    resolves against the initial containing block, which has the
    viewport's dimensions, not the document's.

    Only called when a caller supplies a real `viewport_height` -- every
    caller that doesn't keeps today's behaviour at zero extra cost.
    `viewport_width`, when given, applies the same correction horizontally."""
    seen = set()
    for element in list(node_map.values()):
        if id(element) in seen or not _is_element(element):
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
                _shift_box(fragment, dx, dy)
            for child in _child_nodes(element):
                if _is_element(child):
                    _shift_subtree(child, dx, dy)


def _fix_rtl_block_positioning(node_map: dict) -> None:
    """CSS 2.1 10.3.3: for an ordinary in-flow block-level box with
    `width`/`margin-left`/`margin-right` all non-`auto`, the over-
    constrained case ignores the *specified* `margin-right` and solves
    for it in `ltr` (Taffy's own default -- already flush against the
    specified `margin-left`, needing no correction) but ignores
    `margin-left` instead in `rtl`, honoring the real, specified
    `margin-right` and solving for `margin-left` -- which can come out
    smaller, larger, or even negative than whatever was actually written,
    not just `0`. Taffy has no `direction` concept at all, so it always
    positions such a block from a literal, un-recalculated `margin-left`
    regardless; this recomputes that one edge, in both directions,
    exactly as the spec's own formula would.

    Applies both to CSS 2.1 9.2.1.1's split interruption blocks (their
    real containing block is `_chromonic_split_container`, tracked
    separately from the DOM parent a non-replaced inline never
    establishes one of) and to an ordinary, un-split block child (its
    containing block is simply its own `parentElement`)."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        container = element.__dict__.get("_chromonic_split_container")
        if container is None:
            native_self = element.__dict__.get("_chromonic_native_style")
            if native_self is None or native_self.get("position") in ("absolute", "fixed"):
                continue
            if native_self.get("display") != "block":
                continue
            # `native_self["position"]` can't tell a genuine CSS
            # `position:relative` apart from plain `static` here --
            # `style_bridge._position()` maps both to the same Taffy-level
            # `"relative"` string (Taffy has no `static` of its own; an
            # un-positioned box is just "relative" with `inset` forced to
            # auto, see `to_dict()`). A *real* `position:relative` box
            # already gets its own correct horizontal offset from Taffy's
            # native relative-position handling (its `right`/`left` inset
            # included) -- this function's margin-based recalculation is
            # CSS 2.1 10.3.3's rule for an ordinary, non-positioned block's
            # margin box, a wholly different mechanism, and applying it on
            # top would silently discard Taffy's already-correct offset.
            # Confirmed directly on `right-offset-002.xht`/`right-007.xht`:
            # a `position:relative` block with `right` (not `left`) set,
            # inside a `direction:rtl` container, had its correct Taffy-
            # computed offset overwritten by this function's margin-only
            # recalculation, which has no notion of `right` at all.
            own_resolved = element.__dict__.get("_chromonic_resolved_style")
            if own_resolved is not None:
                own_position = own_resolved[1].position
                if getattr(own_position, "value", own_position) == "relative":
                    continue
            container = getattr(element, "parentElement", None)
            if container is None:
                continue
        # CSS 2.1 10.3.3's ltr/rtl branch is decided by the *containing
        # block's* own `direction` -- the actual block formatting context
        # this block is positioned within -- not a wrapping non-replaced
        # inline's own (it never establishes one itself). A `<span
        # style="direction:ltr">` wrapping a split block inside an outer
        # `direction:rtl` container still positions the block by the
        # outer container's `rtl`, confirmed directly against Chrome.
        if _element_direction(container) != "rtl":
            continue
        # CSS 2.1 10.3.3 is the rule for a block in *block flow*. A flex
        # (or grid) item -- a table cell inside its flex-row `<tr>`, or a
        # real flex item -- is positioned by its container's own
        # algorithm; right-aligning each one independently here stacked
        # every cell of an `rtl` table row on top of each other at the
        # row's right edge (border-conflict-element-002.xht).
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
            _shift_subtree(element, delta, 0.0)


def _element_own_baseline(element) -> "float | None":
    """The offset, from `element`'s own border-box top, of the CSS 2.1
    10.8.1 baseline a `display:flex; align-items:baseline` *row* should
    align it on -- `None` if it has no real in-flow line box at all (the
    spec's own fallback: align on its bottom margin edge instead, exactly
    what Taffy's own baseline algorithm already does unprompted).

    Needed because Taffy's flex baseline alignment only ever looks at a
    node's own reported baseline, which is real for a measured text leaf
    but silently `None` (synthesized from its own bottom edge instead, see
    `taffy::compute::flexbox`) for anything built from further Taffy
    children -- including a CSS 2.1 9.2.1.1 split's own anonymous block
    boxes, whose real text lives several accumulated levels down, never
    reaching Taffy's own baseline search at all. CSS 2.1 10.8.1: the
    baseline is that of the *last* in-flow line box, not the first."""
    box = element.__dict__.get("_layout_box")
    if box is None:
        return None
    plan = getattr(element, "_chromonic_inline_plan", None)
    if plan is not None:
        baselines = getattr(plan, "_line_baselines", None)
        has_content = getattr(plan, "_line_has_content", None)
        if baselines:
            for y in sorted(baselines, reverse=True):
                if has_content is not None and not has_content.get(y):
                    continue
                return y + baselines[y]
        return None
    owners = element.__dict__.get("_chromonic_split_plan_owners")
    if owners:
        for index in sorted(owners, reverse=True):
            owner = owners[index]
            owner_plan = getattr(owner, "_chromonic_inline_plan", None)
            owner_box = owner.__dict__.get("_layout_box")
            if owner_plan is None or owner_box is None:
                continue
            baselines = getattr(owner_plan, "_line_baselines", None)
            has_content = getattr(owner_plan, "_line_has_content", None)
            if not baselines:
                continue
            for y in sorted(baselines, reverse=True):
                if has_content is not None and not has_content.get(y):
                    continue
                return (owner_box.y - box.y) + y + baselines[y]
        return None
    if getattr(element, "_chromonic_is_table_root", False):
        # CSS 2.1 10.8.1: an `inline-table`'s baseline is its first row's
        # (table-vertical-align-baseline-009.xht: a 50px Ahem "X" beside
        # an inline-table of two such rows sits level with the first).
        baseline = _first_baseline(element)
        return None if baseline is None else baseline - box.y
    if (_is_element(element) and not getattr(element, "_chromonic_has_layout_children", False)
            and (getattr(element, "_chromonic_text_lines", None) or [])):
        # A text-bearing element built as its own flex item (a plain
        # `<span>` beside an atomic sibling): its baseline is its font's,
        # on the first line -- the last for an `inline-block` (10.8.1) --
        # not its bottom edge.
        lines = getattr(element, "_chromonic_text_lines", None) or []
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        line_height = float(getattr(element, "_chromonic_line_height", 0.0) or 0.0) or normal
        computed = getattr(element, "_chromonic_computed_style", None)
        display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
        index = len(lines) - 1 if display == "inline-block" else 0
        padding = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        offset = float(element.__dict__.get("_chromonic_content_offset_y", 0.0) or 0.0)
        return (box.border_top + padding[0] + offset + index * line_height
                + math.floor((line_height - (ascent + descent)) / 2) + ascent)
    if not _is_element(element):
        # A plain text leaf of the `elif inline_items:` flex-row
        # approximation (`tree.new_text_leaf`, no `_InlineFormattingPlan`
        # of its own) -- its baseline is just its own font's ascent.
        paint_style = getattr(element, "_chromonic_paint_style", None)
        if not paint_style:
            return None
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else (ascent + descent) or normal
        return ascent + math.floor((line_height - (ascent + descent)) / 2)
    return None


def _fix_flex_row_baseline_alignment(node_map: dict) -> None:
    """Correct `display:flex; align-items:baseline` rows built by the
    `elif inline_items:` mixed-text-and-elements flex-row approximation
    (`build()`) -- Taffy's own baseline alignment silently falls back to
    each item's own bottom edge whenever that item isn't a measured text
    leaf itself (see `_element_own_baseline`'s docstring), which is wrong
    for exactly the case this approximation exists to handle: real text
    sharing a row with a nested element (e.g. an `inline-block`) whose own
    text lives several Taffy levels down. Gated on `_chromonic_flex_row_
    members`, set only by that one approximation -- never a real author
    flexbox, whose own explicit `align-items:baseline` must keep Taffy's
    own (here, correct-by-definition) behavior untouched."""
    for element in node_map.values():
        # Not gated on `_is_element`: `_group_inline_element_runs`' own
        # anonymous-block wrapper (`_AnonymousInlineRun`, a pseudo-node
        # with `nodeType` 3, not a real `Element`) sets this attribute too,
        # for a run of consecutive real inline-level element siblings --
        # excluding it here silently skipped that whole case (confirmed on
        # `wpt/css/CSS2/visudet/content-height-001.html`).
        members = element.__dict__.get("_chromonic_flex_row_members") if hasattr(element, "__dict__") else None
        if not members or len(members) < 2:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        rows: list = []
        current: list = []
        prev_x = None
        # An absolutely-positioned member sits where its insets put it,
        # never on the line's baseline (table-vertical-align-baseline-
        # 009.xht: `position: absolute; bottom: 0`).
        in_flow = [member for member in members
                   if member.__dict__.get("_layout_box") is not None
                   and (member.__dict__.get("_chromonic_native_style") or {}).get("position")
                   not in ("absolute", "fixed")]
        prev_bottom = None
        # An rtl line (`row-reverse`, see `build()`'s `elif inline_items:`)
        # advances leftwards, so "x turns back" means x *increasing*.
        reversed_row = (element.__dict__.get("_chromonic_native_style") or {}).get("flex_direction") == "row-reverse"
        for member in in_flow:
            member_box = member.__dict__["_layout_box"]
            # A new wrapped row starts where x turns back -- or, for a
            # member alone on its row (`align-content-wrap-004.html`: four
            # inline-blocks each wider than the 100px column item, all at
            # x=8), where x fails to advance and the member sits below
            # the previous one; the old strict "x decreased" test folded
            # those four rows onto one baseline.
            turned = prev_x is not None and (
                (member_box.x > prev_x + 0.01) if reversed_row else (member_box.x < prev_x - 0.01))
            if prev_x is not None and (
                    turned
                    or (abs(member_box.x - prev_x) <= 0.01 and member_box.y >= prev_bottom - 0.01)):
                if len(current) > 1:
                    rows.append(current)
                current = []
            current.append(member)
            prev_x = member_box.x
            prev_bottom = member_box.y + member_box.height
        if len(current) > 1:
            rows.append(current)
        for row in rows:
            # Taffy's own cross-axis sizing for a custom `MeasureFunc` leaf
            # inside an `align-items:baseline` row doesn't reliably keep
            # that leaf's own correctly-measured `height:auto` result (see
            # `_make_measure`'s own `_chromonic_measured_height` stash, and
            # `_InlineFormattingPlan.height` for a leaf with its own inline
            # plan) -- re-assert it here, before any baseline math below
            # reads a (possibly still-wrong) `member_box.height`.
            for member in row:
                member_native = member.__dict__.get("_chromonic_native_style")
                if member_native is None or member_native.get("height") != "auto":
                    continue
                plan = member.__dict__.get("_chromonic_inline_plan")
                wanted = (plan.height if plan is not None
                          else member.__dict__.get("_chromonic_measured_height"))
                if wanted is None:
                    continue
                member_box = member.__dict__.get("_layout_box")
                if member_box is None:
                    continue
                delta = wanted - member_box.height
                if abs(delta) < 0.01:
                    continue
                member.__dict__["_layout_box"] = dataclasses.replace(
                    member_box, height=member_box.height + delta,
                    client_height=member_box.client_height + delta,
                )
            # Taffy already applied its *own* (here, sometimes wrong)
            # cross-axis baseline offset to every member's current `y` --
            # using that directly as a baseline reference would double-
            # count it. Every flex item starts flush with the row's own
            # cross-start edge before any such offset is added, so the
            # member Taffy already trusted most (the one it moved least,
            # i.e. the smallest current `y`) recovers that shared,
            # un-offset row top.
            entries = [(member, member.__dict__.get("_layout_box")) for member in row]
            row_top = min(member_box.y for _m, member_box in entries)
            baselines = []
            for member, member_box in entries:
                own_baseline = _element_own_baseline(member)
                baselines.append((member, member_box,
                                  own_baseline if own_baseline is not None else member_box.height))
            row_baseline = max(own for _m, _b, own in baselines)
            for member, member_box, own_baseline in baselines:
                target = row_top + (row_baseline - own_baseline)
                delta = target - member_box.y
                if abs(delta) < 0.01:
                    continue
                if _is_element(member):
                    _shift_subtree(member, 0.0, delta)
                else:
                    _shift_box(member, 0.0, delta)
        # Taffy sized this `height:auto` container from its own (wrong)
        # baseline offsets too -- a member it dropped 100px to meet a
        # sibling's baseline made the line 100px taller than the members
        # now need. With a single line of members, the container's
        # content ends where its lowest member's margin edge now does
        # (table-vertical-align-baseline-008.xht: a 200px float around a
        # 100px inline-block and a 100px inline-table).
        native = element.__dict__.get("_chromonic_native_style") or {}
        if len(rows) == 1 and native.get("height") == "auto" and len(rows[0]) == len(in_flow):
            padding = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
            content_top = box.y + box.border_top + padding[0]
            bottom = content_top
            for member in rows[0]:
                member_box = member.__dict__.get("_layout_box")
                margin = (member.__dict__.get("_chromonic_native_style") or {}).get("margin") or (0.0,) * 4
                bottom = max(bottom, member_box.y + member_box.height + _numeric_edge(margin[2]))
            border_bottom = box.height - box.client_height - box.border_top
            new_height = (bottom - content_top) + padding[0] + padding[2] + box.border_top + border_bottom
            delta = new_height - box.height
            if abs(delta) > 0.5:
                element.__dict__["_layout_box"] = dataclasses.replace(
                    box, height=new_height, client_height=box.client_height + delta)
                _shift_later_siblings_for_height_delta(element, delta)


_BASELINE_ALIGNMENTS = ("baseline", "first-baseline", "last-baseline")


def _fix_inline_float_position(node_map: dict) -> None:
    """CSS 2.1 9.5: correct the position of a float found mixed into
    running text (`elif inline_items:`'s flex-row-of-text approximation
    marks these on `element._chromonic_inline_floats`, in DOM order,
    excluded from that row's own baseline alignment). Taffy already gave
    each one a real, content-sized box, wrapped onto some row by the
    row's own flex-wrap -- treated here as a reasonable stand-in for
    "which line of text it interrupted" (its own `y`), corrected only in
    `x`: flush to the container's left/right content edge (rule 1), and
    dropped below any earlier same-container float it would otherwise
    overlap (rule 7), via a per-container running list so two floats in
    the same paragraph still stack correctly. Does not narrow the
    surrounding text around the float's own rectangle (a real "inline
    layout consults active floats" implementation is a substantially
    bigger feature -- logged in PLAN.md) -- only the float's own
    geometry is corrected."""
    for element in list(node_map.values()):
        floats = getattr(element, "_chromonic_inline_floats", None) if hasattr(element, "__dict__") else None
        if not floats:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = box.x + box.border_left + pl
        content_right = content_left + (box.client_width - pl - pr)
        active_floats: list = []
        for child in floats:
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            child_computed = child_resolved[0] if child_resolved is not None else None
            side = "left"
            if child_computed is not None:
                float_value = (getattr(child_computed, "float", None) or "").strip().lower()
                if float_value == "right":
                    side = "right"
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") or (0.0,) * 4
            mt, mr, mb, ml = (_numeric_edge(v) for v in margin)
            top = child_box.y
            top = _cleared_y(child_computed, active_floats, top)
            # Rule 7: this float's own outer top may not be higher than
            # any earlier same-container float it would otherwise
            # overlap -- dropped below the lowest blocking one, same
            # collision check `_fix_float_flow_after_block_sibling` uses
            # for block-level float siblings.
            while True:
                blocking = [a for a in active_floats
                            if a["top"] < top + child_box.height and a["bottom"] > top]
                if not blocking:
                    break
                new_top = min(a["bottom"] for a in blocking)
                if new_top <= top + 1e-6:
                    break
                top = new_top
            if side == "right":
                new_x = content_right - mr - child_box.width
                left_blocking = [a for a in active_floats if a["side"] == "left"
                                 and a["top"] < top + child_box.height and a["bottom"] > top]
                if left_blocking:
                    new_x = max(new_x, max(a["edge"] for a in left_blocking))
            else:
                new_x = content_left + ml
                right_blocking = [a for a in active_floats if a["side"] == "right"
                                  and a["top"] < top + child_box.height and a["bottom"] > top]
                if right_blocking:
                    new_x = min(new_x, min(a["edge"] for a in right_blocking) - child_box.width)
            dx, dy = new_x - child_box.x, top - child_box.y
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                _shift_subtree(child, dx, dy)
                child_box = child.__dict__["_layout_box"]
            edge = child_box.x + child_box.width if side == "left" else child_box.x
            active_floats.append({"side": side, "edge": edge, "top": child_box.y,
                                  "bottom": child_box.y + child_box.height + mb})


def _fix_flex_baseline_alignment(node_map: dict) -> None:
    """CSS Flexbox 8.3: an author `display: flex` row whose items align on
    `baseline` (`align-items`, or an item's own `align-self`). Taffy only
    knows a baseline for a measured text leaf; an item whose text lives
    further down (a `<div>` holding an `<a>`, `align-self-006.html`) gets
    its bottom edge synthesized instead, so every such item was bottom-
    aligned. Each flex line is re-aligned here on the items' real first
    (or last) baselines, and when that makes the line taller than Taffy
    made it, stretched/centred/end-aligned items in the line, later lines
    and a `height: auto` container follow."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        if not native or native.get("display") != "flex":
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS:
            continue
        computed = resolved[0]
        direction = (getattr(computed, "flexDirection", "row") or "row").strip().lower()
        if direction not in ("row", "row-reverse"):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        container_align, _safe = _alignment_parts(getattr(computed, "alignItems", "normal"))
        items = []
        children = element.__dict__.get("_chromonic_normalized_children") or _child_nodes(element)
        for child in children:
            if not _is_element(child) and not isinstance(child, _AnonymousTableBox):
                continue
            child_native = child.__dict__.get("_chromonic_native_style")
            child_box = child.__dict__.get("_layout_box")
            if child_native is None or child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            align = "auto"
            if child_resolved is not None:
                if not _renders(child_resolved[1]):
                    continue
                align, _safe = _alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
            if align == "auto":
                align = container_align
            items.append((child, align))
        if not any(align in _BASELINE_ALIGNMENTS for _child, align in items):
            continue
        # Visual order is `order`-sorted DOM order (stable), the same
        # order `build()` handed Taffy the items in.
        items.sort(key=lambda entry: _css_order((getattr(entry[0], "_chromonic_resolved_style", None) or (None,))[0]))
        # Flex lines: visual order runs along the main axis, so a line
        # breaks wherever the main-axis position turns back.
        lines: list = []
        current: list = []
        prev_x = prev_bottom = None
        for child, align in items:
            child_box = child.__dict__["_layout_box"]
            x = child_box.x
            turned = (prev_x is not None and (
                (x < prev_x - 0.01 if direction == "row" else x > prev_x + 0.01)
                or (abs(x - prev_x) <= 0.01 and child_box.y >= prev_bottom - 0.01)))
            if turned:
                lines.append(current)
                current = []
            current.append((child, align))
            prev_x = x
            prev_bottom = child_box.y + child_box.height
        if current:
            lines.append(current)
        total_delta = 0.0
        for line in lines:
            if total_delta:
                for child, _align in line:
                    _shift_subtree(child, 0.0, total_delta)
            entries = []
            for child, align in line:
                child_box = child.__dict__["_layout_box"]
                margin = (child.__dict__.get("_chromonic_native_style") or {}).get("margin") or (0.0,) * 4
                mt, mb = _numeric_edge(margin[0]), _numeric_edge(margin[2])
                entries.append((child, align, child_box, mt, mb))
            line_top = min(child_box.y - mt for _c, _a, child_box, mt, _mb in entries)
            old_bottom = max(child_box.y + child_box.height + mb for _c, _a, child_box, _mt, mb in entries)
            refs = []
            for child, align, child_box, mt, mb in entries:
                if align not in _BASELINE_ALIGNMENTS:
                    continue
                if align == "last-baseline":
                    own = _element_own_baseline(child)
                else:
                    absolute = _first_baseline(child)
                    own = None if absolute is None else absolute - child_box.y
                if own is None:
                    own = child_box.height  # no line box: synthesized from the border-box bottom
                refs.append((child, child_box, mt, mb, own))
            if not refs:
                continue
            line_baseline = max(mt + own for _c, _b, mt, _mb, own in refs)
            new_bottom = old_bottom
            for child, child_box, mt, mb, own in refs:
                new_y = line_top + line_baseline - own
                if abs(new_y - child_box.y) > 0.01:
                    _shift_subtree(child, 0.0, new_y - child_box.y)
                new_bottom = max(new_bottom, new_y + child_box.height + mb)
            delta = new_bottom - old_bottom
            if delta <= 0.01:
                continue
            for child, align, child_box, mt, mb in entries:
                if align in _BASELINE_ALIGNMENTS:
                    continue
                child_box = child.__dict__["_layout_box"]
                child_native = child.__dict__.get("_chromonic_native_style") or {}
                if align in ("stretch", "normal") and child_native.get("height") == "auto":
                    child.__dict__["_layout_box"] = dataclasses.replace(
                        child_box, height=child_box.height + delta,
                        client_height=child_box.client_height + delta)
                elif align == "center":
                    _shift_subtree(child, 0.0, delta / 2.0)
                elif align in ("flex-end", "end", "self-end"):
                    _shift_subtree(child, 0.0, delta)
            total_delta += delta
        if total_delta > 0.01 and native.get("height") == "auto":
            box = element.__dict__["_layout_box"]
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=box.height + total_delta, client_height=box.client_height + total_delta)
            _shift_later_siblings_for_height_delta(element, total_delta)


def _fix_flex_safe_alignment(node_map: dict) -> None:
    """CSS Box Alignment 3 `safe`: an alignment that would make content
    overflow its container falls back to `start` instead. Taffy has no
    overflow-position notion (`style_bridge._align_keyword` drops the
    `safe` prefix), so here, after layout, an in-flow flex item whose
    `safe`-aligned cross size exceeds its single-line container's content
    box is moved to the cross start, and a `safe` `justify-content` whose
    items overflow the main axis packs them from the main start
    (`flexbox-safe-overflow-position-001.html`)."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if (not native or native.get("display") != "flex" or resolved is None
                or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        computed = resolved[0]
        row = (getattr(computed, "flexDirection", "row") or "row").strip().lower() in ("row", "row-reverse")
        justify, justify_safe = _alignment_parts(getattr(computed, "justifyContent", "normal"))
        items_align, items_safe = _alignment_parts(getattr(computed, "alignItems", "normal"))
        pad = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_x = box.x + box.border_left + pad[3]
        content_y = box.y + box.border_top + pad[0]
        content_w = box.client_width - pad[1] - pad[3]
        content_h = box.client_height - pad[0] - pad[2]
        items = []
        for child in element.__dict__.get("_chromonic_normalized_children") or _child_nodes(element):
            if not (_is_element(child) or isinstance(child, _AnonymousTableBox)):
                continue
            child_native = child.__dict__.get("_chromonic_native_style") or {}
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            align, safe = "auto", False
            if child_resolved is not None:
                if not _renders(child_resolved[1]):
                    continue
                align, safe = _alignment_parts(getattr(child_resolved[0], "alignSelf", "auto"))
            if align == "auto":
                align, safe = items_align, items_safe
            margin = child_native.get("margin") or (0.0,) * 4
            mt, mr, mb, ml = (_numeric_edge(edge) for edge in margin)
            items.append((child, child_box, align, safe, mt, mr, mb, ml))
        if not items:
            continue
        # Cross axis, per item.
        for child, child_box, align, safe, mt, mr, mb, ml in items:
            if not safe or align in ("start", "flex-start", "self-start", "normal", "stretch", "left"):
                continue
            if row:
                if child_box.height + mt + mb > content_h + 0.01:
                    _shift_subtree(child, 0.0, content_y + mt - child_box.y)
            else:
                if child_box.width + ml + mr > content_w + 0.01:
                    _shift_subtree(child, content_x + ml - child_box.x, 0.0)
        # Main axis, whole line (single-line containers only). In a
        # `-reverse` direction `flex-start` is the physical end, so `safe
        # flex-start` overflowing also packs from the physical start
        # (flexbox-safe-overflow-position-003.html).
        reverse = (getattr(computed, "flexDirection", "row") or "row").strip().lower().endswith("-reverse")
        overflow_keywords = ("center", "end", "flex-end", "right", "space-around", "space-evenly") + (
            ("flex-start", "space-between", "normal") if reverse else ())
        if justify_safe and justify in overflow_keywords:
            if row:
                total = sum(b.width + ml + mr for _c, b, _a, _s, _mt, mr, _mb, ml in items)
                if total > content_w + 0.01:
                    cursor = content_x
                    for child, child_box, _a, _s, _mt, mr, _mb, ml in items:
                        _shift_subtree(child, cursor + ml - child_box.x, 0.0)
                        cursor += ml + child_box.width + mr
            else:
                total = sum(b.height + mt + mb for _c, b, _a, _s, mt, _mr, mb, _ml in items)
                if total > content_h + 0.01:
                    cursor = content_y
                    for child, child_box, _a, _s, mt, _mr, mb, _ml in items:
                        _shift_subtree(child, 0.0, cursor + mt - child_box.y)
                        cursor += mt + child_box.height + mb


def _fix_flex_rtl_mirroring(node_map: dict) -> None:
    """CSS Flexbox 5.1/8: in a `direction: rtl` flex container the main
    axis of a row runs right-to-left, and the cross axis of a column
    starts at the right -- both are the container's horizontal axis
    mirrored. Taffy has no writing direction, so every in-flow item's
    margin box is reflected here across the container's content box
    (flexbox-mbp-horiz-001-rtl.xhtml: the first item is flush right;
    flexbox-align-self-vert-rtl-001.xhtml: `align-self: flex-start`
    columns hug the right edge). Runs after the other flex passes so it
    mirrors their final positions; absolutely positioned children keep
    their own (already direction-aware) static position."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        native = element.__dict__.get("_chromonic_native_style")
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if (not native or native.get("display") != "flex" or resolved is None
                or getattr(resolved[1].display, "value", "") not in _FLEX_DISPLAYS):
            continue
        if _element_direction(element, resolved[0]) != "rtl":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pad = element.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_x = box.x + box.border_left + pad[3]
        content_w = box.client_width - pad[1] - pad[3]
        for child in element.__dict__.get("_chromonic_normalized_children") or _child_nodes(element):
            if not (_is_element(child) or isinstance(child, _AnonymousTableBox)):
                continue
            child_native = child.__dict__.get("_chromonic_native_style") or {}
            child_box = child.__dict__.get("_layout_box")
            if child_box is None or child_native.get("position") in ("absolute", "fixed"):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            if child_resolved is not None and not _renders(child_resolved[1]):
                continue
            margin = child_native.get("margin") or (0.0,) * 4
            ml, mr = _numeric_edge(margin[3]), _numeric_edge(margin[1])
            outer_left = child_box.x - ml
            outer_w = child_box.width + ml + mr
            new_outer_left = content_x + content_w - (outer_left - content_x) - outer_w
            dx = new_outer_left + ml - child_box.x
            if abs(dx) > 0.01:
                _shift_subtree(child, dx, 0.0)


def _apply_root_margin_offset(root_element, node_map: dict) -> None:
    """Shift the whole laid-out tree by the compute root's own margin.

    Taffy's root-compute has no parent context, so a root with `width`/
    `height:auto` correctly shrinks to leave room for its own margin but
    never offsets its own box by it -- the root always comes back at
    `(0, 0)` regardless of margin.

    `chromonic` hands Taffy `<body>` as this root, but CSS-wise `<html>`
    is the real root, and `<body>`'s own margin genuinely offsets it
    within `<html>`'s content box.

    Root-anchored `position:absolute`/`fixed` elements are excluded:
    their containing block is the viewport, unaffected by `<body>`'s
    margin. The vertical axis is skipped when `_adjust_body_collapsed_
    margins` already folded the root's top margin into its position."""
    style = getattr(root_element, "_chromonic_native_style", None)
    box = root_element.__dict__.get("_layout_box")
    if style is None or box is None:
        return
    margin_top, _margin_right, _margin_bottom, margin_left = style["margin"]
    # CSS: a percentage margin resolves against the containing block's
    # *width* on every side, vertical included -- not a typo.
    dx = _resolve_inset(margin_left, box.width) or 0.0
    already_collapsed = "_chromonic_margin_collapsed" in root_element.__dict__
    dy = 0.0 if already_collapsed else (_resolve_inset(margin_top, box.width) or 0.0)
    # `<html>`'s own padding/border offsets `<body>` within it the same
    # way `<body>`'s own margin does -- same root-anchored exclusion applies.
    html_edges = _document_element_box_edges(root_element)
    if html_edges is not None:
        html_left, _html_right, html_top, _html_bottom = html_edges
        dx += html_left
        dy += html_top
    if not dx and not dy:
        return
    root_anchored_ids: set = set()
    for element in node_map.values():
        if _is_element(element) and _is_root_anchored(element):
            root_anchored_ids.add(id(element))
    seen = set()
    for node in list(node_map.values()):
        if id(node) in seen:
            continue
        seen.add(id(node))
        owner = node if _is_element(node) else getattr(node, "parent", None)
        # A root-anchored element's own containing block is the viewport,
        # unaffected by `<body>`'s margin -- and so is everything painted
        # inside it, not just its own box, so the whole ancestor chain
        # must be checked, not just `owner` itself.
        while owner is not None:
            if id(owner) in root_anchored_ids:
                break
            owner = getattr(owner, "parentElement", None)
        else:
            _shift_box(node, dx, dy)


def layout(root_element, *, width: float, height: "float | None" = None, reuse_styles: bool = False,
           viewport_height: "float | None" = None) -> dict:
    """Build a fresh Taffy tree from `root_element` down, compute layout at
    `width` x `height` (`height=None` sizes to content), and write every
    node's box back onto its domonic element. Returns `{node_id: element}`
    for anyone (painting, hit-testing) who wants to walk the same tree
    without re-discovering it.

    This is the *whole* invalidation story for the POC: call `layout()`
    again after any mutation (`element.style.width = ...`, adding/removing
    children, ...) and every affected box is recomputed and rewritten.

    `reuse_styles=False` (the default) re-resolves every element's CSS
    from scratch. Pass `reuse_styles=True` only when nothing about any
    element's class/inline style/stylesheets could have changed since the
    last `layout()` call (see `_describe`'s docstring) -- currently only
    `native_browser.py`'s image-arrival poll qualifies.

    `viewport_height`, when given, corrects `position:absolute`/`fixed`
    elements with no positioned ancestor to resolve against the real
    viewport height rather than `root_element`'s own (possibly content-
    grown) box. Leave `None` for callers with no separate viewport-vs-
    document distinction."""
    from . import webfonts
    if webfonts.prepare_layout(root_element):
        reuse_styles = False
    tree = Tree()
    node_map: dict[int, object] = {}
    with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
        root_id = build(tree, root_element, node_map, reuse_styles=reuse_styles)
    available_width = _constrain_root_to_document_element(tree, root_element, root_id, width)
    compute_height = _root_compute_height(root_element, height, viewport_height)
    boxes = tree.compute(root_id, available_width, compute_height)
    _write_boxes(boxes, node_map)
    return _finish_layout_pass(tree, node_map, root_element, width=width, viewport_height=viewport_height)


def _scan_layout_pass_features(node_map: dict) -> dict:
    """One combined O(node count) pre-scan, collecting a handful of cheap
    presence flags that let `_finish_layout_pass` skip an entire
    downstream correction pass's own O(node count) scan outright when a
    page has none of what that pass looks for, instead of every single
    one of its ~15 passes unconditionally re-walking the whole tree even
    on the (common) pages that don't use floats, `position:absolute`/
    `fixed`, or the rarer split-inline/flex-row-approximation machinery
    at all. Each flag mirrors the *exact* marker attribute its
    corresponding pass(es) already gate every element on internally as
    their own first check -- so "this flag is False" and "that pass would
    have been a no-op anyway" are guaranteed to agree by construction;
    this changes nothing about what any pass does, only whether it's
    given the chance to do it."""
    has_split_wrapper = False
    has_flex_row_members = False
    has_float_flow = False
    has_absolute = False
    has_absolute_or_fixed = False
    has_table = False
    has_auto_horizontal_margin = False
    has_rtl = False
    for element in node_map.values():
        d = getattr(element, "__dict__", None)
        if d is None:
            continue
        if d.get("_chromonic_is_table_root"):
            has_table = True
        if d.get("_chromonic_split_wrapper_ref") is not None:
            has_split_wrapper = True
        members = d.get("_chromonic_flex_row_members")
        if members and len(members) >= 2:
            has_flex_row_members = True
        if d.get("_chromonic_float_flow_children"):
            has_float_flow = True
        style = d.get("_chromonic_native_style")
        if style is not None:
            margin = style.get("margin") or ()
            if len(margin) == 4 and (margin[1] == "auto" or margin[3] == "auto"):
                has_auto_horizontal_margin = True
            position = style.get("position")
            if position == "absolute":
                has_absolute = True
                has_absolute_or_fixed = True
            elif position == "fixed":
                has_absolute_or_fixed = True
        paint_style = d.get("_chromonic_paint_style") or {}
        if (paint_style.get("direction") or "").strip().lower() == "rtl":
            has_rtl = True
        elif getattr(element, "getAttribute", None) is not None:
            if (element.getAttribute("dir") or "").strip().lower() == "rtl":
                has_rtl = True
    return {
        "split_wrapper": has_split_wrapper,
        "flex_row_members": has_flex_row_members,
        "float_flow": has_float_flow,
        "absolute": has_absolute,
        "absolute_or_fixed": has_absolute_or_fixed,
        "table": has_table,
        "auto_horizontal_margin": has_auto_horizontal_margin,
        "rtl": has_rtl,
    }


def _finish_layout_pass(tree_obj, node_map, root_element, *, width, viewport_height):
    """The post-`tree.compute()` correction pipeline, shared by every entry
    point that computes real Taffy geometry (`layout()`, `LayoutProjection.
    layout()`/`.compute()`) -- previously duplicated verbatim across all
    three, which is how fixes wired into only one of them silently never
    ran for a real, incrementally-updated page."""
    features = _scan_layout_pass_features(node_map)
    # `_adjust_body_collapsed_margins` runs twice in this pass -- its
    # `_chromonic_scroll_extent` needs Taffy's real, uncorrected box as its
    # baseline, which the second call would otherwise only see already
    # corrected (and smaller). Stashed once, before either call.
    root_element.__dict__["_chromonic_pristine_box"] = root_element.__dict__.get("_layout_box")
    if features["split_wrapper"]:
        _fix_nested_split_flow_extent(node_map)
    _adjust_body_collapsed_margins(root_element)
    _apply_root_margin_offset(root_element, node_map)
    if features["flex_row_members"]:
        _fix_flex_row_baseline_alignment(node_map)
    _fix_inline_float_position(node_map)
    _fix_flex_baseline_alignment(node_map)
    _fix_flex_safe_alignment(node_map)
    _fix_flex_rtl_mirroring(node_map)
    if features["rtl"]:
        _fix_rtl_block_positioning(node_map)
    _fix_relative_rtl_insets(node_map)
    # Before the absolute-positioning fixups below: an inline-context
    # escapee's real static position is only known once `_InlineFormatting
    # Plan.publish()` has run -- `_fix_absolute_static_position_fallback`
    # reads `element._chromonic_static_position`, which this sets.
    _publish_inline_formatting(node_map)
    _apply_linebox_strut_height(node_map)
    _apply_empty_inline_block_min_height(node_map)
    shrink_to_fit_shifted = _fix_float_shrink_to_fit_width(tree_obj, node_map)
    if features["table"]:
        shrink_to_fit_shifted |= _fix_table_shrink_to_fit_width(tree_obj, node_map)
    # Both shrink-to-fit fixes above reposition their whole subtree via
    # `_write_boxes` (a fresh, isolated `tree.compute()`) + `_shift_subtree`
    # -- but `_write_boxes` only ever touches elements with a *real* Taffy
    # node (the block-level container itself and its Taffy children), never
    # a plain inline element whose own box instead comes from `_publish_
    # inline_formatting`'s fragment-based tracking (`_finalize_inline_owner_
    # boxes`). `_shift_subtree` still walks and shifts those elements too
    # (it recurses through every DOM child, not just real Taffy nodes),
    # double-applying the correction on top of a box that was never reset in
    # the first place. Re-publishing here recomputes every such element's
    # box fresh from its (now finally correct) container box instead of
    # shifting a stale one. Confirmed directly on a one-cell auto-width
    # `<table><tr><td><span>1.</span></td></tr></table>`: the `<span>`
    # landed one full shrink-to-fit correction below and right of where its
    # `<td>` actually ended up -- on a real page (an HTML `<table>`-based
    # site with narrow, auto-width columns), enough to push every cell's
    # inline content off past its own row entirely.
    #
    # Only worth its own O(node count) pass when a shift actually
    # happened -- gated, not unconditional, since `_finish_layout_pass`
    # also runs on every high-frequency incremental `LayoutProjection.
    # compute()` call (e.g. `examples/particles2.py`'s per-frame position
    # updates), where an unconditional second full-tree republish here
    # was a measurable, needless per-frame cost on pages with no floats
    # or auto-width tables at all.
    if shrink_to_fit_shifted:
        _publish_inline_formatting(node_map)
        # The fresh `tree.compute()` also discarded the two line-box
        # height corrections above inside the recomputed subtree
        # (empty-cells-008.xht: a table cell holding only a 0x0 image is
        # one strut tall, and so are its row and table) -- both are
        # grow-only and idempotent, so they simply run again.
        _apply_linebox_strut_height(node_map)
        _apply_empty_inline_block_min_height(node_map)
    # After the shrink-to-fit pass: that recomputes an auto-width table's
    # whole subtree from scratch (`_write_boxes`), which would discard
    # any row heights distributed before it.
    if features["table"]:
        _enforce_fixed_column_boxes(node_map)
        _settle_tables(node_map)
        _publish_table_column_boxes(node_map)
    _publish_svg_shape_boxes(node_map)
    if features["float_flow"]:
        _fix_float_flow_after_block_sibling(node_map)
        _fix_float_flow_container_auto_height(node_map)
    _fix_nested_bfc_float_auto_height(node_map)
    _resync_interruption_marker_heights(node_map)
    # A nested split wrapper's own `_layout_box` doesn't exist until
    # `_publish_inline_formatting` (just above) unions its fragments --
    # this pass's first run, before that, silently skipped every such
    # wrapper, and the interruption blocks' own boxes it reads have since
    # moved (`_apply_root_margin_offset`) -- recomputed fresh now that
    # both are finally real and final, or `_adjust_body_collapsed_margins`
    # below reads a stale, pre-offset `_chromonic_flow_extent_box`.
    if features["split_wrapper"]:
        _fix_nested_split_flow_extent(node_map)
    # Re-anchor body's own auto-height now that a float-flow BFC child's
    # height may have just shifted -- idempotent, so this re-derives it
    # from the now-final positions instead of the stale ones above.
    _adjust_body_collapsed_margins(root_element)
    if features["absolute"]:
        _fix_absolute_shrink_to_fit_extent(node_map)
        _fix_absolute_horizontal_auto_margins(node_map)
        _fix_absolute_vertical_auto_margins(node_map)
        _fix_absolute_width_against_containing_block(node_map)
        _fix_absolute_height_against_containing_block(node_map)
        _fix_absolute_static_position_fallback(node_map)
    if viewport_height is not None and features["absolute_or_fixed"]:
        _fix_viewport_anchored_positioning(node_map, viewport_height, width)
    if features["auto_horizontal_margin"]:
        _publish_used_horizontal_margins(node_map)
    return node_map
