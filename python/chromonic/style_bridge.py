"""domonic.layout.LayoutStyle -> the plain, FFI-trivial dict `chromonic._native`
understands.

See `../PLAN.md` ("The Rust<->Python boundary") for the vocabulary: a length
is a `float` (px), `("pct", fraction)`, `("fr", n)` (track sizes only), or
the string `"auto"`; a grid track can also be `"min-content"`/
`"max-content"`, `("fit-content", length)`, `("minmax", min, max)`, or
`("repeat", count, [track, ...])` (`count` an int or `"auto-fill"`/
`"auto-fit"`). This is a deliberately *narrow* translator -- it covers
exactly what the examples need, not the full `LayoutStyle` surface (named
grid lines/areas, `aspect-ratio`, `box-sizing` are all left for a real
second pass -- see PLAN.md's "Explicitly out of scope"). Absolute positioning's `inset`
(`top`/`right`/`bottom`/`left`) *is* modelled, added for phase 6's particle
demo -- `style.inset` is already the same `Edges` shape `margin` is, so it's
the same `_edges()` translation.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re

from domonic.layout import AUTO, Edges, Fr, GridLine, GridSpan, Keyword, Length, LayoutStyle, Percent, Ratio


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


def _non_negative_or_auto(value):
    """A negative `flex-basis` (`css-flexbox/flex-basis-004.html`: `-50px`)
    is an invalid declaration -- dropped by the cascade, leaving the
    initial `auto` (so `width: 30px` sizes the item). domonic keeps it,
    and Taffy would size the item at 0."""
    if isinstance(value, (int, float)) and value < 0:
        return "auto"
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "pct" and value[1] < 0:
        return "auto"
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


_TRACK_FUNCTION_RE = re.compile(r"^([a-z-]+)\((.*)\)$", re.I | re.S)
_NAMED_LINE_RE = re.compile(r"^\[.*\]$")


def _split_top_level(text: str, sep: str) -> list:
    """`text` split on `sep`, ignoring an occurrence nested inside `(...)`
    (`minmax(10px, 1fr)`'s own internal comma must not split `repeat(2,
    minmax(10px, 1fr))`'s outer count-from-tracks comma)."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _split_track_tokens(text: str) -> list:
    """The same whitespace-outside-parens splitting domonic's own
    `_split_track_list` does, applied to a `repeat()`'s own (already-
    extracted, still function-call-bearing) inner track list."""
    tokens, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _track_value(text: str):
    """One already-tokenized `grid-template-*`/`grid-auto-*` track (domonic
    hands a `repeat()`/`minmax()`/`fit-content()` call over as a single
    opaque `Keyword` -- CSS Grid 1 §7.2's whole function-call vocabulary is
    therefore parsed here, from the raw text, not through domonic's cascade
    at all) -> the shape `src/lib.rs`'s track parser accepts: a plain
    length, `("fr", n)`, `"auto"`/`"min-content"`/`"max-content"`,
    `("fit-content", length)`, or `("minmax", min, max)`. `None` for
    anything unrecognised (a named line `[foo]`, an unresolved custom
    property, a track function nesting deeper than CSS itself allows, ...)."""
    text = text.strip()
    low = text.lower()
    if low in ("auto", "min-content", "max-content"):
        return low
    if low.endswith("fr"):
        try:
            return ("fr", float(text[:-2]))
        except ValueError:
            return None
    if low.endswith("px"):
        try:
            return float(text[:-2])
        except ValueError:
            return None
    if low.endswith("%"):
        try:
            return ("pct", float(text[:-1]) / 100.0)
        except ValueError:
            return None
    match = _TRACK_FUNCTION_RE.match(text)
    if not match:
        return None
    name, inner = match.group(1).lower(), match.group(2)
    if name == "fit-content":
        # The argument is always a plain length/percentage (CSS Sizing 3
        # never lets `fit-content()` nest a keyword or another function).
        value = _track_value(inner)
        if isinstance(value, (int, float)) or (isinstance(value, tuple) and value[0] == "pct"):
            return ("fit-content", value)
        return None
    if name == "minmax":
        parts = _split_top_level(inner, ",")
        if len(parts) == 2:
            min_value, max_value = _track_value(parts[0]), _track_value(parts[1])
            if min_value is not None and max_value is not None:
                return ("minmax", min_value, max_value)
    return None


def _repeat_track_list(inner: str) -> "list | None":
    """`repeat()`'s own body, already split from its count argument --
    the (possibly multi-track, per CSS Grid 1 §7.2.3.1) space-separated
    list repeated each time; a bare `[line-name]` token is dropped
    (named lines aren't modelled -- see PLAN.md)."""
    tracks = []
    for token in _split_track_tokens(inner):
        if _NAMED_LINE_RE.match(token):
            continue
        value = _track_value(token)
        tracks.append(value if value is not None else ("fr", 1.0))
    return tracks or None


def _tracks(tracks: list) -> list:
    result = []
    for track in tracks:
        if isinstance(track, Keyword):
            text = track.value.strip()
            if _NAMED_LINE_RE.match(text):
                continue  # a bare `[line-name]` line-list entry, not a track itself
            match = _TRACK_FUNCTION_RE.match(text)
            if match and match.group(1).lower() == "repeat":
                parts = _split_top_level(match.group(2), ",")
                if len(parts) >= 2:
                    count_text = parts[0].strip().lower()
                    inner_tracks = _repeat_track_list(",".join(parts[1:]))
                    if inner_tracks is not None:
                        if count_text in ("auto-fill", "auto-fit"):
                            count = count_text
                        else:
                            try:
                                count = int(count_text)
                            except ValueError:
                                count = 1
                        result.append(("repeat", count, inner_tracks))
                        continue
            value = _track_value(text)
            result.append(value if value is not None else ("fr", 1.0))
        else:
            result.append(_len(track, default=("fr", 1.0)))
    return result


def _display(kw: Keyword) -> str:
    value = kw.value.strip()
    fallback = _SIMPLE_VAR_FALLBACK.match(value)
    if fallback:
        value = fallback.group(1).strip()
    if value in ("-ms-flexbox", "-webkit-flex", "inline-flex", "-webkit-inline-flex", "-ms-inline-flexbox"):
        # `inline-flex` is a flex container too -- only its *outer*
        # display differs (an atomic inline, handled by `tree.py`'s
        # inline paths); mapped to "block" before, its items stacked
        # vertically (`flexbox-align-self-horiz-001-block.xhtml`).
        return "flex"
    if value == "inline-grid":
        return "grid"
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


def _align_keyword(kw: Keyword, *, content: bool, inline_axis: bool = True) -> str:
    """CSS Box Alignment 3's full `align-*`/`justify-*` vocabulary, reduced
    to the subset `src/lib.rs`'s `parse_align_items`/`parse_align_content`
    accept (the old `_keyword()` pass-through let `safe center`, `last
    baseline`, `self-start` and friends reach the Rust side as-is, where the
    unrecognised keyword raised a `ValueError` for the whole page -- every
    `css-flexbox/abspos/*align-self*` fixture errored out that way).

    - `safe`/`unsafe` overflow-safety prefixes are dropped: Taffy has no
      overflow-position notion, and `unsafe` is exactly the plain keyword.
    - `first baseline` is `baseline`; `last baseline` (no Taffy equivalent)
      also falls back to `baseline` on the items axis, and to `flex-end` on
      the content axis where Box Alignment's fallback for it is `end`
      (`first`/plain `baseline` on `align-content` falls back to `start`).
    - `self-start`/`self-end` are the item's own writing-mode start/end;
      Taffy has no writing mode, so they are `start`/`end`.
    - `left`/`right` (justify-content/-self only) are physical; with no
      vertical writing modes here they are the same as `start`/`end`.
    - Anything still unknown (`anchor-center`, an unresolved `var()`...)
      becomes `normal` rather than an error.
    """
    parts = [part for part in kw.value.replace("-", " ").lower().split()
             if part not in ("safe", "unsafe")]
    if not parts:
        return "normal"
    if parts[-1] == "baseline":
        last = parts[0] == "last"
        if content:
            return "flex-end" if last else "flex-start"
        return "baseline"
    word = "-".join(parts)
    if word in ("left", "right") and not inline_axis:
        # CSS Box Alignment 3: `left`/`right` on an axis that isn't the
        # inline axis (justify-content in a column flex container,
        # flexbox-justify-content-vert-001a.xhtml) behave as `start`.
        return "start"
    word = {"self-start": "start", "self-end": "end", "left": "start", "right": "end",
            "space-between": "space-between", "space-around": "space-around",
            "space-evenly": "space-evenly", "flex-start": "flex-start",
            "flex-end": "flex-end"}.get(word, word)
    known = {"normal", "auto", "start", "end", "flex-start", "flex-end", "center", "stretch"}
    if content:
        known |= {"space-between", "space-around", "space-evenly"}
    return word if word in known else "normal"


def _flex_wrap(kw: Keyword) -> str:
    """`flex-wrap`, reduced to Taffy's `nowrap`/`wrap`/`wrap-reverse`. The
    tentative `balance` keyword (`css-flexbox/flex-wrap-balance-*.html`,
    `wrap-reverse balance`) asks for the lines to be balanced -- not
    modelled, so it wraps like plain `wrap`/`wrap-reverse` instead of
    erroring out of the whole page; anything else unknown is `nowrap`."""
    parts = kw.value.replace("-", " ").lower().split()
    reverse = "reverse" in parts
    if "wrap" in parts or "balance" in parts:
        return "wrap-reverse" if reverse else "wrap"
    return "nowrap"


def _flex_direction(kw: Keyword) -> str:
    value = kw.value.strip().lower()
    return value if value in ("row", "row-reverse", "column", "column-reverse") else "row"


def _aspect_ratio(value) -> "float | None":
    """CSS Sizing 4 `aspect-ratio` -> the plain `width/height` float
    `Style.aspect_ratio` (`src/lib.rs`) wants, or `None` for `auto` (no
    declared ratio -- a replaced element's own *intrinsic* ratio, set
    separately in `tree.py`'s `_apply_image_intrinsic_size`/etc., still
    applies as before). domonic's own parser already collapses the
    `auto <ratio>` / `<ratio> auto` combined syntax down to just the
    ratio half (dropping which form was written) -- CSS's own "prefer the
    replaced element's natural ratio when `auto` was *also* given"
    nuance isn't distinguishable from a bare `<ratio>` here, so a
    replaced element with both an intrinsic size and an `auto <ratio>`
    declaration incorrectly prefers the declared ratio outright. Not
    patched upstream; narrower than it looks in practice (a real page
    combining `aspect-ratio: auto <ratio>` with a sized image is rare)."""
    # CSS Sizing 4 `<ratio> = <number [0,inf]> [ / <number [0,inf]> ]?`,
    # but a *zero* value on either side makes the ratio invalid (the
    # underlying `<number>` production requires it to round-trip through
    # a real division), and domonic doesn't reject it -- read literally,
    # a `0/1` ratio divides down to `0.0`, and `1/0` would divide up to
    # `inf`, both of which crash something downstream expecting a finite
    # float (confirmed on `css-sizing/aspect-ratio/zero-or-infinity-
    # 001.html`: `OverflowError: cannot convert float infinity to
    # integer`). Treated as `auto` (no declared ratio) instead, same as
    # `AUTO` itself.
    if isinstance(value, Ratio) and value.width > 0 and value.height > 0:
        return value.width / value.height
    return None


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
        "flex_direction": _flex_direction(style.flexDirection),
        "flex_wrap": _flex_wrap(style.flexWrap),
        # A negative `flex-grow`/`flex-shrink` is an invalid declaration
        # (`css-flexbox/flex-shrink-002.html`: `flex-shrink: -2` is dropped,
        # leaving the initial `1`); domonic keeps it and Taffy would treat
        # it as a real negative factor.
        "flex_grow": (float(style.flexGrow) if not isinstance(style.flexGrow, Keyword)
                      and float(style.flexGrow) >= 0 else 0.0),
        "flex_shrink": (float(style.flexShrink) if not isinstance(style.flexShrink, Keyword)
                        and float(style.flexShrink) >= 0 else 1.0),
        "flex_basis": _non_negative_or_auto(_len(style.flexBasis)),
        "align_items": _align_keyword(style.alignItems, content=False),
        "align_self": _align_keyword(style.alignSelf, content=False),
        "align_content": _align_keyword(style.alignContent, content=True),
        "justify_content": _align_keyword(
            style.justifyContent, content=True,
            inline_axis=not _flex_direction(style.flexDirection).startswith("column")),
        # CSS Box Alignment 3: grid's own inline-axis item alignment --
        # `justify-self` (`justify-items` has no `LayoutStyle` field at
        # all; domonic doesn't recognise it as a property, so it's read
        # straight off the raw cascade in `tree.build()` instead, the
        # same workaround `justify-self`'s own field already relies on).
        "justify_self": _align_keyword(style.justifySelf, content=False),
        "grid_auto_flow": _keyword(style.gridAutoFlow),
        "grid_template_columns": _tracks(style.gridTemplateColumns),
        "grid_template_rows": _tracks(style.gridTemplateRows),
        # CSS Grid 1 §7.5: the size of a track the grid creates on demand
        # (an item placed/auto-placed past the explicit `grid-template-*`
        # tracks) -- absent, Taffy's own default (a single implicit
        # `auto` track, repeated as needed) already matches the CSS
        # initial value, so an empty list here is the correct default.
        "grid_auto_rows": _tracks(style.gridAutoRows),
        "grid_auto_columns": _tracks(style.gridAutoColumns),
        "grid_column": (_grid_line(style.gridColumnStart), _grid_line(style.gridColumnEnd)),
        "grid_row": (_grid_line(style.gridRowStart), _grid_line(style.gridRowEnd)),
        # CSS Sizing 4 `aspect-ratio` -- `None` (the common case, no
        # author declaration) leaves `tree.py`'s own replaced-element
        # intrinsic-ratio handling as the only source, exactly as before
        # this field existed at all.
        "aspect_ratio": _aspect_ratio(style.aspectRatio),
    }
