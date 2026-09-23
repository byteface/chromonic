"""Sandboxed JavaScript execution for remote pages.

`myjs`'s default JS environment is deliberately unsandboxed -- every
`Session` unconditionally exposes a real filesystem (`fs`), a real shell
(`sh`, `require("child_process")`), `require(name)` falling through to
`importlib.import_module` for *any* installed Python package, a raw
Python `eval()` (`py`), and native OS dialogs/clipboard/file pickers.
That is exactly right for `pyscript.py`'s `<script type="text/python">`
feature -- trusted, first-party application code, the same as running
`python app.py` yourself (see its own docstring) -- and exactly wrong for
a `<script>` a `_load_remote`'d page happened to ship, which could be
anything a website author (or an ad/tracker bundled into that page)
wrote.

Running that page's own JavaScript at all is ordinary browser behaviour
-- every real browser does it unconditionally, on every site, and that is
not a targeted attempt to satisfy any one site's bot-detection, just what
"browser" means. What is *not* ordinary is a webpage script reaching a
real filesystem or shell: no production browser's JS sandbox exposes
either to web content. `run_scripts` below gives a remote page's own
`<script>` elements a real interpreter -- attached to chromonic's
already-parsed domonic document (not a separate DOM `myjs` builds
itself) -- with every one of those non-standard host bindings replaced
by an inert stub first.
"""

from __future__ import annotations

from pathlib import Path

#: Host-binding keys with a genuine web-standard equivalent, or that are
#: simple pure functions with no filesystem/process/interpreter access --
#: kept as-is. Everything else `myjs.host`/`myjs.ffi` currently expose,
#: and anything a future `myjs` version adds under a new key, is replaced
#: with an inert stub by `sandboxed_scope` below: an allowlist, not a
#: denylist, so an upstream addition is unreachable by default instead of
#: silently slipping through unnoticed.
_SAFE_HOST_KEYS = frozenset({"atob", "btoa", "sleep", "now", "hash"})


def _stub(*_args, **_kwargs):
    return None


#: `domonic_libs.acorn.interpret._GLOBAL_DENYLIST` deliberately excludes
#: these from its auto-bound globals (they'd otherwise collide with the
#: `document`/`window` *instances* `Session` binds separately, presumably)
#: -- but a real browser exposes the *interface* too, as a bare global,
#: for ordinary `instanceof`/`typeof` feature-detection. Its absence is
#: what a real site's own bundle tripped on directly (`ReferenceError:
#: Document is not defined`, from `readthedocs-addons.js`): restored here
#: since domonic already has a real class for every one of them -- an
#: upstream `myjs`/`domonic_libs` gap, logged in PLAN.md, worked around
#: locally rather than patched in the vendored package.
def _dom_interface_globals() -> dict:
    import domonic.dom as dom
    from domonic.window import Window

    out = {
        name: getattr(dom, name)
        for name in ("Document", "Node", "Element", "Text", "Comment", "CharacterData", "Attr")
    }
    out["Window"] = Window
    out.update(_cssom_interface_globals())
    return out


#: `domonic.style` -- the whole CSSOM (stylesheets, rules, `CSSStyle
#: Declaration`, ...) -- isn't one of the three modules `domonic_libs`'s
#: own `_collect_domonic_globals` scans (`domonic.javascript`, `domonic.
#: webapi.*`, `domonic.dom`) at all, so every interface in it is missing
#: the same way `Document` was, just for a different reason (never
#: scanned, rather than scanned-then-denylisted). `Style`/`CSSParser`/
#: `Utils`/`ComputedStyleDeclaration` are domonic's own internal names,
#: not real spec interfaces a browser would expose, so excluded here the
#: same way `domonic_libs`'s own scan excludes its non-interface helpers.
_CSSOM_INTERNAL_NAMES = frozenset({"Style", "CSSParser", "Utils", "ComputedStyleDeclaration"})


def _cssom_interface_globals() -> dict:
    import inspect
    import sys

    import domonic.style  # noqa: F401 -- registers the real submodule; see browser.py's own note

    style_module = sys.modules["domonic.style"]
    return {
        name: obj
        for name, obj in vars(style_module).items()
        if (inspect.isclass(obj) and name[:1].isupper()
            and name not in _CSSOM_INTERNAL_NAMES
            and getattr(obj, "__module__", "").startswith("domonic"))
    }


def sandboxed_scope() -> dict:
    """A `myjs.Session(scope=...)` override that neutralizes every host
    binding a real webpage's script should never be able to reach, and
    restores a handful of standard DOM interface globals `myjs` otherwise
    omits (see `_dom_interface_globals`)."""
    from myjs import __version__
    from myjs.ffi import scope as ffi_scope
    from myjs.host import scope as host_scope

    dangerous = {**ffi_scope(), **host_scope(__version__)}
    safe = {key: value for key, value in dangerous.items() if key in _SAFE_HOST_KEYS}
    for key in dangerous:
        safe.setdefault(key, _stub)
    # `require(name)` otherwise falls through to `importlib.import_module`
    # for *any* installed Python package -- no allowlist is safe to build
    # against that, so a remote script gets none of it, unconditionally
    # (this also short-circuits `Session.__init__`'s own re-wrapping of
    # `require`, which only kicks in when `"require" not in scope`).
    safe["require"] = _stub
    safe.update(_dom_interface_globals())
    return safe


