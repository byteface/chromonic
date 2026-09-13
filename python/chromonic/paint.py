"""Paint a domonic tree that `tree.layout()` has already run, via skia-python.

Reads exactly two things per element: the `LayoutBox` `tree.py` just wrote
(`element.get_layout_box()`) and its resolved paint-relevant style
(background/border/text colours, font size/weight/style/family --
`domonic.layout.LayoutStyle` deliberately excludes these, they aren't
layout inputs). Neither is built here: `tree.py`'s own walk (`build()`/
`_describe()`) already built a `ComputedStyleDeclaration` for every element
a moment ago (to resolve its `LayoutStyle`) and extracted exactly the
handful of properties this file needs from it (`_chromonic_paint_style`) --
reading both back instead of re-deriving them is a real, measured perf win
(see `tree.py`'s module docstring): a `ComputedStyleDeclaration` attribute
access re-resolves from the underlying style text on *every* access, with
nothing cached on the object itself, so doing this once per element per
*layout* rather than once per element per *paint* matters whenever
painting runs more than layout does -- `native_browser.py`'s scroll/expose
repaints are exactly that case (never relayout, see PLAN.md's "Phase 9").

Backgrounds, borders, text, and `<img>` images (phase 7, see
`browser_images.py`) are the paint surface here -- no shadows, gradients,
or CSS transforms. That is the explicit POC scope (see PLAN.md); real
browsers have another decade of `paint.py` in them.
"""

from __future__ import annotations

import functools
import re

import skia

from domonic import _fontmetrics
from domonic.style import ComputedStyleDeclaration

from . import fonts

ELEMENT_NODE = 1
_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


