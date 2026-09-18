"""CSS border-width keywords (`thin`/`medium`/`thick`) are never resolved to
a pixel length, and the *used* border-width for a side whose `border-style`
is `none`/`hidden` is always `0` regardless of any specified width -- see
`PLAN.md`'s domonic issues log for the full writeup.

`border-style: solid` with no explicit `border-width` computes to the
initial `border-width: medium` (CSS 2.1 8.5.3), a real length -- every
browser (Chrome included) resolves it to `3px` (`thin`/`thick` to
`1px`/`5px`). domonic's length resolvers only recognise a numeric-plus-unit
token; a bare keyword never matches, so it's left unresolved.

Border-width resolution is genuinely border-*-width-specific, not a
property-agnostic length concern: whether a side's `border-width` keyword
(or any width at all) actually applies depends on that side's own
`border-style` -- a `none`/`hidden` side's *used* width is always `0`, no
matter what `border-width` says, and this project's own fixture-suite
default (an unqualified `border: solid` matching a plain, borderless
`<div>`'s already-cascaded `border-width: medium` from a UA-stylesheet-style
reset) confirmed exactly this: resolving `medium` unconditionally to `3px`
put a visible `3px` border on every ordinary element regardless of whether
its `border-style` ever called for one. So this is resolved once per side,
with `border-style` context, not folded into the generic length parser
(`_length_string_to_px`, used for every other length-valued property too,
where a bare `thin`/`medium`/`thick` keyword-anywhere would be wrong to
resolve at all).

Two separate call paths need this, patched independently:
- `getComputedStyle()` (`domonic.style.ComputedStyleDeclaration.
  _to_used_length`) already special-cases `border-*-width` to return
  `"0px"` before ever reaching length parsing when the matching
  `border-*-style` is `none`/`hidden` (`_compute_property_value`) -- it
  only still needs the *keyword* itself understood for the remaining
  (style-drawn) case, so `_to_used_length` is wrapped to resolve
  `thin`/`medium`/`thick` for a border-width `target` before delegating.
- `domonic.layout.LayoutStyle._from_computed()` has no such style-aware
  gate at all (it reads each property from the raw cascade independently,
  by design -- see `domonic_layout_calc_var_patch.py`'s docstring). Wrapped
  here to recompute all four `borderWidth` edges from scratch, per side,
  after calling the original: `border-style` none/hidden -> `Length(0.0)`;
  otherwise a `thin`/`medium`/`thick` keyword -> its length; otherwise the
  original (already-correct, e.g. a real `border-width: 4px`) edge value is
  kept untouched."""
from __future__ import annotations

import sys
from dataclasses import replace

import domonic.layout  # noqa: F401 -- ensures `domonic.layout` is in `sys.modules`
import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as the other `domonic_*_patch`
# modules in this package: only a `sys.modules` lookup by dotted name
# reaches the real submodules here, not attribute access on the `domonic`
# package itself (or `import domonic.x as _x`, which resolves through that
# same package attribute access).
_style = sys.modules["domonic.style"]
_layout = sys.modules["domonic.layout"]

_INSTALLED = False
_ORIGINAL_TO_USED_LENGTH = _style.ComputedStyleDeclaration._to_used_length
_ORIGINAL_FROM_COMPUTED = _layout.LayoutStyle._from_computed.__func__

# CSS 2.1 8.5.3's own initial `border-width` value is `medium`; every real
# browser's convention for these three keywords (unspecified by the spec
# itself beyond "thin < medium < thick") is 1px/3px/5px.
_BORDER_WIDTH_KEYWORD_PX = {"thin": 1.0, "medium": 3.0, "thick": 5.0}
_BORDER_WIDTH_TARGETS = frozenset(
    {"border-top-width", "border-right-width", "border-bottom-width", "border-left-width"}
)
_BORDER_WIDTH_SIDES = ("top", "right", "bottom", "left")
_BORDER_STYLE_PROPS = {
    "top": "border-top-style", "right": "border-right-style",
    "bottom": "border-bottom-style", "left": "border-left-style",
}


def _to_used_length_with_border_keywords(self, target, value):
    if target in _BORDER_WIDTH_TARGETS:
        keyword_px = _BORDER_WIDTH_KEYWORD_PX.get(value.strip().lower())
        if keyword_px is not None:
            return f"{keyword_px:g}px"
    return _ORIGINAL_TO_USED_LENGTH(self, target, value)


def _from_computed_with_border_keywords(cls, computed):
    result = _ORIGINAL_FROM_COMPUTED(cls, computed)
    Length = _layout.Length
    raw = computed._resolved.get
    edges = {}
    for side in _BORDER_WIDTH_SIDES:
        style_value = (raw(_BORDER_STYLE_PROPS[side]) or "none").strip().lower()
        if style_value in ("none", "hidden"):
            edges[side] = Length(0.0)
            continue
        width_value = (raw(f"border-{side}-width") or "").strip().lower()
        keyword_px = _BORDER_WIDTH_KEYWORD_PX.get(width_value)
        edges[side] = Length(keyword_px) if keyword_px is not None else getattr(result.borderWidth, side)
    new_border_width = _layout.Edges(edges["top"], edges["right"], edges["bottom"], edges["left"])
    return replace(result, borderWidth=new_border_width)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style.ComputedStyleDeclaration._to_used_length = _to_used_length_with_border_keywords
    _layout.LayoutStyle._from_computed = classmethod(_from_computed_with_border_keywords)
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style.ComputedStyleDeclaration._to_used_length = _ORIGINAL_TO_USED_LENGTH
    _layout.LayoutStyle._from_computed = classmethod(_ORIGINAL_FROM_COMPUTED)
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
