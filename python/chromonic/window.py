"""Phase 2: a real, interactive window -- not a static PNG.

`pywebview` (already a dependency elsewhere in this repo, e.g.
`domonic_libs.App`) hosts the window here, but strictly as a **window and an
input-event bridge** -- it is handed a bare `<img>` and a few lines of JS
that report click coordinates back to Python. It never sees the domonic
tree and never renders any of it; Skia paints every pixel of every frame,
exactly as in phase 1. Clicking the page:

    mouse click (in the OS window)
        -> JS reports (x, y) to Python via pywebview's js_api bridge
        -> hittest.hit_test(root, x, y)              (chromonic, pure Python)
        -> element.dispatchEvent(MouseEvent("click")) (a REAL domonic DOM
           event -- bubbling included -- so a real addEventListener on any
           ancestor fires, exactly like a browser)
        -> tree.layout(root, ...)                     (mark dirty -> rerun
           Taffy -- POC-scope invalidation is still "redo everything", see
           PLAN.md)
        -> paint.render_png(root, ...)                (repaint)
        -> window.evaluate_js(...) swaps the <img> src to the new frame

Phase 3 (`run(..., on_tick=...)`, `examples/animate.py`) adds a continuous
clock on top of the same bridge: the hosted page calls
`window.pywebview.api.tick()`, which runs the caller's `on_tick` closure
(mutating the DOM, e.g. an element's `style.height`), relayouts, and
repaints -- so chromonic drives a real, live animation, still through nothing
but Taffy relayouts and Skia repaints, no different in kind from a
click-triggered one.

**The clock self-paces; it does not run on a bare `setInterval`.** An
earlier version fired `window.pywebview.api.tick()` from `setInterval` on a
fixed timer, which does not wait for one call to finish before the next is
due. Since a `tick()` round-trip is a synchronous call into Python (relayout
+ repaint + a base64 PNG handed back across the bridge), any scene slow
enough that one tick takes longer than the requested interval (a few hundred
particles in `examples/particles.py` gets there easily -- see its own
"Known limitations") caused calls to queue up faster than Python could drain
them, an unbounded backlog that eventually made the whole window
unresponsive, force-quit unresponsive, not just slow. Fixed by having the
hosted page schedule each next tick only after the current one's Promise
resolves (`_TICK_LOOP_JS` below) -- so a slow scene simply runs at whatever
rate it can actually sustain (exactly what `examples/particles.py`'s FPS
readout already measures and shows), with at most one `tick()` ever in
flight.

`Interaction` is the framework-agnostic half of this (render / handle_click
/ tick) and is what `tests/test_chromonic.py` actually exercises -- there is no
display in CI/this sandbox to drive a real window, so `run()` below is meant
to be launched by hand (`examples/live.py`, `examples/animate.py`), not
asserted on automatically.
"""

from __future__ import annotations

import base64

from . import paint, tree

_HTML = """<!doctype html>
<html>
<head>
<style>
  html, body { margin: 0; padding: 0; background: #ffffff; }
  img { display: block; cursor: pointer; }
</style>
</head>
<body>
  <img id="frame" src="" alt="chromonic frame" />
  <script>
    document.getElementById("frame").addEventListener("click", function (event) {
      var rect = event.target.getBoundingClientRect();
      window.pywebview.api.on_click(event.clientX - rect.left, event.clientY - rect.top);
    });
  </script>
</body>
</html>"""

# Self-pacing tick clock: schedule the *next* `tick()` only once the current
# one's Promise resolves (pywebview's `js_api` bridge returns a Promise from
# every call), instead of a plain `setInterval` that fires on a fixed timer
# regardless of whether the previous call finished. See the module docstring
# ("The clock self-paces...") for why a bare `setInterval` can queue up an
# unbounded backlog and hang the whole window on a slow scene. `.finally()`
# (not `.then()`) so a single failed tick doesn't stop the clock outright.
_TICK_LOOP_JS = """
(function loop() {{
  window.pywebview.api.tick().finally(function () {{
    setTimeout(loop, {interval_ms});
  }});
}})();
"""


