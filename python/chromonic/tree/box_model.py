from __future__ import annotations

import math

from domonic import _fontmetrics

from .. import fonts, style_bridge, ua_style
from . import anonymous_boxes, dom, inline_formatting, table_layout




def _numeric_edge(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0



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
    if isinstance(element, anonymous_boxes._AnonymousTableBox):
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



def _establishes_containing_block(style_obj) -> bool:
    """Whether `position` makes this element a valid containing block for
    `position:absolute`/`fixed` descendants -- anything but `static`."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"



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
        # cell's text. The rows must be settled first (see `table_layout._settle_table`).
        table_layout._settle_table(element)
        for row in getattr(element, "_chromonic_table_rows", None) or ():
            row_box = row.__dict__.get("_layout_box")
            if row_box is not None:
                return table_layout._table_row_baseline(row, row_box)
        return None
    def line_baseline(owner, top, line_height):
        paint = getattr(owner, "_chromonic_paint_style", None) or getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint.get("font_size"), default=16.0)
        family = paint.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = inline_formatting._parse_font_weight(paint.get("font_weight"))
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
        if not dom._is_element(child):
            continue
        native = getattr(child, "_chromonic_native_style", None) or {}
        if native.get("position") in ("absolute", "fixed"):
            continue
        baseline = _first_baseline(child)
        if baseline is not None:
            return baseline
    return None



def _alignment_parts(value) -> "tuple[str, bool]":
    """A raw `align-*`/`justify-*` computed value as (keyword, safe) --
    `safe center` -> ("center", True), `last baseline` -> ("last-baseline",
    False), `unsafe end` -> ("end", False)."""
    parts = [part for part in (value or "").strip().lower().split()]
    safe = "safe" in parts
    parts = [part for part in parts if part not in ("safe", "unsafe")]
    return ("-".join(parts) or "normal", safe)



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
    if (dom._is_element(element) and not getattr(element, "_chromonic_has_layout_children", False)
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
        weight = inline_formatting._parse_font_weight(paint_style.get("font_weight"))
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
    if not dom._is_element(element):
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
        weight = inline_formatting._parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        resolved_line_height = inline_formatting._resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else (ascent + descent) or normal
        return ascent + math.floor((line_height - (ascent + descent)) / 2)
    return None



_BASELINE_ALIGNMENTS = ("baseline", "first-baseline", "last-baseline")
