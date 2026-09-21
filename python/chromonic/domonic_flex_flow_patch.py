"""`domonic._cssom.expand_shorthand`'s generic multi-longhand branch (used
for `overflow`, `gap`, `place-content`, `place-items`, `place-self`,
`flex-flow`, `columns`) treats a single-token shorthand value as applying to
*every* longhand uniformly -- correct for `overflow: hidden` (both
`overflow-x`/`overflow-y` become `hidden`) or `gap: 10px` (both `row-gap`/
`column-gap` become `10px`), where all longhands share one value space, but
wrong for `flex-flow`, whose two longhands (`flex-direction`, `flex-wrap`)
have disjoint value spaces and are independently optional per CSS Flexbox
3 section 8. `flex-flow: row` (direction only, wrap omitted -- valid CSS,
used throughout Wikipedia's Vector skin: `.mw-body-content{flex-flow:row}`)
broadcasts `"row"` to `flex-wrap` too. Confirmed directly: expanding
`("flex-flow", "row")` returns `[("flex-direction", "row"), ("flex-wrap",
"row")]`, and `style_bridge.py` later reads that bogus `flex-wrap: row`
straight into the Rust layout engine, which only accepts `nowrap`/`wrap`/
`wrap-reverse` and raises `ValueError: unrecognised flex-wrap: "row"` --
crashing layout on any page using single-keyword `flex-flow`, not just
mis-rendering it. Not patched upstream.

Patched by wrapping `expand_shorthand`: for `name == "flex-flow"` with
exactly one token, classify that token into `flex-direction` or `flex-wrap`
by which keyword set it belongs to and default the other longhand to its
initial value (`row` / `nowrap`), instead of assigning it to both. Every
other shorthand keeps the original function's behavior unchanged."""
from __future__ import annotations

import sys

import domonic._cssom  # noqa: F401 -- ensures `domonic._cssom` is in `sys.modules`

_cssom = sys.modules["domonic._cssom"]

_INSTALLED = False
_ORIGINAL_EXPAND_SHORTHAND = _cssom.expand_shorthand

_FLEX_DIRECTION_KEYWORDS = {"row", "row-reverse", "column", "column-reverse"}
_FLEX_WRAP_KEYWORDS = {"nowrap", "wrap", "wrap-reverse"}


def _expand_shorthand_with_flex_flow_fix(name, value):
    if name == "flex-flow":
        parts = (value or "").split()
        if len(parts) == 1:
            token = parts[0]
            lower = token.lower()
            if lower in _FLEX_WRAP_KEYWORDS:
                return [("flex-direction", "row"), ("flex-wrap", token)]
            if lower in _FLEX_DIRECTION_KEYWORDS:
                return [("flex-direction", token), ("flex-wrap", "nowrap")]
    return _ORIGINAL_EXPAND_SHORTHAND(name, value)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _cssom.expand_shorthand = _expand_shorthand_with_flex_flow_fix
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _cssom.expand_shorthand = _ORIGINAL_EXPAND_SHORTHAND
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
