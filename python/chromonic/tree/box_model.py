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
    # Every real HTML UA stylesheet gives these display:inline by default
    # too -- missing here left them untrusted by `_trusts_computed_inline`,
    # so `_is_inline_level` said False regardless of computed style,
    # dropping a mixed image+text container out of inline flow entirely --
    # wpt/css/CSS2/visudet/replaced-elements-width-40.html.
    "img", "canvas", "svg", "svg:svg", "iframe",
})


# Replaced elements and form controls size themselves from authored
# `width`/`height` even at `display:inline` -- unlike an ordinary inline
# element, whose box is purely a function of its content.
_REPLACED_OR_CONTROL_TAGS = frozenset({
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select", "button", "iframe", "video",
})



_LEGACY_TEXT_ALIGN = {
    "-webkit-center": "center", "-moz-center": "center",
    "-webkit-right": "right", "-moz-right": "right",
    "-webkit-left": "left", "-moz-left": "left",
}


def _text_align(value) -> str:
    """A computed `text-align` for text: the legacy `-webkit-center` family
    (HTML's `<center>` and `align=` attributes) aligns text exactly like
    `center`/`right`/`left`; the extra it does -- also aligning narrower
    child blocks -- is Taffy's block layout (`style["text_align"]`)."""
    text = (value or "").strip().lower()
    return _LEGACY_TEXT_ALIGN.get(text, text)


def _ua_stylesheet_applied(element) -> bool:
    """Whether ua_style.apply() ran on element's document -- cached per
    document (checked once per element, on the hot build() path)."""
    document = getattr(element, "ownerDocument", None)
    if document is None:
        return False
    cached = getattr(document, "_chromonic_ua_applied_cache", None)
    if cached is None:
        cached = document.querySelector("style[data-chromonic-ua]") is not None
        document._chromonic_ua_applied_cache = cached
    return cached



def _trusts_computed_inline(element, tag_name: str) -> bool:
    """Whether a computed display:inline/inline-block on element can be
    trusted as real author intent rather than domonic's un-cascaded
    default -- true for a tag assumed usually-inline, or one ua_style.py
    gives an explicit block default when that stylesheet actually ran."""
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
        # An atomic inline-level flex/grid container --
        # flex-inline.html. No tag gate: no UA default ever computes to
        # these, so the value is unambiguous author intent.
        return True
    if value not in ("inline", "inline-block", "inline-table"):
        return False
    tag_name = (getattr(element, "tagName", "") or "").lower()
    return _trusts_computed_inline(element, tag_name)



def _is_floated(child_computed) -> bool:
    """Whether an author explicitly gave this element float:left/right --
    unlike display, float's initial value is always none regardless of
    tag, so any non-none value is unambiguous author intent, no tag gate
    needed (contrast `_is_inline_level`)."""
    float_value = getattr(child_computed, "float", None)
    return isinstance(float_value, str) and float_value.strip().lower() in ("left", "right")



def _is_absolutely_positioned(style_obj) -> bool:
    position = style_obj.position
    return getattr(position, "value", position) in ("absolute", "fixed")



def _establishes_bfc(computed) -> bool:
    """CSS 2.1 9.4.1: whether this box establishes its own new block
    formatting context -- float/absolute/fixed positioning/flow-root/
    inline-block/table-cell/table-caption, or any non-visible overflow.
    Plain overflow:visible must NOT establish one -- only a real BFC box
    avoids a float; an ordinary block's border box may extend behind one."""
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
    """Whether position makes this element a valid containing block for
    position:absolute/fixed descendants -- anything but static."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"



def _alignment_parts(value) -> "tuple[str, bool]":
    """A raw align-*/justify-* computed value as (keyword, safe) -- "safe
    center" -> ("center", True), "last baseline" -> ("last-baseline",
    False), "unsafe end" -> ("end", False)."""
    parts = [part for part in (value or "").strip().lower().split()]
    safe = "safe" in parts
    parts = [part for part in parts if part not in ("safe", "unsafe")]
    return ("-".join(parts) or "normal", safe)



_BASELINE_ALIGNMENTS = ("baseline", "first-baseline", "last-baseline")
