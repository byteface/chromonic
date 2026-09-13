from __future__ import annotations

import argparse
from pathlib import Path

from domonic.style import ComputedStyleDeclaration
from domonic import _fontmetrics
from myjs import Page

from chromonic import paint, tree, ua_style

from .schema import RECT_FIELDS, STYLE_PROPERTIES, VIEWPORT, result, write_json


def _rect_dict(x, y, width, height):
    return {"x": float(x), "y": float(y), "width": float(width), "height": float(height)}


def _fragments(element, rect):
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
            for index, text in enumerate(lines):
                leading = float(margins[3]) if index == 0 else 0.0
                trailing = float(margins[1]) if index == len(lines) - 1 else 0.0
                text_rects.append(_rect_dict(box.x - leading, box.y + index * line_height,
                                             (widths[index] if index < len(widths) else box.width) + leading + trailing,
                                             line_height) | {"text": text})
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
            if text[-1:].isspace():
                width -= _fontmetrics.advance_width(
                    " ", font_size, _fontmetrics.is_bold(paint_style.get("font_weight")))
            fragment_height = line_height
            if (str(paint_style.get("font_family", "")).strip().lower().startswith("arial")
                    and font_size >= 18 and line_height >= font_size + 2):
                fragment_height -= 1.0
            content_width = element.get_layout_box().client_width - padding[1] - padding[3]
            text_align = getattr(getattr(element, "_chromonic_computed_style", None),
                                 "textAlign", "start")
            align_offset = ((content_width - width) / 2 if text_align == "center"
                            else content_width - width if text_align in ("right", "end") else 0.0)
            text_rects.append(_rect_dict(
                rect.x + element.get_layout_box().border_left + padding[3] + align_offset,
                y + index * line_height, width, fragment_height,
            ) | {"text": text})
    return {"element": element_rects, "text": text_rects}


def run(fixture: Path, output: Path, screenshot: Path, *, viewport=VIEWPORT) -> dict:
    page = Page(fixture.read_text(), run=False)
    ua_style.apply(page.document)
    page.session.window._own["innerWidth"] = viewport[0]
    page.session.window._own["innerHeight"] = viewport[1]
    projection = tree.LayoutProjection()
    projection.layout(page.document.body, width=viewport[0], height=None, viewport_height=viewport[1])
    elements = {}
    marked = page.document.querySelectorAll("[data-layout], [data-layout-root] *")
    for element in marked:
        element_id = element.getAttribute("id")
        if not element_id:
            raise ValueError(f"data-layout element without id in {fixture.name}")
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
