"""`@media` queries always match, regardless of the actual viewport -- see
`PLAN.md`'s domonic issues log for the full writeup (first found comparing
`eventual.technology`, in an earlier session; this is the actual fix).

`@supports` is patched here too, in the same traversal function, for the
same underlying reason: `_iter_style_rules`'s generic fallback (see below)
evaluates *every* non-media, non-layer condition rule -- `CSSSupportsRule`
included -- through `_evaluate_media_condition`, a media-*feature*
evaluator (`min-width`/`max-width`/...) that finds no such feature in an
`@supports` condition text like `(display: grid)` and, finding nothing to
fail on, falls through to "matches" unconditionally. Domonic already has
the real evaluator for this -- `CSS.supports()` (`_supports_condition`,
correctly handling `not`/`and`/`or` and nested parens per spec) -- it was
simply never wired into rule traversal at all, so every `@supports` block
applied regardless of its actual condition, the exact same "always
matches" bug class as the original `@media` one above, just coincidental
rather than a guaranteed `True` return.

`domonic.style._iter_style_rules()` decides whether to descend into an
`@media` block with:

    condition = getattr(rule, "conditionText", None) or getattr(rule, "media", None)
    if condition is not None and hasattr(condition, "matches"):
        matches = condition.matches
    else:
        matches = True

`conditionText` is a plain `str` (e.g. `"only screen and (min-width:
1132px)"`) and `rule.media` is a `MediaList` (a plain `list` subclass) --
*neither* type, nor anything else in domonic, has a `.matches` attribute at
all (confirmed: no `MediaQueryList` class exists in `domonic.style`), so
`hasattr(condition, "matches")` is always `False` and every `@media` block
is unconditionally treated as matching, for every rule, regardless of the
real viewport. Confirmed directly on a real site (csszengarden.com,
viewport 733px wide): `@media only screen and (min-width: 1132px)
{ .supporting { display: inline; ... } }` applied its desktop-only,
`display: inline` two-column layout trick at mobile width, cascading into
every descendant rendering 3-5x the viewport's own width.

Patched by replacing `_iter_style_rules` with a copy that evaluates
`conditionText` for real against the actual viewport (`style_bridge`'s own
`_VIEWPORT` contextvar, already set by every layout entry point) instead of
the always-`True` fallback. `_evaluate_media_condition` is a best-effort,
common-case evaluator -- `screen`/`all`/`print` media types, `and`-joined
`(min-width)`/`(max-width)`/`(min-height)`/`(max-height)` features in
px/em/rem, comma-separated queries as top-level OR -- covering the
overwhelming majority of real responsive CSS (including all three of
csszengarden's own queries). Anything it doesn't recognise (`not (...)`,
range syntax, `orientation`, `hover`, `prefers-*`, ...) falls back to
matching, the same as domonic's previous (also-unconditional) behaviour,
rather than risk hiding real content behind a query this evaluator
misjudged."""
from __future__ import annotations

import re
import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as this package's other
# `domonic_*_patch` modules: only a `sys.modules` lookup by dotted name
# reaches the real submodule, not attribute access on the `domonic` package
# itself (or `import domonic.style as _style`, which resolves through that
# same package attribute access).
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_ITER_STYLE_RULES = _style._iter_style_rules

_MEDIA_FEATURE_RE = re.compile(
    r"\(\s*(min|max)-(width|height)\s*:\s*([\d.]+)\s*(px|em|rem)?\s*\)", re.I
)


def _evaluate_media_condition(condition_text: "str | None", viewport) -> bool:
    text = (condition_text or "").strip().lower()
    if not text:
        return True
    viewport_width, viewport_height = viewport
    # Comma = OR at the top level; each comma-branch's own "and"-joined
    # feature terms all need to hold for that one branch to match.
    for branch in text.split(","):
        branch = branch.strip()
        if not branch:
            continue
        if "print" in branch and "screen" not in branch:
            continue  # this project only ever renders screen media
        if "not " in branch or branch.startswith("not"):
            return True  # negation -- not handled, don't risk hiding content
        branch_matches = True
        for minmax, axis, number, unit in _MEDIA_FEATURE_RE.findall(branch):
            value = float(number) * (16.0 if unit in ("em", "rem") else 1.0)
            actual = viewport_width if axis == "width" else viewport_height
            if actual is None:
                continue
            if minmax == "min" and actual < value:
                branch_matches = False
            elif minmax == "max" and actual > value:
                branch_matches = False
        if branch_matches:
            return True
    return False


def _iter_style_rules_with_real_media_match(rules, *, viewport, layers=None, layer=0):
    """Copy of `domonic.style._iter_style_rules`, with real `conditionText`
    evaluation (`_evaluate_media_condition`) in place of the always-`True`
    fallback -- see this module's own docstring for why the original can
    never do anything else. Structurally identical otherwise (`@layer`
    nesting/nameList handling, `CSSStyleRule` yielding) so it stays a drop-in
    replacement if domonic's own version changes elsewhere."""
    CSSLayerStatementRule = _style.CSSLayerStatementRule
    CSSStyleRule = _style.CSSStyleRule
    CSSLayerBlockRule = _style.CSSLayerBlockRule
    CSSSupportsRule = _style.CSSSupportsRule
    if layers is None:
        layers = {}
    for rule in rules or ():
        if isinstance(rule, CSSLayerStatementRule):
            for layer_name in rule.nameList:
                layers.setdefault(layer_name.strip(), len(layers) + 1)
            continue
        inner = getattr(rule, "cssRules", None)
        if isinstance(rule, CSSStyleRule):
            yield rule, layer
            continue
        if not inner:
            continue
        if isinstance(rule, CSSLayerBlockRule):
            layer_name = (rule.name or "").strip() or f"\x00anon{id(rule)}"
            child_layer = layers.setdefault(layer_name, len(layers) + 1)
            yield from _iter_style_rules_with_real_media_match(
                inner, viewport=viewport, layers=layers, layer=child_layer)
            continue
        condition_text = getattr(rule, "conditionText", None)
        if isinstance(rule, CSSSupportsRule):
            matches = _style.CSS.supports(condition_text or "")
        else:
            matches = _evaluate_media_condition(condition_text, viewport)
        if matches:
            yield from _iter_style_rules_with_real_media_match(
                inner, viewport=viewport, layers=layers, layer=layer)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._iter_style_rules = _iter_style_rules_with_real_media_match
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._iter_style_rules = _ORIGINAL_ITER_STYLE_RULES
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
