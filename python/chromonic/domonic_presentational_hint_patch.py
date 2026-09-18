"""Legacy HTML presentational attributes (`<table width="85%">`,
`<img height="40">`, `<body bgcolor="...">`, ...) are converted to real
inline `style=""` text by `browser._apply_presentational_attributes` --
which, per the real CSS cascade, makes them beat *any* author stylesheet
declaration regardless of specificity (inline style only loses to an
`!important` author rule). Real browsers treat a presentational attribute
as the *weakest* possible declaration instead -- conceptually the very
first rule of the document, so literally any later author rule for the
same property (even a plain, non-`!important`, equal-or-lower-specificity
one) overrides it.

Confirmed directly on `news.ycombinator.com` at a 733px viewport:
`#hnmain`'s HTML `width="85%"` attribute, real `news.css` has both a
desktop `#hnmain { min-width: 796px; }` and, inside `@media (min-width:
300px) and (max-width: 750px)`, `#hnmain { width: 100%; min-width: 0; }`
-- real Chrome (733px is inside that range) renders `#hnmain` at the full
733px. chromonic instead rendered it at `623.05px` (exactly 85% of 733):
the mobile media query's `min-width: 0` *did* win (an author rule beating
another author rule, ordinary cascade), but its `width: 100%` lost to the
literal inline `style="width:85%"` chromonic had synthesized from the HTML
attribute -- a real, if legacy, per-attribute regression this project's
own presentational-attribute support had introduced. With `#hnmain` stuck
89 fewer content-columns wide than real Chrome, nearly everything in every
row wrapped onto extra lines it shouldn't have, visibly inflating the
whole page's height (measured ~3458px vs Chrome's ~1451px) -- a single
`width` mismatch on one ancestor cascading into hundreds of descendants.

Patched by moving where chromonic's own presentational-attribute
declarations get read into the cascade at all, not by touching domonic's
inline-style handling (real inline `style=""` text must keep winning
normally -- an author-written `<table style="...">` on the very same
element is still supposed to beat what the `width=`/`height=`/`bgcolor=`
attributes ask for, exactly like today). `browser._apply_presentational_
attributes` now records `element._chromonic_presentational_hints` (a plain
`{property: value}` dict) instead of writing into `style=""`; `_resolve`
is wrapped to seed the cascade's `resolved` dict from those hints *before*
author declarations are applied, so the existing `for name, (value, _) in
author.items(): resolved[name] = value` loop -- completely unmodified --
already overwrites a hint for any covering author declaration regardless
of its specificity, the same way it already overwrites one lower-priority
author declaration with a higher-priority one. Real inline `style=""` text
is applied after that, exactly as before -- unaffected by this patch."""
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


def _resolve_with_presentational_hints(self):
    element = self._element
    resolved: dict = {}

    hints = getattr(element, "_chromonic_presentational_hints", None)
    if hints:
        resolved.update(hints)

    author = self._collect_author_declarations()
    important_author = {name for name, (_, imp) in author.items() if imp}
    for name, (value, _) in author.items():
        resolved[name] = value
    inline = getattr(element, "getAttribute", lambda *_: "")("style") or ""
    for name, value, priority in _style._parse_css_declarations(inline):
        if priority != "important":
            covering = _style._cssom.LONGHAND_TO_SHORTHANDS.get(name, ())
            if name in important_author or any(shorthand in important_author for shorthand in covering):
                continue
        resolved[name] = value

    for name in list(resolved):
        if _style._cssom.is_shorthand(name):
            for long_name, long_value in _style._cssom.expand_shorthand(name, resolved[name]) or []:
                resolved.setdefault(long_name, long_value)

    parent = getattr(element, "parentNode", None)
    parent_computed = None
    if parent is not None and getattr(parent, "nodeType", None) == 1:
        cache = self._chain_cache
        parent_computed = cache.get(id(parent))
        if parent_computed is None:
            parent_computed = ComputedStyleDeclaration(parent, None, _chain_cache=cache)
            cache[id(parent)] = parent_computed
    return _style._ResolvedView(resolved, parent_computed)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    ComputedStyleDeclaration._resolve = _resolve_with_presentational_hints
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
