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
import urllib.parse
from collections import namedtuple

import skia

from domonic import _fontmetrics
from domonic.style import ComputedStyleDeclaration

from . import fonts

ELEMENT_NODE = 1
_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
_URL_RE = re.compile(r"url\(\s*(?:\"([^\"]*)\"|\'([^\']*)\'|([^)^\"\'\s][^)]*?))\s*\)", re.I)


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


def _background_image_url(value: "str | None") -> "str | None":
    match = _URL_RE.search(value or "")
    if not match:
        return None
    return next((group.strip() for group in match.groups() if group is not None), None)


def _split_top_level(text: "str | None") -> "list[str]":
    """Split a CSS value on commas that aren't inside `url(...)`/`fn(...)`
    -- `background-image`/`-size`/`-position`/`-repeat` are each their own
    comma-separated list, one entry per layer, and a `url(...)` can itself
    contain a comma (a `data:` URI) that must not split it."""
    parts, current, depth = [], [], 0
    for char in text or "":
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts


def _layer_value(values: "list[str]", index: int, default: str) -> str:
    """CSS repeats a shorter `background-*` list to match the number of
    `background-image` layers (spec: "if there are more comma-separated
    images than values for a property, the values are repeated")."""
    return values[index % len(values)] if values else default


_BG_SIZE_KEYWORDS = {"cover", "contain"}


def _resolve_layer_size(size_token: str, natural_w: float, natural_h: float,
                         box_w: float, box_h: float) -> "tuple[float, float]":
    token = (size_token or "auto").strip().lower()
    if not natural_w or not natural_h:
        return (box_w, box_h) if token in _BG_SIZE_KEYWORDS else (natural_w, natural_h)
    if token == "cover":
        scale = max(box_w / natural_w, box_h / natural_h)
        return natural_w * scale, natural_h * scale
    if token == "contain":
        scale = min(box_w / natural_w, box_h / natural_h)
        return natural_w * scale, natural_h * scale

    def component(part: "str | None", axis_box: float) -> "float | None":
        if not part or part == "auto":
            return None
        if part.endswith("%"):
            try:
                return axis_box * float(part[:-1]) / 100.0
            except ValueError:
                return None
        try:
            return float(part.rstrip("px"))
        except ValueError:
            return None

    parts = token.split()
    width = component(parts[0] if parts else None, box_w)
    height = component(parts[1] if len(parts) > 1 else None, box_h)
    if width is None and height is None:
        return float(natural_w), float(natural_h)
    if width is None:
        width = natural_w * (height / natural_h)
    if height is None:
        height = natural_h * (width / natural_w)
    return width, height


_BG_POS_H_KEYWORDS = {"left": "0%", "right": "100%", "center": "50%"}
_BG_POS_V_KEYWORDS = {"top": "0%", "bottom": "100%", "center": "50%"}


def _resolve_layer_position(pos_token: str, layer_w: float, layer_h: float,
                             box_w: float, box_h: float) -> "tuple[float, float]":
    tokens = (pos_token or "0% 0%").strip().lower().split() or ["0%", "0%"]
    if len(tokens) == 1:
        tokens = [tokens[0], "center"]
    x_token, y_token = tokens[0], tokens[1]
    # Keyword pairs are order-independent ("top right" as well as "right
    # top"); a lone vertical keyword in the first slot (or horizontal in
    # the second) means they were written swapped from the usual x-then-y.
    if x_token in ("top", "bottom") or y_token in ("left", "right"):
        x_token, y_token = y_token, x_token
    x_value = _BG_POS_H_KEYWORDS.get(x_token, x_token)
    y_value = _BG_POS_V_KEYWORDS.get(y_token, y_token)

    def offset(value: str, axis_box: float, axis_layer: float) -> float:
        if value.endswith("%"):
            try:
                fraction = float(value[:-1]) / 100.0
            except ValueError:
                return 0.0
            return (axis_box - axis_layer) * fraction
        try:
            return float(value.rstrip("px"))
        except ValueError:
            return 0.0

    return offset(x_value, box_w, layer_w), offset(y_value, box_h, layer_h)


