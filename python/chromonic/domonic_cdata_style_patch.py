"""`<style>` content wrapped in `<![CDATA[ ... ]]>` is not parsed as CSS --
see `PLAN.md`'s domonic issues log for the full writeup.

Real WPT `.xht` fixtures commonly wrap a `<style>` element's content in a
CDATA section (`<style type="text/css"><![CDATA[ ... ]]></style>`) --
valid, common XHTML practice needed for strict XML well-formedness. domonic
parses HTML only (confirmed: loading the same fixture via a local `file://`
path, which lets real Chrome switch into XML/XHTML parsing mode from the
`.xht` extension alone, makes no difference here -- domonic has no XML mode
to switch into at all), so `<![CDATA[`/`]]>` are never recognised as
markup and survive as literal text inside the `<style>` element's
`textContent` -- `CSSStyleSheet.replaceSync()` then hands the whole thing,
CDATA markers included, to the CSS parser, which cannot make sense of a
stylesheet that starts with `<![CDATA[` and silently produces no rules at
all. Confirmed directly: `tests/wpt/css/CSS2/box/rtl-ib.xht`'s
`.r { direction: rtl; }` never applied, `getComputedStyle(div).direction`
stayed the initial `"ltr"` regardless.

Patched defensively here rather than in domonic's HTML parser (which would
need real CDATA-section recognition, a substantially bigger change) by
stripping a leading `<![CDATA[` / trailing `]]>` wrapper from the text
`CSSStyleSheet.replaceSync()` receives, whenever the whole (stripped) text
is wrapped in exactly one -- ordinary CSS text is never affected, since
real CSS has no legitimate reason to both start with `<![CDATA[` and end
with `]]>`."""
from __future__ import annotations

import re
import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# Same `domonic/__init__.py`-shadowing caveat as the other `domonic_*_patch`
# modules in this package: only a `sys.modules` lookup by dotted name
# reaches the real submodule.
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_REPLACE_SYNC = _style.CSSStyleSheet.replaceSync
_CDATA_WRAPPER_RE = re.compile(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", re.S)


def _strip_cdata_wrapper(text: str) -> str:
    match = _CDATA_WRAPPER_RE.match(text)
    return match.group(1) if match else text


def _replace_sync_stripping_cdata(self, text: str):
    return _ORIGINAL_REPLACE_SYNC(self, _strip_cdata_wrapper(text or ""))


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style.CSSStyleSheet.replaceSync = _replace_sync_stripping_cdata
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style.CSSStyleSheet.replaceSync = _ORIGINAL_REPLACE_SYNC
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
