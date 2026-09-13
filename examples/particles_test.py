"""chromonic: native GLFW/Skia particle stress demo."""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import skia  # noqa: E402
from domonic.html import div  # noqa: E402

from chromonic import paint, tree  # noqa: E402
from chromonic.native_browser import GLRenderer  # noqa: E402
from chromonic.window import Interaction  # noqa: E402

WIDTH = 800
HEIGHT = 600
TOOLBAR = 46
PARTICLE_SIZE = 8
MIN_SPEED = 1.5
MAX_SPEED = 5.0
MAX_PARTICLES = 3000

COLORS = [
    "#e53e3e",
    "#dd6b20",
    "#d69e2e",
    "#38a169",
    "#3182ce",
    "#5a67d8",
    "#805ad5",
    "#d53f8c",
]


class Particle:
    __slots__ = ("element", "style_prefix", "x", "y", "vx", "vy")

    def __init__(
        self,
        element,
        style_prefix: str,
        x: float,
        y: float,
        vx: float,
        vy: float,
    ):
        self.element = element
        self.style_prefix = style_prefix
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy

    def step(self, width: float, height: float) -> None:
        self.x += self.vx
        self.y += self.vy

        max_x = width - PARTICLE_SIZE
        max_y = height - PARTICLE_SIZE

        if self.x < 0.0 or self.x > max_x:
            self.vx = -self.vx
            self.x = min(max(self.x, 0.0), max_x)

        if self.y < 0.0 or self.y > max_y:
            self.vy = -self.vy
            self.y = min(max(self.y, 0.0), max_y)

        self.element.setAttribute(
            "style",
            f"{self.style_prefix} left:{self.x:.1f}px; top:{self.y:.1f}px;",
        )


def _make_particle(width: float, height: float) -> Particle:
    x = random.uniform(0, width - PARTICLE_SIZE)
    y = random.uniform(0, height - PARTICLE_SIZE)

    angle = random.uniform(0, 2 * math.pi)
    speed = random.uniform(MIN_SPEED, MAX_SPEED)

    style_prefix = (
        f"position:absolute; "
        f"width:{PARTICLE_SIZE}px; "
        f"height:{PARTICLE_SIZE}px; "
        f"background-color:{random.choice(COLORS)};"
    )

    element = div(
        _style=f"{style_prefix} left:{x:.1f}px; top:{y:.1f}px;"
    )

    return Particle(
        element,
        style_prefix,
        x,
        y,
        speed * math.cos(angle),
        speed * math.sin(angle),
    )


def build_stage(
    count: int,
    width: float = WIDTH,
    height: float = HEIGHT,
):
    particles = [
        _make_particle(width, height)
        for _ in range(count)
    ]

    stage = div(
        *(particle.element for particle in particles),
        _style=(
            f"position:relative; "
            f"width:{width}px; "
            f"height:{height}px; "
            f"background-color:#0f1115;"
        ),
    )

    return stage, particles


class ParticleInteraction(Interaction):
    def __init__(
        self,
        *,
        width: float,
        height: float,
        count: int,
    ):
        super().__init__(
            None,
            width=width,
            height=height,
        )

        self.particles: list[Particle] = []
        self.set_count(count)

    def set_count(self, count: int) -> None:
        count = max(0, min(int(count), MAX_PARTICLES))

        self.root, self.particles = build_stage(
            count,
            self.width,
            self.height,
        )

        tree.layout(
            self.root,
            width=self.width,
            height=self.height,
        )

    def tick(self) -> None:
        for particle in self.particles:
            particle.step(
                self.width,
                self.height,
            )

        tree.layout(
            self.root,
            width=self.width,
            height=self.height,
        )


