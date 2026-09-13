"""A simple, navigable browser on top of the rest of chromonic -- built for one
job: letting the user *visually* check how domonic's DOM/CSSOM/layout
resolves a real, live page (a real address bar, real links, side by side
against an actual browser), not to be a general-purpose one.

Fetch + parse + external-stylesheet application is not reimplemented here --
it reuses `myjs.Page.load(url, run=False)`, this repo's own already-tested
fetch/parse backbone (see `packaging/myjs`): the page's `<script>`s never
run (chromonic has no interest in executing JS, only in rendering the DOM/CSSOM
domonic builds), but its `<link rel=stylesheet>`s are fetched (concurrently)
and folded in first, exactly as `Page` already does for headless JS testing.
`document.body` -- not `document.documentElement` -- is the render root, the
same choice a real browser's viewport makes (`<head>` never paints).

    chromonic.browser.run("https://example.com/")   # opens a window, needs a display

Everything below `run()` (`BrowserInteraction`) has no `pywebview` import and
is exercised headlessly in `tests/test_chromonic.py`, same pattern as
`window.Interaction`.

Two small defensive patterns are borrowed from `perusal` (this repo's other
"fetch and show a real page" tool) rather than reinvented: address-bar
leniency (typing `example.com` becomes `https://example.com`, matching
`perusal.browser.normalize_url`) and refusing to navigate anywhere that
isn't an absolute `http(s)` URL (matching `perusal.core.validate_url` --
without it, a typed non-URL string would fall through to `myjs.Page.load`'s
local-file branch and quietly read a file off disk). Reimplemented locally
in plain `urllib` rather than imported, since perusal is a separate,
unrelated standalone package.
"""

from __future__ import annotations

import base64
import json
import urllib.parse

from . import hittest, tree, window


def _is_url(s: str) -> bool:
    return isinstance(s, str) and s.split(":", 1)[0].lower() in ("http", "https")


def _normalize_address(value: str) -> str:
    """Address-bar leniency: typing `example.com` (no scheme) becomes
    `https://example.com` -- the same rule `perusal.browser.normalize_url`
    already applies to *its* address bar, reimplemented here in plain
    `urllib` rather than importing perusal (a separate, unrelated standalone
    package) for one small helper. Unlike perusal's version, a value that
    still isn't a plausible URL after that is left alone (and will simply
    fail `_validate_navigable` below) -- this browser has no search-engine
    fallback to send it to."""
    value = (value or "").strip()
    if not value:
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    if "." in value and " " not in value:
        return "https://" + value
    return value


def _validate_navigable(url: str) -> None:
    """The same restriction `perusal.core.validate_url` applies to every
    fetch it makes: an absolute `http(s)` URL with a real hostname, nothing
    else. Without this, a typed address that isn't a URL at all would fall
    through to `myjs.Page.load`'s local-file branch (any non-`http(s)`
    string is treated as a filesystem path) and quietly read a file off
    disk instead of failing -- worth refusing outright for an address bar a
    person types into."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"chromonic's browser only navigates absolute http(s) URLs, got: {url!r}")


def warm_interpreter() -> None:
    """Pay `myjs`'s one-time JS-interpreter-globals setup cost now, not
    during the first real page load.

    Measured profiling `native_browser.py`'s "still feels slow to load"
    (`cProfile` on `View.navigate()`, not guessing): the very first
    `myjs.Page`/`Session` constructed in a process spends ~100-150ms inside
    `domonic_libs.acorn.interpret._collect_domonic_globals` -- reflectively
    collecting every public constructor in `domonic.javascript`/
    `domonic.webapi.*`/`domonic.dom` into the JS global object `myjs`
    builds for every page, `run=False` or not (`chromonic` never runs page
    scripts, but still needs a `Session` for `page.session.window` --
    `native_browser.py`'s `View.relayout()` sets `innerWidth`/`innerHeight`
    on it so `@media` queries see the real viewport). That collection is
    itself cached at the `domonic_libs.acorn.interpret` module level
    (`_DOMONIC_GLOBALS`), so it only actually runs once per process --
    confirmed directly: navigating to a second, different URL in the same
    process measured 3x faster than the first. This function forces that
    one-time cost to happen now, with no network I/O of its own (`css=False`,
    a literal HTML string, no URL fetched) -- called once from
    `native_browser.py`'s `View.__init__`, the same "pay it at startup, not
    during the first frame" idea `fonts.warm_cache()` already uses."""
    from myjs import Page

    Page("<html></html>", run=False, css=False)


