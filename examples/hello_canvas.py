"""Live animated Domonic Canvas rendered by Chromonic.

Run:
    .venv/bin/python chromonic/examples/canvas_animation.py

Space pauses.
Esc closes.
"""

from __future__ import annotations

import argparse
import math
import random
import time

import glfw

import chromonic
from chromonic import paint, tree
from chromonic.native_browser import GLRenderer
from domonic.html import body, canvas, div, h1, p


WIDTH = 900
HEIGHT = 600

CANVAS_W = 860
CANVAS_H = 500


chromonic.initialize()


class Particle:
    def __init__(self):
        self.angle = random.random() * math.tau
        self.radius = random.uniform(35, 220)
        self.speed = random.uniform(0.004, 0.018)
        self.size = random.uniform(1.5, 4.5)
        self.phase = random.random() * math.tau


class CanvasView:
    def __init__(self, width=WIDTH, height=HEIGHT, count=140):
        self.width = width
        self.height = height

        self.paused = False
        self.dirty = True
        self.fps = 0.0

        self.start = time.perf_counter()

        self.particles = [
            Particle()
            for _ in range(count)
        ]

        # ------------------------------------------------------------
        # Ordinary Domonic canvas element
        # ------------------------------------------------------------

        self.drawing = canvas(
            _id="drawing",
            _width=str(CANVAS_W),
            _height=str(CANVAS_H),
            _style=(
                f"display:block;"
                f"width:{CANVAS_W}px;"
                f"height:{CANVAS_H}px;"
                f"border:2px solid #24304d;"
            ),
        )

        self.ctx = self.drawing.getContext("2d")

        # ------------------------------------------------------------
        # Ordinary Domonic page
        # ------------------------------------------------------------

        self.root = body(
            div(
                h1(
                    "Chromonic Canvas",
                    _style=(
                        "display:block;"
                        "margin:0;"
                        "height:34px;"
                        "color:#e2e8f0;"
                    ),
                ),
                p(
                    "Live Domonic Canvas API → Chromonic → GPU",
                    _style=(
                        "display:block;"
                        "margin:0 0 12px;"
                        "height:22px;"
                        "color:#94a3b8;"
                    ),
                ),
                self.drawing,
                _style=(
                    f"display:block;"
                    f"width:{CANVAS_W}px;"
                    f"margin:18px;"
                ),
            ),
            _style=(
                "display:block;"
                "margin:0;"
                "background-color:#070b18;"
            ),
        )

        # ------------------------------------------------------------
        # Layout happens once.
        #
        # We are changing pixels inside <canvas>, NOT its geometry.
        # ------------------------------------------------------------

        self.layout_projection = tree.LayoutProjection()

        self.layout_projection.layout(
            self.root,
            width=self.width,
            height=self.height,
            viewport_height=self.height,
        )

        self.display_list = paint.build_display_list(self.root)

        # Record first frame before first paint.
        self.tick()

    def tick(self):
        if self.paused:
            return

        t = time.perf_counter() - self.start
        ctx = self.ctx

        # ------------------------------------------------------------
        # Canvas is immediate-mode:
        # throw away previous frame's display commands.
        # ------------------------------------------------------------

        ctx.commands.clear()

        # background
        ctx.fillStyle = "#070b18"
        ctx.fillRect(0, 0, CANVAS_W, CANVAS_H)

        cx = CANVAS_W / 2
        cy = CANVAS_H / 2

        # ------------------------------------------------------------
        # Perspective/grid background
        # ------------------------------------------------------------

        horizon = 330

        ctx.strokeStyle = "#17375e"
        ctx.lineWidth = 1

        for i in range(-12, 13):
            ctx.beginPath()
            ctx.moveTo(cx, horizon)
            ctx.lineTo(cx + i * 90, CANVAS_H)
            ctx.stroke()

        for i in range(11):
            y = horizon + i * 17

            ctx.beginPath()
            ctx.moveTo(0, y)
            ctx.lineTo(CANVAS_W, y)
            ctx.stroke()

        # ------------------------------------------------------------
        # Orbit rings
        # ------------------------------------------------------------

        for radius in (60, 110, 165, 220):
            ctx.beginPath()

            steps = 80

            for step in range(steps + 1):
                a = step / steps * math.tau

                x = cx + math.cos(a) * radius
                y = cy + math.sin(a) * radius * 0.48

                if step == 0:
                    ctx.moveTo(x, y)
                else:
                    ctx.lineTo(x, y)

            ctx.strokeStyle = "#243b66"
            ctx.lineWidth = 1
            ctx.stroke()

        # ------------------------------------------------------------
        # Update particles
        # ------------------------------------------------------------

        points = []

        for index, particle in enumerate(self.particles):
            particle.angle += particle.speed

            radius = (
                particle.radius
                + math.sin(t * 1.5 + particle.phase) * 14
            )

            x = cx + math.cos(particle.angle) * radius
            y = cy + math.sin(particle.angle) * radius * 0.48

            points.append((x, y))

            if index % 3 == 0:
                ctx.fillStyle = "#67e8f9"
            elif index % 3 == 1:
                ctx.fillStyle = "#f472b6"
            else:
                ctx.fillStyle = "#facc15"

            size = particle.size

            ctx.fillRect(
                x - size / 2,
                y - size / 2,
                size,
                size,
            )

        # ------------------------------------------------------------
        # Nearby connections
        # ------------------------------------------------------------

        ctx.strokeStyle = "#334155"
        ctx.lineWidth = 1

        for i, (x1, y1) in enumerate(points):

            # deliberately only check a few neighbours rather
            # than making this O(n²)
            for j in range(i + 1, min(i + 8, len(points))):
                x2, y2 = points[j]

                dx = x2 - x1
                dy = y2 - y1

                if dx * dx + dy * dy < 2600:
                    ctx.beginPath()
                    ctx.moveTo(x1, y1)
                    ctx.lineTo(x2, y2)
                    ctx.stroke()

        # ------------------------------------------------------------
        # Animated centre
        # ------------------------------------------------------------

        pulse = 7 + math.sin(t * 4) * 3

        ctx.fillStyle = "#ffffff"
        ctx.fillRect(
            cx - pulse / 2,
            cy - pulse / 2,
            pulse,
            pulse,
        )

        # ------------------------------------------------------------
        # Text
        # ------------------------------------------------------------

        ctx.fillStyle = "#f8fafc"
        ctx.font = "bold 38px sans-serif"
        ctx.textAlign = "center"

        ctx.fillText(
            "PYTHON CANVAS IS ALIVE",
            cx,
            58 + math.sin(t * 2) * 4,
        )

        ctx.fillStyle = "#94a3b8"
        ctx.font = "16px sans-serif"

        ctx.fillText(
            f"{len(self.particles)} particles  •  {self.fps:.1f} fps",
            cx,
            CANVAS_H - 20,
        )

        self.dirty = True

    def draw(self, gpu_canvas):
        # No application-level Skia drawing here.
        #
        # Chromonic's painter encounters our DOM <canvas>,
        # then replays the Domonic CanvasRenderingContext2D
        # command stream into this native Skia canvas.

        paint.paint_display_list(
            gpu_canvas,
            self.display_list,
            top=0.0,
            bottom=self.height,
        )

        self.dirty = False


