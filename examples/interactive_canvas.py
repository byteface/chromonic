"""
Chromonic Native Gravity Field
==============================

Domonic Canvas API -> Chromonic Canvas replay -> Skia GPU -> GLFW

Controls
--------
Mouse       attract particles
Left click  gravity explosion
Space       pause
R           reset
G           toggle gravity attractors
T           toggle trails
+ / -       particle count
Esc         quit

Run:
    .venv/bin/python chromonic/examples/gravity_canvas.py

Optional:
    .venv/bin/python chromonic/examples/gravity_canvas.py 5000
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


WINDOW_W = 1280
WINDOW_H = 820

CANVAS_W = 1220
CANVAS_H = 700

DEFAULT_COUNT = 1000
MIN_COUNT = 100
MAX_COUNT = 20_000

TAU = math.tau


# ---------------------------------------------------------------------
# Particle
# ---------------------------------------------------------------------

class Particle:
    __slots__ = (
        "x", "y",
        "vx", "vy",
        "px", "py",
        "size",
        "phase",
        "heat",
    )

    def __init__(self):
        self.reset()

    def reset(self):
        angle = random.random() * TAU
        radius = random.uniform(40, 310)

        cx = CANVAS_W * 0.5
        cy = CANVAS_H * 0.52

        self.x = cx + math.cos(angle) * radius
        self.y = cy + math.sin(angle) * radius * 0.56

        # tangential orbital velocity
        speed = random.uniform(0.3, 1.6)

        self.vx = -math.sin(angle) * speed
        self.vy = math.cos(angle) * speed * 0.72

        self.px = self.x
        self.py = self.y

        self.size = random.choice((1.0, 1.0, 1.0, 1.5, 2.0))
        self.phase = random.random() * TAU
        self.heat = random.random()


# ---------------------------------------------------------------------
# Native view
# ---------------------------------------------------------------------

class GravityView:

    def __init__(self, width, height, count=DEFAULT_COUNT):
        self.width = width
        self.height = height

        self.paused = False
        self.trails = True
        self.gravity_enabled = True

        self.mouse_x = CANVAS_W / 2
        self.mouse_y = CANVAS_H / 2
        self.mouse_inside = False
        self.mouse_down = False

        self.fps = 0.0
        self.frame_no = 0
        self.dirty = True

        self.start_time = time.perf_counter()
        self.previous_time = self.start_time

        self.particles = [
            Particle()
            for _ in range(count)
        ]

        # -------------------------------------------------------------
        # Domonic Canvas
        # -------------------------------------------------------------

        self.drawing = canvas(
            _id="gravity",
            _width=str(CANVAS_W),
            _height=str(CANVAS_H),
            _style=(
                f"display:block;"
                f"width:{CANVAS_W}px;"
                f"height:{CANVAS_H}px;"
                "border:1px solid #24304d;"
                "background:#030712;"
            ),
        )

        self.ctx = self.drawing.getContext("2d")

        # -------------------------------------------------------------
        # Ordinary DOM around it
        # -------------------------------------------------------------

        self.root = body(

            div(

                h1(
                    "CHROMONIC // GRAVITY FIELD",
                    _style=(
                        "display:block;"
                        "margin:0;"
                        "height:34px;"
                        "color:#f8fafc;"
                    ),
                ),

                p(
                    "Domonic Canvas • Python physics • native GPU presentation",
                    _style=(
                        "display:block;"
                        "margin:0 0 12px;"
                        "height:22px;"
                        "color:#64748b;"
                    ),
                ),

                self.drawing,

                _style=(
                    f"display:block;"
                    f"width:{CANVAS_W}px;"
                    "margin:18px;"
                ),
            ),

            _style=(
                "display:block;"
                "margin:0;"
                "background-color:#020617;"
            ),
        )

        # DOM layout only needs doing once.
        self.layout_projection = tree.LayoutProjection()

        self.layout_projection.layout(
            self.root,
            width=width,
            height=height,
            viewport_height=height,
        )

        self.display_list = paint.build_display_list(self.root)

        self.tick()

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def set_count(self, count):
        count = max(MIN_COUNT, min(MAX_COUNT, int(count)))

        current = len(self.particles)

        if count > current:
            self.particles.extend(
                Particle()
                for _ in range(count - current)
            )

        elif count < current:
            del self.particles[count:]

    def reset(self):
        for particle in self.particles:
            particle.reset()

    def explode(self, x, y):
        """Push particles away from click position."""

        for p in self.particles:

            dx = p.x - x
            dy = p.y - y

            distance2 = dx * dx + dy * dy + 12.0

            if distance2 > 80_000:
                continue

            inv = 1.0 / math.sqrt(distance2)

            strength = 125.0 / (1.0 + distance2 * 0.0012)

            p.vx += dx * inv * strength
            p.vy += dy * inv * strength

    def physics(self, dt, t):

        # Don't allow a stalled debugger/window drag to explode physics.
        dt = min(dt, 0.033)

        scale = dt * 60.0

        cx = CANVAS_W * 0.5
        cy = CANVAS_H * 0.52

        # Two moving gravitational bodies.
        a1x = cx + math.cos(t * 0.43) * 155
        a1y = cy + math.sin(t * 0.67) * 90

        a2x = cx + math.cos(t * 0.31 + math.pi) * 225
        a2y = cy + math.sin(t * 0.47 + 1.3) * 120

        mouse_strength = 0.0

        if self.mouse_inside:
            mouse_strength = 70.0 if not self.mouse_down else 180.0

        for p in self.particles:

            p.px = p.x
            p.py = p.y

            ax = 0.0
            ay = 0.0

            if self.gravity_enabled:

                # attractor 1
                dx = a1x - p.x
                dy = a1y - p.y

                d2 = dx * dx + dy * dy + 900.0
                inv = 1.0 / math.sqrt(d2)

                force = 95.0 / d2

                ax += dx * inv * force
                ay += dy * inv * force

                # attractor 2
                dx = a2x - p.x
                dy = a2y - p.y

                d2 = dx * dx + dy * dy + 1200.0
                inv = 1.0 / math.sqrt(d2)

                force = 125.0 / d2

                ax += dx * inv * force
                ay += dy * inv * force

                # weak central gravity
                dx = cx - p.x
                dy = cy - p.y

                d2 = dx * dx + dy * dy + 5000.0
                inv = 1.0 / math.sqrt(d2)

                force = 75.0 / d2

                ax += dx * inv * force
                ay += dy * inv * force

            # mouse gravity
            if mouse_strength:

                dx = self.mouse_x - p.x
                dy = self.mouse_y - p.y

                d2 = dx * dx + dy * dy + 700.0

                inv = 1.0 / math.sqrt(d2)

                force = mouse_strength / d2

                ax += dx * inv * force
                ay += dy * inv * force

            # turbulence
            ax += math.sin(p.phase + t * 0.9) * 0.0008
            ay += math.cos(p.phase * 1.7 + t * 0.7) * 0.0008

            p.vx += ax * scale
            p.vy += ay * scale

            # tiny drag
            p.vx *= 0.9993
            p.vy *= 0.9993

            p.x += p.vx * scale
            p.y += p.vy * scale

            velocity = abs(p.vx) + abs(p.vy)

            p.heat = min(
                1.0,
                velocity * 0.16,
            )

            # wrap edges
            if p.x < -20:
                p.x = CANVAS_W + 20
                p.px = p.x

            elif p.x > CANVAS_W + 20:
                p.x = -20
                p.px = p.x

            if p.y < -20:
                p.y = CANVAS_H + 20
                p.py = p.y

            elif p.y > CANVAS_H + 20:
                p.y = -20
                p.py = p.y

        return a1x, a1y, a2x, a2y

    # ------------------------------------------------------------------
    # Canvas frame
    # ------------------------------------------------------------------

    def record_frame(self, t, attractors):

        ctx = self.ctx

        # Throw away previous Canvas display list.
        ctx.commands.clear()

        # -------------------------------------------------------------
        # Background
        # -------------------------------------------------------------

        ctx.fillStyle = "#030712"
        ctx.fillRect(
            0,
            0,
            CANVAS_W,
            CANVAS_H,
        )

        # subtle bands
        ctx.fillStyle = "#071020"
        ctx.fillRect(0, CANVAS_H * .46, CANVAS_W, CANVAS_H * .54)

        ctx.fillStyle = "#091426"
        ctx.fillRect(0, CANVAS_H * .66, CANVAS_W, CANVAS_H * .34)

        # -------------------------------------------------------------
        # Perspective grid
        # -------------------------------------------------------------

        horizon = 430
        cx = CANVAS_W / 2

        ctx.strokeStyle = "#12304a"
        ctx.lineWidth = 1

        for i in range(-15, 16):

            ctx.beginPath()

            ctx.moveTo(
                cx,
                horizon,
            )

            ctx.lineTo(
                cx + i * 125,
                CANVAS_H,
            )

            ctx.stroke()

        y = horizon + 12
        step = 12

        while y < CANVAS_H:

            ctx.beginPath()

            ctx.moveTo(
                0,
                y,
            )

            ctx.lineTo(
                CANVAS_W,
                y,
            )

            ctx.stroke()

            step *= 1.11
            y += step

        # -------------------------------------------------------------
        # Attractors
        # -------------------------------------------------------------

        a1x, a1y, a2x, a2y = attractors

        for x, y, colour in (
            (a1x, a1y, "#f472b6"),
            (a2x, a2y, "#22d3ee"),
        ):

            # cross-hair glow
            ctx.strokeStyle = colour
            ctx.lineWidth = 1

            ctx.beginPath()
            ctx.moveTo(x - 14, y)
            ctx.lineTo(x + 14, y)
            ctx.stroke()

            ctx.beginPath()
            ctx.moveTo(x, y - 14)
            ctx.lineTo(x, y + 14)
            ctx.stroke()

            ctx.fillStyle = colour
            ctx.fillRect(
                x - 3,
                y - 3,
                6,
                6,
            )

        # -------------------------------------------------------------
        # Particle trails
        # -------------------------------------------------------------

        if self.trails:

            ctx.lineWidth = 1

            for index, p in enumerate(self.particles):

                if index % 3 == 0:
                    ctx.strokeStyle = "#164e63"

                elif index % 3 == 1:
                    ctx.strokeStyle = "#4c1d55"

                else:
                    ctx.strokeStyle = "#3f3f19"

                ctx.beginPath()
                ctx.moveTo(p.px, p.py)
                ctx.lineTo(p.x, p.y)
                ctx.stroke()

        # -------------------------------------------------------------
        # Particle heads
        # -------------------------------------------------------------

        for index, p in enumerate(self.particles):

            if p.heat > 0.72:
                colour = "#ffffff"

            elif index % 5 == 0:
                colour = "#67e8f9"

            elif index % 5 == 1:
                colour = "#f472b6"

            elif index % 5 == 2:
                colour = "#fde047"

            elif index % 5 == 3:
                colour = "#a78bfa"

            else:
                colour = "#34d399"

            ctx.fillStyle = colour

            ctx.fillRect(
                p.x,
                p.y,
                p.size,
                p.size,
            )

        # -------------------------------------------------------------
        # Mouse target
        # -------------------------------------------------------------

        if self.mouse_inside:

            radius = 16 if not self.mouse_down else 26

            ctx.strokeStyle = (
                "#38bdf8"
                if not self.mouse_down
                else "#ffffff"
            )

            ctx.lineWidth = 1

            ctx.beginPath()

            steps = 24

            for i in range(steps + 1):

                angle = i / steps * TAU

                x = (
                    self.mouse_x
                    + math.cos(angle) * radius
                )

                y = (
                    self.mouse_y
                    + math.sin(angle) * radius
                )

                if i == 0:
                    ctx.moveTo(x, y)
                else:
                    ctx.lineTo(x, y)

            ctx.stroke()

        # -------------------------------------------------------------
        # HUD
        # -------------------------------------------------------------

        ctx.fillStyle = "#020617"
        ctx.fillRect(
            16,
            16,
            310,
            112,
        )

        ctx.fillStyle = "#f8fafc"
        ctx.font = "bold 24px sans-serif"
        ctx.textAlign = "left"

        ctx.fillText(
            "GRAVITY FIELD",
            32,
            48,
        )

        ctx.font = "14px sans-serif"
        ctx.fillStyle = "#67e8f9"

        ctx.fillText(
            f"{len(self.particles):,} particles",
            32,
            74,
        )

        ctx.fillStyle = "#94a3b8"

        ctx.fillText(
            f"{self.fps:5.1f} fps   frame {self.frame_no:,}",
            32,
            96,
        )

        status = []

        status.append(
            "gravity:on"
            if self.gravity_enabled
            else "gravity:off"
        )

        status.append(
            "trails:on"
            if self.trails
            else "trails:off"
        )

        ctx.fillText(
            "   ".join(status),
            32,
            116,
        )

        # branding
        ctx.textAlign = "right"
        ctx.fillStyle = "#475569"
        ctx.font = "14px sans-serif"

        ctx.fillText(
            "DOMONIC CANVAS → CHROMONIC → NATIVE GPU",
            CANVAS_W - 22,
            CANVAS_H - 20,
        )

    # ------------------------------------------------------------------
    # Frame
    # ------------------------------------------------------------------

    def tick(self):

        if self.paused:
            return

        now = time.perf_counter()

        dt = now - self.previous_time
        self.previous_time = now

        t = now - self.start_time

        attractors = self.physics(
            dt,
            t,
        )

        self.record_frame(
            t,
            attractors,
        )

        self.frame_no += 1
        self.dirty = True

    # ------------------------------------------------------------------
    # Native renderer callback
    # ------------------------------------------------------------------

    def draw(self, gpu_canvas):

        # No raw Skia drawing here.
        #
        # Chromonic's normal paint traversal sees the <canvas>
        # and replays the Domonic CanvasRenderingContext2D command list.

        paint.paint_display_list(
            gpu_canvas,
            self.display_list,
            top=0.0,
            bottom=self.height,
        )

        self.dirty = False


# ---------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------

def run(count=DEFAULT_COUNT, frames=None):

    chromonic.initialize()

    if not glfw.init():
        raise RuntimeError(
            "GLFW needs a working display"
        )

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
            WINDOW_W,
            WINDOW_H,
            "Chromonic — Native Gravity Canvas",
            None,
            None,
        )

        if not win:
            raise RuntimeError(
                "Could not create GPU window"
            )

        glfw.make_context_current(win)

        # Turn this off if you want to see the real maximum FPS.
        glfw.swap_interval(1)

        renderer = GLRenderer()

        width, height = glfw.get_window_size(win)

        view = GravityView(
            width,
            height,
            count=count,
        )

        # -------------------------------------------------------------
        # Mouse
        # -------------------------------------------------------------

        def cursor(window, x, y):

            # DOM canvas starts roughly below heading/margins.
            #
            # Better eventually:
            # map this using drawing.getBoundingClientRect().
            #
            canvas_box = (
                view.drawing.__dict__
                .get("_layout_box")
            )

            if canvas_box is None:
                return

            view.mouse_x = x - canvas_box.x
            view.mouse_y = y - canvas_box.y

            view.mouse_inside = (
                0 <= view.mouse_x <= CANVAS_W
                and
                0 <= view.mouse_y <= CANVAS_H
            )

        def mouse(
            window,
            button,
            action,
            mods,
        ):

            if button != glfw.MOUSE_BUTTON_LEFT:
                return

            if action == glfw.PRESS:

                view.mouse_down = True

                if view.mouse_inside:
                    view.explode(
                        view.mouse_x,
                        view.mouse_y,
                    )

            elif action == glfw.RELEASE:
                view.mouse_down = False

        glfw.set_cursor_pos_callback(
            win,
            cursor,
        )

        glfw.set_mouse_button_callback(
            win,
            mouse,
        )

        # -------------------------------------------------------------
        # Keyboard
        # -------------------------------------------------------------

        def key(
            window,
            key,
            scancode,
            action,
            mods,
        ):

            if action not in (
                glfw.PRESS,
                glfw.REPEAT,
            ):
                return

            if (
                key == glfw.KEY_ESCAPE
                and action == glfw.PRESS
            ):

                glfw.set_window_should_close(
                    window,
                    True,
                )

            elif (
                key == glfw.KEY_SPACE
                and action == glfw.PRESS
            ):

                view.paused = not view.paused

                if not view.paused:
                    view.previous_time = (
                        time.perf_counter()
                    )

                view.dirty = True

            elif (
                key == glfw.KEY_R
                and action == glfw.PRESS
            ):

                view.reset()

            elif (
                key == glfw.KEY_G
                and action == glfw.PRESS
            ):

                view.gravity_enabled = (
                    not view.gravity_enabled
                )

            elif (
                key == glfw.KEY_T
                and action == glfw.PRESS
            ):

                view.trails = (
                    not view.trails
                )

            elif key == glfw.KEY_EQUAL:

                view.set_count(
                    len(view.particles)
                    + 500
                )

            elif key == glfw.KEY_MINUS:

                view.set_count(
                    len(view.particles)
                    - 500
                )

        glfw.set_key_callback(
            win,
            key,
        )

        # -------------------------------------------------------------
        # FPS
        # -------------------------------------------------------------

        fps_elapsed = 0.0
        fps_frames = 0

        last_frame = time.perf_counter()

        drawn = 0

        # -------------------------------------------------------------
        # Main loop
        # -------------------------------------------------------------

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

                delta = now - last_frame
                last_frame = now

                if not view.paused:

                    fps_elapsed += delta
                    fps_frames += 1

                    if fps_elapsed >= 0.5:

                        view.fps = (
                            fps_frames
                            / fps_elapsed
                        )

                        fps_elapsed = 0.0
                        fps_frames = 0

                drawn += 1

                if (
                    frames is not None
                    and drawn >= frames
                ):
                    break

            else:

                glfw.wait_events_timeout(
                    0.05
                )

                last_frame = (
                    time.perf_counter()
                )

    finally:

        if renderer is not None:
            renderer.close()

        if win is not None:
            glfw.destroy_window(win)

        glfw.terminate()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description=__doc__,
    )

    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=DEFAULT_COUNT,
    )

    parser.add_argument(
        "--frames",
        type=int,
    )

    args = parser.parse_args()

    run(
        count=args.count,
        frames=args.frames,
    )