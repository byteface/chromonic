"""The "monospace font-size quirk" every real browser applies and domonic's
cascade does not: an element whose `font-family` is exactly the single
generic `monospace` gets a default (`medium`) font size of 13px, not 16px
-- the reason a bare `<code>`/`<pre>` renders visibly smaller than its
surrounding text, and why `font-family: monospace, monospace` is the
well-known trick for opting *out* of it.

Blink's actual rule (`FontBuilder::CheckForGenericFamilyChange`): whenever
an element's monospace-ness differs from its parent's and its own
`font-size` was not specified as an absolute length, the size it would
otherwise have is scaled by 13/16 on entering monospace (or 16/13 on
leaving it). A `medium`/unspecified size on a monospace element is 13px;
`font-size: 2em` inside a monospace body whose own size is that default
13px is 26px, since the parent it resolves against already carries the
scaled size. Confirmed directly on `wpt/css/CSS2/tables/table-anonymous-
objects-059.xht` (`<body style="font-family: monospace">` + `font-size:
2em`): every cell was 32/26 wider and taller than Chrome's, and the
per-character advance matched once the size did -- same font, wrong size.
Not patched upstream.

Patched by wrapping `_font_size_px` (on top of `domonic_var_font_size_
patch`'s own wrapper -- this module must be imported after it), the one
function every `em`/`line-height`/`getPropertyValue("font-size")` read in
domonic goes through, so descendants resolving `em` against a monospace
ancestor see the scaled size too."""
from __future__ import annotations

import re
import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]
_ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_PREVIOUS_FONT_SIZE_PX = _ComputedStyleDeclaration._font_size_px
_MONOSPACE_SCALE = 13.0 / 16.0
_ABSOLUTE_LENGTH_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(px|pt|pc|in|cm|mm|q|rem)$", re.I)


def _is_exactly_monospace(computed) -> bool:
    if computed is None:
        return False
    family = (getattr(computed, "fontFamily", None) or "").strip().strip("'\"").lower()
    return family == "monospace"


def _own_font_size(computed) -> str:
    """The `font-size` this element *itself* declared (author rules, then
    the inline `style` attribute), or `""`. Not `_resolved`: that also
    carries an *inherited* size already resolved to px -- exactly the case
    the quirk must still scale -- and `_declared` (the same idea) isn't
    built yet at the point `_font_size_px` is first called."""
    declared = getattr(computed, "_declared", None)
    if declared is not None:
        return str(declared.get("font-size") or "").strip()
    value = ""
    try:
        author = computed._collect_author_declarations()
        if "font-size" in author:
            value = str(author["font-size"][0] or "").strip()
    except Exception:
        pass
    element = getattr(computed, "_element", None)
    inline = getattr(element, "getAttribute", lambda *_: "")("style") or ""
    if "font-size" in inline.lower():
        for name, declared_value, _priority in _style._parse_css_declarations(inline):
            if name == "font-size":
                value = str(declared_value or "").strip()
    return value


def _has_absolute_font_size(computed) -> bool:
    raw = _own_font_size(computed)
    return bool(_ABSOLUTE_LENGTH_RE.match(raw)) or "calc(" in raw.lower()


def _font_size_px_with_monospace_quirk(self):
    cached = self.__dict__.get("_font_size_px_monospace_cache")
    if cached is not None:
        return cached
    px = _PREVIOUS_FONT_SIZE_PX(self)
    own_monospace = _is_exactly_monospace(self)
    parent = self._parent_computed()
    parent_monospace = _is_exactly_monospace(parent) if parent is not None else False
    if own_monospace != parent_monospace and not _has_absolute_font_size(self):
        px = px * _MONOSPACE_SCALE if own_monospace else px / _MONOSPACE_SCALE
    self.__dict__["_font_size_px_monospace_cache"] = px
    return px


def install() -> bool:
    """Install once and return whether this call changed domonic. A
    domonic that applies the quirk itself (`ComputedStyleDeclaration.
    _apply_monospace_font_size_quirk`, added after 1.8.4's release) is
    left alone -- wrapping it too scaled a `font-size: 2em` monospace
    element to 21.1px instead of 26 (table-anonymous-objects-059.xht)."""
    global _INSTALLED
    if _INSTALLED:
        return False
    if hasattr(_ComputedStyleDeclaration, "_apply_monospace_font_size_quirk"):
        return False
    _ComputedStyleDeclaration._font_size_px = _font_size_px_with_monospace_quirk
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _ComputedStyleDeclaration._font_size_px = _PREVIOUS_FONT_SIZE_PX
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
