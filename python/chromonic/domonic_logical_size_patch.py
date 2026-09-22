"""CSS Logical Properties 1: `inline-size`/`block-size` (and their `min-`/
`max-` forms, and the `border-inline-start`-style side aliases) map onto
the physical `width`/`height`/... in a horizontal, left-to-right writing
mode -- exactly what domonic's own `_LOGICAL_ALIAS_LONGHANDS` table
already does for `margin-inline-start`, `padding-block-end`, `inset-
inline-start`, .... The size properties simply aren't in that table, so
`inline-size: min-content` (`css-flexbox/align-items-baseline-row-horz.
html`) or `block-size: 100px` reaches the cascade under its logical name
and is never read as a width/height at all -- the element stays `auto`.

Same in-place table update the other `domonic_*_patch` modules use; the
expansion itself (`style.py`'s tokenizer-level `emit(alias, ...)`) needs
no change. Only LTR horizontal writing modes are modelled, matching the
table's own existing caveat. Logged in PLAN.md."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures the real submodule is in `sys.modules`

_style = sys.modules["domonic.style"]

_SIZE_ALIASES = {
    "inline-size": "width",
    "block-size": "height",
    "min-inline-size": "min-width",
    "min-block-size": "min-height",
    "max-inline-size": "max-width",
    "max-block-size": "max-height",
    "border-inline-start": "border-left",
    "border-inline-end": "border-right",
    "border-block-start": "border-top",
    "border-block-end": "border-bottom",
    "border-inline-start-width": "border-left-width",
    "border-inline-end-width": "border-right-width",
    "border-block-start-width": "border-top-width",
    "border-block-end-width": "border-bottom-width",
    "border-inline-start-style": "border-left-style",
    "border-inline-end-style": "border-right-style",
    "border-block-start-style": "border-top-style",
    "border-block-end-style": "border-bottom-style",
    "border-inline-start-color": "border-left-color",
    "border-inline-end-color": "border-right-color",
    "border-block-start-color": "border-top-color",
    "border-block-end-color": "border-bottom-color",
}

_INSTALLED = False
_ADDED: list = []


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    table = getattr(_style, "_LOGICAL_ALIAS_LONGHANDS", None)
    if not isinstance(table, dict):
        return False
    for logical, physical in _SIZE_ALIASES.items():
        if logical not in table:
            table[logical] = physical
            _ADDED.append(logical)
    _INSTALLED = True
    return bool(_ADDED)


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    table = getattr(_style, "_LOGICAL_ALIAS_LONGHANDS", {})
    for logical in _ADDED:
        table.pop(logical, None)
    _ADDED.clear()
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
