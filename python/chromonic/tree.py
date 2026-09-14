"""Walk a live domonic DOM, build a mirroring Taffy tree, run layout, and
write the resulting geometry straight back onto the domonic elements via
`element.set_layout_box(...)` -- the domonic 1.8.0 API that
`getBoundingClientRect`/`clientWidth`/`offsetWidth`/etc. already consult.

Node identity: a plain `dict[int (Taffy node id), Element]` built while
walking (see PLAN.md). Invalidation: POC-scope means "rebuild the whole
thing" -- `layout()` below is meant to be called again, in full, after any
mutation. That is the entire "invalidation strategy": there isn't a dirty-bit
system here, on purpose (see PLAN.md).

Performance note (found stress-testing `examples/particles.py`): computing
an element's cascaded style is the dominant cost of a full relayout, by a
wide margin over Taffy itself -- profiling a few hundred moving elements
showed most wall-clock time inside domonic's `ComputedStyleDeclaration`
construction and CSS-text (re)parsing, not in the Rust layout call. This
file used to ask for that twice per element (once to check `display` before
deciding whether to descend, once more to build the Taffy style dict), and
`paint.py` asked a third time to read paint-only properties (background/
border/text colour). All three now share exactly **one**
`ComputedStyleDeclaration` per element per pass (see `_describe` below) --
built by reaching into `domonic.layout.LayoutStyle._from_computed` directly
(the one actual implementation behind the public `layout_style()`, which
otherwise always builds its own fresh `ComputedStyleDeclaration` internally
and gives a caller no way to reuse one it already has). That reach into a
non-public method is exactly the kind of gap this repo logs as a domonic
wrinkle -- see `docs/domonic-wrinkles.md` #13: a `layout_style(element,
computed=...)` overload, or a `LayoutStyle.from_computed` public alias,
would let a caller like this drop the workaround entirely.
"""

from __future__ import annotations

import functools
import re
import math

import skia

from domonic import _fontmetrics
from domonic import bs4 as domonic_bs4
from domonic.dom import Element
from domonic.layout import LayoutBox, LayoutStyle
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from . import fonts, style_bridge, ua_style
from ._native import Tree, layout_text

# `Utils.case_kebab` (camelCase/snake_case -> kebab-case, via two regex
# substitutions) backs every `ComputedStyleDeclaration` property read
# (`getPropertyValue` -> `_css_property_name` -> `case_kebab`), and is a
# pure function of its input string -- the *same* handful of property names
# ("backgroundColor", "fontSize", ...) get re-run through those regexes on
# every single access, for every element, every relayout. Profiling
# `examples/particles.py` at 1,000 particles found this the single largest
# cost after `ComputedStyleDeclaration` construction itself: 390k calls,
# ~2s cumulative out of a ~6.7s/10-tick run. Memoizing it here (once, at
# import time, process-wide) cut a 1,000-particle `tick()` from ~194ms to
# ~126ms -- measured, not assumed. Not a `tree.py`-only fix: this speeds up
# every `ComputedStyleDeclaration` property access anywhere in the process.
# Safe because `case_kebab` has no side effects and is a pure string
# transform -- same input always gives the same output. Worked around here
# rather than patched in domonic's own source, per this repo's convention
# of stress-testing domonic rather than editing its tree directly.
Utils.case_kebab = staticmethod(functools.lru_cache(maxsize=2048)(Utils.case_kebab))

# A stylesheet's selectors are tested against many elements, but domonic's
# matcher parses their text again for every test.  These parsers are pure: the
# returned structures describe selector text and the matchers only read them.
# Cache them at the renderer boundary so a page pays the parse cost once per
# distinct selector rather than once per candidate element.
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


class _InlineFormattingPlan:
    """Measured shared line boxes for one block's mixed inline contents."""

    def __init__(self, element, runs, parent_style):
        self.element = element
        self.runs = runs
        self.parent_style = parent_style
        self.fragments = []
        self.owner_boxes = {}
        self.height = 0.0

    def measure(self, available_width, _available_height):
        width = float(available_width or 0.0)
        if width <= 0 or width > 1_000_000:
            width = sum(run["intrinsic_width"] for run in self.runs)
        base_height = _resolved_line_height(self.parent_style["line_height"])
        base_font = _fontmetrics.parse_length(self.parent_style["font_size"], default=16.0)
        base_ascent, base_descent, normal = fonts.text_metrics(
            self.parent_style["font_family"], base_font,
            _parse_font_weight(self.parent_style["font_weight"]) >= 600,
            fonts.is_italic(self.parent_style["font_style"]))
        base_height = base_height or normal
        base_above = base_ascent + math.floor((base_height - base_ascent - base_descent) / 2)
        base_below = base_height - base_above
        x = y = 0.0
        above, below = base_above, base_below
        self._line_baselines = {}
        placed = []
        for run_index, run in enumerate(self.runs):
            tokens = run["tokens"]
            for index, (text, token_width) in enumerate(tokens):
                leading = run["leading"] if index == 0 else 0.0
                trailing = run["trailing"] if index == len(tokens) - 1 else 0.0
                advance_width = (max(token_width, run["atomic_width"])
                                 if len(tokens) == 1 else token_width)
                total = leading + advance_width + trailing
                fit_total = total - (run["space_width"] if text[-1:].isspace() else 0.0)
                following_space = 0.0
                if index == len(tokens) - 1 and run["owner"] is not self.element:
                    for later in self.runs[run_index + 1:]:
                        if later["tokens"]:
                            if not later["tokens"][0][0].strip():
                                following_space = later["tokens"][0][1]
                            break
                if x and x + fit_total + following_space > width and text.strip():
                    self._line_baselines[y] = above
                    y += above + below
                    x = 0.0
                    above, below = base_above, base_below
                token_height = run["box_height"]
                above = max(above, run["above"])
                below = max(below, run["below"])
                placed.append((run, text, x + leading, y, token_width, token_height,
                               leading, trailing, advance_width))
                x += total
        self._line_baselines[y] = above
        self.height = y + above + below if placed else 0.0
        self._placed = placed
        return (min(width, max((px + advance for _r, _t, px, _y, _pw, _h, _l, _tr, advance in placed), default=0.0)),
                self.height)

    def publish(self, box, padding):
        origin_x = box.x + box.border_left + padding[3]
        origin_y = box.y + box.border_top + padding[0]
        self.fragments = []
        owner_rects = {}
        grouped = {}
        placed = getattr(self, "_placed", ())
        for placed_index, (run, text, x, y, width, token_height, leading, trailing, advance) in enumerate(placed):
            ends_line = placed_index + 1 == len(placed) or placed[placed_index + 1][3] != y
            collapsed_space = (run["space_width"]
                               if text[-1:].isspace() and ends_line else 0.0)
            visual_width = max(0.0, width - collapsed_space)
            visual_advance = max(0.0, advance - collapsed_space)
            font_size = run["font_size"]
            glyph_height = min(token_height, run["glyph_height"])
            glyph_y = y + self._line_baselines[y] - run["ascent"]
            key = (id(run["source"]), y)
            entry = grouped.get(key)
            if entry is None:
                fragment = _AnonymousTextFragment(run["source"], self.element)
                fragment.owner = run["owner"]
                fragment._chromonic_paint_style = run["paint_style"]
                fragment._chromonic_text_lines = [text]
                fragment._chromonic_text_line_widths = [visual_width]
                fragment._chromonic_line_height = glyph_height
                fragment._layout_box = LayoutBox(
                    x=origin_x + x, y=origin_y + glyph_y,
                    width=visual_width, height=glyph_height,
                    client_width=visual_width, client_height=glyph_height,
                )
                grouped[key] = fragment
                self.fragments.append(fragment)
            else:
                entry._chromonic_text_lines[0] += text
                old = entry._layout_box
                combined_width = origin_x + x + visual_width - old.x
                entry._chromonic_text_line_widths[0] = combined_width
                entry._layout_box = LayoutBox(
                    x=old.x, y=old.y, width=combined_width, height=old.height,
                    client_width=combined_width, client_height=old.client_height,
                )
            owner = run["owner"]
            owner_y = origin_y + (y if run["atomic_width"] else glyph_y - run["top_edge"])
            rect = (origin_x + x - leading, owner_y,
                    visual_advance + leading + trailing, token_height)
            owner_rects.setdefault(owner, []).append(rect)
        for owner, rects in owner_rects.items():
            if owner is self.element:
                continue
            merged = []
            for rect in rects:
                if merged and abs(merged[-1][1] - rect[1]) < 0.01:
                    previous = merged[-1]
                    merged[-1] = (previous[0], previous[1],
                                  rect[0] + rect[2] - previous[0],
                                  max(previous[3], rect[3]))
                else:
                    merged.append(rect)
            rects = merged
            left = min(r[0] for r in rects); top = min(r[1] for r in rects)
            right = max(r[0] + r[2] for r in rects); bottom = max(r[1] + r[3] for r in rects)
            owner.__dict__["_chromonic_inline_boxes"] = rects
            owner.__dict__["_layout_box"] = LayoutBox(
                x=left, y=top, width=right-left, height=bottom-top,
                client_width=right-left, client_height=bottom-top,
            )
            owner._chromonic_has_layout_children = True
            owner._chromonic_owned_fragments = [
                fragment for fragment in self.fragments if fragment.owner is owner
            ]
        self.element._chromonic_inline_fragments = self.fragments

