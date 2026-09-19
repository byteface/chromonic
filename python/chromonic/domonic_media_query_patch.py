"""`@supports` conditions are never evaluated -- see `PLAN.md`'s domonic
issues log for the full writeup.

domonic 1.8.2 fixed `@media`'s own long-standing "always matches
regardless of viewport" bug natively (`_condition_rule_matches` now
routes a `CSSMediaRule` through a real `MediaQueryList._evaluate`
against the actual viewport, in place of the previous always-`False`
`hasattr(condition, "matches")` check) -- so this module no longer needs
to reimplement `@media` matching itself; that part of the original patch
is gone.

`@supports` is a separate, still-unfixed gap in the same function:
`_condition_rule_matches` has no evaluator for `CSSSupportsRule` yet and
falls through to its own explicit "keep the previous always-match
behaviour" for anything but `@media` -- even though domonic already has
the real evaluator this needs, `CSS.supports()` (`_supports_condition`,
correctly handling `not`/`and`/`or` and nested parens per spec), it's
simply never wired into rule traversal for `@supports` specifically.

Patched by wrapping only `_condition_rule_matches` (not `_iter_style_
rules`, which domonic's own `@media` fix already made correct) to route
a `CSSSupportsRule` through `CSS.supports()` and delegate everything else
to the original, unmodified function."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as this package's other
# `domonic_*_patch` modules: only a `sys.modules` lookup by dotted name
# reaches the real submodule, not attribute access on the `domonic` package
# itself (or `import domonic.style as _style`, which resolves through that
# same package attribute access).
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_CONDITION_RULE_MATCHES = _style._condition_rule_matches


def _condition_rule_matches_with_supports(rule, viewport) -> bool:
    if isinstance(rule, _style.CSSSupportsRule):
        return _style.CSS.supports(getattr(rule, "conditionText", None) or "")
    return _ORIGINAL_CONDITION_RULE_MATCHES(rule, viewport)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._condition_rule_matches = _condition_rule_matches_with_supports
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._condition_rule_matches = _ORIGINAL_CONDITION_RULE_MATCHES
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