def _make_page_class():
    """Deferred so importing this module never pulls in `myjs` (and the
    interpreter it builds) unless a remote page actually runs a script."""
    from myjs._engine import PrintConsole, Session
    from myjs.html import _JS_TYPES, Page, _as_js_error, _Location

    from . import netlog

    class RemoteScriptPage(Page):
        """A `myjs.Page` that runs against a document chromonic's own
        `_load_remote` already fetched and parsed, instead of `Page.
        __init__`'s own fresh `_parse()` call -- and with `sandboxed_
        scope()` in place of `myjs`'s normal, unsandboxed globals, since
        this document's `<script>`s came from an arbitrary, untrusted
        remote site, unlike `pyscript.py`'s deliberately unsandboxed
        first-party model.

        External `<script src>` is fetched through `http_session`
        (chromonic's own `_shared_http_session()` in practice, passed in
        rather than imported here to keep this module independent of
        `browser.py`), not `myjs`'s separate fetcher, so cookies and the
        honest `chromonic/1.0` User-Agent stay consistent with every
        other request this page's navigation made.
        """

        def __init__(self, document, *, url: str, http_session):
            # Deliberately does not call `Page.__init__`/`super().__init__`
            # -- that always parses its own fresh DOM from raw HTML source,
            # which is exactly what this must *not* do (the whole point is
            # reusing the document chromonic already built). Everything
            # below mirrors what `Page.__init__` sets up itself, minus the
            # `_parse()` call and the unsandboxed default scope.
            self.url = url
            self.base_url = url
            self.base_dir = Path.cwd()
            self._strip = False
            self._css = False
            self.document = document
            self._http_session = http_session
            page_scope = {"location": _Location(url), **sandboxed_scope()}
            # `PrintConsole` streams every `console.log`/`warn`/`error`/
            # `assert` call a page's own script makes straight to stdout/
            # stderr as it happens, not just whatever this module's own
            # `run_scripts` override below explicitly logs.
            self.session = Session(scope=page_scope, console=PrintConsole(), document=self.document)
            self.session.interp.module_base = str(self.base_dir)
            self.session.interp._cur_module["dir"] = str(self.base_dir)
            self.errors: list = []

        def _read_resource(self, href: str) -> str:
            target = self._resolve(href)
            netlog.log("js", f"GET {target}")
            response = self._http_session.get(target, timeout=15)
            response.raise_for_status()
            netlog.log("js", f"{response.status_code} {target} ({len(response.text)} bytes)")
            return response.text

        def run_scripts(self):
            """Reimplements `Page.run_scripts` (not a call to `super()`)
            only to add a `netlog` line per script and print each error
            immediately as it's caught -- the inherited version silently
            appends to `self.errors` with nothing printed until a caller
            goes looking, which is the opposite of "stream to stdout"."""
            for el in list(self.document.getElementsByTagName("script")):
                stype = (el.getAttribute("type") or "").strip().lower()
                if stype not in _JS_TYPES:
                    continue
                src = el.getAttribute("src")
                label = src or "(inline)"
                try:
                    code = self._script_source(el)
                except Exception as exc:  # noqa: BLE001 -- a bad src must not abort the page
                    from myjs._engine import JSError
                    error = JSError(f"failed to load script: {exc}", name="NetworkError")
                    self.errors.append(error)
                    netlog.log("js", f"error loading {label}: {exc}")
                    continue
                netlog.log("js", f"running {label}")
                try:
                    self.session.interp.run(code, module=(stype == "module") or None)
                except Exception as exc:  # noqa: BLE001 -- one bad script must not abort the page
                    error = _as_js_error(exc)
                    self.errors.append(error)
                    netlog.log("js", f"error in {label}: {error}")
            self.session.interp.loop.run()
            self._fire_lifecycle()

    return RemoteScriptPage


def run_scripts(document, *, url: str, http_session):
    """Execute `document`'s own `<script>` elements (sandboxed, see module
    docstring) and return the page object that ran them -- `.session`
    (its `myjs` interpreter/window, for callers like the devtools console
    that want to evaluate further expressions against it) and `.errors`
    (whatever `myjs.JSError`s were raised; one bad/unsupported script must
    never abort the page load, so every error is captured there rather
    than propagated, mirroring `myjs.html.Page.run_scripts`'s own per-
    script `try`/`except`, reused unmodified via inheritance)."""
    page_class = _make_page_class()
    page = page_class(document, url=url, http_session=http_session)
    page.run_scripts()
    return page