@functools.lru_cache(maxsize=2048)
def _color(text: "str | None"):
    """A CSS colour string -> `skia.Color4f`, or `None` if there's nothing to
    paint (unset, `transparent`, or alpha 0)."""
    if not text or text in ("transparent", "none"):
        return None
    match = _RGB_RE.match(text.strip())
    if match:
        r, g, b = (float(match.group(i)) / 255.0 for i in (1, 2, 3))
        a = float(match.group(4)) if match.group(4) is not None else 1.0
        return None if a <= 0 else skia.Color4f(r, g, b, a)
    match = _HEX_RE.match(text.strip())
    if match:
        value = match.group(1)
        r, g, b = (int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        return skia.Color4f(r, g, b, 1.0)
    return None


def _px(text: "str | None", default: float = 0.0) -> float:
    if not text:
        return default
    try:
        return float(text.rstrip("px"))
    except ValueError:
        return default


_FONT_CACHE: "dict[tuple, skia.Font]" = {}


def _font(size_px: float, *, bold: bool = False, italic: bool = False, family: "str | None" = None) -> skia.Font:
    """A cached `skia.Font` for this exact (size, weight, style, family)
    combination -- see `fonts.py` for how `family` resolves to a typeface,
    with downloaded fonts shared with Parley/fontique text layout."""
    size_px = round(size_px)
    key = (size_px, bold, italic, family)
    font = _FONT_CACHE.get(key)
    if font is None:
        typeface = fonts.resolve_typeface(family, bold=bold, italic=italic)
        font = skia.Font(typeface, size_px)
        _FONT_CACHE[key] = font
    return font


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _paint_image(canvas: "skia.Canvas", element, box) -> None:
    # `browser_images` is imported lazily -- it's the one place in chromonic's
    # core paint/layout path that does network I/O, and most callers
    # (poc.py, particles.py, the pyscript demos, ...) never render an
    # <img> at all.
    from . import browser_images

    image = browser_images.load_image(element.getAttribute("src") or "")
    if image is None:
        return  # no image (missing src, failed fetch, undecodable format) -- paint nothing, not a placeholder
    padding = getattr(element, "_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
    pad_top, pad_right, pad_bottom, pad_left = padding
    x = box.x + box.border_left + pad_left
    y = box.y + box.border_top + pad_top
    width = box.client_width - pad_left - pad_right
    height = box.client_height - pad_top - pad_bottom
    if width <= 0 or height <= 0:
        return
    # stretched to fill the box, no aspect-ratio preservation -- matches a
    # real browser's own default (`object-fit: fill`) for a plain <img>
    # with no CSS of its own; see the module docstring.
    canvas.drawImageRect(image, skia.Rect.MakeXYWH(x, y, width, height))


def _paint_style(element) -> dict:
    """The paint-only properties `tree.py`'s `_describe()` already extracted
    for this element during the last `layout()` pass (`_chromonic_paint_style`)
    -- reading these plain strings back, instead of touching a
    `ComputedStyleDeclaration` attribute directly, is what makes a repaint
    with no relayout in between (a scroll, an unrelated element's style
    changing) not re-resolve every element's colours/fonts from scratch;
    see `tree.py`'s `_extract_paint_style` for the profiling that found
    this. Falls back to building both fresh if `element` was somehow
    painted without a prior `tree.layout()` pass."""
    style = getattr(element, "_chromonic_paint_style", None)
    if style is not None:
        return style
    from . import tree as _tree

    return _tree._extract_paint_style(ComputedStyleDeclaration(element))


def paint_element(canvas: "skia.Canvas", element, box=None) -> None:
    box = element.__dict__.get("_layout_box") if box is None else box
    if box is None:
        return  # never laid out (shouldn't happen for anything tree.layout() visited)

    style = _paint_style(element)

    background = _color(style["background_color"])
    if background is not None:
        canvas.drawRect(
            skia.Rect.MakeXYWH(box.x, box.y, box.width, box.height),
            skia.Paint(Color4f=background, AntiAlias=True),
        )

    tag_name = getattr(element, "_chromonic_tag_name", None)
    if tag_name is None:
        tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name == "img":
        _paint_image(canvas, element, box)
    elif tag_name == "canvas":
        from . import canvas2d
        canvas2d.paint_element(canvas, element, box)

    if box.border_top > 0:
        border_color = _color(style["border_top_color"]) or skia.Color4f(0, 0, 0, 1)
        paint = skia.Paint(Color4f=border_color, AntiAlias=True, Style=skia.Paint.kStroke_Style)
        # a stroked rect straddles the path -- inset by half the border width
        # so the stroke lands on the border box the layout actually computed.
        inset = box.border_top / 2.0
        paint.setStrokeWidth(box.border_top)
        canvas.drawRect(
            skia.Rect.MakeXYWH(box.x + inset, box.y + inset, box.width - box.border_top, box.height - box.border_top),
            paint,
        )

    # `<select>`'s `<option>` children are real DOM children but were never
    # given a layout box -- tree.py treats it as childless too (see its
    # `_select_display_text`), so paint must agree, or it would (a) skip
    # this element's own text (the "not children" branch below never runs)
    # and (b) recurse into `paint_tree` for each <option>, painting nothing
    # useful since none of them has a `get_layout_box()` to paint from.
    has_layout_children = getattr(element, "_chromonic_has_layout_children", None)
    if has_layout_children is None:
        has_layout_children = tag_name != "select" and any(
            _is_element(child) for child in (element.childNodes or [])
        )
    if not has_layout_children:
        # `tree.py`'s measure callback already word-wrapped this element's
        # text to whatever width Taffy gave it and stashed the exact lines
        # here (`_chromonic_text_lines`) -- paint draws precisely those, rather
        # than re-wrapping (it doesn't have Taffy's resolved width to wrap
        # against anyway, and shouldn't need to re-derive what layout
        # already decided). Falls back to the unwrapped single line for
        # anything painted without a prior `tree.layout()` pass.
        lines = getattr(element, "_chromonic_text_lines", None)
        if lines is None:
            text = " ".join((element.textContent or "").split())
            lines = [text] if text else []
        if lines and any(lines):
            padding = getattr(element, "_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
            pad_top, _pad_right, _pad_bottom, pad_left = padding
            font_size = _px(style["font_size"], 16.0)
            bold = _fontmetrics.is_bold(style["font_weight"])
            italic = fonts.is_italic(style["font_style"])
            font = _font(font_size, bold=bold, italic=italic, family=style["font_family"])
            # Parley's own real per-font line height (`tree.py`'s
            # `_make_measure`), not a re-derived Helvetica-table guess --
            # falls back to the same rough multiple text leaves used before
            # Parley, for anything painted without a prior `tree.layout()`.
            line_height = getattr(element, "_chromonic_line_height", None) or font_size * 1.2
            text_color = _color(style["color"]) or skia.Color4f(0, 0, 0, 1)
            text_x = box.x + box.border_left + pad_left
            paint_ = skia.Paint(Color4f=text_color, AntiAlias=True)
            for index, line in enumerate(lines):
                if not line:
                    continue
                # a simple top-aligned baseline per line, line_height apart
                baseline_y = box.y + box.border_top + pad_top + font_size + index * line_height
                canvas.drawString(line, text_x, baseline_y, font, paint_)

    # Direct text nodes in mixed inline content have retained anonymous
    # layout fragments. They are not DOM Elements, so paint them here; real
    # inline child elements are still visited by paint_tree below.
    for fragment in getattr(element, "_chromonic_inline_fragments", ()):
        paint_element(canvas, fragment)


def paint_tree(canvas: "skia.Canvas", root_element) -> None:
    """Paint `root_element` and every descendant, pre-order (a parent's
    background/border always land before its children's -- painting a real
    DOM, not `tree.py`'s `node_map`, which is populated post-order and would
    paint backwards if walked directly)."""
    paint_element(canvas, root_element)
    if getattr(root_element, "_chromonic_tag_name", None) == "select":
        return  # <option>s were never laid out (see paint_element) -- nothing to recurse into
    for child in (root_element.childNodes or []):
        if _is_element(child):
            paint_tree(canvas, child)


def build_display_list(root_element) -> list:
    """Flatten the laid-out DOM into stable paint order once per layout.

    The list stores element references rather than drawing commands because
    geometry is written back onto the domonic elements.  A scroll/expose can
    then iterate the flat list without recursively rediscovering the DOM.
    """
    result = []

    def walk(element):
        if element.__dict__.get("_layout_box") is not None:
            result.append(element)
        if getattr(element, "_chromonic_tag_name", None) == "select":
            return
        for child in (element.childNodes or []):
            if _is_element(child):
                walk(child)

    walk(root_element)
    return result


def paint_display_list(
    canvas: "skia.Canvas", display_list: list, *, top: float, bottom: float,
) -> int:
    """Paint entries whose border boxes intersect the document viewport.

    Each entry is tested independently.  A child that escapes an ancestor's
    box can still paint, which would be lost by pruning entire DOM subtrees.
    Returns the number painted, useful to profiling and diagnostics.
    """
    painted = 0
    for element in display_list:
        box = element.__dict__.get("_layout_box")
        if box is None or box.y + box.height < top or box.y > bottom:
            continue
        paint_element(canvas, element, box)
        painted += 1
    return painted


def render_png(root_element, *, width: int, height: int, background=None) -> bytes:
    surface = skia.Surface(width, height)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorWHITE if background is None else background)
    paint_tree(canvas, root_element)
    image = surface.makeImageSnapshot()
    data = image.encodeToData()
    return bytes(data)
