"""`@media print` (and `@media screen`/`all`) always matches in domonic,
regardless of what's actually being rendered -- see `PLAN.md`'s domonic
issues log for the full writeup. `MediaQueryList._evaluate` has no concept
of a "current media type" at all: it just treats the `all`/`screen`/`print`
media-type keyword itself as always-true (`if text in ("all", "screen",
"print"): return True`), with no way to tell it which one is actually
active. Confirmed directly on `wpt/css/CSS2/box-display/containing-block-
024.xht`: a `@media print { #print { display: none } ... }` block applied
even though chromonic only ever renders a screen/interactive context, never
print/paged media -- hiding content real Chrome (correctly ignoring
`@media print` outside an actual print preview) keeps visible, and applying
print-only sizing rules real Chrome never does either.

Chromonic has no print/paged-media rendering mode at all, so the correct
fix for this project specifically is narrow: `print` never matches, `all`/
`screen` (and any other still-unhandled type) keep matching as before.
Patched by wrapping `MediaQueryList._evaluate` (not `_condition_rule_
matches`, which just calls it) so every caller -- the real CSS cascade,
`Window.matchMedia()`, everything -- gets the same, single, correct
answer."""
from __future__ import annotations

import sys

import domonic.window  # noqa: F401 -- ensures `domonic.window` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as this package's other
# `domonic_*_patch` modules: only a `sys.modules` lookup by dotted name
# reaches the real submodule, not attribute access on the `domonic` package
# itself.
_window = sys.modules["domonic.window"]

_INSTALLED = False
_ORIGINAL_EVALUATE = _window.MediaQueryList._evaluate.__func__


def _evaluate_with_print_never_matching(cls, media, **kwargs):
    text = (media or "").strip().lower()
    # Mirror `_evaluate`'s own `only ` stripping so `only print` (a common
    # authoring pattern, since old browsers without media-query support
    # ignored an unrecognised `only` type entirely) is caught the same way
    # a bare `print` is.
    if text.startswith("only "):
        text = text[5:].strip()
    if text == "print":
        return False
    return _ORIGINAL_EVALUATE(cls, media, **kwargs)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _window.MediaQueryList._evaluate = classmethod(_evaluate_with_print_never_matching)
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _window.MediaQueryList._evaluate = classmethod(_ORIGINAL_EVALUATE)
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
