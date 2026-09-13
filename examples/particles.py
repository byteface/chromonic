"""chromonic: a visual performance demo. N bouncing particles, a live slider to
change N, and a real (measured, not requested) frames-per-second readout --
a way to actually *see* how many absolutely-positioned boxes chromonic can push
through a full Taffy relayout + Skia repaint every animation frame before
the frame rate visibly drops. No new architecture: `tree.py`/`paint.py`/
`window.py` are exactly what phase 3's equalizer used -- this is that same
click-free "clock drives on_tick" loop, at a scale meant to be pushed until
it hurts.

Each particle is a `position:absolute` div inside a `position:relative`
stage, moved every tick by rewriting its own `style.top`/`style.left` --
which is *why* phase 6 first had to teach `chromonic._native`/`style_bridge.py`
about CSS `inset` (`top`/`right`/`bottom`/`left`): every earlier example
only ever needed normal flow (block/flex/grid) layout, where nothing reads
`inset` at all. See "Phase 6" in PLAN.md.

Needs a real display -- run it by hand:

    .venv/bin/pip install 'chromonic[window]'
    .venv/bin/python chromonic/examples/particles.py [initial_count]
"""

from __future__ import annotations

import base64
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div  # noqa: E402

from chromonic import tree, window  # noqa: E402
from chromonic.window import Interaction  # noqa: E402

WIDTH = 800
HEIGHT = 600
PARTICLE_SIZE = 8
MIN_SPEED = 1.5
MAX_SPEED = 5.0
COLORS = [
    "#e53e3e", "#dd6b20", "#d69e2e", "#38a169",
    "#3182ce", "#5a67d8", "#805ad5", "#d53f8c",
]


class Particle:
    """One bouncing box: `element` is the real domonic `div` chromonic paints;
    `step()` is the entire "physics" (linear motion + reflect off the
    stage's edges) and is the only thing that ever touches its position.

    It writes the whole `style` attribute in one `setAttribute` call rather
    than the more obvious `element.style.left = ...; element.style.top =
    ...`. Measured stress-testing this exact demo: two separate
    `style.<prop> = ...` writes each read-parse-mutate-reserialize the
    *entire* inline style text through domonic's `CSSStyleDeclaration`
    machinery, independently of each other -- profiling showed this costing
    roughly **35x** more than one `setAttribute` call with the complete
    string built in Python, for the exact same resulting attribute value (a
    plain string write has nothing to parse). Confirmed safe: `element.style`
    and `ComputedStyleDeclaration` both re-read the live attribute text on
    every access, so nothing downstream (`tree.py`, `paint.py`, a script's
    own `element.style.left` read) can observe a stale value either way."""

    __slots__ = ("element", "style_prefix", "x", "y", "vx", "vy")

    def __init__(self, element, style_prefix: str, x: float, y: float, vx: float, vy: float):
        self.element = element
        self.style_prefix = style_prefix  # everything except left/top -- fixed for this particle's lifetime
        self.x, self.y, self.vx, self.vy = x, y, vx, vy

    def step(self, width: float, height: float) -> None:
        self.x += self.vx
        self.y += self.vy
        max_x, max_y = width - PARTICLE_SIZE, height - PARTICLE_SIZE
        if self.x < 0.0 or self.x > max_x:
            self.vx = -self.vx
            self.x = min(max(self.x, 0.0), max_x)
        if self.y < 0.0 or self.y > max_y:
            self.vy = -self.vy
            self.y = min(max(self.y, 0.0), max_y)
        self.element.setAttribute("style", f"{self.style_prefix} left:{self.x:.1f}px; top:{self.y:.1f}px;")


def _make_particle(width: float, height: float) -> Particle:
    x = random.uniform(0, width - PARTICLE_SIZE)
    y = random.uniform(0, height - PARTICLE_SIZE)
    angle = random.uniform(0, 2 * math.pi)
    speed = random.uniform(MIN_SPEED, MAX_SPEED)
    style_prefix = (
        f"position:absolute; width:{PARTICLE_SIZE}px; height:{PARTICLE_SIZE}px; "
        f"background-color:{random.choice(COLORS)};"
    )
    element = div(_style=f"{style_prefix} left:{x:.1f}px; top:{y:.1f}px;")
    return Particle(element, style_prefix, x, y, speed * math.cos(angle), speed * math.sin(angle))


def build_stage(count: int, width: float = WIDTH, height: float = HEIGHT):
    """A fresh `position:relative` stage containing `count` freshly
    randomised particles. Returns `(stage_element, [Particle, ...])`."""
    particles = [_make_particle(width, height) for _ in range(count)]
    stage = div(
        *(p.element for p in particles),
        _style=f"position:relative; width:{width}px; height:{height}px; background-color:#0f1115;",
    )
    return stage, particles


