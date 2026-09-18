"""A later shorthand in the same rule loses to an earlier one for a longhand
they both set -- see `PLAN.md`'s domonic issues log for the full writeup.

`ComputedStyleDeclaration._resolve()` collects a rule's raw declarations
(shorthand and longhand alike) into a dict keyed by property name, then
expands every shorthand entry into its longhands with `resolved.setdefault
(long_name, long_value)` -- `setdefault` only sets a key that's *absent*,
so whichever shorthand happens to be expanded *first* (source order, since
`resolved` preserves insertion order) wins any longhand two shorthands both
set, regardless of which one is actually later (and so higher-priority) in
the CSS itself. Confirmed directly: `.test { border: solid 1em blue;
border-top: none; }` (`wpt/css/CSS2/positioning/abspos-002.xht`) -- the
later `border-top: none` should override `border-top-style`/`-width`/
`-color` back to `none`/`medium`/`currentcolor` for the top side only, but
`getComputedStyle().borderTopStyle` stayed `"solid"` (`border`'s own
expansion, processed first, already occupied that key) -- an added,
unwanted `border-top-width: medium` (`16px` at this element's font-size)
inflated its own height by that much.

Patched by expanding *every* shorthand at the declaration-tokenizer level
(`_parse_css_declarations`, the same hook `domonic_logical_properties_
patch.py` already uses) instead of leaving it to `_resolve()`'s broken
loop: each expanded longhand keeps the source declaration's position, and
a later declaration for the same longhand -- whether it came from a
different shorthand or the same one repeated -- correctly *replaces* the
earlier one instead of being silently dropped. Scoped to one rule's own
declaration block, exactly where `_parse_css_declarations` already
operates -- this never reaches across different rules/selectors, so the
real cross-rule cascade (specificity, source order, `!important`, layers)
in `_collect_author_declarations` is completely untouched.

Installed *after* `domonic_logical_properties_patch` (see `browser.py`'s
import order) -- it captures whatever `_parse_css_declarations` already is
at import time and chains its own expansion on top, so both patches apply
regardless of which one runs first at parse time."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`
from domonic import _cssom

# Same `domonic/__init__.py`-shadowing caveat as this package's other
# `domonic_*_patch` modules: only a `sys.modules` lookup by dotted name
# reaches the real submodule, not attribute access on the `domonic` package
# itself (or `import domonic.style as _style`, which resolves through that
# same package attribute access).
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_PARSE_CSS_DECLARATIONS = _style._parse_css_declarations


def _parse_css_declarations_with_shorthand_order(css_text: str):
    result: list = []
    index_by_name: dict = {}
    for name, value, priority in _ORIGINAL_PARSE_CSS_DECLARATIONS(css_text):
        expanded = _cssom.expand_shorthand(name, value) if _cssom.is_shorthand(name) else None
        pairs = expanded if expanded is not None else [(name, value)]
        for long_name, long_value in pairs:
            existing = index_by_name.get(long_name)
            if existing is None:
                index_by_name[long_name] = len(result)
                result.append((long_name, long_value, priority))
            else:
                result[existing] = (long_name, long_value, priority)
    return result


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._parse_css_declarations = _parse_css_declarations_with_shorthand_order
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._parse_css_declarations = _ORIGINAL_PARSE_CSS_DECLARATIONS
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
