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

from domonic.layout import AUTO, Edges, Fr, GridLine, GridSpan, Keyword, Length, LayoutStyle, Percent


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


def _non_negative(value):
    if isinstance(value, (int, float)):
        return max(0.0, value)
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "pct":
        # A negative percentage padding (`padding-top: -1%`) is just as
        # invalid as a negative pixel one -- CSS 2.1 8.4 doesn't carve out
        # an exception for percentages -- but the plain `int`/`float`
        # check above only ever caught a length that was *already*
        # resolved to pixels; a percentage stays a `("pct", fraction)`
        # tuple all the way to Taffy (resolved against the containing
        # block at layout time), so its own negative fraction sailed
        # through here untouched. Found on `wpt/css/CSS2/margin-padding-
        # clear/padding-top-089.xht`: `padding-top: -1%` reached Taffy as
        # `("pct", -0.01)` and resolved to a real `-0.96px`, instead of
        # being discarded like any other invalid negative padding.
        return ("pct", max(0.0, value[1]))
    return value


def _padding_edges(edges: Edges) -> list:
    # CSS 2.1 8.4: a negative `padding` is an invalid value, so the whole
    # declaration is dropped and padding stays at its initial value, `0`.
    # domonic's parser doesn't reject it (found via `wpt/css/CSS2/
    # margin-padding-clear/padding-left-001.xht`'s `padding-left: -1px`,
    # which shifted content left by a real pixel instead of being ignored)
    # -- clamping to zero here lands on the same outcome without needing a
    # real "was this declaration invalid" signal, since padding's initial
    # value already *is* zero.
    return [_non_negative(value) for value in _edges(edges)]


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
    # `sticky` stays in normal flow and only offsets once scrolled past its
    # threshold -- chromonic has no scroll-position simulation at all (it
    # always renders the initial, un-stuck scroll position), so the correct
    # approximation is the same one used for an ordinary in-flow element:
    # Taffy's "relative" (participates in flow; `inset` is ignored the same
    # way it would be for `static` -- see `to_dict()` below -- since nothing
    # here ever "sticks"). Previously grouped with `absolute`/`fixed`, which
    # incorrectly pulled every `position:sticky` element (a `sticky` header/
    # nav is extremely common) out of the flow entirely, collapsing its
    # containing block and everything after it.
    return "absolute" if kw.value in ("absolute", "fixed") else "relative"


def _keyword(kw: Keyword) -> str:
    # CSS keywords already match Taffy's vocabulary 1:1 except grid-auto-flow's
    # "row dense" / "column dense" (space-separated in CSS, hyphenated here).
    return kw.value.replace(" ", "-")


def _grid_line(value):
    """A `grid-column`/`grid-row` longhand value, in whatever shape
    `src/lib.rs`'s `parse_grid_placement` accepts: an explicit line number
    (`int`), `("span", N)` for `span N` (auto-placed, N tracks), or `None`
    for `auto`/an unmodelled named line."""
    if isinstance(value, GridLine):
        return value.line
    if isinstance(value, GridSpan):
        return ("span", value.count)
    return None


_AUTO_EDGES = ["auto", "auto", "auto", "auto"]


def to_dict(style: LayoutStyle) -> dict:
    """A `chromonic._native.Tree.new_leaf` / `new_with_children` / `set_style`
    style dict for one element's already-cascaded `LayoutStyle`."""
    # `top`/`right`/`bottom`/`left` never apply to a statically positioned
    # box (CSS 2.1 9.3.1) -- Taffy has no separate "static" position variant,
    # so `_position()` maps it onto "relative" the same as an actual
    # `position:relative`, which *does* apply `inset` as a post-layout
    # offset. Without this, an author who left stray `top`/`left` values on
    # an otherwise-static element (or a UA default that happens to carry
    # one) gets visibly shifted for no CSS-valid reason.
    # `sticky`'s inset only ever takes effect once actually scrolled past its
    # threshold; chromonic never simulates a scrolled state (always renders
    # the initial, un-stuck position), so an authored `top`/`left`/... on a
    # `position:sticky` element must be ignored here the same way it is for
    # `static`, not applied as a real offset the way it would be for a
    # genuine `position:relative`.
    inset = (_AUTO_EDGES if style.position.value in ("static", "sticky") else _edges(style.inset))
    return {
        "display": _display(style.display),
        "position": _position(style.position),
        "box_sizing": _keyword(style.boxSizing),
        "inset": inset,
        "width": _len(style.width),
        "height": _len(style.height),
        "min_width": _len(style.minWidth),
        "min_height": _len(style.minHeight),
        "max_width": _len(style.maxWidth),
        "max_height": _len(style.maxHeight),
        "margin": _edges(style.margin),
        "padding": _padding_edges(style.padding),
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