def _background_image_candidate_urls(url: str, doc) -> "list[str]":
    """Absolute URLs `url` could resolve to, most-likely-correct first.

    A CSS `url()` resolves relative to the stylesheet it was written in,
    not the page -- `webfonts.py` already gets this right for `@font-face
    src` (using `sheet.href` as the base), but `background-image` had no
    such handling at all and just resolved against the *page's* URL
    unconditionally, silently wrong the moment a site's stylesheet and its
    referenced images live in different directories (i.e. almost any real
    site with more than a bare `index.html` + one flat folder). Found on
    `csszengarden.com`: `header`'s `background-image: url(huntington.jpg)`
    lives in `/214/214.css`, so the real image is at `/214/huntington.jpg`
    -- resolved against the page's own `/` URL it 404s outright.

    Since a computed style's resolved string has no record of *which*
    stylesheet's rule actually won, this tries the plausible candidates
    instead of guessing one: every stylesheet with an `href` (most likely
    real source first, page URL last as the correct answer for an inline
    `style=""` background-image, and as an original-behaviour fallback).
    `browser_images.load_image` caches a failed fetch permanently and is
    cheap to call for an already-cached URL, so asking it about several
    candidates costs at most a handful of harmless extra requests -- once,
    ever, per broken guess, not per paint."""
    if urllib.parse.urlsplit(url).scheme:
        return [url]
    bases = []
    for sheet in getattr(doc, "styleSheets", None) or ():
        href = getattr(sheet, "href", None)
        if href:
            bases.append(href)
    bases.append(getattr(doc, "_chromonic_base_url", ""))
    seen = set()
    candidates = []
    for base in bases:
        resolved = urllib.parse.urljoin(base, url)
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def _paint_background_layer(canvas: "skia.Canvas", box, doc,
                             url_token: str, size_token: str, pos_token: str,
                             repeat_token: str) -> None:
    from . import browser_images

    raw_url = _background_image_url(url_token)
    if not raw_url:
        return
    image = None
    for candidate in _background_image_candidate_urls(raw_url, doc):
        image = browser_images.load_image(candidate)
        if image is not None:
            break
    if image is None:
        return
    natural_w, natural_h = float(image.width()), float(image.height())
    if natural_w <= 0 or natural_h <= 0:
        return
    layer_w, layer_h = _resolve_layer_size(size_token, natural_w, natural_h, box.width, box.height)
    if layer_w <= 0 or layer_h <= 0:
        return
    x, y = _resolve_layer_position(pos_token, layer_w, layer_h, box.width, box.height)
    repeat = (repeat_token or "repeat").strip().lower()
    canvas.save()
    try:
        canvas.clipRect(skia.Rect.MakeXYWH(box.x, box.y, box.width, box.height))
        if repeat == "no-repeat":
            canvas.drawImageRect(image, skia.Rect.MakeXYWH(box.x + x, box.y + y, layer_w, layer_h))
            return
        tile_x = skia.TileMode.kRepeat if repeat in ("repeat", "repeat-x") else skia.TileMode.kDecal
        tile_y = skia.TileMode.kRepeat if repeat in ("repeat", "repeat-y") else skia.TileMode.kDecal
        matrix = skia.Matrix()
        matrix.setTranslate(box.x + x, box.y + y)
        matrix.preScale(layer_w / natural_w, layer_h / natural_h)
        shader = image.makeShader(tile_x, tile_y, skia.SamplingOptions(), matrix)
        canvas.drawRect(skia.Rect.MakeXYWH(box.x, box.y, box.width, box.height),
                         skia.Paint(Shader=shader, AntiAlias=True))
    finally:
        canvas.restore()


