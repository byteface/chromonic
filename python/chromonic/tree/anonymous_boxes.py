from __future__ import annotations

import dataclasses
import re

from domonic.layout import AUTO, Edges, Keyword, Length

from . import box_model, dom, inline_formatting




class _AnonymousTextFragment:
    """Retained layout/paint projection for a direct DOM text node."""

    def __init__(self, source, parent):
        self.source = source
        self.parent = parent
        self.childNodes = []
        self.nodeType = dom.TEXT_NODE
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
    """A CSS 2.1 17.2.1 "missing" table box -- an anonymous table/
    inline-table, table-row or table-cell generated around misparented
    table content. Not a DOM node: never in anyone's childNodes (a
    wrapped node's real parentElement is untouched -- `_layout_parent`
    follows _chromonic_anonymous_parent instead), reached only through
    `_normalized_child_nodes`. Carries the same tagName a real table part
    would so every tag-based check treats it as one, and a synthetic
    style (_chromonic_synthetic_style, see dom._describe) instead of a cascade."""

    nodeType = dom.ELEMENT_NODE
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
    """Which CSS 2.1 17.2.1 table box `node` generates, if any: "table",
    "row-group", "row", "cell", "caption", "column", "column-group" -- or
    None for a text node or any other box. An absolutely/fixed positioned
    element blockifies (CSS 2.1 9.7) and is never a table part."""
    if not dom._is_element(node):
        return None
    if isinstance(node, _AnonymousTableBox):
        return {"table": "table", "inline-table": "table", "row": "row", "cell": "cell"}.get(node.kind)
    tag = (getattr(node, "tagName", "") or "").lower()
    if tag in dom._NON_RENDERING_TAGS and tag not in ("col", "colgroup"):
        return None
    computed, style_obj = dom._describe(node, computed_cache)
    display = (getattr(computed, "display", "") or "").strip().lower()
    kind = _TABLE_PART_DISPLAYS.get(display) or _TABLE_PART_TAGS.get(tag)
    if box_model._is_absolutely_positioned(style_obj):
        # CSS 2.1 9.7 blockifies display: table/inline-table stay a table
        # (an absolutely positioned table keeps its rows --
        # top-applies-to-013.xht); every internal part becomes a plain block.
        return "table" if kind == "table" else None
    return kind



