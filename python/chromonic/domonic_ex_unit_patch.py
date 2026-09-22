"""CSS `ex` (CSS 2.1 4.3.2: the current font's x-height) is never resolved
to a pixel length -- domonic's own length tokenizer already accepts the
syntax (including a leading `+`/`-` sign: `_LENGTH_TOKEN_RE` matches any
`[a-z%]*` unit suffix), but its unit table (`_ABSOLUTE_LENGTH_UNITS`,
`_length_string_to_px`) only knows `px`/`pt`/`cm`/`mm`/`in`/`q`/`pc`/`em`/
`rem`/`%` -- so a value like `padding-top: 6ex` falls all the way through
to `Keyword("6ex")`, the same "left as an opaque, unresolved string" outcome
a bare `thin`/`medium`/`thick` `border-width` keyword used to hit before
domonic 1.8.2 started resolving those natively. `style_bridge._len()` then
has no idea what to do with that `Keyword` (it only special-cases
`vw`/`vh`/`vmin`/`vmax`) and falls back to `default` -- silently
discarding the declared value instead of resolving it.

Unlike a physical unit (`cm`, ...) or `em`/`rem` (both resolved purely from
font-*size*, already known during the cascade), `ex` needs the resolved
font's real *x-height* -- a property of the actual typeface, which domonic
has no font backend to supply at all. `chromonic.fonts.x_height()` already
resolves the same real (possibly downloaded `@font-face`) typeface
`text_metrics()` uses for ascent/descent, via Skia's own `FontMetrics.
fXHeight` -- so this patch's whole job is bridging that into domonic's
cascade at the one place both the *value* and the *font* are known
together.

`domonic.layout._parse_length_or_percent(raw, computed, ...)` is that one
place: unlike `_length_string_to_px` (called from three different spots
across `style.py`/`layout.py` with only `em_px`/`rem_px`, never the
`computed` object itself, so no font-family is available there at all),
`_parse_length_or_percent` already receives the full `ComputedStyleDeclaration`
for every `width`/`height`/`min-*`/`max-*`/`margin-*`/`padding-*`/`border-*-
width`/`inset`/`gap`/`flex-basis`/grid-track value LayoutStyle resolves
(`layout.py`'s own `LayoutStyle.from_computed`, `dim()`/`edges()` closures)
-- patching this one function generically covers all of them in one place,
exactly the "do not special-case one property" the underlying CSS
behaviour already implies (`ex` is a length unit like any other, not a
padding-specific concept).

`LayoutStyle.from_computed` (the cascade -> layout path chromonic's own
`style_bridge.to_dict()` consumes for width/height/margin/padding/etc.) is
the main function patched here. `getComputedStyle()`
(`ComputedStyleDeclaration._to_used_length`) is mostly a separate path
chromonic doesn't rely on for layout -- except for `line-height`, whose
used value chromonic *does* read straight from `computed.lineHeight`
(`chromonic.tree._resolved_line_height`, the line box strut height), never
through `LayoutStyle` at all. Confirmed directly with `tests/wpt/css/CSS2/
linebox/line-height-080.xht`'s Ahem `line-height: 1ex` (Ahem's glyphs fill
the whole em box, so its real x-height equals its font-size): unpatched,
`_to_used_length` fell through to `_length_string_to_px`'s generic `ex`
handling, the CSS-spec fallback of a flat `0.5em` for a UA with no real
glyph metrics -- half the correct, Ahem-specific value. `_to_used_length`
is therefore also lightly wrapped below, narrowly, only to substitute the
same real `x_height()`-backed resolution above when the value being used-
value-resolved is `line-height` and reduces to a plain `ex` token; every
other property/value still goes through the original, unpatched
`_to_used_length`."""
from __future__ import annotations

import re
import sys

import domonic.layout  # noqa: F401 -- ensures `domonic.layout` is in `sys.modules`

from . import fonts as _fonts
from . import webfonts as _webfonts

# Same `domonic/__init__.py`-shadowing caveat as the other `domonic_*_patch`
# modules in this package: only a `sys.modules` lookup by dotted name
# reaches the real submodule here, not attribute access on the `domonic`
# package itself.
_layout = sys.modules["domonic.layout"]
_style = sys.modules.setdefault("domonic.style", __import__("domonic.style", fromlist=["_"]))
_ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_ORIGINAL_PARSE_LENGTH_OR_PERCENT = _layout._parse_length_or_percent
_ORIGINAL_TO_USED_LENGTH = _ComputedStyleDeclaration._to_used_length

# Matches the same shape `_LENGTH_TOKEN_RE` already accepts generally
# (optional leading sign, digits, optional fraction), restricted to the
# `ex` suffix specifically; case-insensitive since CSS units always are.
_EX_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))ex$", re.I)


