"""Chrome's absolute `font-size` keyword table, in place of domonic's.

domonic scales every keyword from `medium` by CSS 2's old 1.2 ratio
(`small` 13.333px, `large` 18.667px). Every browser instead uses the HTML
font-size table (Blink `FontSizeFunctions::FontSizeForKeyword`, the
"quirks" table shared by all modes for a 16px medium): `xx-small` 9,
`x-small` 10, `small` 13, `medium` 16, `large` 18, `x-large` 24,
`xx-large` 32, `xxx-large` 48. Confirmed on `wpt/css/CSS2/tables/table-
height-algorithm-012.xht`: a `font-size: large` "Filler Text" measures
74.9px in Chrome (18px Times) and 77.7px here (18.667px), `small` 54.1
against 55.5 -- same face, wrong size. Not patched upstream; logged in
PLAN.md. The table is a module-level dict in `domonic.style`, updated in
place so every reader (`_font_size_px`, the shorthand expansion) sees it."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]

_CHROME_KEYWORDS = {
    "xx-small": 9.0,
    "x-small": 10.0,
    "small": 13.0,
    "medium": 16.0,
    "large": 18.0,
    "x-large": 24.0,
    "xx-large": 32.0,
    "xxx-large": 48.0,
}

_INSTALLED = False
_PREVIOUS: dict = {}


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    table = getattr(_style, "_ABSOLUTE_FONT_SIZE_KEYWORDS", None)
    if not isinstance(table, dict):
        return False
    _PREVIOUS.clear()
    _PREVIOUS.update(table)
    table.update(_CHROME_KEYWORDS)
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    table = getattr(_style, "_ABSOLUTE_FONT_SIZE_KEYWORDS", None)
    if isinstance(table, dict):
        table.clear()
        table.update(_PREVIOUS)
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
