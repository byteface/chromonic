"""domonic's cascade leaves a shorthand unexpanded whenever its value has
whitespace inside parentheses (`_expand_shorthands_for_cascade`'s
`_has_paren_internal_whitespace` guard). The guard is meant for a `var()`
with a fallback (`var(--gap, 4px)`, whose inner space isn't a token
boundary), but it also catches every ordinary function value -- and the
unexpanded shorthand never resolves later, so the whole declaration is
lost: `border-top: solid 12px rgba(19, 67, 71, 0.8)` computes to no
border, `background: linear-gradient(to right, ...)` to no image.

Found on csszengarden.com (the header's top border, the body's two-column
gradient). `domonic._cssom.expand_shorthand` splits both correctly, so the
guard is narrowed to values that actually contain a `var()`."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]
_ORIGINAL = _style._has_paren_internal_whitespace

_INSTALLED = False


def _var_with_inner_whitespace(value: str) -> bool:
    return "var(" in value.lower() and _ORIGINAL(value)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._has_paren_internal_whitespace = _var_with_inner_whitespace
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._has_paren_internal_whitespace = _ORIGINAL
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