def load(url: str):
    """Fetch + parse `url` via `myjs.Page` -- external stylesheets folded in,
    `<script>`s never run. Returns the `myjs.Page`.

    Two things happen to the parsed page before it's handed back, both new
    in phase 7 and both because a real page assumes a real browser's own
    defaults that domonic doesn't supply (see `ua_style.py`'s module
    docstring): a UA stylesheet is applied (`ua_style.apply`), and every
    `<img src>` is resolved to an absolute URL against this page's own URL
    (`browser_images.resolve_image_sources`) -- `tree.py`/`paint.py` fetch
    and decode images by that `src` directly and have no other way to know
    what page an `<img>` came from."""
    from myjs import Page

    from . import browser_images, ua_style

    page = Page.load(url, run=False)
    ua_style.apply(page.document)
    browser_images.resolve_image_sources(page.document, page.url)
    return page


class BrowserInteraction(window.Interaction):
    """`Interaction`, plus navigation: fetching a new URL swaps `self.root`
    under it (viewport size stays fixed across pages), and clicking an
    `<a href>` (or anything inside one) navigates instead of just dispatching
    a DOM event. A small back-history stack makes the toolbar's back button
    work; there's no forward-history/reload beyond that -- "simple" per the
    brief this was built for."""

    def __init__(self, url: str, *, width: float, height: "float | None" = None):
        super().__init__(None, width=width, height=height)
        self._history: list[str] = []
        self._load(url, record=True)

    def _load(self, url: str, *, record: bool) -> None:
        _validate_navigable(url)
        page = load(url)
        self.url = url
        self.root = page.document.body
        if record:
            self._history.append(url)

    def navigate(self, href: str) -> None:
        self._load(urllib.parse.urljoin(self.url, href), record=True)
        tree.layout(self.root, width=self.width, height=self.height)

    def go_back(self) -> bool:
        """Pop back to the previous page, if any. Returns whether it moved."""
        if len(self._history) < 2:
            return False
        self._history.pop()
        self._load(self._history[-1], record=False)
        tree.layout(self.root, width=self.width, height=self.height)
        return True

    def handle_click(self, x: float, y: float):
        """As `Interaction.handle_click`, except a click that lands on (or
        inside) an `<a href>` navigates there instead of only dispatching a
        DOM `click` event -- an in-page `#fragment` link is left alone (no
        page to fetch)."""
        element = hittest.hit_test(self.root, x, y)
        anchor = element
        while anchor is not None and (getattr(anchor, "tagName", "") or "").lower() != "a":
            anchor = getattr(anchor, "parentElement", None)
        href = anchor.getAttribute("href") if anchor is not None else None
        if href and not href.startswith("#") and not href.split(":", 1)[0].lower() in ("javascript", "mailto", "tel"):
            try:
                self.navigate(href)
            except ValueError as error:
                print(f"chromonic: {error}")  # e.g. an href that isn't http(s) after resolving -- ignore, don't crash
            return element
        return super().handle_click(x, y)


_HTML = """<!doctype html>
<html>
<head>
<style>
  html, body { margin: 0; padding: 0; background: #ffffff; font-family: -apple-system, system-ui, sans-serif; }
  #toolbar { display: flex; gap: 6px; padding: 6px; background: #e2e8f0; box-sizing: border-box; }
  #back { padding: 4px 10px; }
  #address { flex: 1; padding: 4px 8px; font-size: 14px; }
  #go { padding: 4px 12px; }
  img { display: block; cursor: pointer; }
</style>
</head>
<body>
  <div id="toolbar">
    <button id="back" title="Back">&#8592;</button>
    <input id="address" type="text" />
    <button id="go">Go</button>
  </div>
  <img id="frame" src="" alt="chromonic frame" />
  <script>
    function goToAddressBar() {
      window.pywebview.api.navigate(document.getElementById('address').value);
    }
    document.getElementById('go').addEventListener('click', goToAddressBar);
    document.getElementById('back').addEventListener('click', function () {
      window.pywebview.api.go_back();
    });
    document.getElementById('address').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') goToAddressBar();
    });
    document.getElementById('frame').addEventListener('click', function (event) {
      var rect = event.target.getBoundingClientRect();
      window.pywebview.api.on_click(event.clientX - rect.left, event.clientY - rect.top);
    });
  </script>
</body>
</html>"""