def _synthesize_anonymous_style(box: "_AnonymousTableBox", parent, computed_cache) -> None:
    parent_computed, parent_style = dom._describe(parent, computed_cache)
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
    """element.childNodes, with CSS 2.1 17.2.1's missing anonymous table
    boxes generated (_AnonymousTableBox, cached on element by kind and
    first wrapped node, so a retained LayoutProjection sees the same box
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
    nodes = list(dom._child_nodes(element))
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
        return getattr(node, "nodeType", None) == dom.TEXT_NODE and not dom._collapsed_text_node(node).strip()

    def renders(node) -> bool:
        if not dom._is_element(node):
            return getattr(node, "nodeType", None) == dom.TEXT_NODE
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag in dom._NON_RENDERING_TAGS:
            return False
        return dom._renders(dom._describe(node, computed_cache)[1])

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
    # still takes the anonymous boxes CSS 2.1 17.2.1 gives any proper
    # table child there -- a display:table-column div inside a row
    # becomes an anonymous cell holding an anonymous table with that one
    # column -- empty-cells-applies-to-012.xht.
    def out_of_flow(node) -> bool:
        # CSS 2.1 9.7: an absolutely positioned child of a table part
        # blockifies and leaves the table's flow -- never wrapped in an
        # anonymous cell -- top-applies-to-001.xht.
        return (dom._is_element(node) and not isinstance(node, _AnonymousTableBox)
                and box_model._is_absolutely_positioned(dom._describe(node, computed_cache)[1]))

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
        parent_style = dom._describe(element, computed_cache)[1] if not isinstance(element, _AnonymousTableBox) else None
        wrap = "inline-table" if (parent_style is not None and box_model._is_inline_level(element, parent_style)) else "table"
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
        # A column/column group generates no box of its own (dom._renders
        # says no) but still belongs inside the table its columns describe
        # -- it must travel into the anonymous table with the rows it sits
        # among, or that table has no columns at all.
        if out_of_flow(node) or (
                not renders(node) and _table_part_kind(node, computed_cache) not in ("column", "column-group")):
            result.append(node)
            continue
        if is_blank_text(node):
            # Whitespace that is a direct child of a table, row group or
            # row is dropped outright (CSS 2.1 17.2.1) -- even between two
            # inline spans sharing one anonymous cell, Chrome renders no
            # space at all -- table-anonymous-objects-085.xht. Inside an
            # ordinary container it stays part of whatever run it sits in.
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
    blocks -- `<div>Hello <p>para</p> world</div>` is three stacked blocks.
    Previously the loose text was silently dropped, which is also why
    wpt/css/CSS2/tables/table-anonymous-objects-093.xht's leading body
    text pushed nothing down. Out-of-flow children (floats, absolutely
    positioned boxes, <br>) stay inside the run they sit in.

    A flex/grid container wraps only runs of text (each element child is
    already its own item; a text run becomes an anonymous item, CSS
    Flexbox 4). An inline element is left alone entirely -- an in-flow
    block inside an inline is 9.2.1.1's other rule,
    `_split_inline_flow_around_blocks`'s job."""
    if not nodes or isinstance(element, _AnonymousTableBox) and element.kind != "cell":
        return nodes
    if _table_part_kind(element, computed_cache) in ("table", "row-group", "row"):
        return nodes
    computed, style_obj = dom._describe(element, computed_cache)
    if box_model._is_inline_level(element, style_obj):
        return nodes
    display = (getattr(computed, "display", "") or "").strip().lower()
    flex_or_grid = display in ("flex", "inline-flex", "grid", "inline-grid")

    def classify(node) -> str:
        if not dom._is_element(node):
            if getattr(node, "nodeType", None) != dom.TEXT_NODE:
                return "skip"
            # Only CSS white space is "blank": Python's strip() also eats
            # U+00A0, but a &nbsp; text node between flex items is a real
            # anonymous item -- css-box-justify-content.html.
            if flex_or_grid:
                # CSS Flexbox 4: a text run that's purely white space never
                # becomes an anonymous flex item, even under white-space:pre
                # -- flexbox-whitespace-handling-001a.xhtml.
                raw = getattr(node, "textContent", None) or getattr(node, "data", "") or ""
                return "text" if raw.strip(inline_formatting._CSS_WHITESPACE_STRIP_CHARS) else "blank"
            return "text" if dom._collapsed_text_node(node).strip(inline_formatting._CSS_WHITESPACE_STRIP_CHARS) else "blank"
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag in dom._NON_RENDERING_TAGS:
            return "skip"
        child_computed, child_style = dom._describe(node, computed_cache)
        if not dom._renders(child_style):
            return "skip"
        if flex_or_grid:
            return "block"
        if (tag == "br" or box_model._is_inline_level(node, child_style) or box_model._is_absolutely_positioned(child_style)
                or box_model._is_floated(child_computed)):
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
    """element.childNodes as the layout tree actually sees them: CSS 2.1
    17.2.1's anonymous table boxes (`_wrap_missing_table_boxes`) and then
    9.2.1.1's anonymous block boxes (`_wrap_inline_runs`) generated around
    the nodes that need them. Remembered on the element for
    `_inline_mixed_content`, which walks the same list."""
    # reuse_styles=True is reserved for passes where neither DOM structure
    # nor CSS can have changed (currently image-intrinsic relayouts), so
    # the anonymous table/block projection is identical too -- reusing the
    # prior list avoids reclassifying every child on an unchanged DOM.
    if reuse_styles and hasattr(element, "__dict__"):
        cached = element.__dict__.get("_chromonic_normalized_children")
        if cached is not None:
            return cached
    nodes = _wrap_inline_runs(element, _wrap_missing_table_boxes(element, computed_cache), computed_cache)
    if hasattr(element, "__dict__"):
        element.__dict__["_chromonic_normalized_children"] = nodes
    return nodes
