"""domonic's html5lib tree builder creates a new Text node for every
character token, so each character reference starts one: `A&nbsp;&nbsp;B
&amp; c` parses to `'A'`, `'\\xa0'`, `'\\xa0'`, `'B '`, `'&'`, `' '`, `'c'`
where a browser has one node. HTML's "insert a character" appends to a
Text node right before the insertion point instead.

Found on `wpt/css/CSS2/box-display/block-in-inline-relpos-001.xht`:
`A&nbsp;&nbsp;` reports one 60px text rect in Chrome, three nodes (one
20px rect) here. Patches `NodeBuilder.insertText` of domonic's cached
html5lib DOM builder module."""
from __future__ import annotations

from domonic.dom import Text
from domonic.ext import html5lib_ as _builder
from domonic.ext._rawdom import _live_args

_NodeBuilder = _builder.getDomModule(_builder.implementation).NodeBuilder
_ORIGINAL_INSERT_TEXT = _NodeBuilder.insertText

_INSTALLED = False


def _insert_text(self, data, insertBefore=None):
    args = _live_args(self.element)
    if insertBefore is None:
        previous = args[-1] if args else None
    else:
        index = next((i for i, node in enumerate(args) if node is insertBefore.element), None)
        previous = args[index - 1] if index else None
    if type(previous) is Text and len(previous.__dict__.get("args") or ()) == 1:
        previous.__dict__["args"] = (previous.__dict__["args"][0] + ("" if data is None else str(data)),)
        return
    _ORIGINAL_INSERT_TEXT(self, data, insertBefore)


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _NodeBuilder.insertText = _insert_text
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _NodeBuilder.insertText = _ORIGINAL_INSERT_TEXT
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
