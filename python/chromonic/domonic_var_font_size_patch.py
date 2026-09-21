"""`ComputedStyleDeclaration._font_size_px` -- the single source every other
used-length resolution in domonic's `style.py` goes through (`calc()`'s
`em`, `line-height`'s unitless multiplier, every other property's own
`em`-relative tokens in `_to_used_length`) -- reads its own element's raw
`font-size` straight from `self._resolved.get("font-size")` and parses it as
a length without ever expanding a `var()` reference in it first, unlike
every *other* property: `_compute_property_value`'s generic path explicitly
calls `_expand_var_references(value, self._custom_property)` before handing
a value to `_to_used_length`, but `_to_used_length`'s own `target ==
"font-size"` branch bypasses that already-expanded `value` argument
entirely and recomputes from the unexpanded raw via `_font_size_px()`.

Confirmed directly: given `:root{--defined:20px}` and `.v{font-size:var(
--defined)}`, `getPropertyValue("font-size")` on `.v` returns `"16px"` (its
*parent's* font-size, `_length_string_to_px("var(--defined)", ...)` fails to
parse and falls back to inherited size) instead of `"20px"`. This is common
in practice, not a synthetic edge case -- Wikipedia's Vector skin (and
effectively every modern site using CSS custom-property design tokens)
declares `font-size: var(--font-size-small, 0.875rem)` throughout, so every
element styled that way silently renders at the wrong, inherited size
instead of its own. Not patched upstream.

Patched by wrapping `_font_size_px`: expand any `var()` in the raw
`font-size` value the same way the generic property path already does,
before parsing it as a length -- falls through to the original
implementation unchanged for the (much more common) case where there's no
`var()` to expand at all."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]
_ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_ORIGINAL_FONT_SIZE_PX = _ComputedStyleDeclaration._font_size_px


def _font_size_px_with_var_expansion(self):
    cached = self.__dict__.get("_font_size_px_cache")
    if cached is not None:
        return cached
    raw = str(self._resolved.get("font-size") or "").strip()
    if "var(" not in raw.lower():
        return _ORIGINAL_FONT_SIZE_PX(self)
    parent = self._parent_computed()
    parent_px = parent._font_size_px() if parent is not None else 16.0
    expanded = _style._expand_var_references(raw, self._custom_property).strip()
    if "calc(" in expanded.lower():
        px = _style._eval_calc_to_px(expanded, em_px=parent_px, rem_px=self._root_font_size_px())
    else:
        px = _style._length_string_to_px(
            expanded, em_px=parent_px, rem_px=self._root_font_size_px(), percent_px=parent_px,
        )
    if px is None:
        px = _style._ABSOLUTE_FONT_SIZE_KEYWORDS.get(expanded.lower(), parent_px)
    self.__dict__["_font_size_px_cache"] = px
    return px


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _ComputedStyleDeclaration._font_size_px = _font_size_px_with_var_expansion
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _ComputedStyleDeclaration._font_size_px = _ORIGINAL_FONT_SIZE_PX
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
