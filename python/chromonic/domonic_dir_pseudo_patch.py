"""CSS Selectors 4's `:dir()` functional pseudo-class (`:dir(ltr)`/`:dir
(rtl)`) is never matched at all -- `domonic.bs4._strip_simple_pseudo`
(the per-compound pseudo-class parser both `Element._matches_selector_
chain`, for a combinator chain, and `domonic_selector_fallback_patch`'s
`_matchElement` wrapper, for a single compound, share) has explicit
support for `:lang()`/`:is()`/`:where()`/`:has()`/`:not()`/the `:nth-*()`
family/`:first-child` etc., but no case for `:dir()` at all -- it falls
through to the same "not a pseudo this fast matcher understands" `None`
every genuinely unsupported pseudo-class hits, so a rule like `:dir(ltr)
{ color: blue }` silently never applies.

Confirmed directly with `wpt/css/selectors/dir-style-01a.html`: every one
of its `:dir(ltr)`/`:dir(rtl)` rules never matched a single element, every
`<div>` keeping its inherited/initial black instead of the rule's blue/
lime.

Patched narrowly, the same way `domonic_link_pseudo_patch.py` added
`:link`/`:visited`: wraps `_strip_simple_pseudo` to recognise a trailing
`:dir(ltr)`/`:dir(rtl)` (case-insensitively; any other argument, e.g. the
same fixture's deliberately-invalid `:dir(foopy)`, parses to a real,
never-matching pseudo rather than failing the selector, matching how a
browser treats an unrecognised-but-syntactically-valid argument) before
falling back to the original for everything else, and wraps `_match_
simple_pseudo` to resolve it via HTML's own directionality algorithm --
walking up from the element for the nearest `dir="ltr"`/`"rtl"` attribute
(a `dir="auto"` or invalid value is skipped, same as a real browser
falling through to the next ancestor), defaulting to `ltr` if none is
found anywhere up to the root. This deliberately doesn't also consult an
element's own *computed* `direction` CSS property (`chromonic.tree.
_element_direction`, used elsewhere in this project for real layout,
does) -- resolving a full cascade from inside a bare selector-matching
utility with no `ComputedStyleDeclaration` in hand isn't a small change,
and every real use of `:dir()` in the wild (this fixture included) drives
it purely from the `dir` attribute, HTML's own mechanism for exactly
this."""
from __future__ import annotations

import re
import sys

import domonic.bs4  # noqa: F401 -- ensures `domonic.bs4` is in `sys.modules`

_bs4 = sys.modules["domonic.bs4"]

_INSTALLED = False
_ORIGINAL_STRIP_SIMPLE_PSEUDO = _bs4._strip_simple_pseudo
_ORIGINAL_MATCH_SIMPLE_PSEUDO = _bs4._match_simple_pseudo
_DIR_RE = re.compile(r":dir\(([^()]+)\)$", re.I)
_Element = _bs4.Element


def _strip_simple_pseudo_with_dir(selector: str):
    match = _DIR_RE.search(selector)
    if match:
        keyword = match.group(1).strip().strip("'\"").lower()
        return selector[: match.start()], ("dir", keyword)
    return _ORIGINAL_STRIP_SIMPLE_PSEUDO(selector)


def _match_simple_pseudo_with_dir(element, pseudo, position_cache=None):
    if pseudo is not None and pseudo[0] == "dir":
        wanted = pseudo[1]
        if wanted not in ("ltr", "rtl"):
            return False  # a syntactically valid but unrecognised argument -- never matches
        node = element
        while isinstance(node, _Element):
            declared = (_bs4._get_attribute(node, "dir") or "").strip().lower()
            if declared in ("ltr", "rtl"):
                return declared == wanted
            node = getattr(node, "parentNode", None)
        return wanted == "ltr"  # HTML's own root default direction
    return _ORIGINAL_MATCH_SIMPLE_PSEUDO(element, pseudo, position_cache)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _bs4._strip_simple_pseudo = _strip_simple_pseudo_with_dir
    _bs4._match_simple_pseudo = _match_simple_pseudo_with_dir
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _bs4._strip_simple_pseudo = _ORIGINAL_STRIP_SIMPLE_PSEUDO
    _bs4._match_simple_pseudo = _ORIGINAL_MATCH_SIMPLE_PSEUDO
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