def _paint_background_image(canvas: "skia.Canvas", element, box, style: dict) -> None:
    """`background-image` is a comma-separated list of independent layers
    (the first listed paints *on top*), each with its own `-size`/
    `-position`/`-repeat` -- a shorter list of any of those three repeats
    to match however many image layers there are (CSS Backgrounds 3 §3.7).
    Painted back-to-front (`reversed`) so the first-listed layer really
    does end up on top of the rest, the same stacking a real browser uses.
    Found on `csszengarden.com`'s `<header>`: `background-image: url(a),
    url(b), url(c), url(d)` (a decorative overlay pattern stacked on the
    real, `background-size: cover`'d photo as the *last* layer) -- painting
    only the first `url(...)` match (the old, single-layer-only behaviour)
    painted the subtle overlay texture alone and never the actual photo,
    which looked indistinguishable from "no background image at all"."""
    images = _split_top_level(style.get("background_image"))
    if not images or images == ["none"]:
        return
    doc = getattr(element, "ownerDocument", None)
    sizes = _split_top_level(style.get("background_size"))
    positions = _split_top_level(style.get("background_position"))
    repeats = _split_top_level(style.get("background_repeat"))
    for index in reversed(range(len(images))):
        _paint_background_layer(
            canvas, box, doc, images[index],
            _layer_value(sizes, index, "auto"),
            _layer_value(positions, index, "0% 0%"),
            _layer_value(repeats, index, "repeat"),
        )


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


#: One rendered line of an element's own text: `x`/`baseline_y` match exactly
#: what `canvas.drawString(text, x, baseline_y, font, ...)` is given for it,
#: so a caller measuring/highlighting against `font`/`text` lands on the same
#: pixels that actually got painted. `element` is whichever node the text
#: belongs to (the owning element, or an anonymous inline-fragment "element"
#: for mixed inline content -- see `paint_element`'s own `fragments` handling).
TextRun = namedtuple("TextRun", "x baseline_y width height font text element")