class ParticleView:
    def __init__(self, interaction: ParticleInteraction):
        self.interaction = interaction
        self.width = interaction.width
        self.height = interaction.height + TOOLBAR

        self.fps = 0.0
        self.slider_x = 120.0
        self.slider_right = 600.0
        self.dragging_slider = False

    @property
    def particle_count(self):
        return len(self.interaction.particles)

    def resize(self, width, height):
        if width <= 0 or height <= TOOLBAR:
            return

        self.width = float(width)
        self.height = float(height)

        self.interaction.width = float(width)
        self.interaction.height = float(height - TOOLBAR)

        self.interaction.set_count(
            self.particle_count
        )

    def set_count_from_x(self, x):
        usable = max(
            1.0,
            self.slider_right - self.slider_x,
        )

        ratio = min(
            1.0,
            max(
                0.0,
                (x - self.slider_x) / usable,
            ),
        )

        count = int(
            round(
                ratio * MAX_PARTICLES / 10
            )
            * 10
        )

        if count != self.particle_count:
            self.interaction.set_count(count)

    def mouse_button(
        self,
        button,
        action,
        x,
        y,
    ):
        import glfw

        if button != glfw.MOUSE_BUTTON_LEFT:
            return

        if action == glfw.PRESS:
            if y <= TOOLBAR:
                self.dragging_slider = True
                self.set_count_from_x(x)

        elif action == glfw.RELEASE:
            self.dragging_slider = False

    def cursor(self, x, y):
        if self.dragging_slider:
            self.set_count_from_x(x)

    def draw(self, canvas):
        canvas.clear(
            skia.Color4f(
                15 / 255,
                17 / 255,
                21 / 255,
                1,
            )
        )

        canvas.save()
        canvas.translate(0, TOOLBAR)

        paint.paint_tree(
            canvas,
            self.interaction.root,
        )

        canvas.restore()

        toolbar = skia.Paint(
            Color4f=skia.Color4f(
                26 / 255,
                32 / 255,
                44 / 255,
                1,
            ),
            AntiAlias=True,
        )

        canvas.drawRect(
            skia.Rect.MakeWH(
                self.width,
                TOOLBAR,
            ),
            toolbar,
        )

        font = paint._font(13)

        text_paint = skia.Paint(
            Color4f=skia.Color4f(
                226 / 255,
                232 / 255,
                240 / 255,
                1,
            ),
            AntiAlias=True,
        )

        canvas.drawString(
            "Particles",
            12,
            29,
            font,
            text_paint,
        )

        self.slider_x = 90
        self.slider_right = max(
            self.slider_x + 50,
            self.width - 180,
        )

        track_y = 23

        canvas.drawLine(
            self.slider_x,
            track_y,
            self.slider_right,
            track_y,
            skia.Paint(
                Color4f=skia.Color4f(
                    0.45,
                    0.5,
                    0.6,
                    1,
                ),
                StrokeWidth=3,
                AntiAlias=True,
            ),
        )

        ratio = (
            self.particle_count
            / MAX_PARTICLES
        )

        knob_x = (
            self.slider_x
            + ratio
            * (
                self.slider_right
                - self.slider_x
            )
        )

        canvas.drawCircle(
            knob_x,
            track_y,
            7,
            skia.Paint(
                Color4f=skia.Color4f(
                    0.2,
                    0.55,
                    0.95,
                    1,
                ),
                AntiAlias=True,
            ),
        )

        canvas.drawString(
            str(self.particle_count),
            self.slider_right + 15,
            29,
            font,
            text_paint,
        )

        canvas.drawString(
            f"{self.fps:.0f} fps",
            self.width - 70,
            29,
            font,
            text_paint,
        )


def run(
    *,
    initial_count: int = 200,
    fps: float = 60.0,
) -> None:
    import glfw

    if not glfw.init():
        raise RuntimeError(
            "GLFW initialization failed"
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
            WIDTH,
            HEIGHT + TOOLBAR,
            "chromonic -- particles",
            None,
            None,
        )

        if not win:
            raise RuntimeError(
                "Could not create GPU window"
            )

        glfw.make_context_current(win)
        glfw.swap_interval(1)

        interaction = ParticleInteraction(
            width=float(WIDTH),
            height=float(HEIGHT),
            count=initial_count,
        )

        view = ParticleView(
            interaction
        )

        renderer = GLRenderer()

        def mouse_button(
            _win,
            button,
            action,
            mods,
        ):
            x, y = glfw.get_cursor_pos(win)

            view.mouse_button(
                button,
                action,
                x,
                y,
            )

        def cursor_pos(
            _win,
            x,
            y,
        ):
            view.cursor(
                x,
                y,
            )

        glfw.set_mouse_button_callback(
            win,
            mouse_button,
        )

        glfw.set_cursor_pos_callback(
            win,
            cursor_pos,
        )

        glfw.set_window_size_callback(
            win,
            lambda _win, w, h:
                view.resize(w, h),
        )

        frame_interval = (
            1.0 / fps
            if fps > 0
            else 0.0
        )

        last_frame = time.perf_counter()
        sample_start = last_frame
        sample_frames = 0

        while not glfw.window_should_close(win):
            glfw.poll_events()

            now = time.perf_counter()

            if (
                frame_interval == 0.0
                or now - last_frame >= frame_interval
            ):
                interaction.tick()

                renderer.draw(
                    view,
                    glfw.get_framebuffer_size(win),
                )

                glfw.swap_buffers(win)

                last_frame = now
                sample_frames += 1

                elapsed = (
                    now - sample_start
                )

                if elapsed >= 0.5:
                    view.fps = (
                        sample_frames
                        / elapsed
                    )

                    sample_frames = 0
                    sample_start = now

    finally:
        if renderer is not None:
            renderer.close()

        if win is not None:
            glfw.destroy_window(win)

        glfw.terminate()


def main() -> int:
    initial_count = (
        int(sys.argv[1])
        if len(sys.argv) > 1
        else 200
    )

    run(
        initial_count=initial_count
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())