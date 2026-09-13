"""chromonic phase 5: executable Python inside HTML.

    <script type="text/python">
    button = document.querySelector("#hello")

    def clicked(event):
        button.textContent = "Clicked"

    button.addEventListener("click", clicked)
    </script>

Detect `<script type="text/python">`, `exec()` its source as plain Python
with `document`/`window` injected, against the *same live domonic DOM
instance* driving the rest of chromonic -- `tree.py`/`paint.py`/`window.py`
never see a copy, so a listener a script registers here is a completely
ordinary `addEventListener` call on a real `Element`. That is also why
nothing in `window.py`'s click handling needed to change for this: a real
domonic `dispatchEvent` doesn't care whether the function it calls came from
a JS interpreter, a Python `exec()`, or was built by hand -- `python function
reference` is already a valid event listener as far as domonic is concerned.
`Interaction.handle_click` already hit-tests, dispatches a real
`MouseEvent`, relayouts, and repaints on every click -- so "the client
visibly redraws" needed zero new plumbing, only a way to *get* a Python
function onto an element's event list from HTML in the first place, which is
everything this module actually does.

Page loading (fetch, parse, apply `<link rel=stylesheet>`) reuses
`myjs.Page.load(url_or_path, run=False)` -- this repo's own tested fetch/
parse/CSS backbone (see `chromonic.browser`'s phase 4 for the same reuse).
`run=False` means *no* script element runs there, JS or Python: `myjs` only
ever runs JS anyway and already silently skips any type it doesn't
recognise (see `myjs/html.py`'s `_JS_TYPES`), so every
`<script type="text/python">` on the page survives to `run_python_scripts`
below completely unexecuted, exactly like any other script type `myjs`
doesn't understand -- no coordination between the two runtimes is needed
beyond that.

**No sandbox, on purpose.** A `<script type="text/python">` runs with the
full power of a plain Python `exec()` -- no import allowlist, no resource
limits, no capability restriction beyond whatever `document`/`window`
happen to expose. That is fine, and the entire design, for **trusted
application code** you or your own app authored -- treat a `.py`-in-HTML
page exactly like running `python app.py` yourself, because that is
functionally what this does. Never point this at arbitrary, remote, or
user-supplied HTML/Python -- there is no isolation here to protect against
it, and none is planned for this POC (see `PLAN.md`).
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path

_PY_TYPES = {"text/python", "application/python", "application/x-python", "python"}
_UA = "chromonic/pyscript (+https://github.com/byteface/domonic-libs)"


class Window:
    """The `window` global handed to a `<script type="text/python">`.
    `.document` is the live document the script's DOM calls act on; anything
    else (`location`, `alert`, `fetch`, `atob`/`btoa`, `setTimeout`, ...)
    forwards to domonic's own `window` singleton (`domonic.window.window`)
    -- the same "explicit attrs win, everything else forwards to the real
    domonic window" shape `domonic_libs.acorn.interpret._Window` already
    uses to build JS's global object. Not reused by import: that one also
    shims `Array`/`Object`/`JSON`/etc. to JS semantics, which a plain Python
    script has no use for and should never be handed."""

    def __init__(self, document):
        self.document = document

    def __getattr__(self, name):
        from domonic.window import window as _dw

        return getattr(_dw, name)


def _is_url(value) -> bool:
    return isinstance(value, str) and value.split(":", 1)[0].lower() in ("http", "https")


def _validate_remote_src(url: str) -> None:
    """The same restriction `perusal.core.validate_url` applies to every
    fetch it makes: an absolute `http(s)` URL with a real hostname, nothing
    else. Applied here to a `<script src=...>` that resolved to a URL, so a
    page can't smuggle a `javascript:`/`data:`/`file:`-scheme `src` past a
    caller that only meant to allow ordinary remote script files."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"refusing to fetch a <script src> outside http(s): {url!r}")


def _is_python_script(element) -> bool:
    return (element.getAttribute("type") or "").strip().lower() in _PY_TYPES


def find_python_scripts(document) -> list:
    """Every `<script type="text/python">` in `document`, in document
    order -- the order a real page's script tags already share one running
    order in."""
    return [el for el in document.getElementsByTagName("script") if _is_python_script(el)]


def _read_src(src: str, *, base_url=None, base_dir=None) -> str:
    """Resolve and read a `<script src="...">`'s own source, relative to the
    page it came from -- the same resolution rule `myjs.html.Page._resolve`
    uses: an absolute/`http(s)`/`//`-scheme `src` wins outright, otherwise
    resolve against the page's own URL if it had one, else its directory."""
    if _is_url(src):
        target = src
    elif src.startswith("//"):
        scheme = (base_url or "https:").split(":", 1)[0]
        target = f"{scheme}:{src}"
    elif base_url:
        target = urllib.parse.urljoin(base_url, src)
    else:
        target = str((base_dir or Path.cwd()) / src)

    if _is_url(target):
        _validate_remote_src(target)
        request = urllib.request.Request(target, headers={"User-Agent": _UA})
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - same-origin script src
            return response.read().decode("utf-8", "replace")
    return Path(target).read_text(encoding="utf-8")


def run_python_scripts(document, *, base_url=None, base_dir=None, window=None) -> dict:
    """Execute every `<script type="text/python">` in `document`, in order,
    in one shared namespace -- a real page's script tags share one global
    scope too, and this POC doesn't attempt a per-script module system (see
    `PLAN.md`) -- against the live `document`, not a copy. `window`, if not
    given, is a fresh `Window(document)`. Returns the shared globals dict, so
    a caller can reach back into a script's own top-level names afterwards
    (the same thing `myjs.Session.eval` lets a caller do for JS)."""
    win = window if window is not None else Window(document)
    scope = {"document": document, "window": win}
    for element in find_python_scripts(document):
        src = element.getAttribute("src")
        source = _read_src(src, base_url=base_url, base_dir=base_dir) if src else (element.textContent or "")
        exec(compile(source, src or "<script>", "exec"), scope)  # noqa: S102 - trusted app code, see module docstring
    return scope


def parse_and_run(html: str, *, base_dir=None, window=None):
    """Parse a raw HTML string (`domonic.dom.DOMParser`, no fetch, no
    stylesheet application -- see `load_and_run` for that) and run its
    `<script type="text/python">`s. Returns `(document, scope)`. The
    lightest-weight way to try phase 5 -- no `myjs`/network dependency, for
    an inline-only page."""
    from domonic.dom import DOMParser

    document = DOMParser().parseFromString(html, "text/html")
    scope = run_python_scripts(document, base_dir=base_dir, window=window)
    return document, scope


def load_and_run(url_or_path: str, *, window=None):
    """Convenience: `myjs.Page.load(url_or_path, run=False)` (fetch/parse,
    `<link rel=stylesheet>` folded in, no script runs there) then
    `run_python_scripts` on the result. Returns `(document, scope)`. Needs
    the same `myjs` optional dependency `chromonic.browser` does
    (`chromonic[browse]`)."""
    from myjs import Page

    page = Page.load(url_or_path, run=False)
    scope = run_python_scripts(page.document, base_url=page.base_url, base_dir=page.base_dir, window=window)
    return page.document, scope
