"""`ComputedStyleDeclaration._to_used_color` substitutes a resolved
`currentcolor` reference via `re.sub(r"(?i)\\bcurrentcolor\\b", current,
value)` -- passing `current` (an arbitrary, page-controlled string) straight
through as `re.sub`'s *replacement* argument, which Python's `re` module
parses as a template: a literal backslash followed by digits (`\\1`...`\\99`)
means "substitute capture group N", not "insert this literal text". Most
resolved colors never contain a backslash and this never bites, but nothing
guarantees that -- confirmed directly with `tests/wpt/css/CSS2/colors/
colors-007.xht`'s `#escape{color: g\\re\\45n}` (a CSS identifier escape
sequence -- `\\r` is a literal escaped `r`, `\\45` is the hex escape for
`E`, unescaping to the keyword `green`): domonic's own escape-sequence
handling leaves `getPropertyValue("color")` still containing the raw
`\\45` substring unresolved, and `re.sub` then reads it as "backreference
group 45", which doesn't exist -- `re.PatternError: invalid group reference
45 at position 5`, aborting the *entire* layout pass for the page over one
CSS declaration, not just mis-coloring that one element. (The escape-
sequence parsing gap that leaves `\\45` unresolved in the first place is a
separate, deeper issue in domonic's CSS tokenizer, not fixed here -- this
patch only stops it from being able to crash the process.)

Patched by wrapping `_to_used_color`: the same substitution, done through
`re.sub`'s *function* form (a lambda returning `current` verbatim) instead
of its string-template form, which is never interpreted as backreferences
regardless of what `current` contains."""
from __future__ import annotations

import re
import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]
_ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_ORIGINAL_TO_USED_COLOR = _ComputedStyleDeclaration._to_used_color
_CURRENTCOLOR_RE = re.compile(r"(?i)\bcurrentcolor\b")


def _to_used_color_with_safe_replace(self, target, value):
    if target != "color" and "currentcolor" in value.lower():
        current = self.getPropertyValue("color")
        if current and current.lower() != "currentcolor":
            value = _CURRENTCOLOR_RE.sub(lambda _match: current, value)
    normalized = _style._cssom.normalize_color(value)
    return normalized if normalized is not None else value


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _ComputedStyleDeclaration._to_used_color = _to_used_color_with_safe_replace
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _ComputedStyleDeclaration._to_used_color = _ORIGINAL_TO_USED_COLOR
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