# Tags that never paint a box of their own, real-page metadata/logic rather
# than content. domonic's cascade has no UA stylesheet giving these
# `display: none` by default (confirmed: `ComputedStyleDeclaration` reports
# plain `inline` for `head`/`title`/`style`/`script`, same as any unknown
# tag) -- a real browser hardcodes exactly this exclusion in its own UA
# stylesheet, so chromonic does the same rather than laying out and painting a
# `<script>` element's source text as if it were a paragraph. Logged
# alongside wrinkle #10 in docs/domonic-wrinkles.md.
_NON_RENDERING_TAGS = frozenset({"script", "style", "head", "title", "meta", "link", "noscript", "template"})


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _extract_paint_style(computed) -> dict:
    """The handful of paint-only properties `paint.py` needs (background/
    border/text colour, font size/weight/style/family), read out of
    `computed` exactly once here and cached as plain strings.

    Profiling `paint_tree()` in isolation (see PLAN.md's "Phase 9" perf
    notes) found this a real, avoidable cost: a `ComputedStyleDeclaration`
    attribute access re-resolves its value from the underlying style text
    on *every single access* -- domonic caches nothing on the object itself
    -- so without this, every repaint (not just every relayout) re-parses
    every element's colours and font properties from scratch. That matters
    whenever painting runs more often than layout does, which is
    `native_browser.py`'s entire scroll/expose story ("scroll/expose only
    paint", never relayout) -- a page that never changes still re-resolved
    every element's style on every single scroll event before this."""
    raw = computed._resolved.get

    def raw_or_computed(name: str, attribute: str) -> str:
        value = raw(name)
        # Custom properties still need element-specific expansion.
        return getattr(computed, attribute) if value and "var(" in value else value

    return {
        "background_color": computed.backgroundColor,
        "background_image": computed.backgroundImage,
        "border_top_color": computed.borderTopColor,
        "color": computed.color,
        "font_size": computed.fontSize,
        # These three have no used-value conversion in getPropertyValue;
        # _ResolvedView already supplies inheritance and initial values.
        "font_weight": raw_or_computed("font-weight", "fontWeight"),
        "font_style": raw_or_computed("font-style", "fontStyle"),
        "font_family": raw_or_computed("font-family", "fontFamily"),
        # not read by paint.py itself -- included so _make_measure can work
        # entirely from this one already-extracted dict (see its docstring)
        # rather than touching `computed` again on a `reuse_styles=True` pass.
        "letter_spacing": computed.letterSpacing,
        "word_spacing": computed.wordSpacing,
        "line_height": computed.lineHeight,
        "white_space": computed.whiteSpace,
        "text_align": computed.textAlign,
        "text_transform": computed.textTransform,
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


def _extract_generated_content(element, computed_cache) -> "tuple[str, str]":
    before = ComputedStyleDeclaration(
        element, "::before", _chain_cache=computed_cache.setdefault("_chromonic_chain_cache", {})
    )
    after = ComputedStyleDeclaration(
        element, "::after", _chain_cache=computed_cache.setdefault("_chromonic_chain_cache", {})
    )
    return (
        _css_generated_content_text(before.content),
        _css_generated_content_text(after.content),
    )


def _describe(element, computed_cache=None, *, reuse_styles=False):
    """`(ComputedStyleDeclaration, LayoutStyle)` for `element`. Stashes the
    `ComputedStyleDeclaration` on the element itself (`_chromonic_computed_style`)
    so `paint.py`'s later walk of the same tree -- reading paint-only
    properties `LayoutStyle` deliberately excludes -- doesn't build a second
    one from scratch, *and* the specific properties paint actually reads off
    it (`_chromonic_paint_style`, see `_extract_paint_style`) so a repaint with
    no relayout in between doesn't re-resolve them either. Both overwritten
    on every *fresh* resolution, so they reflect the most recent one, the
    same lifetime `_chromonic_padding` below already has.

    `reuse_styles=True` skips resolving CSS for this element at all if a
    prior resolution (`element._chromonic_resolved_style`) already exists,
    reusing it as-is. **Real, measured need, not a hypothetical one**:
    profiling a relayout of a real page (`bbc.co.uk`, ~4,600 elements) found
    a *single* `tree.layout()` call taking **6.7 seconds** -- almost all of
    it inside domonic's `ComputedStyleDeclaration`/`_collect_author_declarations`,
    which re-parses every candidate CSS selector's text from scratch against
    every element it's tested against, with no per-selector or per-property
    caching at all (a real, serious domonic performance gap -- logged as
    `docs/domonic-wrinkles.md` #16). A background `<img>` arriving (or a
    scroll, or a resize with the same viewport bucket) never changes *any*
    element's class/inline-style/stylesheets -- there is nothing for a fresh
    CSS resolution to find that a cached one wouldn't already have -- so
    `native_browser.py`'s image-triggered relayouts pass `reuse_styles=True`
    and skip that 6.7s entirely; a real DOM mutation (a click handler
    changing a class) still needs a fresh resolution and does not.

    `computed_cache` holds our own `(computed, style_obj)` tuples keyed by
    `id(element)`, and is handed to domonic as `_chain_cache` too -- but
    domonic's own chain cache expects that key to map to a bare
    `ComputedStyleDeclaration` (`_parent_computed()` does `cache.get(id(parent))`
    and calls `._font_size_px()` straight on whatever it finds), not our
    tuple. The two must not share one dict: a nested dict under a string key
    (never collides with the `int` ids used everywhere else) is what
    actually gets passed as `_chain_cache`, so domonic's own ancestor-style
    sharing keeps working without seeing our tuples at all."""
    cache = {} if computed_cache is None else computed_cache
    cached = cache.get(id(element))
    if cached is not None:
        return cached

    if reuse_styles:
        prior = getattr(element, "_chromonic_resolved_style", None)
        if prior is not None:
            cache[id(element)] = prior
            return prior

    # Share ancestor resolution across this layout pass, not across frames.
    # domonic's default cache only spans one element's ancestor walk; fresh
    # walks for siblings otherwise resolve the same parents repeatedly. Kept
    # in its own nested dict, not `cache` itself -- see the docstring above.
    chain_cache = cache.setdefault("_chromonic_chain_cache", {})
    computed = ComputedStyleDeclaration(element, _chain_cache=chain_cache)
    # domonic's own `_parent_computed()` only *reads* `chain_cache` looking
    # for an already-built parent -- it never registers a
    # `ComputedStyleDeclaration` under its own id when one is constructed
    # directly (as here), only when it builds one itself as somebody else's
    # parent. Without this, a child resolved right after `element` wouldn't
    # find `element` in `chain_cache` and would silently build (and resolve)
    # a second, separate `ComputedStyleDeclaration` for it.
    chain_cache[id(element)] = computed
    style_obj = LayoutStyle._from_computed(computed)
    element._chromonic_computed_style = computed
    element._chromonic_paint_style = _extract_paint_style(computed)
    element._chromonic_before_text, element._chromonic_after_text = _extract_generated_content(element, cache)
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


def _renders(style_obj) -> bool:
    """Whether an element already known to be an ordinary rendering tag (see
    `_NON_RENDERING_TAGS`, checked by the caller before this) should still be
    walked into the Taffy tree -- false for anything the cascade resolved to
    `display: none` (a real browser's "don't lay this out, don't paint it,
    don't hit-test it" is exactly `display: none`)."""
    display = style_obj.display
    return getattr(display, "value", display) != "none"


def _child_elements(element, computed_cache=None, *, reuse_styles=False) -> list:
    """`[(child, computed, style_obj), ...]` for children that should
    render -- each child's style computed exactly once here, then handed
    straight to the recursive `build()` call below instead of being
    recomputed there."""
    result = []
    for child in element.childNodes or []:
        if not _is_element(child):
            continue
        if (getattr(child, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
            continue
        computed, style_obj = _describe(child, computed_cache, reuse_styles=reuse_styles)
        if _renders(style_obj):
            result.append((child, computed, style_obj))
    return result


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
    # Mixed content (text alongside child *elements*) is out of this POC's
    # scope -- an element with any child element is a Taffy branch, full
    # stop; only a childless element's own text is ever measured.
    text = (
        getattr(element, "_chromonic_before_text", "")
        + (element.textContent or "")
        + getattr(element, "_chromonic_after_text", "")
    )
    style = element.__dict__.get("_chromonic_paint_style", {})
    text = _apply_text_transform(text, style.get("text_transform"))
    if style.get("white_space") in ("pre", "pre-wrap", "break-spaces"):
        return text
    return " ".join(text.split())


def _collapsed_text_node(node) -> str:
    raw = getattr(node, "textContent", None)
    if raw is None:
        raw = getattr(node, "data", "")
    if not raw or not raw.strip():
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _inline_mixed_content(element, children):
    """Return DOM-order inline items when a block contains direct text.

    Block children deliberately opt out: they establish line breaks and need
    a fuller anonymous-block implementation. This path handles the common
    prose case of text interleaved with spans, links, strong/em and code.
    """
    if not any(getattr(node, "nodeType", None) == TEXT_NODE and _collapsed_text_node(node).strip()
               for node in (element.childNodes or [])):
        return None
    by_id = {id(child): (child, computed, style_obj) for child, computed, style_obj in children}
    # Out-of-flow positioned children do not break an inline formatting run.
    # Keep them in the retained projection so Taffy can anchor them, while the
    # surrounding direct text still gets its own measurable fragment.
    if any(not (_is_inline_level(child, style_obj) or _is_absolutely_positioned(style_obj))
           for child, _computed, style_obj in children):
        return None
    items = []
    pending_space = False
    previous_was_element = False
    for node in element.childNodes or []:
        if getattr(node, "nodeType", None) == TEXT_NODE:
            text = _collapsed_text_node(node)
            if text:
                fragment = getattr(node, "_chromonic_fragment", None)
                if fragment is None:
                    fragment = _AnonymousTextFragment(node, element)
                    node._chromonic_fragment = fragment
                fragment._chromonic_leading_collapsed_space = (
                    pending_space or (previous_was_element and text[:1].isalnum())
                )
                items.append(("text", fragment, text, None, None))
                pending_space = False
                previous_was_element = False
            elif (getattr(node, "textContent", "") or ""):
                pending_space = True
        elif id(node) in by_id:
            child, computed, style_obj = by_id[id(node)]
            items.append(("element", child, None, computed, style_obj))
            previous_was_element = True
    return items


def _inline_text_style(parent_style):
    style = dict(parent_style)
    style.update({
        "display": "block", "position": "relative", "width": "auto", "height": "auto",
        "inset": ["auto", "auto", "auto", "auto"],
        "min_width": "auto", "min_height": "auto", "max_width": "auto", "max_height": "auto",
        "margin": [0.0, 0.0, 0.0, 0.0], "padding": [0.0, 0.0, 0.0, 0.0],
        "border": [0.0, 0.0, 0.0, 0.0], "flex_grow": 0.0, "flex_shrink": 1.0,
    })
    return style


def _numeric_edge(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _make_inline_formatting_plan(element, inline_items, style):
    """Build styled text runs for a shared inline formatting context."""
    if any(kind == "element" and _is_absolutely_positioned(child_style)
           for kind, _item, _text, _computed, child_style in inline_items):
        return None
    runs = []
    for kind, item, collapsed, child_computed, child_style in inline_items:
        if kind == "text":
            source = item.source
            owner = element
            paint_style = element._chromonic_paint_style
            native = None
        else:
            # Nested markup is flattened only when it contains text and no
            # element descendants. More involved nested inline trees stay on
            # the established retained projection until recursively flattened.
            if any(_is_element(node) for node in (item.childNodes or [])):
                return None
            source = next((node for node in (item.childNodes or [])
                           if getattr(node, "nodeType", None) == TEXT_NODE), item)
            collapsed = _own_text(item)
            if not collapsed:
                continue
            owner = item
            paint_style = item._chromonic_paint_style
            native = style_bridge.to_dict(child_style)
            item._chromonic_native_style = native
        raw = getattr(source, "textContent", "") or collapsed or ""
        text = _apply_text_transform(re.sub(r"\s+", " ", raw), paint_style.get("text_transform"))
        if not text.strip():
            continue
        # Whitespace collapses across run boundaries. Keep a single leading
        # or trailing space only when the source actually contains one.
        inferred_leading = (kind == "text" and
                            getattr(item, "_chromonic_leading_collapsed_space", False))
        text = ((" " if raw[:1].isspace() or inferred_leading else "") + text.strip()
                + (" " if raw[-1:].isspace() else ""))
        font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
        family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
        weight = _parse_font_weight(paint_style["font_weight"])
        italic = fonts.is_italic(paint_style["font_style"])
        ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
        glyph_height = ascent + descent
        used_line_height = _resolved_line_height(paint_style["line_height"]) or normal_height
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
    for left, right in zip(runs, runs[1:]):
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
    return _InlineFormattingPlan(element, runs, element._chromonic_paint_style) if runs else None


def _group_inline_element_runs(tree, parent, entries, parent_style, node_map, projection):
    """Wrap consecutive inline siblings in retained anonymous flex rows."""
    if parent_style["display"] != "block":
        return [node_id for _child, _style, node_id in entries]
    output, run_index, index = [], 0, 0
    cache = parent.__dict__.setdefault("_chromonic_inline_runs", {})
    while index < len(entries):
        child, child_style, node_id = entries[index]
        if not _is_inline_level(child, child_style):
            output.append(node_id)
            index += 1
            continue
        run = []
        while index < len(entries) and _is_inline_level(entries[index][0], entries[index][1]):
            run.append(entries[index][2])
            index += 1
        wrapper = cache.get(run_index)
        if wrapper is None:
            wrapper = cache[run_index] = _AnonymousInlineRun(None, parent)
        run_index += 1
        wrapper_style = _inline_text_style(parent_style)
        # A real inline formatting context's default cross-axis alignment is
        # the text baseline, not CSS's flex initial value ("normal"/stretch)
        # -- without this, a shorter inline-block sibling top-aligns with a
        # taller one instead of sitting on the shared baseline the way
        # `elif inline_items:` below already gives siblings mixed with text.
        wrapper_style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap",
                              "align_items": "baseline"})
        wrapper._chromonic_native_style = wrapper_style
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
    matching (`fontique`), shaping, and genuine Unicode line-breaking,
    replacing what used to be `domonic._fontmetrics`'s one hardcoded
    Helvetica-shaped advance-width table plus a hand-rolled greedy
    word-wrap. The raw CSS `font-family` value ("Georgia, 'Times New
    Roman', serif", or the literal string `"none"` when unset) is taken
    from `paint_style` (`tree.py`'s `_extract_paint_style`) -- the same
    already-extracted dict `paint.py` reads, deliberately, *not* a fresh
    `computed.fontFamily` access: see `_describe`'s docstring for why
    (a `ComputedStyleDeclaration` attribute access is expensive -- it
    re-resolves from scratch every time -- and this text leaf shouldn't
    pay that cost a second time when the paint-style extraction already
    paid it once). Parley/`parlance` parse a real CSS font-family list
    directly, generic keywords included, so it's passed straight through
    with no translation needed on this side (unlike `chromonic.fonts`, which
    still has to resolve a *painting* typeface itself -- skia-python does
    its own font matching and shares no glyph-ID space with Parley's, so
    Parley's job here is strictly the layout decisions: where lines break
    and how much space the result needs, not what actually gets drawn).

    `Element.getBBox()` is SVG-only (domonic zeroes it for any other
    element -- see `dom.py`'s `getBBox`) and was never a measurement option
    here to begin with; this reach into `chromonic._native` for a real text
    layout engine is what stands in for it."""
    font_family = paint_style["font_family"]
    if font_family == "none":
        font_family = ""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    font_weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    letter_spacing = _fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0)
    word_spacing = _fontmetrics.parse_length(paint_style["word_spacing"], default=0.0)
    line_height = _resolved_line_height(paint_style["line_height"])
    ascent, descent, normal_height = fonts.text_metrics(font_family, font_size, font_weight >= 600, italic)

    def measure(available_width, available_height):
        width, height, lines = layout_text(
            text, font_family, font_size,
            font_weight=font_weight, italic=italic,
            max_width=None if paint_style.get("white_space") in ("pre", "nowrap") else available_width,
            letter_spacing=letter_spacing, word_spacing=word_spacing, line_height=line_height,
        )
        if line_height is None and lines:
            # Chrome exposes integral CSS line-box heights for the platform
            # fonts in our fixtures (15px Arial -> 17px, 32px -> 34px), while
            # Parley's raw font metrics retain fractional ascender/descent
            # values. Keep glyph widths subpixel-precise, but normalize the
            # implicit `normal` line box before it accumulates down a page.
            lines = [(line_text, line_width, normal_height)
                     for line_text, line_width, _height in lines]
            height = normal_height * len(lines)
        # stashed for paint.py -- the *only* record of how this text wrapped
        # (and, now, its real per-line height); paint draws exactly these
        # lines rather than re-wrapping or re-measuring itself.
        element._chromonic_text_lines = [line_text for line_text, _line_width, _line_height in lines]
        element._chromonic_text_line_widths = [line_width for _line_text, line_width, _line_height in lines]
        element._chromonic_line_height = lines[0][2] if lines else font_size * 1.2
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
    text = str(value) if value else element.getAttribute("placeholder") or ""
    style = element.__dict__.get("_chromonic_paint_style", {})
    return _apply_text_transform(text, style.get("text_transform"))


def _select_display_text(element) -> str:
    """`<select>` is a native, *closed* dropdown: a real browser shows only
    its currently-selected option's text, never all of them stacked as
    visible content -- the full list only ever appears in an OS-native
    popup while it's open, which isn't something chromonic draws at all.
    Found rendering `https://www.wikipedia.org/`: its language-picker
    `<select>` has 250+ `<option>`s, and with no special handling at all a
    `<select>` is just an ordinary element with element children --
    `build()` recursed into every `<option>` as if it were regular block
    content, stacking all 250+ as real, visible, full-height boxes right
    on the page (the "wall of grey language names" overlapping everything
    below the search box). Picks the first `<option selected>`, falling
    back to the first `<option>` at all (a real `<select>` with nothing
    marked `selected` shows its first option), and `""` if it has none."""
    options = element.getElementsByTagName("option")
    if not options:
        return ""
    for opt in options:
        if opt.hasAttribute("selected"):
            return " ".join((opt.textContent or "").split())
    return " ".join((options[0].textContent or "").split())


def _apply_image_intrinsic_size(style: dict, element) -> None:
    """`<img>` is a "replaced element": an `auto` width/height uses its
    *intrinsic* (natural pixel) size, full stop -- unlike a text leaf, which
    reflows its height to whatever width the surrounding block layout gives
    it. A text leaf's `measure(available_width, ...)` callback works for
    text because Taffy always resolves an ordinary block box to its
    container's full width before consulting `measure` for a height at that
    width -- so the same mechanism can't give an image its own, *narrower*
    intrinsic width: a block child's width isn't `measure`'s to give. The
    only way to get a genuinely intrinsic-sized box is to put the number
    directly in the style *before* Taffy ever sees it -- baking the image's
    real pixel dimensions in as if they'd been written as `width:NNpx` --
    which is exactly what this does, only for whichever of `width`/`height`
    the cascade actually left `auto` (an explicit CSS size on the `<img>`
    is left alone, same as any other element's explicit size always wins).
    See `browser_images.py` for the fetch/decode/cache side of this."""
    from . import browser_images

    image = browser_images.load_image(element.getAttribute("src") or "")
    if image is None:
        return  # no image to size from -- left as whatever the cascade said (probably auto -> an empty box)
    if style["width"] == "auto":
        style["width"] = float(image.width())
    if style["height"] == "auto":
        style["height"] = float(image.height())


def _apply_canvas_intrinsic_size(style: dict, element) -> None:
    """Canvas is a replaced element with a 300 x 150 default bitmap."""
    if style["width"] == "auto":
        style["width"] = float(element.getAttribute("width") or 300)
    if style["height"] == "auto":
        style["height"] = float(element.getAttribute("height") or 150)


def _apply_svg_intrinsic_size(style: dict, element) -> None:
    """Treat an outer SVG viewport as one replaced element for HTML layout."""
    width_attr = (element.getAttribute("width") or "").strip()
    height_attr = (element.getAttribute("height") or "").strip()
    width = (_fontmetrics.parse_length(width_attr, default=None)
             if width_attr and not width_attr.endswith("%") else None)
    height = (_fontmetrics.parse_length(height_attr, default=None)
              if height_attr and not height_attr.endswith("%") else None)
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


# Tags a real browser's UA stylesheet defaults to `display: inline`. Used
# only as a *fallback tag guess* now -- `ua_style.apply()` (run by every real
# `browser.load()`) already gives `div`/`p`/the other ordinary block tags a
# UA-default `display: block`, so `_is_inline_level` below can trust a
# genuinely computed "inline"/"inline-block" directly for a loaded page.
# This set only still matters for a raw DOM tree built without
# `browser.load()` (some unit tests do this to skip the UA stylesheet
# entirely), where domonic's un-cascaded initial value of "inline" applies
# to every tag alike and tag-name is the only signal left.
_USUALLY_INLINE_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "small", "code", "label", "abbr",
    "cite", "mark", "sub", "sup", "time", "kbd", "samp", "var", "q", "u", "s",
    "button", "input", "select", "textarea",
})

# Replaced elements and form controls size themselves from authored
# `width`/`height` even at `display:inline` -- unlike an ordinary inline
# element, whose box is purely a function of its content.
_REPLACED_OR_CONTROL_TAGS = frozenset({
    "img", "canvas", "svg", "input", "textarea", "select", "button",
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
    """Whether a genuinely computed `display: inline`/`inline-block` on
    `element` can be trusted as real author intent rather than domonic's
    un-cascaded initial value (every tag's raw default, absent a UA
    stylesheet). True either for a tag this module already assumes is
    usually inline (`_USUALLY_INLINE_TAGS`), or -- more generally -- for
    any tag `ua_style.py`'s stylesheet gives an explicit `display: block`
    default (`ua_style.BLOCK_DEFAULT_TAGS`), *when that stylesheet actually
    ran* (`browser.load()` always runs it; a raw DOM tree built without it,
    as some unit tests do, did not). A tag in neither set -- `<tr>`/`<td>`/
    `<th>`/etc., which this project doesn't give a UA default at all -- has
    no way to disambiguate and is never trusted here, same as before."""
    if tag_name in _USUALLY_INLINE_TAGS:
        return True
    return tag_name in ua_style.BLOCK_DEFAULT_TAGS and _ua_stylesheet_applied(element)


def _is_inline_level(element, style_obj) -> bool:
    display = style_obj.display
    value = getattr(display, "value", display)
    if isinstance(value, str):
        match = style_bridge._SIMPLE_VAR_FALLBACK.match(value.strip())
        if match:
            value = match.group(1).strip()
    if value not in ("inline", "inline-block"):
        return False
    tag_name = (getattr(element, "tagName", "") or "").lower()
    return _trusts_computed_inline(element, tag_name)


def _is_floated(child_computed) -> bool:
    """Whether an author explicitly gave this element `float: left`/`right`
    -- unlike `display`, CSS's initial value for `float` is always `none`
    regardless of tag, so there's no domonic-default-ambiguity to guard
    against here (see `_is_inline_level`'s docstring for why *that* check
    needs a tag gate first): any non-`none` `float` is unambiguous author
    intent to leave normal flow and pack to one side of its line, the same
    "wants to sit in a row, not stack" signal an inline tag gives, just via
    a different CSS mechanism. Found needing this on `https://www.wikipedia.org/`:
    its "10 largest Wikipedias" grid is nothing but ten `float: left` boxes,
    no flexbox at all -- chromonic has no float implementation whatsoever (a
    separate, much larger project -- see PLAN.md), so without this they
    rendered stacked one per line, overlapping whatever content came after
    the (also-unimplemented) cleared/collapsed space they should have
    occupied."""
    float_value = getattr(child_computed, "float", None)
    return isinstance(float_value, str) and float_value.strip().lower() in ("left", "right")


def _wants_horizontal_flow(element, computed, style_obj) -> bool:
    return _is_inline_level(element, style_obj) or _is_floated(computed)


def _approximate_inline_flow(
    style: dict, child_elements: list, child_computeds: list, child_styles: list, computed,
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
    if style["display"] != "block":
        return  # already flex/grid/none -- a real, explicit layout mode wins, no guessing over it
    if len(child_elements) < 2:
        return
    qualifies = [
        _wants_horizontal_flow(child, child_computed, child_style)
        for child, child_computed, child_style in zip(child_elements, child_computeds, child_styles)
    ]
    # A strict "every child must qualify" reads safer but breaks on real
    # pages (see the docstring above for suckless.org's own exception) -- a
    # majority is safe *because* each individual check is already narrow
    # (a tag gate for inline, an always-explicit property for float), so
    # nothing reaching this point is a stray, misidentified ordinary block
    # element.
    if sum(qualifies) < len(child_elements) * 0.8:
        return
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    inline_tag_qualifies = any(
        _is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
    )
    if inline_tag_qualifies and style["gap"] == (0.0, 0.0):
        # Real inline flow gets its spacing from the whitespace *text
        # nodes* between elements in the source (`<a>home</a>\n<a>dwm</a>`
        # collapses to one space, same as any other run of whitespace) --
        # exactly the "mixed content" this POC doesn't measure (see
        # `_own_text`'s docstring), so without this, flex-wrap packs
        # elements edge to edge with no gap at all ("homedwmstcore...").
        # Approximated as one space character's width in the container's
        # own font -- not exact (a flex gap is uniform; real inline
        # whitespace can vary/collapse differently), but far closer than
        # no gap, and only applied when nothing already set an explicit one.
        # Skipped for a purely `float`-qualified group: floated grids get
        # their spacing from each item's own margin, same as any other
        # block box, and already flow through Taffy's ordinary flex-item
        # margin handling with no compensation needed.
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        bold = _fontmetrics.is_bold(computed.fontWeight)
        space_width = _fontmetrics.advance_width(" ", font_size, bold)
        style["gap"] = (0.0, space_width)


def _is_absolutely_positioned(style_obj) -> bool:
    position = style_obj.position
    return getattr(position, "value", position) in ("absolute", "fixed")


def _establishes_containing_block(style_obj) -> bool:
    """Whether an element's own CSS `position` makes it a valid *containing
    block* for `position:absolute`/`fixed` descendants -- anything but
    `static` (`relative`/`absolute`/`fixed`/`sticky`), per the CSS spec.
    `sticky` is treated the same as `relative` here (chromonic doesn't
    implement sticky's scroll-triggered repositioning at all --
    `style_bridge._position()` already maps both to Taffy's plain
    "relative", so this matches)."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"


def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created (including `element` itself). `computed`/`style_obj`, if given,
    are `element`'s already-computed style (see `_describe`) -- the caller
    (a parent's own `build()` call, via `_child_elements`) already had to
    compute them to decide whether `element` should be here at all, so the
    root of the walk is the only place they're ever computed twice... which
    is to say, never: `layout()` below computes the root's once, too.

    `is_containing_block`/`escapees` implement CSS's real containing-block
    rule for `position:absolute`/`fixed` -- **not** "positioned relative to
    the literal DOM parent", which is what a naive DOM-mirroring tree gives
    for free and which is wrong the moment that parent isn't itself
    positioned. A real browser resolves an absolutely-positioned element
    against its *nearest ancestor with `position != static`*, or the
    viewport if there is none -- found rendering `https://www.wikipedia.org/`
    through `browse2.py` and comparing against Chrome: a `position:absolute`
    search box with no positioned ancestor anywhere landed near its literal
    (`position:static`) DOM parent's origin instead of the page's, visibly
    overlapping unrelated content. Reproduced minimally (see
    `tests/test_chromonic.py`) and fixed here: every call defaults to
    `is_containing_block=True` (the root -- always a valid containing
    block, matching the CSS "initial containing block" is the viewport
    rule, and there's no ancestor above it to have gotten this from
    anyway); when true, this call owns a *fresh* `escapees` list -- any
    descendant, at any depth, that's absolutely positioned but whose own
    literal parent is *not* a containing block gets built normally but
    added to *this* list instead of its literal parent's Taffy children,
    so it ends up exactly one edge away from its real containing block in
    the Taffy tree, matching what CSS actually specifies. An intermediate
    `position:static` element passes its inherited `escapees` straight
    through unchanged, for exactly as many levels as it takes to reach one."""
    if computed_cache is None:
        computed_cache = {}
    if computed is None or style_obj is None:
        computed, style_obj = _describe(element, computed_cache, reuse_styles=reuse_styles)
    style = getattr(element, "_chromonic_native_style", None) if reuse_styles else None
    if style is None:
        style = style_bridge.to_dict(style_obj)
        # Not modelled in `LayoutStyle`/`style_bridge.to_dict()` at all, so
        # read straight off `computed` here. CSS 2.1 8.3.1: a non-`visible`
        # `overflow` makes an element establish a new block formatting
        # context, which -- among other things -- stops an in-flow child's
        # margin from collapsing through it. Taffy (`src/lib.rs`) already
        # implements this correctly *given* `Style.overflow`; leaving every
        # node at Taffy's `Overflow::Visible` default (this field was never
        # threaded through before) silently disabled that CSS rule
        # entirely. Any value Rust's `parse_overflow_axis` doesn't
        # recognise falls back to "visible" rather than erroring.
        _valid_overflow = ("visible", "clip", "hidden", "scroll", "auto")
        overflow_x = getattr(computed, "overflowX", "visible") or "visible"
        overflow_y = getattr(computed, "overflowY", "visible") or "visible"
        style["overflow"] = (
            overflow_x if overflow_x in _valid_overflow else "visible",
            overflow_y if overflow_y in _valid_overflow else "visible",
        )
        element._chromonic_native_style = style
    if is_grid_item and style["min_width"] == "auto":
        # Prevent an auto-width block descendant from feeding its containing
        # grid's full available width back as the track's intrinsic minimum.
        style["min_width"] = 0.0
    own_escapees = [] if is_containing_block else escapees
    tag_name = (getattr(element, "tagName", "") or "").lower()
    element._chromonic_tag_name = tag_name
    if (tag_name not in _REPLACED_OR_CONTROL_TAGS
            and getattr(style_obj.display, "value", "") == "inline"
            and _trusts_computed_inline(element, tag_name)):
        # CSS 2.1 10.3.1: `width`/`height` never apply to a non-replaced
        # inline box -- only its content (and any inline-block/replaced
        # descendant) determines its size. `style_bridge._display()` already
        # collapsed "inline" onto Taffy's plain "block" display (Taffy has
        # no inline mode), which would otherwise make an authored
        # `width`/`height` on e.g. a `<div style="display:inline">` a hard
        # box size instead of being ignored like a real browser does. Same
        # `_trusts_computed_inline` gate as `_is_inline_level` -- a tag with
        # no UA default at all (`<tr>`/`<td>`/...), or a raw DOM tree built
        # without `browser.load()` (skipping `ua_style.py`, as some unit
        # tests do), still sees domonic's un-cascaded "inline" default and
        # must not have its authored size discarded on that basis alone.
        style["width"] = "auto"
        style["height"] = "auto"
        # CSS 2.1 10.3.1/10.6.1: `margin-top`/`margin-bottom` are likewise
        # accepted but have no effect on a non-replaced inline box's height
        # (only `margin-left`/`margin-right` add real horizontal spacing).
        # Taffy has no inline mode to know that on its own -- left as
        # ordinary flex-item margins, they inflated the anonymous inline
        # run's cross-axis size. Found via `wpt/css/CSS2/margin-padding-
        # clear/margin-applies-to-008.xht` (`div div { display: inline;
        # margin: 50px }`): the line grew 100px taller instead of staying
        # at the text's own line height.
        margin = list(style["margin"])
        margin[0] = margin[2] = 0.0
        style["margin"] = margin
    if (getattr(style_obj.display, "value", "") == "inline-block"
            and _trusts_computed_inline(element, tag_name)):
        # CSS 2.1 9.2.1/CSS Display 3: `inline-block` is an atomic
        # inline-level box that establishes its own block formatting
        # context, so (like `overflow:hidden`, fixed above) an in-flow
        # child's margin must not collapse through it -- confirmed on
        # `wpt/css/CSS2/margin-padding-clear/margin-collapse-015a.xht`
        # ("An element with its display set to 'inline-block' does not
        # collapse its margins with its children"). Taffy has no
        # `inline-block` display mode to key off (it's mapped onto plain
        # `Display::Block`, same as an ordinary block, by
        # `style_bridge._display()`), so this is signalled the same way
        # `overflow` was: a field Rust's `parse_style` turns into
        # `Contain::PAINT`, which establishes a new formatting context in
        # Taffy without the side effect `Contain::LAYOUT` would have had on
        # the separate inline-block baseline-alignment fix (see
        # `get_contain` in `src/lib.rs`).
        style["establishes_bfc"] = True
    if tag_name == "table":
        element._chromonic_border_collapse = computed.borderCollapse == "collapse"
        if element._chromonic_border_collapse:
            # Collapsed borders straddle the table grid edge. Reserving half
            # an outer border on each side gives rows the same 359px inner
            # grid inside a 360px border box that Chrome reports.
            style.update({"box_sizing": "border-box", "padding": [0.5] * 4})
    if tag_name == "tr":
        # Taffy has no table formatting mode. A row is nevertheless a
        # horizontal formatting context, and this retained projection gives
        # ordinary fixed/equal-column tables the right fundamental geometry.
        style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap"})
    elif tag_name in ("td", "th") and style["width"] == "auto":
        style.update({"flex_grow": 1.0, "flex_shrink": 1.0,
                      "flex_basis": 0.0, "min_width": 0.0})
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and getattr(ancestor, "_chromonic_tag_name", None) != "table":
            ancestor = getattr(ancestor, "parentElement", None)
        if ancestor is not None and getattr(ancestor, "_chromonic_border_collapse", False):
            style["border"] = [value / 2.0 if isinstance(value, (int, float)) else value
                               for value in style["border"]]
    # `<select>`'s `<option>` children are never real layout content -- see
    # `_select_display_text` -- so it's treated as childless here
    # regardless of what's actually in the DOM, the same way `<img>` below
    # is a leaf regardless of it usually having no children at all.
    children = [] if tag_name in ("select", "svg") else _child_elements(
        element, computed_cache, reuse_styles=reuse_styles
    )
    element._chromonic_has_layout_children = bool(children)
    if tag_name == "button":
        _apply_button_intrinsic_width(style, element)
    inline_items = _inline_mixed_content(element, children) if children else None
    inline_plan = (_make_inline_formatting_plan(element, inline_items, style)
                   if inline_items else None)

    if inline_plan is not None:
        element._chromonic_has_layout_children = True
        if style["display"] == "block" and style["width"] == "auto":
            style["width"] = ("pct", 1.0)
        measure_key = ("inline-context", tuple(element._chromonic_paint_style.items()), tuple(
            (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
             tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
             run["leading"], run["trailing"], run["top_edge"], run["atomic_width"])
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
    elif inline_items:
        element.__dict__.pop("_chromonic_inline_plan", None)
        style["display"] = "flex"
        style["flex_direction"] = "row"
        style["flex_wrap"] = "wrap"
        style["align_items"] = "baseline"
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
        for item_index, (kind, item, text, child_computed, child_style) in enumerate(inline_items):
            if kind == "element":
                normal_child_ids.append(build(
                    tree, item, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=False, escapees=own_escapees,
                    reuse_styles=reuse_styles, projection=projection,
                ))
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
        element._chromonic_inline_fragments = fragments
        all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
        node_id = (projection.upsert(element, style, all_child_ids, None, None)
                   if projection else tree.new_with_children(style, all_child_ids))
    elif children:
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        _approximate_inline_flow(
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
                # `element` (this call, the child's literal DOM parent) is
                # not a valid containing block -- this child's real one is
                # further up, whatever ancestor `escapees` actually belongs
                # to. Build it normally, but hand its node id to *that*
                # ancestor instead of adding it to `element`'s own children.
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
        if normal_entries:
            normal_child_ids = _group_inline_element_runs(
                tree, element, normal_entries, style, node_map, projection,
            )
        all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
        node_id = (projection.upsert(element, style, all_child_ids, None, None)
                   if projection else tree.new_with_children(style, all_child_ids))
    elif tag_name in ("img", "canvas", "svg"):
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if tag_name == "img":
            _apply_image_intrinsic_size(style, element)
        elif tag_name == "canvas":
            _apply_canvas_intrinsic_size(style, element)
        else:
            _apply_svg_intrinsic_size(style, element)
        element._chromonic_text_lines = []
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
        self.node_map = {}

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
            self.tree.remove(self.nodes.pop(key))
            self.state.pop(key, None)

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
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, width, compute_height)
        _write_boxes(boxes, self.node_map)
        _adjust_body_collapsed_margins(root_element)
        _apply_root_margin_offset(root_element, self.node_map)
        if viewport_height is not None:
            _fix_viewport_anchored_positioning(self.node_map, viewport_height, width)
        _publish_inline_formatting(self.node_map)
        return self.node_map

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
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, width, compute_height)
        _write_boxes(boxes, self.node_map)
        _adjust_body_collapsed_margins(root_element)
        _apply_root_margin_offset(root_element, self.node_map)
        if viewport_height is not None:
            _fix_viewport_anchored_positioning(self.node_map, viewport_height, width)
        _publish_inline_formatting(self.node_map)
        return self.node_map


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


def warm_text_layout() -> None:
    """Pay Parley's one-time `FontContext` setup cost now, not during the
    first real text on the first real page. Measured: the very first
    `layout_text()` call in a process costs ~100ms (font enumeration --
    building `fontique`'s font database); every call after that is close to
    free (backed by the `thread_local!` `FontContext`/`LayoutContext` in
    `src/lib.rs`, reused for the life of the process). The same "pay it at
    startup" idea `fonts.warm_cache()` and `browser.warm_interpreter()`
    already use -- called once from `native_browser.py`'s `View.__init__`."""
    layout_text("warm", "sans-serif", 16.0)


def _write_boxes(boxes, node_map):
    """Publish native geometry back onto the authoritative Domonic nodes."""
    for node_id, box in boxes.items():
        x, y, w, h, bt, br, bb, bl, pt, pr, pb, pl = box
        element = node_map[node_id]
        state = element.__dict__
        # domonic.layout.set_layout_box and Element.set_layout_box ultimately
        # perform this exact private-state assignment. We already have the
        # element here, so avoid two wrappers plus Element.__setattr__ for
        # every node while preserving all public geometry readers.
        state["_layout_box"] = LayoutBox(
            x=x, y=y, width=w, height=h,
            client_width=w - bl - br,
            client_height=h - bt - bb,
            border_top=bt, border_left=bl,
        )
        state["_chromonic_padding"] = (pt, pr, pb, pl)


def _publish_inline_formatting(node_map) -> None:
    """Project shared line fragments after parent boxes reach final positions."""
    seen = set()
    for element in node_map.values():
        if id(element) in seen:
            continue
        seen.add(id(element))
        plan = getattr(element, "_chromonic_inline_plan", None)
        box = element.__dict__.get("_layout_box")
        if plan is not None and box is not None:
            plan.publish(box, element.__dict__.get("_chromonic_padding", (0.0,) * 4))


def _adjust_body_collapsed_margins(root_element):
    """Publish Chrome-compatible body geometry for collapsed child margins.

    Taffy correctly positions block children with collapsed sibling margins,
    but a root node has no containing block into which its first/last margin
    struts can escape. HTML's body is special: Chrome's body rect excludes
    those escaped margins. Restrict this correction to the simple eligible
    body case; complex block formatting stays with Taffy.

    `_chromonic_scroll_extent` (set below, only once this correction
    actually applies) doubles as the signal `_apply_root_margin_offset`
    uses to know this pass already accounted for the root's own top
    margin via collapsing, so it doesn't also add that margin a second
    time -- cleared unconditionally up front so a stale value can never
    survive from an earlier layout pass where a different code path
    applied (e.g. this page's `height` used to be `auto` and no longer is).
    """
    root_element.__dict__.pop("_chromonic_scroll_extent", None)
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return
    style = getattr(root_element, "_chromonic_native_style", {})
    if style.get("display") != "block":
        return
    if any(value not in (0.0, "auto") for name in ("padding", "border")
           for value in style.get(name, ())):
        return
    boxes = []
    visible_boxes = []
    for child in root_element.childNodes or []:
        if not _is_element(child):
            continue
        child_style = getattr(child, "_chromonic_native_style", {})
        if child_style.get("position") == "absolute":
            continue
        box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        boxes.append(box)
        # A CSS-empty box (no border/padding/height and, since only
        # out-of-flow descendants can leave a box with zero height here, no
        # in-flow content of its own) doesn't stop a preceding margin from
        # collapsing straight through it -- its own Taffy `y` is only where
        # that margin *would have* landed had the box actually rendered
        # something there, not real content extent. Counting it toward
        # `bottom` double-counts that same margin as literal separation
        # space inside body's box instead of letting it escape past this
        # empty child the way Chrome does. Found on the CSS2.1 suite's
        # `margin-*`/`padding-*` tests (`wpt/css/CSS2/margin-padding-clear`):
        # an absolutely-positioned-only wrapper `<div>` after a `<p>` added
        # the `<p>`'s own collapsed-through bottom margin to `body`'s height
        # a second time.
        if box.height == 0 and not any(
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
    # sibling's box visually stick out past a later one (`box.y + box.height`
    # for that earlier child now exceeds the true last child's bottom edge) --
    # found on `wpt/css/CSS2/margin-padding-clear/padding-bottom-003.xht`
    # (`#div2 { margin-top: -3px }` pulling it back over `#div1`'s own
    # bottom edge): Chrome's body height tracks `#div2` (the real last
    # child) regardless, ignoring `#div1`'s now-irrelevant extra 1px.
    top = boxes[0].y
    bottom = boxes[-1].y + boxes[-1].height
    old = root_element.__dict__.get("_layout_box")
    if old is not None:
        # The escaped final margin still contributes to the document's scroll
        # extent even though it is outside body.getBoundingClientRect() --
        # and unlike the rendered height above, the scrollable area *does*
        # need the true max over every child, first/last or not.
        explicit_height = style.get("height") != "auto"
        corrected_height = old.height if explicit_height else bottom - top
        corrected_client_height = old.client_height if explicit_height else corrected_height
        root_element.__dict__["_chromonic_scroll_extent"] = max(
            old.y + old.height, bottom, *(box.y + box.height for box in boxes)
        )
        root_element.__dict__["_layout_box"] = LayoutBox(
            x=old.x, y=top, width=old.width, height=corrected_height,
            client_width=old.client_width, client_height=corrected_client_height,
            border_top=old.border_top, border_left=old.border_left,
        )


def _is_root_anchored(element) -> bool:
    """Whether `element` is `position:absolute`/`fixed` with no positioned
    ancestor -- its containing block is the document root/viewport itself,
    not a real ancestor's box. Shared by `_fix_viewport_anchored_positioning`
    (which corrects such elements against the true viewport) and
    `_apply_root_margin_offset` (which must *not* shift them, since the
    viewport's origin is unaffected by the root element's own margin)."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is None or not _is_absolutely_positioned(resolved[1]):
        return False
    return _find_containing_block_ancestor(element) is None


def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning (see `_establishes_containing_block`),
    or `None` if none exists -- meaning `element`'s containing block is the
    document root itself."""
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
    """`(new_y, new_height)` for an element whose containing block is the
    document root, resolved against the *true* `viewport_height` instead of
    whatever height the root's own Taffy box happened to compute to --
    either may be `None`, meaning "leave that one as Taffy already computed
    it" (see `_fix_viewport_anchored_positioning`'s docstring for why)."""
    top, _right, bottom, _left = style["inset"]
    top_v = _resolve_inset(top, viewport_height)
    bottom_v = _resolve_inset(bottom, viewport_height)
    margin_top, _mr, margin_bottom, _ml = style["margin"]
    mt = _resolve_inset(margin_top, viewport_height) or 0.0
    mb = _resolve_inset(margin_bottom, viewport_height) or 0.0
    height = style["height"]
    if isinstance(height, tuple) and height[0] == "pct":
        # A percentage height resolves against the containing block's own
        # height regardless of whether `bottom` is also set -- unlike the
        # top+bottom-both-set case below, this doesn't need the opposite
        # inset to become definite (`abspos-containing-block-004.xht`:
        # `top:0; height:100%`, no `bottom` at all -- Taffy had nothing to
        # resolve `100%` against but the root's own, too-small Taffy box).
        new_height = height[1] * viewport_height
        if top_v is not None:
            return top_v + mt, new_height
        if bottom_v is not None:
            return viewport_height - bottom_v - mb - new_height, new_height
        return None, new_height
    if bottom_v is None:
        # `top` alone (or neither) determines this element's position --
        # independent of the containing block's height either way, so
        # whatever Taffy already computed is already correct.
        return None, None
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


def _resolve_viewport_anchored_box_x(style: dict, box, viewport_width: float):
    """`(new_x, new_width)` for a root-anchored element, the same horizontal
    counterpart to `_resolve_viewport_anchored_box`'s vertical correction --
    see that function's docstring for the shape of the logic, mirrored here
    across `left`/`right`/`margin-left`/`margin-right`/`width` instead of
    `top`/`bottom`/`margin-top`/`margin-bottom`/`height`. Needed because the
    root's own Taffy box, unlike its height, is *not* always the true
    viewport width either: `_apply_root_margin_offset` documents that the
    root correctly shrinks for its own margin, and a body with horizontal
    margin (`abspos-containing-block-001.xht`: `body { margin: 100px }`, a
    `position:fixed` box with `left:0; right:0`) shrinks the root's width by
    that margin the same way it shrinks height -- previously uncorrected."""
    _top, right, _bottom, left = style["inset"]
    left_v = _resolve_inset(left, viewport_width)
    right_v = _resolve_inset(right, viewport_width)
    _mt, margin_right, _mb, margin_left = style["margin"]
    ml = _resolve_inset(margin_left, viewport_width) or 0.0
    mr = _resolve_inset(margin_right, viewport_width) or 0.0
    width = style["width"]
    if isinstance(width, tuple) and width[0] == "pct":
        # Same percentage-independent-of-the-opposite-inset resolution as
        # `_resolve_viewport_anchored_box`'s height branch -- also catches
        # the synthetic `("pct", 1.0)` `tree.build()` assigns a block box
        # with inline content and `width:auto` (line ~1253) to stretch it
        # to its containing block, which for a root-anchored box must be
        # the true viewport width, not the root's own (possibly
        # margin-shrunk) Taffy box.
        new_width = width[1] * viewport_width
        if left_v is not None:
            return left_v + ml, new_width
        if right_v is not None:
            return viewport_width - right_v - mr - new_width, new_width
        return None, new_width
    if right_v is None:
        return None, None
    if left_v is None:
        return viewport_width - right_v - mr - box.width, None
    if width != "auto":
        return None, None
    return left_v + ml, viewport_width - left_v - ml - right_v - mr


def _shift_box(node, dx: float, dy: float) -> None:
    box = node.__dict__.get("_layout_box")
    if box is not None:
        node.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=box.width, height=box.height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )


def _shift_subtree(element, dx: float, dy: float) -> None:
    """Shift `element` and everything painted inside it (nested elements,
    retained inline text fragments) by `(dx, dy)` -- used to carry a
    corrected element's own position through to its descendants, whose
    boxes Taffy computed as offsets from `element`'s own (now-corrected)
    origin. A uniform shift preserves every internal relationship Taffy
    already got right; only the subtree's absolute origin moves."""
    _shift_box(element, dx, dy)
    for fragment in getattr(element, "_chromonic_inline_fragments", None) or ():
        _shift_box(fragment, dx, dy)
    for child in getattr(element, "childNodes", None) or ():
        if _is_element(child):
            _shift_subtree(child, dx, dy)


def _fix_viewport_anchored_positioning(node_map: dict, viewport_height: float,
                                        viewport_width: "float | None" = None) -> None:
    """Correct the vertical position (and, when stretched, height) Taffy
    computed for any `position:absolute`/`fixed` element whose containing
    block is the document root itself -- i.e. no ancestor establishes one
    (`_find_containing_block_ancestor` finds none).

    Taffy resolves such an element's `top`/`bottom`/`height` insets against
    the root element's *own* Taffy box -- which, for a scrollable page laid
    out with `height=None` (size-to-content, see `layout()`), is the full
    *document* height, not the viewport. That's backwards for exactly this
    fallback case: CSS's real rule is that an absolutely/fixed-positioned
    element with no positioned ancestor resolves against the *initial
    containing block*, which has the viewport's dimensions, not the
    document's -- confirmed directly against real Chrome (`bottom:0` on a
    plain `position:absolute` with no positioned ancestor, on a page taller
    than the viewport, lands at the *viewport's* bottom edge, not the
    document's -- the same place a `position:fixed` element with the same
    `bottom:0` lands). Found running `chromonic`'s Chrome-comparison layout
    harness (`chromonic/tests/layout/`, `position_stack.html`): a
    `position:fixed` element anchored with `bottom:18px` on a `900px`-tall
    body measured `300px` too low, exactly the gap between the body's own
    height and the real (fixed, `600px`) viewport.

    Only called when a caller supplies a real `viewport_height` distinct
    from "whatever height the root computed to" (see `layout()`'s
    docstring) -- every existing caller that doesn't pass one keeps today's
    behaviour exactly, at zero extra cost. `viewport_width`, when given,
    applies the same correction horizontally -- the root's own width is
    usually already the true viewport width (chromonic has no
    horizontal-scroll model), but not when the root itself carries
    horizontal margin (`_apply_root_margin_offset` shrinks the root's own
    box for that margin the same way it does for height); every existing
    caller passes it, so there is no untouched-behaviour case to preserve
    here the way there is for `viewport_height`."""
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
                             else _resolve_viewport_anchored_box_x(style, box, viewport_width))
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
            for child in getattr(element, "childNodes", None) or ():
                if _is_element(child):
                    _shift_subtree(child, dx, dy)


def _apply_root_margin_offset(root_element, node_map: dict) -> None:
    """Shift the whole laid-out tree by the compute root's own margin.

    Taffy's root-compute entry point has no parent context for the root
    node, so a root with `width`/`height:auto` correctly *shrinks* to leave
    room for its own margin (ordinary block sizing: available space minus
    margin) but never actually *offsets* its own box by that margin the
    way a normal child would within a real parent's content box -- the
    root always comes back positioned at `(0, 0)` regardless of its own
    margin.

    `chromonic` hands Taffy `<body>` as this root, but CSS-wise `<body>`
    isn't really the document's root -- `<html>` is, and `<body>`'s own
    margin (`8px` by every real UA stylesheet, including this one's --
    `ua_style.py`) genuinely offsets it within `<html>`'s content box.
    Found running the layout harness (`ua_defaults.html`, a page with no
    author CSS at all, so nothing but this default margin in play): *every*
    element measured `8px` left of where Chrome puts it -- the whole
    document was shifted, not any one element, since everything's absolute
    position is ultimately `<body>`'s own position plus offsets from it.

    Root-anchored `position:absolute`/`fixed` elements (`_is_root_anchored`)
    are deliberately excluded: their containing block is the *viewport*,
    anchored at the true canvas origin, which `<body>`'s own margin has no
    effect on in real CSS either -- `position:fixed; top:0` sits at the
    literal viewport top regardless of `<body>`'s margin.

    The vertical axis is skipped when `_adjust_body_collapsed_margins`
    already moved the root's own top edge by folding its top margin into a
    collapsed-margin-derived position (see that function's own docstring)
    -- otherwise this would add the same top margin a second time."""
    style = getattr(root_element, "_chromonic_native_style", None)
    box = root_element.__dict__.get("_layout_box")
    if style is None or box is None:
        return
    margin_top, _margin_right, _margin_bottom, margin_left = style["margin"]
    # CSS: a percentage margin resolves against the containing block's
    # *width* on every side, vertical included -- not a typo.
    dx = _resolve_inset(margin_left, box.width) or 0.0
    already_collapsed = "_chromonic_scroll_extent" in root_element.__dict__
    dy = 0.0 if already_collapsed else (_resolve_inset(margin_top, box.width) or 0.0)
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
        if owner is not None and id(owner) in root_anchored_ids:
            continue
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

    `reuse_styles=False` (the default, and what every mutation above needs)
    re-resolves every element's CSS from scratch, exactly as always. Pass
    `reuse_styles=True` only when nothing about *any* element's class,
    inline style, or stylesheets could have changed since the last
    `layout()` call -- see `_describe`'s docstring for why that's worth
    doing (domonic's CSS cascade resolution is the dominant cost of a
    relayout on a real page) and for the full safety contract. Currently
    the only caller that qualifies is `native_browser.py`'s image-arrival
    poll: a newly-decoded `<img>`'s intrinsic size is applied independently
    of the CSS cache (see `_apply_image_intrinsic_size`), so skipping CSS
    re-resolution there is safe. A viewport resize or a real DOM event
    handler are not -- both must keep the default.

    `viewport_height`, when given, corrects `position:absolute`/`fixed`
    elements with no positioned ancestor to resolve against the *real*
    viewport height rather than `root_element`'s own (possibly
    content-grown, when `height=None`) box -- see
    `_fix_viewport_anchored_positioning`'s docstring. Leave it `None` for
    callers with no separate "viewport vs. document" distinction at all
    (a fixed-size, non-scrolling `Interaction`, or any `height` that's
    already the whole story) -- passing the real, distinct viewport height
    only matters once a page's document can grow taller than what's
    actually visible.
    """
    from . import webfonts
    if webfonts.prepare_layout(root_element):
        reuse_styles = False
    tree = Tree()
    node_map: dict[int, object] = {}
    with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
        root_id = build(tree, root_element, node_map, reuse_styles=reuse_styles)
    compute_height = _root_compute_height(root_element, height, viewport_height)
    boxes = tree.compute(root_id, width, compute_height)
    _write_boxes(boxes, node_map)
    _adjust_body_collapsed_margins(root_element)
    _apply_root_margin_offset(root_element, node_map)
    if viewport_height is not None:
        _fix_viewport_anchored_positioning(node_map, viewport_height, width)
    _publish_inline_formatting(node_map)
    return node_map
