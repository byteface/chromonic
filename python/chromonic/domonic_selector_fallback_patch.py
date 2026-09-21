"""`ComputedStyleDeclaration`'s own cascade (`_collect_author_declarations`)
matches each candidate rule's selector against an element via
`element._matchElement(element, selector) or (element._matches_selector_
chain(selector) is True)` -- but `_matchElement` only understands a small,
explicitly whitelisted subset of structural pseudo-classes (`_STRUCTURAL_
PSEUDO_CLASSES`: `:root`, `:empty`, `:first-child`, `:last-child`, `:only-
child`, `:first-of-type`, `:last-of-type`, `:only-of-type`) and returns
`None`/fails to parse anything else -- `:nth-child()`, `:nth-of-type()`,
`:hover`, etc. -- and `_matches_selector_chain` only ever handles a
*combinator* chain (`len(parts) > 1`), returning `None` immediately for a
single compound selector like `div:nth-of-type(2)`. So a rule whose selector
is one compound using an unsupported pseudo-class is silently never matched
during cascade resolution -- confirmed directly: `Element.matches()` (which
has a further fallback to a full `querySelectorAll()`-based match domonic's
cascade code never calls) correctly reports `div:nth-of-type(2)` matching
the second `<div>`, but `getComputedStyle()` on that same element never
applies that rule's declarations at all, always falling through to a less
specific rule instead (confirmed on `wpt/css/CSS2/visudet/content-height-
001.html`: `div:nth-of-type(2) { line-height: 30px }` never applies, every
`<div>` computes the base rule's `200px` instead).

Patched by wrapping `Element._matchElement` itself: when the original
returns `False` for a selector that has no combinator (a single compound),
fall back to the same per-compound bs4-backed matcher (`_parse_stripped_
selector`/`_match_parsed_selector`/`_match_simple_pseudo`, from `domonic.
bs4`) that `_matches_selector_chain` already uses for each compound in a
combinator chain -- giving `_matchElement` (and therefore the cascade) the
same matching power `Element.matches()` already has, without a full
document-wide `querySelectorAll()` per rule per element. Every existing
caller of `_matchElement` (`.matches()`, `getElementsByTagName()`, the
legacy selector fallback) keeps its existing behavior for anything the fast
path already handled; this only adds an answer where the fast path
previously gave up."""
from __future__ import annotations

import sys

import domonic.dom  # noqa: F401 -- ensures `domonic.dom` is in `sys.modules`

_dom = sys.modules["domonic.dom"]
_Element = _dom.Element

_INSTALLED = False
_ORIGINAL_MATCH_ELEMENT = _Element._matchElement


def _match_element_with_bs4_fallback(self, element, query):
    if _ORIGINAL_MATCH_ELEMENT(self, element, query):
        return True
    selector = str(query or "").strip()
    if not selector:
        return False
    try:
        from domonic.bs4 import (
            _match_parsed_selector,
            _match_simple_pseudo,
            _parse_stripped_selector,
            _split_simple_selector_chain,
            _strip_simple_pseudo,
        )
    except Exception:
        return False
    # A genuine combinator chain (`div > span`) has more than one part --
    # `_matches_selector_chain` (called separately, alongside this method,
    # by every caller that needs it) already handles that case, so this
    # stays narrowly scoped to the single-compound gap this patch exists
    # for. A plain character scan for `" >+~"` anywhere in the string was
    # tried first and is wrong: a functional pseudo-class's own argument
    # can legitimately contain those characters without this selector
    # being a chain at all (`:has(> span)`'s relative-selector argument),
    # and `_split_simple_selector_chain` already correctly tells the two
    # apart (it respects parens), so reusing it here instead of a second,
    # cruder check is both more correct and no extra work. Confirmed
    # directly on `wpt/css/selectors/has-style-sharing-001.html`: the
    # char-scan rejected `:has(> span)` outright (a single compound, not a
    # chain), so `:has(> span) { background: green }` never applied
    # during cascade resolution even though `.matches()` -- which has its
    # own further `querySelectorAll()` fallback this cascade path doesn't
    # use -- correctly reported the element matching it.
    parts = _split_simple_selector_chain(selector)
    if not parts or len(parts) != 1:
        return False
    stripped = _strip_simple_pseudo(selector)
    if stripped is None:
        return False
    simple_sel, pseudo = stripped
    compound = _parse_stripped_selector(simple_sel)
    if compound is None:
        return False
    try:
        return bool(
            isinstance(element, _Element)
            and _match_parsed_selector(element, compound)
            and _match_simple_pseudo(element, pseudo, {})
        )
    except Exception:
        return False


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _Element._matchElement = _match_element_with_bs4_fallback
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _Element._matchElement = _ORIGINAL_MATCH_ELEMENT
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
