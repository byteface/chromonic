"""`<details>` parses with no `.open`/`.toggle()` -- domonic defines a full
`HTMLDetailsElement` (dom.py: `open` property backed by the `open`
attribute, dispatching `ToggleEvent`; `toggle()`) but its HTML tag registry
never wires `details` to it. `domonic/html.py`'s per-tag class table has,
right next to each other:

    progress = type("progress", (HTMLProgressElement,), {"name": "progress"})
    meter = type("meter", (HTMLMeterElement,), {"name": "meter"})
    details = type("details", (Element,), {"name": "details"})

every sibling semantic element (`progress`, `meter`, `dialog`, `select`,
`textarea`, ...) is wired to its real `HTMLXxxElement` subclass; `details`
alone is left on bare `Element`, so `document.createElement('details')` and
every parsed `<details>` come back without `.open`/`.toggle()` at all --
confirmed directly (`type(el).__mro__` has no `HTMLDetailsElement`). Not
patched upstream; logged in PLAN.md.

Fixed by attaching `HTMLDetailsElement`'s `open` property and `toggle()`
method onto the existing `details` class object in place, rather than
replacing the class -- anything that already imported/cached it (or looked
it up via `globals()` during parsing) keeps working against the same
class, now with the missing behaviour. Read via `sys.modules["domonic.
html"]`, not the `domonic.html` attribute -- `domonic/__init__.py` rebinds
that attribute to the `<html>` tag class itself (the same quirk
`domonic_font_size_keywords_patch.py` already works around for
`domonic.style`), so plain `import domonic.html; domonic.html.details`
resolves against the tag class, not the submodule, and silently finds
nothing."""
from __future__ import annotations

import sys

import domonic.dom
import domonic.html  # noqa: F401 -- ensures the real submodule is registered in `sys.modules`

_html = sys.modules["domonic.html"]

_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return False
    details_cls = getattr(_html, "details", None)
    rich_cls = getattr(domonic.dom, "HTMLDetailsElement", None)
    if details_cls is None or rich_cls is None or issubclass(details_cls, rich_cls):
        return False
    details_cls.open = rich_cls.open
    details_cls.toggle = rich_cls.toggle
    _INSTALLED = True
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
