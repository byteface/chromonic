"""Live stdout tracing for network fetches and myjs console/error output.

Chromonic has no single fetch chokepoint -- `browser.py`'s own session
fetches the page, `browser_images.py`/`webfonts.py` use plain `urllib.
request`, domonic's `_scrape._parse(css=True)` fetches `<link rel=
stylesheet>` internally, and `js_sandbox.py` fetches external `<script
src>`. `log()` below is the one shared print used at each of those call
sites so they all read the same way; `install()` additionally wraps the
one fetch this module doesn't already have a call site for (domonic's
own internal stylesheet fetch) since duplicating its href-resolution/
credential logic here just to log it isn't worth it.

Always-on, plain `print()`, not the `logging` module: this is meant to be
watched live in a terminal, not configured via log levels/handlers.
"""

from __future__ import annotations

import sys

_installed = False


def log(kind: str, message: str) -> None:
    print(f"[{kind}] {message}", file=sys.stdout, flush=True)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    _wrap_domonic_stylesheet_fetch()


def _wrap_domonic_stylesheet_fetch() -> None:
    from domonic import _scrape

    original = _scrape._fetch_stylesheet_text

    def logged(href, source_request, request_kwargs=None):
        log("css", f"GET {href}")
        try:
            text = original(href, source_request, request_kwargs)
        except Exception as error:  # noqa: BLE001 -- re-raised immediately, just logged first
            log("css", f"FAILED {href}: {error}")
            raise
        log("css", f"200 {href} ({len(text)} bytes)")
        return text

    _scrape._fetch_stylesheet_text = logged
