"""CSS `ch` (CSS Values 4 6.2: the advance width of the `"0"` (U+0030
DIGIT ZERO) glyph in the element's own current font) is never resolved to
a pixel length by domonic -- the same gap `domonic_ex_unit_patch.py`
documents for `ex`: `_length_string_to_px`'s unit table doesn't know
`ch` at all, so a value like `width: 10ch` falls through to
`Keyword("10ch")`, an opaque, unresolved string `style_bridge._len()`
then silently discards.

Found on `wpt/css/CSS2/normal-flow/block-in-inline-align-001.html` (and
its sibling `block-in-inline-align-*` fixtures): `section { width: 20ch }`
/ `div { width: 10ch }`, with no `font-family` declared (so the page's
UA-default font applies) -- Taffy fell back to the full available width
(`784px`) for both, instead of `160px`/`80px`, cascading into every
downstream alignment/geometry assertion in those fixtures (their real
target, `text-align` not moving a block-in-inline split, was never
actually exercised -- it was drowned out by the containing width being
wrong first).

Patches the exact same function `domonic_ex_unit_patch.py` does
(`domonic.layout._parse_length_or_percent`), for the exact same reason
(that's the one place both the *value* and the resolved *font* are known
together for every length-valued layout property) -- chained around
whatever is already installed there (captured as `_ORIGINAL_PARSE_LENGTH_
OR_PERCENT` at this module's own import time), so installing both this
and the `ex` patch in either order still resolves both units correctly:
each one checks its own suffix first and falls back to whatever the
other already installed otherwise."""
from __future__ import annotations

import re
import sys

import domonic.layout  # noqa: F401 -- ensures `domonic.layout` is in `sys.modules`

from . import fonts as _fonts
from . import webfonts as _webfonts

_layout = sys.modules["domonic.layout"]

_INSTALLED = False
_ORIGINAL_PARSE_LENGTH_OR_PERCENT = _layout._parse_length_or_percent

_CH_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))ch$", re.I)


def _is_italic(style_value: "str | None") -> bool:
    return (style_value or "").strip().lower() in ("italic", "oblique")


def _weight_value(value) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 700.0 if value == "bold" else 400.0


def _resolve_ch_px(text: str, computed) -> "float | None":
    match = _CH_RE.match(text.strip())
    if not match:
        return None
    number = float(match.group(1))
    family = getattr(computed, "fontFamily", "") or ""
    if family == "none":
        family = ""
    weight = _weight_value(getattr(computed, "fontWeight", None))
    italic = _is_italic(getattr(computed, "fontStyle", None))
    # Same downloaded-`@font-face`-uses-a-private-alias substitution
    # `domonic_ex_unit_patch.py`'s own `_resolve_ex_px` already needs and
    # documents -- `resolve_typeface("Ahem")` alone would silently
    # measure the wrong (system fallback) font's "0" glyph otherwise.
    registry = _webfonts.registry(getattr(computed, "_element", None))
    if registry is not None:
        family = registry.family_list(family, weight, italic)
    zero_width = _fonts.zero_advance_width(family, computed._font_size_px(), bold=weight >= 600, italic=italic)
    return number * zero_width


def _parse_length_or_percent_with_ch(raw, computed, **kwargs):
    text = (raw or "").strip()
    if text:
        resolved = _resolve_ch_px(text, computed)
        if resolved is not None:
            return _layout.Length(resolved)
    return _ORIGINAL_PARSE_LENGTH_OR_PERCENT(raw, computed, **kwargs)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _layout._parse_length_or_percent = _parse_length_or_percent_with_ch
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
