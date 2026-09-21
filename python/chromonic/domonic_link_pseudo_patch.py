"""CSS `:link`/`:visited` (CSS 2.1 5.11.2, the two original "dynamic"
pseudo-classes -- present since CSS1) are never matched during the cascade
at all. `Element._parse_simple_selector` only accepts a pseudo-class name
listed in `Element._STRUCTURAL_PSEUDO_CLASSES` (`root`/`empty`/`first-
child`/... -- none of them `link`/`visited`); any other name fails the
whole selector's parse and returns `None`. `ComputedStyleDeclaration`'s
cascade (`style.py`'s rule-index lookup) matches a selector via
`element._matchElement(element, base_selector) or (element.
_matches_selector_chain(base_selector) is True)` -- neither of which has
any further fallback for the cascade specifically (unlike `Element.
matches()`, which falls back to `querySelectorAll()` for a selector its
own fast matcher can't parse) -- so a rule using `a:link`/`a:visited`
anywhere, however simple, silently never applies during layout at all.

Confirmed directly rendering `https://news.ycombinator.com/`: its own
stylesheet has a plain `a:link { color: #000000; text-decoration: none; }`
rule for exactly this (real Chrome renders every story title in black),
but chromonic fell all the way back to `ua_style.py`'s own UA-layer
`a { color: #0000ee; }` instead -- every link rendering in the wrong,
default hyperlink-blue color, a large and immediately visible difference
from a real browser on any HN-style page (and, since `a:link`/`a:visited`
are two of the single most common pseudo-classes on the real web, likely
on a great many other real sites too).

Patched narrowly, matching how domonic already treats other pseudo-
classes it *has no state for*: chromonic never simulates navigation
history, so every real hyperlink is honestly in the unvisited `:link`
state from its perspective (the same stance a fresh/incognito browser
context takes) -- `:link` matches any `<a>`/`<area>` with a non-empty
`href`; `:visited` never matches anything, rather than guessing."""
from __future__ import annotations

import sys

import domonic.dom  # noqa: F401 -- ensures `domonic.dom` is in `sys.modules`

_dom = sys.modules["domonic.dom"]
_Element = _dom.Element

_INSTALLED = False
_ORIGINAL_STRUCTURAL_PSEUDO_CLASSES = _Element._STRUCTURAL_PSEUDO_CLASSES
_PATCHED_STRUCTURAL_PSEUDO_CLASSES = _ORIGINAL_STRUCTURAL_PSEUDO_CLASSES | {"link", "visited"}
_ORIGINAL_MATCHES_STRUCTURAL_PSEUDO = _Element._matches_structural_pseudo

_LINK_TAGS = ("a", "area")


def _matches_structural_pseudo_with_link(element, pseudo: str) -> bool:
    if pseudo == "link":
        tag = (getattr(element, "tagName", "") or "").lower()
        return tag in _LINK_TAGS and bool(element.getAttribute("href"))
    if pseudo == "visited":
        return False
    return _ORIGINAL_MATCHES_STRUCTURAL_PSEUDO(element, pseudo)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _Element._STRUCTURAL_PSEUDO_CLASSES = _PATCHED_STRUCTURAL_PSEUDO_CLASSES
    _Element._matches_structural_pseudo = staticmethod(_matches_structural_pseudo_with_link)
    # `_parse_simple_selector` caches its result per selector string
    # (`functools.lru_cache`) -- clear it so a selector already parsed
    # (and rejected) under the old, narrower pseudo-class set is re-parsed
    # under the new one instead of staying stuck on its stale `None`.
    _Element._parse_simple_selector.cache_clear()
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _Element._STRUCTURAL_PSEUDO_CLASSES = _ORIGINAL_STRUCTURAL_PSEUDO_CLASSES
    _Element._matches_structural_pseudo = staticmethod(_ORIGINAL_MATCHES_STRUCTURAL_PSEUDO)
    _Element._parse_simple_selector.cache_clear()
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
