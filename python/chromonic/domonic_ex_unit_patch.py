"""CSS `ex` (CSS 2.1 4.3.2: the current font's x-height) is never resolved
to a pixel length -- domonic's own length tokenizer already accepts the
syntax (including a leading `+`/`-` sign: `_LENGTH_TOKEN_RE` matches any
`[a-z%]*` unit suffix), but its unit table (`_ABSOLUTE_LENGTH_UNITS`,
`_length_string_to_px`) only knows `px`/`pt`/`cm`/`mm`/`in`/`q`/`pc`/`em`/
`rem`/`%` -- so a value like `padding-top: 6ex` falls all the way through
to `Keyword("6ex")`, the same "left as an opaque, unresolved string" outcome
`domonic_border_width_keyword_patch.py` documents for `thin`/`medium`/
`thick`. `style_bridge._len()` then has no idea what to do with that
`Keyword` (it only special-cases `vw`/`vh`/`vmin`/`vmax`) and falls back to
`default` -- silently discarding the declared value instead of resolving
it.

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
(`layout.py`'s own `_from_computed`, `dim()`/`edges()` closures) -- patching
this one function generically covers all of them in one place, exactly the
"do not special-case one property" the underlying CSS behaviour already
implies (`ex` is a length unit like any other, not a padding-specific
concept).

Only this one function is patched: `LayoutStyle._from_computed` (the
cascade -> layout path chromonic's own `style_bridge.to_dict()` actually
consumes) is what needs `ex` for real layout geometry; `getComputedStyle()`
(`ComputedStyleDeclaration._to_used_length`) is a separate path chromonic
doesn't rely on for layout, left unpatched (deliberately, not an oversight)."""
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

_INSTALLED = False
_ORIGINAL_PARSE_LENGTH_OR_PERCENT = _layout._parse_length_or_percent

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
    # (set by `LayoutStyle._from_computed` itself) is enough to find this
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


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _layout._parse_length_or_percent = _parse_length_or_percent_with_ex
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _layout._parse_length_or_percent = _ORIGINAL_PARSE_LENGTH_OR_PERCENT
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