def run(count=140, *, frames=None):

    if not glfw.init():
        raise RuntimeError("GLFW needs a working display")

    win = None
    renderer = None

    try:

        glfw.window_hint(
            glfw.CONTEXT_VERSION_MAJOR,
            3,
        )
        glfw.window_hint(
            glfw.CONTEXT_VERSION_MINOR,
            2,
        )
        glfw.window_hint(
            glfw.OPENGL_PROFILE,
            glfw.OPENGL_CORE_PROFILE,
        )
        glfw.window_hint(
            glfw.OPENGL_FORWARD_COMPAT,
            True,
        )
        glfw.window_hint(
            glfw.STENCIL_BITS,
            8,
        )

        win = glfw.create_window(
            WIDTH,
            HEIGHT,
            "Chromonic — Domonic Canvas Animation",
            None,
            None,
        )

        if not win:
            raise RuntimeError(
                "Could not create GPU window"
            )

        glfw.make_context_current(win)

        # VSYNC
        glfw.swap_interval(1)

        renderer = GLRenderer()

        view = CanvasView(
            *glfw.get_window_size(win),
            count=count,
        )

        # ------------------------------------------------------------
        # controls
        # ------------------------------------------------------------

        def key(window, key, code, action, mods):

            if action != glfw.PRESS:
                return

            if key == glfw.KEY_ESCAPE:
                glfw.set_window_should_close(
                    window,
                    True,
                )

            elif key == glfw.KEY_SPACE:
                view.paused = not view.paused
                view.dirty = True

        glfw.set_key_callback(
            win,
            key,
        )

        glfw.set_framebuffer_size_callback(
            win,
            lambda *args: setattr(
                view,
                "dirty",
                True,
            ),
        )

        # ------------------------------------------------------------
        # frame loop
        # ------------------------------------------------------------

        last = time.perf_counter()

        elapsed = 0.0
        samples = 0
        drawn = 0

        while not glfw.window_should_close(win):

            glfw.poll_events()

            if not view.paused:
                view.tick()

            if view.dirty or frames is not None:

                renderer.draw(
                    view,
                    glfw.get_framebuffer_size(win),
                )

                glfw.swap_buffers(win)

                now = time.perf_counter()

                if not view.paused:
                    elapsed += now - last
                    samples += 1

                    if elapsed >= 0.5:
                        view.fps = samples / elapsed
                        elapsed = 0.0
                        samples = 0

                last = now

                drawn += 1

                if frames is not None and drawn >= frames:
                    break

            else:

                glfw.wait_events_timeout(0.05)
                last = time.perf_counter()

    finally:

        if renderer is not None:
            renderer.close()

        if win is not None:
            glfw.destroy_window(win)

        glfw.terminate()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description=__doc__,
    )

    parser.add_argument(
        "count",
        type=int,
        nargs="?",
        default=140,
    )

    parser.add_argument(
        "--frames",
        type=int,
    )

    args = parser.parse_args()

    run(
        args.count,
        frames=args.frames,
    )