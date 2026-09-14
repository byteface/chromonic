"""domonic.layout.LayoutStyle -> the plain, FFI-trivial dict `chromonic._native`
understands.

See `../PLAN.md` ("The Rust<->Python boundary") for the vocabulary: a length
is a `float` (px), `("pct", fraction)`, `("fr", n)` (track sizes only), or
the string `"auto"`. This is a deliberately *narrow* translator -- it covers
exactly what the examples need, not the full `LayoutStyle` surface (named
grid lines/areas, `repeat()`/`minmax()`/`fit-content()` tracks,
`aspect-ratio`, `box-sizing` are all left for a real second pass -- see
PLAN.md's "Explicitly out of scope"). Absolute positioning's `inset`
(`top`/`right`/`bottom`/`left`) *is* modelled, added for phase 6's particle
demo -- `style.inset` is already the same `Edges` shape `margin` is, so it's
the same `_edges()` translation.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re

from domonic.layout import AUTO, Edges, Fr, GridLine, Keyword, Length, LayoutStyle, Percent


_VIEWPORT = ContextVar("chromonic_style_viewport", default=(None, None))
_VIEWPORT_LENGTH = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(vw|vh|vmin|vmax)$", re.I
)
_SIMPLE_VAR_FALLBACK = re.compile(r"^var\([^,]+,\s*([^)]+)\)$", re.I)


@contextmanager
def viewport(width, height):
    """Resolve viewport units for one tree projection without global state."""
    token = _VIEWPORT.set((float(width) if width is not None else None,
                           float(height) if height is not None else None))
    try:
        yield
    finally:
        _VIEWPORT.reset(token)


def _len(value, default="auto"):
    if value is AUTO:
        return "auto"
    if isinstance(value, Length):
        return float(value.px)
    if isinstance(value, Percent):
        return ("pct", float(value.fraction))
    if isinstance(value, Fr):
        return ("fr", float(value.value))
    if isinstance(value, Keyword):
        match = _VIEWPORT_LENGTH.match(value.value.strip())
        if match:
            width, height = _VIEWPORT.get()
            unit = match.group(2).lower()
            basis = (width if unit == "vw" else height if unit == "vh"
                     else min(width, height) if unit == "vmin" and None not in (width, height)
                     else max(width, height) if unit == "vmax" and None not in (width, height)
                     else None)
            if basis is not None:
                return float(match.group(1)) * basis / 100.0
    # Keyword (min-content, fit-content(), an unresolved calc()...) or a
    # Ratio/GridSpan/named GridLine landing here by mistake -- none of these
    # are in this POC's vocabulary, so fall back rather than guess.
    return default


def _edges(edges: Edges) -> list:
    return [_len(edges.top), _len(edges.right), _len(edges.bottom), _len(edges.left)]


_REPEAT_TRACK = re.compile(r"^repeat\(\s*(\d+)\s*,\s*([^(),]+)\s*\)$", re.I)


def _keyword_track(value: str):
    value = value.strip().lower()
    if value.endswith("fr"):
        try:
            return ("fr", float(value[:-2]))
        except ValueError:
            return None
    if value.endswith("px"):
        try:
            return float(value[:-2])
        except ValueError:
            return None
    if value.endswith("%"):
        try:
            return ("pct", float(value[:-1]) / 100.0)
        except ValueError:
            return None
    return "auto" if value == "auto" else None


def _tracks(tracks: list) -> list:
    result = []
    for track in tracks:
        if isinstance(track, Keyword):
            repeated = _REPEAT_TRACK.match(track.value)
            if repeated:
                parsed = _keyword_track(repeated.group(2))
                if parsed is not None:
                    result.extend([parsed] * int(repeated.group(1)))
                    continue
            parsed = _keyword_track(track.value)
            result.append(parsed if parsed is not None else ("fr", 1.0))
        else:
            result.append(_len(track, default=("fr", 1.0)))
    return result


def _display(kw: Keyword) -> str:
    value = kw.value.strip()
    fallback = _SIMPLE_VAR_FALLBACK.match(value)
    if fallback:
        value = fallback.group(1).strip()
    if value in ("-ms-flexbox", "-webkit-flex"):
        return "flex"
    if value in ("flex", "grid", "none"):
        return value
    return "block"  # inline, inline-block, list-item, table, ... -- not modelled here


def _position(kw: Keyword) -> str:
    return "absolute" if kw.value in ("absolute", "fixed", "sticky") else "relative"


def _keyword(kw: Keyword) -> str:
    # CSS keywords already match Taffy's vocabulary 1:1 except grid-auto-flow's
    # "row dense" / "column dense" (space-separated in CSS, hyphenated here).
    return kw.value.replace(" ", "-")


def _grid_line(value) -> "int | None":
    return value.line if isinstance(value, GridLine) else None  # AUTO / GridSpan / named -- not modelled


def to_dict(style: LayoutStyle) -> dict:
    """A `chromonic._native.Tree.new_leaf` / `new_with_children` / `set_style`
    style dict for one element's already-cascaded `LayoutStyle`."""
    return {
        "display": _display(style.display),
        "position": _position(style.position),
        "box_sizing": _keyword(style.boxSizing),
        "inset": _edges(style.inset),
        "width": _len(style.width),
        "height": _len(style.height),
        "min_width": _len(style.minWidth),
        "min_height": _len(style.minHeight),
        "max_width": _len(style.maxWidth),
        "max_height": _len(style.maxHeight),
        "margin": _edges(style.margin),
        "padding": _edges(style.padding),
        "border": _edges(style.borderWidth),
        "gap": (_len(style.gap.row, default=0.0), _len(style.gap.column, default=0.0)),
        "flex_direction": _keyword(style.flexDirection),
        "flex_wrap": _keyword(style.flexWrap),
        "flex_grow": float(style.flexGrow) if not isinstance(style.flexGrow, Keyword) else 0.0,
        "flex_shrink": float(style.flexShrink) if not isinstance(style.flexShrink, Keyword) else 1.0,
        "flex_basis": _len(style.flexBasis),
        "align_items": _keyword(style.alignItems),
        "align_self": _keyword(style.alignSelf),
        "align_content": _keyword(style.alignContent),
        "justify_content": _keyword(style.justifyContent),
        "grid_auto_flow": _keyword(style.gridAutoFlow),
        "grid_template_columns": _tracks(style.gridTemplateColumns),
        "grid_template_rows": _tracks(style.gridTemplateRows),
        "grid_column": (_grid_line(style.gridColumnStart), _grid_line(style.gridColumnEnd)),
        "grid_row": (_grid_line(style.gridRowStart), _grid_line(style.gridRowEnd)),
    }
