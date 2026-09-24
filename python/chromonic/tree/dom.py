from __future__ import annotations

import re

from domonic.dom import Element
from domonic.layout import LayoutStyle
from domonic.style import ComputedStyleDeclaration

from .. import fonts
from . import anonymous_boxes, box_model, inline_formatting



ELEMENT_NODE = 1

TEXT_NODE = 3



def _layout_parent(node):
    """`node`'s parent *box* -- its anonymous table wrapper when CSS 2.1
    17.2.1 generated one around it this pass, else its real DOM parent."""
    anonymous = node.__dict__.get("_chromonic_anonymous_parent") if hasattr(node, "__dict__") else None
    return anonymous if anonymous is not None else getattr(node, "parentElement", None)



class _PseudoElement:
    """A ::before/::after generated box -- not a real DOM node, just enough
    surface for build()/paint.py to treat it like a childless element.
    Never appears in real childNodes -- reached only via
    `_inline_mixed_content`'s synthesized items and _chromonic_inline_fragments."""

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


# Metadata/logic tags a real browser hardcodes as never painting a box;
# domonic's cascade gives these no default of its own. colgroup/col (CSS
# 2.1 17.2.1: "are not rendered") belong here too -- without it they fell
# through to ordinary block treatment, corrupting header/body/footer
# reordering (17.5.3) badly enough that thead landed above the caption.
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
    """CSS direction's real used value for `element`. An explicit author
    CSS declaration (inherited normally by domonic's own cascade) always
    wins. But domonic's cascade has no UA-stylesheet mapping for HTML's
    dir attribute at all (every real browser has `[dir=rtl] {
    direction:rtl }`), and no attribute-selector support to add one via
    `ua_style.py` -- so a plain `<section dir="rtl">` with no CSS direction
    resolves ltr here, silently wrong. Falls back to walking the DOM for
    the attribute directly only when the cascade resolved plain ltr, which
    can't shadow a real author declaration except the rare
    self-contradictory case of also writing `direction:ltr` on a
    `dir="rtl"` attribute."""
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



def _extract_paint_style(computed) -> dict:
    """The handful of paint-only properties paint.py needs, read out of
    `computed` once and cached as plain strings -- a ComputedStyleDeclaration
    attribute access re-resolves from the underlying style text every time,
    so without this every repaint re-parses every element's colours/fonts."""
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
        "border_right_color": computed.getPropertyValue("border-right-color"),
        "border_bottom_color": computed.getPropertyValue("border-bottom-color"),
        "border_left_color": computed.getPropertyValue("border-left-color"),
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

    # domonic's own rule index (built the moment any element's real
    # ComputedStyleDeclaration resolves, always true here since _describe()
    # already resolved element's own style) tracks which pseudo-element
    # names any selector in the document's stylesheets targets --
    # `_build_rule_index` in domonic/style.py. Most pages never author a
    # ::before/::after rule, so when neither name is in that set, skip
    # building two full ComputedStyleDeclarations just to learn content is
    # normal on both. Safe for the rest of this pass: chain_cache is fresh
    # per pass and nothing mutates the DOM/stylesheets mid-pass. If the
    # entry isn't populated yet, fall through to the full resolution below.
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
    ComputedStyleDeclaration and its extracted paint style on the element
    itself so paint.py's later walk, and a repaint with no relayout in
    between, don't re-resolve either.

    reuse_styles=True skips resolving CSS entirely if a prior resolution
    (element._chromonic_resolved_style) exists, reusing it as-is -- CSS
    resolution, not Taffy, dominates relayout cost, so a relayout
    triggered by something that can't have changed any element's
    class/inline-style/stylesheets (an image arriving, a same-bucket
    resize) passes this to skip it.

    computed_cache holds our own (computed, style_obj) tuples keyed by
    id(element), kept separate from domonic's own _chain_cache (which
    expects bare ComputedStyleDeclaration values) under a nested dict."""
    cache = {} if computed_cache is None else computed_cache
    cached = cache.get(id(element))
    if cached is not None:
        return cached

    synthetic = element.__dict__.get("_chromonic_synthetic_style") if hasattr(element, "__dict__") else None
    if synthetic is not None:
        # An anonymous table box (_AnonymousTableBox): no cascade to run,
        # its style was synthesized from its parent's when generated.
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
    # domonic's _parent_computed() only reads chain_cache, never registers
    # itself in it -- without this, a child resolved right after element
    # would build a second, separate ComputedStyleDeclaration for it.
    chain_cache[id(element)] = computed
    style_obj = LayoutStyle.from_computed(computed)
    element._chromonic_computed_style = computed
    element._chromonic_paint_style = _extract_paint_style(computed)
    (element._chromonic_before_text, element._chromonic_after_text,
     element._chromonic_before_pseudo, element._chromonic_after_pseudo) = _extract_generated_content(element, cache)
    from .. import webfonts
    webfonts.resolve_style(element, element._chromonic_paint_style)
    # Give Parley and Skia the same platform choice for CSS monospace.
    family = element._chromonic_paint_style["font_family"]
    if family and family.strip().lower() in ("monospace", "ui-monospace"):
        element._chromonic_paint_style["font_family"] = fonts._GENERIC_FAMILIES[family.strip().lower()]
    result = (computed, style_obj)
    element._chromonic_resolved_style = result
    cache[id(element)] = result
    return result



#: CSS 2.1 17.2.1: table-column/table-column-group "are not rendered" --
#: no box at all, same as display:none, whether via a literal
#: colgroup/col tag (`_NON_RENDERING_TAGS`) or an arbitrary element authored
#: with one of these display values directly (`_renders` covers that case).
_NON_RENDERING_DISPLAYS = frozenset({"table-column", "table-column-group"})



def _renders(style_obj) -> bool:
    """Whether an element already known to be an ordinary rendering tag (see
    `_NON_RENDERING_TAGS`) should still be walked into the Taffy tree --
    false for display:none, or table-column/table-column-group (see
    `_NON_RENDERING_DISPLAYS`), unless absolutely positioned. CSS 2.1 9.7
    blockifies display for any position:absolute/fixed box regardless of
    its specified value -- an author-written display:table-column-group
    on an absolutely positioned element computes to block, an ordinary
    rendered box, not the 17.2.1 "not rendered" rule -- confirmed on
    bottom-applies-to-005.xht. The float-blockified case 9.7 also covers
    isn't handled here -- would need `computed`, not just `style_obj`."""
    display = style_obj.display
    value = getattr(display, "value", display)
    if value == "none":
        return False
    if value in _NON_RENDERING_DISPLAYS:
        return box_model._is_absolutely_positioned(style_obj)
    return True



