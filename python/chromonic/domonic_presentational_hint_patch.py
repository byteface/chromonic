"""Translate the old HTML attributes real legacy pages still use.

Hacker News is the canonical small repro: the orange bar is
``<td bgcolor="#ff6600">`` and the logo is an SVG ``<img>`` whose layout
size comes from ``width``/``height`` attributes. domonic exposes those as
attributes, not computed CSS, so normalize the narrow set Chromonic needs
before resolving styles.

Recorded as ``element._chromonic_presentational_hints`` -- a plain
``{property: value}`` dict, populated by ``browser._apply_presentational_
attributes`` -- rather than written into the element's real ``style=""``
text. A presentational attribute is the *weakest* possible declaration per
the real CSS cascade (conceptually the first rule of the document), so it
must lose to *any* later author stylesheet rule for the same property
regardless of specificity; real inline ``style=""`` text does not work
that way (it beats every non-``!important`` author rule outright), so
writing into it made a legacy ``width="85%"``-style attribute far stronger
than real browsers ever make it. Confirmed directly on
``news.ycombinator.com``: `#hnmain`'s ``width="85%"`` attribute was beating
a real, later, higher-priority ``#hnmain { width: 100% }`` inside an
author media query, rendering the whole table (and everything inside it)
~110px too narrow.

Patched by wrapping ``_collect_author_declarations`` (not ``_resolve``
itself) -- that's the one function both the original ``_resolve`` and
domonic 1.8.2's rewritten one (shorthand-vs-longhand cascade-order fix,
pseudo-element inheritance fix, `!important`-aware merging) already call
unmodified to gather what a real author stylesheet declared, so hooking
in here means every later improvement to ``_resolve`` itself keeps
applying for free -- an earlier version of this patch replaced ``_resolve``
outright and silently regressed both of those domonic 1.8.2 fixes the
moment it landed, since its own from-scratch reimplementation never
picked either one up. A hint is merged in only for a property name
``_collect_author_declarations`` found *no* real declaration for at all
(non-``important``, so a later real declaration for it -- author or
inline -- still overrides normally downstream in ``_resolve``) -- this
can still lose to a `ua_style.py` UA-stylesheet default for the same
property (that default *is* a real declaration `_collect_author_
declarations` returns, from this same function's own point of view,
indistinguishable here from genuine author CSS) -- a known, narrower
limitation than the real cascade's UA-loses-to-hint priority, unchanged
from this patch's original behaviour, not attempted here."""
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
_ORIGINAL_COLLECT_AUTHOR_DECLARATIONS = ComputedStyleDeclaration._collect_author_declarations


def _collect_author_declarations_with_hints(self):
    result = _ORIGINAL_COLLECT_AUTHOR_DECLARATIONS(self)
    hints = getattr(self._element, "_chromonic_presentational_hints", None)
    if not hints:
        return result
    merged = dict(result)
    for name, value in hints.items():
        if name not in merged:
            merged[name] = (value, False)
    return merged


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    ComputedStyleDeclaration._collect_author_declarations = _collect_author_declarations_with_hints
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    ComputedStyleDeclaration._collect_author_declarations = _ORIGINAL_COLLECT_AUTHOR_DECLARATIONS
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