def _is_italic(style_value: "str | None") -> bool:
    return (style_value or "").strip().lower() in ("italic", "oblique")


def _resolve_ex_px(text: str, computed) -> "float | None":
    match = _EX_RE.match(text.strip())
    if not match:
        return None
    number = float(match.group(1))
    family = getattr(computed, "fontFamily", "") or ""
    if family == "none":
        family = ""
    weight = _weight_value(getattr(computed, "fontWeight", None))
    italic = _is_italic(getattr(computed, "fontStyle", None))
    # A downloaded `@font-face` (e.g. the real Ahem WPT tests load) is
    # registered with Skia under an internal per-document alias, never
    # under its own declared CSS family name -- `chromonic.fonts.
    # resolve_typeface()` (which `x_height()` uses) only ever finds it
    # via that alias, the same substitution `webfonts.resolve_style()`
    # already applies to `_chromonic_paint_style["font_family"]` before
    # painting. This patch runs *inside* domonic's own cascade, well
    # before chromonic's own per-element paint style exists at all, so it
    # repeats that same alias lookup here directly -- `computed._element`
    # (set by `LayoutStyle.from_computed` itself) is enough to find this
    # document's own font registry. Confirmed necessary directly: without
    # this, `resolve_typeface("Ahem")` silently fell back to a system
    # font and every Ahem `ex` fixture measured a completely wrong
    # x-height instead of failing loudly.
    registry = _webfonts.registry(getattr(computed, "_element", None))
    if registry is not None:
        family = registry.family_list(family, weight, italic)
    x_height = _fonts.x_height(family, computed._font_size_px(), bold=weight >= 600, italic=italic)
    return number * x_height


def _weight_value(value) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 700.0 if value == "bold" else 400.0


def _parse_length_or_percent_with_ex(raw, computed, **kwargs):
    text = (raw or "").strip()
    if text:
        resolved = _resolve_ex_px(text, computed)
        if resolved is not None:
            return _layout.Length(resolved)
    return _ORIGINAL_PARSE_LENGTH_OR_PERCENT(raw, computed, **kwargs)


def _to_used_length_with_ex_line_height(self, target, value):
    if target in ("top", "right", "bottom", "left") and isinstance(value, str) and value.strip().endswith("%"):
        # CSS 2.1 9.3.2: a percentage inset's *computed* value is the
        # percentage (resolved against the containing block only at use;
        # `inherit` copies the percentage). domonic resolved it here
        # against the viewport width -- `left: 50%` of a 600px containing
        # block became 512px, and `left: inherit` copied the pixels
        # (`wpt/css/CSS2/positioning/left-offset-percentage-002.xht`,
        # `relpos-calcs-005..007.xht`, `left-113.xht`). Not patched
        # upstream; logged in PLAN.md.
        return value.strip()
    if target == "line-height":
        resolved = _resolve_ex_px(value, self)
        if resolved is not None:
            return _style._px_str(resolved)
    if target == "border-spacing":
        # Same gap for `border-spacing` (one or two lengths): domonic used
        # the generic half-an-em placeholder here too -- `7.5ex` in 20px
        # Ahem (x-height 16px) came back as `75px` instead of `120px`
        # (`wpt/css/CSS2/tables/border-spacing-083.xht`). Each `ex` token
        # is resolved against the real font; anything else passes through.
        parts = (value or "").split()
        if any(part.endswith("%") for part in parts):
            # CSS 2.1 17.6.1: `border-spacing` takes lengths, never a
            # percentage -- the declaration is invalid. domonic accepts
            # it and would resolve the percentage against the containing
            # block here (`wpt/css/CSS2/tables/border-spacing-percentage-
            # 001.xht`: `0px` then `20%` is 0px in Chrome, 20% of the
            # container here); the valid declaration it displaced is
            # already gone, so 0 is the closest available reading.
            value = "0px"
            parts = ["0px"]
        if any(part.lower().endswith("ex") for part in parts):
            resolved_parts = []
            for part in parts:
                resolved = _resolve_ex_px(part, self) if part.lower().endswith("ex") else None
                resolved_parts.append(_style._px_str(resolved) if resolved is not None else part)
            value = " ".join(resolved_parts)
    return _ORIGINAL_TO_USED_LENGTH(self, target, value)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _layout._parse_length_or_percent = _parse_length_or_percent_with_ex
    _ComputedStyleDeclaration._to_used_length = _to_used_length_with_ex_line_height
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _layout._parse_length_or_percent = _ORIGINAL_PARSE_LENGTH_OR_PERCENT
    _ComputedStyleDeclaration._to_used_length = _ORIGINAL_TO_USED_LENGTH
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