def text_line_runs(element, box, style):
    """This element's own text, broken into per-line paint geometry.

    Extracted out of `paint_element` so text-selection hit-testing/highlight
    rendering (`native_browser.py`) can share the *exact* same line-wrapping,
    alignment, and baseline math the drawing path uses, rather than a second,
    independently-derived approximation that could silently drift from what's
    actually on screen. `paint_element` is the only other caller."""
    lines = getattr(element, "_chromonic_text_lines", None)
    if lines is None:
        from . import tree as _tree
        text = " ".join(_tree._rendering_text_content(element).split())
        lines = [text] if text else []
    if not lines or not any(lines):
        return
    padding = getattr(element, "_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
    pad_top, pad_right, _pad_bottom, pad_left = padding
    font_size = _px(style["font_size"], 16.0)
    bold = _fontmetrics.is_bold(style["font_weight"])
    italic = fonts.is_italic(style["font_style"])
    font = _font(font_size, bold=bold, italic=italic, family=style["font_family"])
    # Parley's own real per-font line height (`tree.py`'s `_make_measure`),
    # not a re-derived Helvetica-table guess -- falls back to the same rough
    # multiple used before Parley, for anything painted without a prior
    # `tree.layout()` pass.
    line_height = getattr(element, "_chromonic_line_height", None) or font_size * 1.2
    text_x = box.x + box.border_left + pad_left
    # A table cell's `vertical-align: middle`/`bottom` pushes its own text
    # down inside the (row-height) box -- see `tree._align_table_cell_content`.
    content_offset_y = float(getattr(element, "_chromonic_content_offset_y", 0.0) or 0.0)
    line_widths = getattr(element, "_chromonic_text_line_widths", [])
    # Lines shortened by floats start further right / lower (`tree`'s
    # `_make_measure`); None means x=0 and `line_height` apart.
    line_xs = getattr(element, "_chromonic_text_line_x", None) or []
    line_ys = getattr(element, "_chromonic_text_line_y", None) or []
    line_avail = getattr(element, "_chromonic_text_line_avail", None) or []
    content_width = box.client_width - pad_left - pad_right
    align = (style.get("text_align") or "").strip().lower()
    # CSS Text 3 `text-align-last`: a block's own final formatted line uses
    # this instead, when set to something other than the `auto` default
    # (which just means "same as text-align").
    align_last = (style.get("text_align_last") or "auto").strip().lower()
    if align in ("start", "", "end") or align_last in ("start", "end"):
        from . import tree as _tree
        is_rtl = _tree._element_direction(element) == "rtl"
        if align in ("start", ""):
            align = "right" if is_rtl else "left"
        elif align == "end":
            align = "left" if is_rtl else "right"
        if align_last == "start":
            align_last = "right" if is_rtl else "left"
        elif align_last == "end":
            align_last = "left" if is_rtl else "right"
    for index, line in enumerate(lines):
        if not line:
            continue
        line_align = align_last if (index == len(lines) - 1 and align_last != "auto") else align
        line_x = text_x + (line_xs[index] if index < len(line_xs) else 0.0)
        line_room = line_avail[index] if index < len(line_avail) else content_width
        width = line_widths[index] if index < len(line_widths) else font.measureText(line)
        if line_align == "center":
            line_x += max(0.0, (line_room - width) / 2.0)
        elif line_align in ("right", "end"):
            line_x += max(0.0, line_room - width)
        # a simple top-aligned baseline per line, line_height apart
        line_top = line_ys[index] if index < len(line_ys) else index * line_height
        baseline_y = box.y + box.border_top + pad_top + content_offset_y + font_size + line_top
        yield TextRun(x=line_x, baseline_y=baseline_y, width=width, height=line_height,
                       font=font, text=line, element=element)


def _numeric_attr(element, name: str, default: float) -> float:
    raw = element.getAttribute(name) if hasattr(element, "getAttribute") else None
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _paint_gauge_fill(canvas: "skia.Canvas", element, box, tag_name: str) -> None:
    """<progress>/<meter>'s filled portion -- neither has real OS widget
    chrome to fall back on, so this is chromonic's own flat-fill approximation."""
    minimum = _numeric_attr(element, "min", 0.0)
    maximum = _numeric_attr(element, "max", 1.0)
    value = _numeric_attr(element, "value", minimum)
    span = maximum - minimum
    fraction = 0.0 if span <= 0 else max(0.0, min(1.0, (value - minimum) / span))
    inset = box.border_top + 1.0
    fill_color = skia.Color4f(0.2, 0.55, 0.2, 1) if tag_name == "progress" else skia.Color4f(0.25, 0.45, 0.75, 1)
    width = max(0.0, (box.width - 2 * inset) * fraction)
    height = max(0.0, box.height - 2 * inset)
    if width > 0 and height > 0:
        canvas.drawRect(
            skia.Rect.MakeXYWH(box.x + inset, box.y + inset, width, height),
            skia.Paint(Color4f=fill_color, AntiAlias=True),
        )


def _paint_range_slider(canvas: "skia.Canvas", element, box) -> None:
    minimum = _numeric_attr(element, "min", 0.0)
    maximum = _numeric_attr(element, "max", 100.0)
    value = _numeric_attr(element, "value", (minimum + maximum) / 2.0)
    span = maximum - minimum
    fraction = 0.0 if span <= 0 else max(0.0, min(1.0, (value - minimum) / span))
    track_y = box.y + box.height / 2.0
    track_paint = skia.Paint(Color4f=skia.Color4f(0.7, 0.7, 0.7, 1), AntiAlias=True, Style=skia.Paint.kStroke_Style)
    track_paint.setStrokeWidth(3.0)
    thumb_radius = max(1.0, min(7.0, box.height / 2.0 - 1.0))
    canvas.drawLine(box.x + thumb_radius, track_y, box.x + box.width - thumb_radius, track_y, track_paint)
    thumb_x = box.x + thumb_radius + fraction * max(0.0, box.width - 2 * thumb_radius)
    canvas.drawCircle(thumb_x, track_y, thumb_radius, skia.Paint(Color4f=skia.Color4f(0.2, 0.45, 0.85, 1), AntiAlias=True))


def _paint_disclosure_triangle(canvas: "skia.Canvas", element, box) -> None:
    """A <summary>'s marker -- real browsers give it a ::marker box outside
    its content; chromonic instead reserves space via `summary`'s own
    `padding-left` (ua_style.py) and paints directly into that padding."""
    details = getattr(element, "parentElement", None)
    is_open = bool(
        details is not None
        and (getattr(details, "tagName", "") or "").lower() == "details"
        and getattr(details, "open", False)
    )
    size = 4.5
    cx = box.x + box.border_left + 8.0
    cy = box.y + box.height / 2.0
    path = skia.Path()
    if is_open:
        path.moveTo(cx - size, cy - size / 2)
        path.lineTo(cx + size, cy - size / 2)
        path.lineTo(cx, cy + size / 2)
    else:
        path.moveTo(cx - size / 2, cy - size)
        path.lineTo(cx - size / 2, cy + size)
        path.lineTo(cx + size / 2, cy)
    path.close()
    canvas.drawPath(path, skia.Paint(Color4f=skia.Color4f(0.2, 0.2, 0.2, 1), AntiAlias=True))


def _paint_listbox_rows(canvas: "skia.Canvas", element, box, style) -> None:
    """`<select multiple>`/`size` -- every option as its own always-visible
    row filling the box `builder.py` sized for exactly this many rows,
    instead of the classic closed dropdown's single selected-option line."""
    from . import tree as _tree
    row_height = _tree.replaced_elements.LISTBOX_ROW_HEIGHT
    font_size = _px(style["font_size"], 13.3333)
    font = _font(font_size, family=style["font_family"])
    ink = skia.Paint(Color4f=_color(style["color"]) or skia.Color4f(0, 0, 0, 1), AntiAlias=True)
    for index, option in enumerate(element.options):
        row_top = box.y + box.border_top + index * row_height
        if row_top >= box.y + box.height:
            break
        if option.selected:
            canvas.drawRect(
                skia.Rect.MakeXYWH(box.x + box.border_left, row_top, box.client_width, row_height),
                skia.Paint(Color=0xffcce4ff, AntiAlias=True),
            )
        canvas.drawString(option.textContent or '', box.x + box.border_left + 4.0,
                           row_top + row_height - 5.0, font, ink)


def _paint_form_control_decoration(canvas: "skia.Canvas", element, box, tag_name: str) -> None:
    """Extra chromonic-painted decoration a form control needs beyond its
    generic background/border/text (already painted above in `paint_element`):
    a checkbox/radio's checked mark, a range slider's track+thumb, a color
    swatch's fill, a progress/meter's gauge fill, a <summary>'s disclosure
    triangle. None of these have real OS widget chrome to fall back on --
    this is chromonic's own closest approximation of each."""
    if tag_name == "input":
        input_type = (getattr(element, "type", "") or "text").lower()
        if input_type in ("checkbox", "radio") and getattr(element, "checked", False):
            inset = box.border_top + 3.0
            ink = skia.Paint(Color4f=skia.Color4f(0.13, 0.13, 0.13, 1), AntiAlias=True)
            if input_type == "radio":
                cx, cy = box.x + box.width / 2.0, box.y + box.height / 2.0
                radius = max(0.0, min(box.width, box.height) / 2.0 - inset)
                if radius > 0:
                    canvas.drawCircle(cx, cy, radius, ink)
            else:
                w, h = max(0.0, box.width - 2 * inset), max(0.0, box.height - 2 * inset)
                if w > 0 and h > 0:
                    canvas.drawRect(skia.Rect.MakeXYWH(box.x + inset, box.y + inset, w, h), ink)
        elif input_type == "range":
            _paint_range_slider(canvas, element, box)
        elif input_type == "color":
            swatch = _color(getattr(element, "value", "") or "#000000") or skia.Color4f(0, 0, 0, 1)
            inset = box.border_top + 2.0
            w, h = max(0.0, box.width - 2 * inset), max(0.0, box.height - 2 * inset)
            if w > 0 and h > 0:
                canvas.drawRect(
                    skia.Rect.MakeXYWH(box.x + inset, box.y + inset, w, h),
                    skia.Paint(Color4f=swatch, AntiAlias=True),
                )
    elif tag_name in ("progress", "meter"):
        _paint_gauge_fill(canvas, element, box, tag_name)
    elif tag_name == "summary":
        _paint_disclosure_triangle(canvas, element, box)


def _paint_video(canvas: "skia.Canvas", element, box) -> None:
    """The current decoded frame, same stretch-to-fill/no-object-fit
    default as `_paint_image` -- see that function's own comment. No
    frame yet (still starting up, no source, or ffmpeg/ffprobe missing):
    paint nothing extra, leaving just the `video { background-color:
    black }` UA default (ua_style.py) as the placeholder, the same way a
    real player shows a black rect before its first frame."""
    from . import video_backend
    decoder = video_backend.decoder_for(element)
    if decoder is None:
        return
    image = decoder.current_frame_image()
    if image is None:
        return
    padding = getattr(element, "_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
    pad_top, pad_right, pad_bottom, pad_left = padding
    x = box.x + box.border_left + pad_left
    y = box.y + box.border_top + pad_top
    width = box.client_width - pad_left - pad_right
    height = box.client_height - pad_top - pad_bottom
    if width <= 0 or height <= 0:
        return
    canvas.drawImageRect(image, skia.Rect.MakeXYWH(x, y, width, height))


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
    _paint_background_image(canvas, element, box, style)

    tag_name = getattr(element, "_chromonic_tag_name", None)
    if tag_name is None:
        tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name == "img":
        _paint_image(canvas, element, box)
    elif tag_name == "canvas":
        from . import canvas2d
        canvas2d.paint_element(canvas, element, box)
    elif tag_name == "video":
        _paint_video(canvas, element, box)

    is_radio = tag_name == "input" and (getattr(element, "type", "") or "").lower() == "radio"
    if is_radio and box.border_top > 0:
        # No general border-radius support to reach for here (paint.py
        # doesn't track it at all yet) -- a radio's round *outer* edge is
        # special-cased the same way its inner checked-mark already is
        # (`_paint_form_control_decoration`), rather than a square border
        # on what's supposed to read as a circle. Always uniform in
        # practice (ua_style.py), so `box.border_top` alone is fine here.
        border_color = _color(style["border_top_color"]) or skia.Color4f(0, 0, 0, 1)
        paint = skia.Paint(Color4f=border_color, AntiAlias=True, Style=skia.Paint.kStroke_Style)
        inset = box.border_top / 2.0
        paint.setStrokeWidth(box.border_top)
        cx, cy = box.x + box.width / 2.0, box.y + box.height / 2.0
        radius = max(0.0, min(box.width, box.height) / 2.0 - inset)
        canvas.drawCircle(cx, cy, radius, paint)
    elif not is_radio:
        # Each side painted independently, in its own width/colour -- a
        # single rect-stroke (this used to be one `drawRect` sized off
        # `box.border_top` alone) can't represent a `border-bottom`-only
        # box, or differing per-side widths/colours, at all: with every
        # other side truly at 0 width, `box.border_top == 0` skipped
        # painting anything, silently, even though 3 of the 4 sides were
        # real declared borders. Corners aren't mitred (each edge is a
        # plain stroked line, not a mitred quad) -- a real gap, just a
        # smaller one than "renders nothing" was.
        native = getattr(element, "_chromonic_native_style", None) or {}
        top, right, bottom, left = native.get("border") or (box.border_top, 0.0, 0.0, box.border_left)
        black = skia.Color4f(0, 0, 0, 1)
        if top > 0:
            paint = skia.Paint(Color4f=_color(style["border_top_color"]) or black, AntiAlias=True)
            paint.setStrokeWidth(top)
            canvas.drawLine(box.x, box.y + top / 2.0, box.x + box.width, box.y + top / 2.0, paint)
        if right > 0:
            paint = skia.Paint(Color4f=_color(style["border_right_color"]) or black, AntiAlias=True)
            paint.setStrokeWidth(right)
            x = box.x + box.width - right / 2.0
            canvas.drawLine(x, box.y, x, box.y + box.height, paint)
        if bottom > 0:
            paint = skia.Paint(Color4f=_color(style["border_bottom_color"]) or black, AntiAlias=True)
            paint.setStrokeWidth(bottom)
            y = box.y + box.height - bottom / 2.0
            canvas.drawLine(box.x, y, box.x + box.width, y, paint)
        if left > 0:
            paint = skia.Paint(Color4f=_color(style["border_left_color"]) or black, AntiAlias=True)
            paint.setStrokeWidth(left)
            canvas.drawLine(box.x + left / 2.0, box.y, box.x + left / 2.0, box.y + box.height, paint)

    if tag_name in ("input", "progress", "meter", "summary"):
        _paint_form_control_decoration(canvas, element, box, tag_name)

    # `<select>`'s `<option>` children are real DOM children but were never
    # given a layout box -- tree.py treats it as childless too (see its
    # `_select_display_text`), so paint must agree, or it would (a) skip
    # this element's own text (the "not children" branch below never runs)
    # and (b) recurse into `paint_tree` for each <option>, painting nothing
    # useful since none of them has a `get_layout_box()` to paint from.
    is_listbox = tag_name == "select" and getattr(element, "_chromonic_listbox_rows", 0)
    if is_listbox:
        _paint_listbox_rows(canvas, element, box, style)
    has_layout_children = getattr(element, "_chromonic_has_layout_children", None)
    if has_layout_children is None:
        has_layout_children = tag_name != "select" and any(
            _is_element(child) for child in (element.childNodes or [])
        )
    if not has_layout_children and not is_listbox:
        text_color = _color(style["color"]) or skia.Color4f(0, 0, 0, 1)
        paint_ = skia.Paint(Color4f=text_color, AntiAlias=True)
        # `overflow: hidden`/`clip` (ua_style.py's default for input/
        # textarea/select among others) must clip this element's own text
        # to its padding edge, same as the fragments branch below already
        # does -- otherwise a textarea with more lines than fit its height,
        # or a line wider than its width, paints straight through the
        # box's own border into whatever's next to/below it.
        clips_own_text = style.get("overflow_x") in ("hidden", "clip") or style.get("overflow_y") in ("hidden", "clip")
        if clips_own_text:
            canvas.save()
            canvas.clipRect(skia.Rect.MakeXYWH(
                box.x + box.border_left, box.y + box.border_top,
                box.client_width, box.client_height,
            ))
        try:
            for run in text_line_runs(element, box, style):
                canvas.drawString(run.text, run.x, run.baseline_y, run.font, paint_)
        finally:
            if clips_own_text:
                canvas.restore()

    # Direct text nodes in mixed inline content (and any `::before`/
    # `::after` generated-content box, see `tree.py`'s `_PseudoElement`)
    # have retained anonymous layout fragments. They are not DOM Elements,
    # so paint them here; real inline child elements are still visited by
    # paint_tree below.
    fragments = getattr(element, "_chromonic_inline_fragments", ())
    if fragments:
        # `overflow: hidden`/`clip` clips a box's own content to its
        # padding edge -- real, common pattern for exactly the elements
        # that reach this list: an icon-font `::before` deliberately
        # positioned over text pushed below the visible box via padding,
        # so the real (accessible, for screen readers) text never
        # actually shows (`csszengarden.com`'s footer nav links: `<a>
        # HTML</a>`, `overflow:hidden; height:40px; padding-top:40px`,
        # `::before{content:"5"}` as the visible icon glyph -- without
        # this, chromonic painted the literal word "HTML" *and* the icon,
        # overlapping). Scoped to this element's own retained fragments,
        # not a general clip-stack for real DOM descendants (a much larger
        # feature this project doesn't have yet) -- sufficient for the
        # overwhelmingly common case of an otherwise-childless element
        # whose only "children" are text/generated-content fragments.
        clips = style.get("overflow_x") in ("hidden", "clip") or style.get("overflow_y") in ("hidden", "clip")
        if clips:
            canvas.save()
            canvas.clipRect(skia.Rect.MakeXYWH(
                box.x + box.border_left, box.y + box.border_top,
                box.client_width, box.client_height,
            ))
        try:
            for fragment in fragments:
                paint_element(canvas, fragment)
        finally:
            if clips:
                canvas.restore()


def paint_tree(canvas: "skia.Canvas", root_element) -> None:
    """Paint `root_element` and every descendant, pre-order (a parent's
    background/border always land before its children's -- painting a real
    DOM, not `tree.py`'s `node_map`, which is populated post-order and would
    paint backwards if walked directly)."""
    paint_element(canvas, root_element)
    if getattr(root_element, "_chromonic_tag_name", None) == "select":
        return  # <option>s were never laid out (see paint_element) -- nothing to recurse into
    for child in (root_element.childNodes or []):
        if not _is_element(child):
            continue
        # An open <dialog> is painted separately, centred over everything
        # else (`native_browser.py`'s `draw_open_dialogs`) -- skipped here
        # so it isn't also painted once more at its in-flow document
        # position.
        if (getattr(child, "_chromonic_tag_name", None) or "").lower() == "dialog":
            continue
        paint_tree(canvas, child)
    _paint_anonymous_boxes(canvas, root_element)


def _paint_anonymous_boxes(canvas: "skia.Canvas", element) -> None:
    """CSS 2.1 anonymous boxes `tree.py` generated under `element`
    (`_AnonymousTableBox`: anonymous tables/rows/cells and the anonymous
    block around loose text) live outside the DOM -- their own boxes (a
    text-only anonymous block/cell paints its lines) are painted here; the
    real nodes they wrap are already reached by the DOM walk above."""
    for anonymous in (element.__dict__.get("_chromonic_anonymous_table_boxes") or {}).values():
        if anonymous.__dict__.get("_layout_box") is not None:
            paint_element(canvas, anonymous)
        _paint_anonymous_boxes(canvas, anonymous)


def build_display_list(root_element) -> list:
    """Flatten the laid-out DOM into stable paint order once per layout.

    The list stores element references rather than drawing commands because
    geometry is written back onto the domonic elements.  A scroll/expose can
    then iterate the flat list without recursively rediscovering the DOM.
    """
    result = []

    def walk_anonymous(element):
        # Same anonymous boxes `_paint_anonymous_boxes` covers for the
        # direct paint path -- own boxes only, never their real children.
        for anonymous in (element.__dict__.get("_chromonic_anonymous_table_boxes") or {}).values():
            if anonymous.__dict__.get("_layout_box") is not None:
                result.append(anonymous)
            walk_anonymous(anonymous)

    def walk(element):
        # An open <dialog> paints separately, centred on top of everything
        # else (`native_browser.py`'s `draw_open_dialogs`) -- excluded here,
        # subtree included, so it isn't also painted at its in-flow position.
        if (getattr(element, "_chromonic_tag_name", None) or "").lower() == "dialog":
            return
        if element.__dict__.get("_layout_box") is not None:
            result.append(element)
        if getattr(element, "_chromonic_tag_name", None) == "select":
            return
        for child in (element.childNodes or []):
            if _is_element(child):
                walk(child)
        walk_anonymous(element)

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
