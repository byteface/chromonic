"""`calc()` expressions containing `var(...)` are never evaluated in
`domonic.layout.LayoutStyle._from_computed()` -- see `PLAN.md`'s domonic
issues log for the full writeup.

`LayoutStyle._from_computed()` deliberately reads the *raw*, pre-used-value
cascaded string for each property (`raw = computed._resolved.get`, not
`getComputedStyle`'s own resolved accessors) so genuinely layout-dependent
values (`auto`, a percentage) stay as typed placeholders for Taffy to
resolve against real available space -- a legitimate design choice, not a
bug. But `domonic.layout._parse_length_or_percent()` already tries to
evaluate a `calc(...)` it finds this way (`_eval_calc_to_px`), and that
*is* a bug: a `calc()` with no `%` inside is not actually layout-dependent
once its `var(...)` references are substituted, but they never are here --
`getComputedStyle`'s own resolution path (`_compute_property_value`)
expands `var()` before handing a value to the same evaluator, but
`LayoutStyle._from_computed()`'s raw path skips straight to evaluation,
so `calc(var(--spacing)*16)` still contains the literal, non-numeric
`var(--spacing)` token and `_eval_calc_to_px` silently gives up, falling
back to an opaque `Keyword` that `chromonic.style_bridge._len()` cannot
turn into a number either -- landing on `"auto"` instead of the real
length. Confirmed on a real site's Tailwind v4 output, where every spacing
value uses exactly this `calc(var(--spacing)*N)` shape: a `height:
calc(var(--spacing)*16)` (`64px`, confirmed correct via `getComputedStyle`)
produced a `LayoutStyle.height` of `Keyword(value='calc(var(--spacing)*16)')`
-- silently dropped to `auto`, collapsing the element to zero/content
height instead of the real `64px`.

Fixed by expanding `var()` references (the same way `getComputedStyle`
does, `_expand_var_references` against the element's own cascade) before
`_parse_length_or_percent` ever sees the text, whenever there is one to
expand -- zero behaviour change for the overwhelming majority of values
that don't reference a custom property at all."""
from __future__ import annotations

import sys

import domonic.layout  # noqa: F401 -- ensures `domonic.layout` is in `sys.modules`
import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as `domonic_logical_properties_
# patch.py`: only a `sys.modules` lookup by dotted name reaches the real
# submodules here, not attribute access on the `domonic` package itself.
_layout = sys.modules["domonic.layout"]
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_PARSE_LENGTH_OR_PERCENT = _layout._parse_length_or_percent
_expand_var_references = _style._expand_var_references


def _parse_length_or_percent_with_var(raw, computed, **kwargs):
    text = raw or ""
    if "var(" in text:
        raw = _expand_var_references(text, computed._custom_property).strip()
    return _ORIGINAL_PARSE_LENGTH_OR_PERCENT(raw, computed, **kwargs)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _layout._parse_length_or_percent = _parse_length_or_percent_with_var
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