class _Api(window._Api):
    """As `window._Api`, plus the toolbar's two extra bridge calls
    (`navigate`, `go_back`) and pushing the current URL to the address bar on
    every repaint, not just the frame."""

    def __init__(self, interaction: "BrowserInteraction"):
        super().__init__(interaction)
        self._image_generation = 0

    def navigate(self, url: str) -> None:
        try:
            self._interaction.navigate(_normalize_address(url))
        except ValueError as error:
            print(f"chromonic: {error}")  # a typed address that isn't navigable -- leave the current page up
            return
        self.push_frame(relayout=False)

    def go_back(self) -> None:
        if self._interaction.go_back():
            self.push_frame(relayout=False)

    def tick(self) -> None:
        """Polls for background `<img>` fetches (`browser_images.py`)
        finishing since the last check -- unlike `window._Api.tick()`'s
        animation clock, this does *not* unconditionally relayout+repaint
        every interval (see `run()`'s tick loop below): a page with nothing
        left to load costs nothing extra between navigations. Only pushes a
        fresh frame (a real relayout -- an image's arrival can change its
        box's size) when an image actually finished."""
        from . import browser_images

        current = browser_images.generation()
        if current != self._image_generation:
            self._image_generation = current
            # `reuse_styles=True` -- see `window.Interaction.render`'s
            # docstring: an image finishing a background fetch never
            # changes any element's class/inline-style/stylesheets, so this
            # relayout can skip domonic's CSS cascade entirely (the same
            # fix `native_browser.py`'s `poll_images` uses, and for the
            # same measured reason -- see `docs/domonic-wrinkles.md` #16).
            self.push_frame(reuse_styles=True)

    def push_frame(self, *, relayout: bool = True, reuse_styles: bool = False) -> None:
        if self._window is None:
            return
        png = self._interaction.render(relayout=relayout, reuse_styles=reuse_styles)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        address = json.dumps(self._interaction.url)
        self._window.evaluate_js(
            f"document.getElementById('frame').src = {data_uri!r};"
            f"document.getElementById('address').value = {address};"
        )


_IMAGE_POLL_MS = 300


def run(url: str, *, width: int = 1000, height: int = 800, title: str = "chromonic") -> None:
    """Boot a real window at `url` with a working address bar and clickable
    links, and block until it's closed. Needs a real display -- run this from
    a script (see `examples/browse.py`), not from an automated check.

    The viewport (`width` x `height`) is fixed across navigations -- no
    scrolling, same known limitation as `window.run` (see PLAN.md).

    Runs `window._TICK_LOOP_JS` from startup, unconditionally -- not an
    animation clock (there's no `on_tick`), just `_Api.tick()` polling
    `browser_images.py` every `_IMAGE_POLL_MS` for a background `<img>`
    fetch finishing, so images pop in as they arrive instead of the whole
    page waiting for the slowest one before it can be shown at all (fetches
    themselves start the moment `tree.layout()` first sees an `<img>`;
    this loop is only what notices and repaints once one's ready)."""
    import webview

    interaction = BrowserInteraction(url, width=float(width), height=float(height))
    api = _Api(interaction)
    win = webview.create_window(title, html=_HTML, js_api=api, width=width, height=height + 80)
    api.attach(win)

    def _on_loaded():
        api.push_frame()
        win.evaluate_js(window._TICK_LOOP_JS.format(interval_ms=_IMAGE_POLL_MS))

    win.events.loaded += _on_loaded
    webview.start()