class ParticleInteraction(Interaction):
    """`Interaction`, plus a live particle count: `set_count()` throws away
    the current stage and builds a fresh one at the new count (no attempt
    to preserve existing particles' positions across a resize -- "simple"
    is the point). `tick()` steps every particle, same shape as
    `Interaction.on_tick` but with a variable body instead of a fixed
    closure, since the thing being animated can itself be swapped out."""

    def __init__(self, *, width: float, height: float, count: int):
        super().__init__(None, width=width, height=height)
        self.particles: list[Particle] = []
        self.set_count(count)

    def set_count(self, count: int) -> None:
        self.root, self.particles = build_stage(count, self.width, self.height)

    def tick(self) -> None:
        for particle in self.particles:
            particle.step(self.width, self.height)
        tree.layout(self.root, width=self.width, height=self.height)


_HTML = """<!doctype html>
<html>
<head>
<style>
  html, body {{ margin: 0; padding: 0; background: #0f1115; font-family: -apple-system, system-ui, sans-serif; color: #e2e8f0; }}
  #toolbar {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: #1a202c; box-sizing: border-box; }}
  #toolbar label {{ font-size: 13px; white-space: nowrap; }}
  #count-slider {{ flex: 1; }}
  #count, #fps {{ font-variant-numeric: tabular-nums; min-width: 70px; font-size: 13px; }}
  img {{ display: block; }}
</style>
</head>
<body>
  <div id="toolbar">
    <label for="count-slider">Particles</label>
    <input id="count-slider" type="range" min="0" max="3000" step="10" value="{initial_count}" />
    <span id="count">{initial_count}</span>
    <span id="fps">-- fps</span>
  </div>
  <img id="frame" src="" alt="chromonic particles" />
  <script>
    var slider = document.getElementById('count-slider');
    slider.addEventListener('input', function () {{
      document.getElementById('count').textContent = slider.value;
      window.pywebview.api.set_count(parseInt(slider.value, 10));
    }});
  </script>
</body>
</html>"""


class _Api(window._Api):
    """As `window._Api`, plus `set_count` (the slider's bridge call) and a
    *measured* FPS pushed to the toolbar each frame -- the wall-clock time
    between one `tick()` call and the next, not the rate the JS
    `setInterval` merely *asked* for. Since pywebview's `js_api` call is a
    synchronous round-trip from the page's perspective, a slow relayout+
    repaint here delays the next tick just as it would in a real browser's
    render loop -- so this number is an honest "what you're actually
    getting", the whole reason this demo exists."""

    def __init__(self, interaction: ParticleInteraction):
        super().__init__(interaction)
        self._last_tick: "float | None" = None

    def set_count(self, count) -> None:
        try:
            count = max(0, min(int(count), 3000))
        except (TypeError, ValueError):
            return
        self._interaction.set_count(count)
        self._last_tick = None  # don't let a resize's own delay count as a slow frame
        self.push_frame()

    def tick(self) -> None:
        now = time.perf_counter()
        fps = 1.0 / (now - self._last_tick) if self._last_tick else None
        self._last_tick = now
        self._interaction.tick()
        self.push_frame(fps=fps, relayout=False)

    def push_frame(self, *, fps: "float | None" = None, relayout: bool = True) -> None:
        if self._window is None:
            return
        png = self._interaction.render(relayout=relayout)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        script = f"document.getElementById('frame').src = {data_uri!r};"
        if fps is not None:
            script += f"document.getElementById('fps').textContent = {f'{fps:.0f} fps'!r};"
        self._window.evaluate_js(script)


def run(*, initial_count: int = 200, fps: float = 60.0) -> None:
    """Boot the particle demo in a real window. Needs a real display -- run
    this from a script, not from an automated check."""
    import webview

    interaction = ParticleInteraction(width=float(WIDTH), height=float(HEIGHT), count=initial_count)
    api = _Api(interaction)
    html = _HTML.format(initial_count=initial_count)
    win = webview.create_window("chromonic -- particles", html=html, js_api=api, width=WIDTH, height=HEIGHT + 46)
    api.attach(win)

    def _on_loaded():
        api.push_frame()
        interval_ms = max(1, round(1000.0 / fps))
        # `window._TICK_LOOP_JS`, not a bare `setInterval`: this demo is
        # exactly the scene slow enough (a few hundred particles) to hit the
        # unbounded-backlog hang a fixed timer causes -- see window.py's
        # module docstring.
        win.evaluate_js(window._TICK_LOOP_JS.format(interval_ms=interval_ms))

    win.events.loaded += _on_loaded
    webview.start()


def main() -> int:
    initial_count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    run(initial_count=initial_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