def _child_elements(element, computed_cache=None, *, reuse_styles=False) -> list:
    """`[(child, computed, style_obj), ...]` for children that should
    render -- each child's style computed once here, then handed straight
    to the recursive build() call instead of being recomputed there.
    Anonymous table/block boxes (CSS 2.1 17.2.1/9.2.1.1) appear here in
    place of the nodes they wrap -- see `anonymous_boxes._normalized_child_nodes`."""
    result = []
    if computed_cache is None:
        computed_cache = {}
    # <details> without `open` renders only its first <summary> -- the rest
    # of its content is the disclosed part, hidden until toggled.
    is_closed_details = (
        (getattr(element, "tagName", "") or "").lower() == "details"
        and not getattr(element, "open", False)
    )
    seen_summary = False
    for child in anonymous_boxes._normalized_child_nodes(
        element, computed_cache, reuse_styles=reuse_styles,
    ):
        if not _is_element(child):
            continue
        if (getattr(child, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
            continue
        if is_closed_details:
            if (getattr(child, "tagName", "") or "").lower() == "summary" and not seen_summary:
                seen_summary = True
            else:
                _clear_stale_layout_geometry(child)
                continue
        computed, style_obj = _describe(child, computed_cache, reuse_styles=reuse_styles)
        if _renders(style_obj):
            result.append((child, computed, style_obj))
        else:
            _clear_stale_layout_geometry(child)
    return result



def _clear_stale_layout_geometry(element) -> None:
    """`element` just resolved to display:none -- clear its (and its
    subtree's) _layout_box/_chromonic_inline_fragments rather than leaving
    them from whenever it last rendered, or paint_tree/hit-testing/
    getBoundingClientRect() (which walk the real DOM, not node_map) keep
    reading it as still having a box."""
    element.__dict__.pop("_layout_box", None)
    element.__dict__.pop("_chromonic_inline_fragments", None)
    for child in _child_nodes(element):
        if _is_element(child):
            _clear_stale_layout_geometry(child)



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
    # not raw .textContent, since a "childless" element can still have a
    # <style>/<script> descendant whose raw source text isn't real prose.
    text = (
        getattr(element, "_chromonic_before_text", "")
        + _rendering_text_content(element)
        + getattr(element, "_chromonic_after_text", "")
    )
    style = element.__dict__.get("_chromonic_paint_style", {})
    text = _apply_text_transform(text, style.get("text_transform"))
    if style.get("white_space") in ("pre", "pre-wrap", "break-spaces"):
        return text
    return inline_formatting._CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", text).strip(inline_formatting._CSS_WHITESPACE_STRIP_CHARS)



def _collapsed_text_node(node) -> str:
    raw = getattr(node, "textContent", None)
    if raw is None:
        raw = getattr(node, "data", "")
    if not raw:
        return ""
    return inline_formatting._CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw).strip(inline_formatting._CSS_WHITESPACE_STRIP_CHARS)



def _rendering_text_content(element) -> str:
    """element.textContent, but skipping any descendant subtree rooted at a
    `_NON_RENDERING_TAGS` tag -- plain .textContent includes their raw
    source text verbatim, which is never actually rendered.

    No childNodes at all (e.g. a _PseudoElement's generated-content text)
    falls back to element.textContent directly."""
    child_nodes = getattr(element, "childNodes", None)
    if not child_nodes:
        return getattr(element, "textContent", None) or ""
    parts = []

    def walk(node):
        # A plain-string child (domonic's programmatic constructors never
        # wrap one in a Text node) has no nodeType at all, so it must be
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


# Tags with their own dedicated `build()` branch that must always run --
# see the `has_pseudo` check that uses this, right before `inline_items` is
# computed.
_NO_GENERATED_CONTENT_TAGS = frozenset({
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select",
})