class Interaction:
    """Render + click-handling (+ animation ticking) for one live page, with
    no pywebview/Skia dependency in its *interface* (`paint`/`tree` are the
    only imports) -- the part of phase 2/3 that is actually tested."""

    def __init__(
        self, root_element, *, width: float, height: "float | None" = None, on_tick=None,
    ):
        self.root = root_element
        self.width = width
        self.height = height
        # called once per animation frame, before relayout -- a plain
        # closure that mutates the live DOM (`element.style.height = ...`),
        # same as a click handler does. `None` means no animation: the page
        # only ever repaints in response to a click.
        self.on_tick = on_tick

    def tick(self) -> None:
        """Advance one animation frame: run `on_tick` (if set) and relayout.
        The caller (`_Api.tick`, driven by a `setInterval` in the hosted
        page -- see `run(on_tick=...)`) repaints via `.render()`."""
        if self.on_tick is not None:
            self.on_tick()
        tree.layout(self.root, width=self.width, height=self.height)

    def render(self, *, relayout: bool = True, reuse_styles: bool = False) -> bytes:
        """Paint a fresh layout by default; event bridges can reuse the layout
        just produced by tick/click/navigation with ``relayout=False``.

        This is an explicit same-event reuse, not a persistent DOM cache:
        callers mutating the DOM directly retain the normal fresh-render path.

        ``reuse_styles`` is a *further* option on top of a real relayout --
        see `tree.layout`'s docstring for the full safety contract (skips
        domonic's own, expensive CSS cascade for elements whose styling
        inputs can't have changed). Only `browser.py`'s image-arrival poll
        passes it; nothing else calling `render()` may, since `on_tick`
        callbacks and click-dispatched DOM handlers can both mutate style.
        """
        if relayout or self.root.get_layout_box() is None:
            tree.layout(self.root, width=self.width, height=self.height, reuse_styles=reuse_styles)
        box = self.root.get_layout_box()
        pixel_height = int(round(box.height)) if self.height is None else int(self.height)
        return paint.render_png(self.root, width=int(self.width), height=max(pixel_height, 1))

    def handle_click(self, x: float, y: float):
        """Hit-test `(x, y)`, dispatch a real bubbling `click` `MouseEvent`
        on whatever's there (so any `addEventListener("click", ...)` on the
        hit element or an ancestor fires, exactly like a browser), then
        relayout. Returns the element that was hit, or `None`."""
        from domonic.events import MouseEvent

        from . import hittest

        element = hittest.hit_test(self.root, x, y)
        if element is not None:
            element.dispatchEvent(MouseEvent("click", {"bubbles": True, "clientX": x, "clientY": y}))
        tree.layout(self.root, width=self.width, height=self.height)
        return element


class _Api:
    """The object handed to `pywebview` as `js_api` -- its public methods
    become `window.pywebview.api.<name>()` in the page's JS, per pywebview's
    own bridge (the same mechanism `domonic_libs.App`'s `Bridge` uses)."""

    def __init__(self, interaction: Interaction):
        self._interaction = interaction
        self._window = None

    def attach(self, window) -> None:
        self._window = window

    def on_click(self, x: float, y: float) -> None:
        self._interaction.handle_click(x, y)
        self.push_frame(relayout=False)

    def tick(self) -> None:
        self._interaction.tick()
        self.push_frame(relayout=False)

    def push_frame(self, *, relayout: bool = True) -> None:
        if self._window is None:
            return
        png = self._interaction.render(relayout=relayout)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        self._window.evaluate_js(f"document.getElementById('frame').src = {data_uri!r};")


def run(
    root_element, *, width: int, height: "int | None" = None, title: str = "chromonic",
    on_tick=None, fps: float = 30.0,
) -> None:
    """Boot a real, interactive window and block until it's closed. Run this
    from a script (see `examples/live.py` / `examples/animate.py`) -- it
    needs a real display, so it is deliberately not something an automated
    check calls.

    `on_tick`, if given, is called once per animation frame (roughly `fps`
    times a second) *before* each relayout+repaint -- a plain closure that
    mutates the live DOM, exactly like a click handler does (see
    `examples/animate.py`). The clock is a `setInterval` in the hosted page
    calling back into Python (the same `js_api` bridge a click uses, just on
    a timer instead of an event) -- there is no separate Python thread."""
    import webview

    interaction = Interaction(
        root_element, width=float(width), height=float(height) if height else None, on_tick=on_tick,
    )
    api = _Api(interaction)
    window = webview.create_window(
        title, html=_HTML, js_api=api, width=width, height=(height or 600) + 40,
    )
    api.attach(window)

    def _on_loaded():
        api.push_frame()
        if on_tick is not None:
            interval_ms = max(1, round(1000.0 / fps))
            window.evaluate_js(_TICK_LOOP_JS.format(interval_ms=interval_ms))

    window.events.loaded += _on_loaded
    webview.start()
