"""`CSSStyleSheet.disabled` exists on domonic's `StyleSheet` base class (a
plain `bool`, settable via the constructor or attribute assignment -- see
`self.disabled: bool = False` in `StyleSheet.__init__`) but nothing respects
it, in two separate ways:

1. `ComputedStyleDeclaration._collect_author_declarations` gathers every
   sheet in `document.styleSheets`/`adoptedStyleSheets` unconditionally and
   hands the full list straight to `_build_rule_index`, which has no
   `disabled` check of its own either -- a disabled sheet's rules still win
   the cascade exactly as if it were enabled.
2. Even fixed, (1) alone isn't enough: both the per-document rule-index
   cache and the per-element `ComputedStyleDeclaration` cache (`element.
   __dict__["_computed_style_cache"]`) are keyed by `_cssom.stylesheet_
   epoch()`, which only `insertRule`/`deleteRule`/`replace(Sync)` bump --
   plain `sheet.disabled = True` assignment doesn't touch it, so every
   cached computed style (which, for any element already queried once, is
   most of them) keeps serving its pre-toggle answer forever.

Confirmed directly: setting `sheet.disabled = True` on a page's stylesheet
has no visible effect on `getComputedStyle()` at all, matching neither
CSSOM's spec ("a disabled sheet contributes no rules to the cascade") nor
what any reasonable caller would expect (native_browser.py's stylesheets
on/off toggle, F9, needs exactly this and silently did nothing without
both fixes together).

Patched by: (a) wrapping `_build_rule_index` to filter disabled sheets out
of `sheet_list` before building the rule index, and (b) replacing
`StyleSheet.disabled` with a real property that bumps `_cssom.
bump_stylesheet_epoch()` whenever the flag actually changes, so both cache
layers above see it as a real, cache-busting mutation -- the same way
`insertRule`/`deleteRule` already do. Everything else about either
function/class is untouched."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`
import domonic._cssom  # noqa: F401 -- ensures `domonic._cssom` is in `sys.modules`

_style = sys.modules["domonic.style"]
_cssom = sys.modules["domonic._cssom"]
_StyleSheet = _style.StyleSheet

_INSTALLED = False
_ORIGINAL_BUILD_RULE_INDEX = _style._build_rule_index
_ORIGINAL_DISABLED_DESCRIPTOR = _StyleSheet.__dict__.get("disabled")


def _build_rule_index_respecting_disabled(sheet_list, viewport):
    enabled = [sheet for sheet in sheet_list if not getattr(sheet, "disabled", False)]
    return _ORIGINAL_BUILD_RULE_INDEX(enabled, viewport)


def _get_disabled(self) -> bool:
    return self.__dict__.get("_chromonic_disabled", False)


def _set_disabled(self, value) -> None:
    value = bool(value)
    if self.__dict__.get("_chromonic_disabled", False) != value:
        self.__dict__["_chromonic_disabled"] = value
        _cssom.bump_stylesheet_epoch()
    else:
        self.__dict__["_chromonic_disabled"] = value


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._build_rule_index = _build_rule_index_respecting_disabled
    _StyleSheet.disabled = property(_get_disabled, _set_disabled)
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._build_rule_index = _ORIGINAL_BUILD_RULE_INDEX
    if _ORIGINAL_DISABLED_DESCRIPTOR is None:
        delattr(_StyleSheet, "disabled")
    else:
        _StyleSheet.disabled = _ORIGINAL_DISABLED_DESCRIPTOR
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
