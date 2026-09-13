"""chromonic: a visual performance demo. N bouncing particles, a live slider to
change N, and a real (measured, not requested) frames-per-second readout --
a way to actually *see* how many absolutely-positioned boxes chromonic can push
through a full Taffy relayout + Skia repaint every animation frame before
the frame rate visibly drops. No new architecture: `tree.py`/`paint.py`/
`window.py` first exercised -- now presented through the same native
GLFW/Skia path as `particles2.py`, at a scale meant to be pushed until it
hurts.

Each particle is a `position:absolute` div inside a `position:relative`
stage, moved every tick by rewriting its own `style.top`/`style.left` --
which is *why* phase 6 first had to teach `chromonic._native`/`style_bridge.py`
about CSS `inset` (`top`/`right`/`bottom`/`left`): every earlier example
only ever needed normal flow (block/flex/grid) layout, where nothing reads
`inset` at all. See "Phase 6" in PLAN.md.

Needs a real display -- run it by hand:

    make develop
    .venv/bin/python chromonic/examples/particles.py [initial_count]
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div  # noqa: E402

from chromonic import tree  # noqa: E402
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


def run(*, initial_count: int = 200, fps: float = 60.0, frames=None) -> None:
    """Boot the particle demo in a real window. Needs a real display -- run
    this from a script, not from an automated check."""
    from particles2 import run as run_native

    run_native(initial_count, frames=frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, nargs="?", default=200)
    parser.add_argument("--frames", type=int)
    args = parser.parse_args()
    if args.frames is not None and args.frames < 1:
        parser.error("--frames must be positive")
    run(initial_count=args.count, frames=args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
