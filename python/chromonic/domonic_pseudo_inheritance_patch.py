"""A `::before`/`::after` pseudo-element inherits CSS properties (like
`font-family`) from its own DOM parent's parent, skipping the *originating
element itself* -- so any inherited property the originating element's own
rules set is invisible to its own generated content.

`ComputedStyleDeclaration._resolve()` determines what a style inherits from
with `parent = getattr(element, "parentNode", None)` unconditionally -- for
an ordinary (non-pseudo) resolution `element` genuinely is the DOM node
whose parent should supply inherited values, but for a pseudo-scoped one
(`ComputedStyleDeclaration(element, "::before")`) `element` is still the
*real* element the pseudo is attached to, not the pseudo itself -- there is
no separate pseudo-element node to have its own `parentNode`. Per CSS
Pseudo-Elements ("the parent of a generated box is the element to whose box
tree the pseudo-element is attached"), `::before`/`::after` must inherit
from `element`'s own computed style, not `element.parentNode`'s.

Confirmed directly on a real site (`eventual.technology`) using Font
Awesome 6: `.fas { font-family: "Font Awesome 6 Free"; font-weight: 900; }`
sets the icon font on the element itself (`<i class="fas fa-cloud">`), and
relies on ordinary CSS inheritance to carry it into `.fas::before`'s
`content: "\\f0c2"` glyph -- domonic's `::before` resolution instead
inherited from `<i>`'s *parent* (never touching `<i>`'s own `.fas` rule at
all), so the pseudo-element's `font-family` fell through to whatever
generic font the surrounding page inherits, and the Private-Use-Area glyph
codepoint rendered as nothing/tofu in that font instead of the real icon.

This previously went unnoticed because chromonic's own `::before`/`::after`
support (before `tree._PseudoElement`) never gave a pseudo-element its own
independently-resolved style at all -- it read the *owning* element's own
already-correct paint style directly, accidentally sidestepping this
inheritance question entirely. Giving `::before`/`::after` a real,
separately-positioned/sized/painted box (needed for icon-font content,
`position:absolute` pseudo-elements, decorative images, etc.) means their
own style really does need to resolve correctly on its own -- which exposed
this real, pre-existing domonic cascade bug.

Patched by wrapping `_resolve()` (chains after whatever it already is --
`domonic_presentational_hint_patch`, alphabetically installed first, is
unaffected either way) and, only for a pseudo-scoped resolution, replacing
the `_ResolvedView`'s inheritance parent with `element`'s own computed
style -- reusing it from the shared chain cache when `tree.py`'s
`_describe()` already resolved it moments earlier (the overwhelmingly
common case), building one fresh only when called standalone. The
already-computed `_declared` dict is reused as-is; only which style
inherited values fall back to changes."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as this package's other
# `domonic_*_patch` modules: only a `sys.modules` lookup by dotted name
# reaches the real submodule, not attribute access on the `domonic` package
# itself (or `import domonic.style as _style`, which resolves through that
# same package attribute access).
_style = sys.modules["domonic.style"]
ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_ORIGINAL_RESOLVE = ComputedStyleDeclaration._resolve


def _resolve_with_pseudo_inheritance(self):
    view = _ORIGINAL_RESOLVE(self)
    if not self._pseudo:
        return view
    element = self._element
    cache = self._chain_cache
    parent_computed = cache.get(id(element))
    if parent_computed is None:
        parent_computed = ComputedStyleDeclaration(element, None, _chain_cache=cache)
        cache[id(element)] = parent_computed
    return _style._ResolvedView(view._declared, parent_computed)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    ComputedStyleDeclaration._resolve = _resolve_with_pseudo_inheritance
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    ComputedStyleDeclaration._resolve = _ORIGINAL_RESOLVE
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
