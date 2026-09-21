from __future__ import annotations

import argparse
from pathlib import Path
import time

import chromonic

from domonic.style import ComputedStyleDeclaration
from domonic import _fontmetrics

from chromonic import browser, browser_images, fonts, paint, tree, webfonts
import math

from .schema import RECT_FIELDS, STYLE_PROPERTIES, VIEWPORT, result, write_json


def _rect_dict(x, y, width, height):
    return {"x": float(x), "y": float(y), "width": float(width), "height": float(height)}


def _fragments(element, rect):
    if element.get_layout_box() is None:
        return {"element": [], "text": []}
    inline_boxes = getattr(element, "_chromonic_inline_boxes", None)
    element_rects = ([_rect_dict(*box) for box in inline_boxes] if inline_boxes
                     else [_rect_dict(rect.x, rect.y, rect.width, rect.height)])
    fragments = getattr(element, "_chromonic_owned_fragments", None)
    if fragments is None:
        fragments = [fragment for fragment in
                     (getattr(element, "_chromonic_inline_fragments", None) or [])
                     if getattr(fragment, "owner", element) is element]
    if fragments:
        text_rects = []
        for fragment in fragments:
            box = fragment.__dict__.get("_layout_box")
            if box is None:
                continue
            lines = getattr(fragment, "_chromonic_text_lines", None) or []
            widths = getattr(fragment, "_chromonic_text_line_widths", [])
            line_height = float(getattr(fragment, "_chromonic_line_height", 0.0) or 0.0)
            raw = getattr(getattr(fragment, "source", None), "textContent", "") or ""
            margins = getattr(fragment, "_chromonic_native_style", {}).get("margin") or (0.0,) * 4
            # A text fragment's own reported rect is its *glyph* box
            # (ascent+descent), never the full line-height -- matching the
            # other, non-shared-plan branch below (already correct there)
            # and real `getClientRects()`, which never reports a line's
            # own leading as part of a text range's box. Confirmed on
            # `wpt/css/CSS2/visudet/inline-block-baseline-010.xht`
            # (`line-height:5`, i.e. 75px, on 15px text): this branch was
            # reporting `text[].height` as the full `75`, not the real
            # `17`, and `text[].y` at the *line's* own top instead of the
            # glyph's (offset by the line's own half-leading below it).
            fragment_paint_style = getattr(fragment, "_chromonic_paint_style", None) or {}
            glyph_font_size = _fontmetrics.parse_length(fragment_paint_style.get("font_size"), default=16.0)
            glyph_family = fragment_paint_style.get("font_family", "") or ""
            glyph_weight = tree._parse_font_weight(fragment_paint_style.get("font_weight"))
            glyph_italic = fonts.is_italic(fragment_paint_style.get("font_style"))
            glyph_ascent, glyph_descent, _normal = fonts.text_metrics(
                glyph_family, glyph_font_size, glyph_weight >= 600, glyph_italic)
            glyph_height = glyph_ascent + glyph_descent
            half_leading = math.floor((line_height - glyph_height) / 2) if line_height else 0.0
            for index, text in enumerate(lines):
                leading = float(margins[3]) if index == 0 else 0.0
                trailing = float(margins[1]) if index == len(lines) - 1 else 0.0
                text_rects.append(_rect_dict(box.x - leading, box.y + index * line_height + half_leading,
                                             (widths[index] if index < len(widths) else box.width) + leading + trailing,
                                             glyph_height) | {"text": text})
        return {"element": element_rects, "text": text_rects}
    lines = getattr(element, "_chromonic_text_lines", None) or []
    line_height = float(getattr(element, "_chromonic_line_height", 0.0) or 0.0)
    text_rects = []
    if lines and not getattr(element, "_chromonic_has_layout_children", False):
        padding = getattr(element, "_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        y = rect.y + element.get_layout_box().border_top + padding[0]
        widths = getattr(element, "_chromonic_text_line_widths", [])
        for index, text in enumerate(lines):
            width = widths[index] if index < len(widths) else rect.width
            paint_style = getattr(element, "_chromonic_paint_style", {})
            font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
            family = paint_style.get("font_family", "")
            weight = tree._parse_font_weight(paint_style.get("font_weight"))
            italic = fonts.is_italic(paint_style.get("font_style"))
            # `.isspace()`/plain `.rstrip()` both treat U+00A0 (`&nbsp;`) as
            # whitespace (it carries the Unicode "White_Space" property) --
            # CSS 2.1 16.6.1 never collapses/trims it, only plain ASCII
            # space/tab/newline/CR/FF. Found on `wpt/css/CSS2/positioning/
            # positioning-float-001.xht`: `<span>P A&nbsp;</span>`'s text
            # fragment measured `12px` narrower than the element's own
            # (correctly nbsp-inclusive) shrink-to-fit width -- the trailing
            # `&nbsp;` was silently stripped from this line's own re-measurement.
            if (text and text[-1] in tree._CSS_WHITESPACE_STRIP_CHARS
                    and paint_style.get("white_space") not in ("pre", "pre-wrap", "break-spaces")):
                width = tree.layout_text(text.rstrip(tree._CSS_WHITESPACE_STRIP_CHARS),
                                          family, font_size, font_weight=weight, italic=italic)[0]
            ascent, descent, _normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
            fragment_height = ascent + descent
            text_top = math.floor((line_height - fragment_height) / 2)
            content_width = element.get_layout_box().client_width - padding[1] - padding[3]
            computed_style = getattr(element, "_chromonic_computed_style", None)
            text_align = getattr(computed_style, "textAlign", "start")
            # CSS Text 3 `text-align-last`: the element's own final line uses
            # this instead, unless it's `auto` (same as `text-align`, except
            # `justify` -- whose last line is never itself justified).
            text_align_last = getattr(computed_style, "textAlignLast", "auto") or "auto"
            if index == len(lines) - 1 and text_align_last != "auto":
                text_align = text_align_last
            if text_align in ("start", "end"):
                # `start`/`end` resolve to a physical side based on
                # `direction` -- domonic's own cascade has no UA mapping for
                # HTML's `dir` attribute (`tree._element_direction` walks
                # the DOM for it directly; see its own docstring).
                is_rtl = tree._element_direction(element, computed_style) == "rtl"
                text_align = ("right" if is_rtl else "left") if text_align == "start" else (
                    "left" if is_rtl else "right")
            align_offset = ((content_width - width) / 2 if text_align == "center"
                            else content_width - width if text_align in ("right", "end") else 0.0)
            text_rects.append(_rect_dict(
                rect.x + element.get_layout_box().border_left + padding[3] + align_offset,
                y + index * line_height + text_top, width, fragment_height,
            ) | {"text": text})
            # DOM Range includes a zero-width rectangle for a preserved line
            # break in addition to the glyph rectangle preceding it.
            if text.endswith('\n') and paint_style.get("white_space") in ("pre", "pre-wrap", "break-spaces"):
                text_rects.append(_rect_dict(
                    rect.x + element.get_layout_box().border_left + padding[3] + align_offset + width,
                    y + index * line_height + text_top, 0, fragment_height,
                ) | {"text": "\n"})
    return {"element": element_rects, "text": text_rects}


def run(fixture: Path, output: Path, screenshot: Path, *, viewport=VIEWPORT, load_url: "str | None" = None) -> dict:
    # `load_url`, when given, is loaded instead of the local file path --
    # for a fixture whose stylesheets/fonts reference absolute-root paths
    # (`/fonts/ahem.css`, common in the real web-platform-tests suite),
    # only a real HTTP(S) URL resolves those correctly; `browser.load()`
    # already fetches over HTTP(S) or the filesystem based on `_is_url()`.
    page = browser.load(load_url if load_url is not None else str(fixture.resolve()))
    registry = webfonts.registry(page.document.body)
    if registry is not None:
        for face in registry.faces:
            try:
                face.future.result(timeout=15)
            except Exception:
                pass  # poll records fetch/decode failures with the family name.
        registry.poll()
        if registry.errors:
            raise RuntimeError("font loading failed: " + "; ".join(registry.errors))
    browser.set_viewport(page, viewport[0], viewport[1])
    projection = tree.LayoutProjection()
    image_generation = browser_images.generation()
    projection.layout(page.document.body, width=viewport[0], height=None, viewport_height=viewport[1])
    deadline = time.monotonic() + 15
    while browser_images.has_pending():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"images did not finish loading for {fixture.name}")
        time.sleep(0.01)
    # Initial layout starts image requests; capture their final intrinsic sizes.
    if browser_images.generation() != image_generation:
        projection.layout(page.document.body, width=viewport[0], height=None, viewport_height=viewport[1])
    elements = {}
    marked = page.document.querySelectorAll("[data-layout], [data-layout-root] [id]")
    for element in marked:
        element_id = element.getAttribute("id")
        if not element_id:
            raise ValueError(f"data-layout element without id in {fixture.name}")
        if element_id in elements:
            raise ValueError(f"duplicate data-layout id {element_id!r} in {fixture.name}")
        rect = element.getBoundingClientRect()
        computed = getattr(element, "_chromonic_computed_style", None)
        if computed is None:
            computed = ComputedStyleDeclaration(element)
        elements[element_id] = {
            "rect": {field: float(getattr(rect, field)) for field in RECT_FIELDS},
            "style": {name: computed.getPropertyValue(name) for name in STYLE_PROPERTIES},
            "fragments": _fragments(element, rect),
        }
    captured = result(fixture.name, "chromonic", elements, viewport)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "ours.json", captured)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(paint.render_png(
        page.document.body, width=viewport[0], height=viewport[1],
    ))
    return captured


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    run(args.fixture, args.output, args.output / "ours.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
