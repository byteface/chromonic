from __future__ import annotations

import json
from pathlib import Path

VIEWPORT = (800, 600)
RECT_FIELDS = ("x", "y", "width", "height")
STYLE_PROPERTIES = (
    "display", "position", "box-sizing", "overflow-x", "overflow-y",
    "width", "height", "min-width", "min-height", "max-width", "max-height",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "font-size", "line-height", "flex-direction", "flex-wrap", "flex-grow",
    "flex-shrink", "flex-basis", "align-items", "align-self", "align-content",
    "justify-content", "grid-auto-flow", "grid-template-columns", "grid-template-rows",
    "color", "background-color", "background-image", "border-radius",
    "visibility", "opacity", "z-index", "white-space", "text-align",
    "vertical-align", "font-family", "font-weight", "font-style",
    "list-style-type", "table-layout",
)


def result(fixture: str, engine: str, elements: dict, viewport=VIEWPORT) -> dict:
    return {
        "schema": 2,
        "fixture": fixture,
        "engine": engine,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "elements": elements,
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
