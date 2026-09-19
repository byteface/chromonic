"""Native Chromonic window.

GLFW owns the OS window/input.
GLRenderer owns the Skia/OpenGL surface.
domonic owns events.
Chromonic owns hit-testing/layout/paint.
"""

from __future__ import annotations

import base64
import time

from . import hittest, paint, tree
from .native_browser import GLRenderer


class Interaction:
    def __init__(
        self,
        root_element,
        *,
        width: float,
        height: "float | None" = None,
        on_tick=None,
    ):
        self.root = root_element
        self.width = width
        self.height = height
        self.on_tick = on_tick
        self.focused_element = None

    def relayout(self) -> None:
        tree.layout(
            self.root,
            width=self.width,
            height=self.height,
        )

    def tick(self) -> None:
        if self.on_tick is not None:
            self.on_tick()

        tree.layout(
            self.root,
            width=self.width,
            height=self.height,
        )

    def render(self, *, relayout: bool = True, reuse_styles: bool = False) -> bytes:
        if relayout or self.root.get_layout_box() is None:
            tree.layout(
                self.root,
                width=self.width,
                height=self.height,
                reuse_styles=reuse_styles,
            )
        box = self.root.get_layout_box()
        pixel_height = int(round(box.height)) if self.height is None else int(self.height)
        return paint.render_png(self.root, width=int(self.width), height=max(pixel_height, 1))

    def handle_click(self, x: float, y: float):
        from domonic.events import MouseEvent

        element = hittest.hit_test(self.root, x, y)

        if element is not None:
            if getattr(element, "tagName", "").lower() in {"input", "textarea", "select", "button"}:
                self.focused_element = element
            element.dispatchEvent(
                MouseEvent(
                    "click",
                    {
                        "bubbles": True,
                        "clientX": x,
                        "clientY": y,
                    },
                )
            )

        self.relayout()

        return element

    def handle_text(self, text: str):
        element = self.focused_element
        if element is None or getattr(element, "tagName", "").lower() not in {"input", "textarea"}:
            return None
        element.value = getattr(element, "value", "") + text
        self.relayout()
        return element

    def handle_key(self, key: str):
        from domonic.events import KeyboardEvent

        element = self.focused_element
        if element is None:
            return None
        if key == "Backspace" and getattr(element, "tagName", "").lower() in {"input", "textarea"}:
            element.value = getattr(element, "value", "")[:-1]
        element.dispatchEvent(
            KeyboardEvent(
                "keydown",
                {
                    "bubbles": True,
                    "key": key,
                },
            )
        )
        self.relayout()
        return element


class _Api:
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

    def push_frame(self, *, relayout: bool = True, reuse_styles: bool = False) -> None:
        if self._window is None:
            return
        png = self._interaction.render(relayout=relayout, reuse_styles=reuse_styles)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        self._window.evaluate_js(f"document.getElementById('frame').src = {data_uri!r};")


class _View:
    """Tiny adapter for the existing GLRenderer."""

    def __init__(self, interaction):
        self.interaction = interaction
        self.width = interaction.width
        self.height = interaction.height

    def resize(self, width, height):
        self.width = self.interaction.width = float(width)
        self.height = self.interaction.height = float(height)

        tree.layout(
            self.interaction.root,
            width=self.width,
            height=self.height,
        )

    def draw(self, canvas):
        canvas.clear(0xFFFFFFFF)
        paint.paint_tree(canvas, self.interaction.root)

    def record_frame_time(self, elapsed_ms):
        # `GLRenderer.draw()` (native_browser.py, shared with this
        # simpler run loop) always reports its own paint time back onto
        # whatever `view` it was given -- `native_browser.View` has real
        # perf-metrics fields (`last_frame_ms`/`avg_frame_ms`) for this;
        # this "tiny adapter" deliberately has none, so there is nothing
        # useful to record it into, but the call still has to land
        # somewhere or every frame raises `AttributeError`.
        pass


def run(
    root_element,
    *,
    width: int,
    height: int = 600,
    title: str = "chromonic",
    on_tick=None,
    fps: float = 30.0,
) -> None:
    import glfw

    if not glfw.init():
        raise RuntimeError("GLFW initialization failed")

    win = None
    renderer = None

    try:
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)

        win = glfw.create_window(width, height, title, None, None)
        if not win:
            raise RuntimeError("Could not create GPU window")

        glfw.make_context_current(win)
        glfw.swap_interval(1)

        interaction = Interaction(
            root_element,
            width=float(width),
            height=float(height),
            on_tick=on_tick,
        )

        view = _View(interaction)
        renderer = GLRenderer()

        tree.layout(root_element, width=width, height=height)

        def mouse_button(_win, button, action, mods):
            if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS:
                cursor = glfw.get_cursor_pos(win)
                interaction.handle_click(*cursor)

        def char_callback(_win, codepoint):
            interaction.handle_text(chr(codepoint))

        def key_callback(_win, key, scancode, action, mods):
            if action not in (glfw.PRESS, glfw.REPEAT):
                return
            names = {
                glfw.KEY_ENTER: "Enter",
                glfw.KEY_BACKSPACE: "Backspace",
                glfw.KEY_ESCAPE: "Escape",
                glfw.KEY_TAB: "Tab",
            }
            name = names.get(key)
            if name is not None:
                interaction.handle_key(name)

        def on_window_size(_win, w, h):
            view.resize(w, h)

        glfw.set_mouse_button_callback(win, mouse_button)
        glfw.set_char_callback(win, char_callback)
        glfw.set_key_callback(win, key_callback)
        glfw.set_window_size_callback(win, on_window_size)

        last_tick = time.perf_counter()
        interval = 1.0 / fps if fps > 0 else 0.0
        accumulator = 0.0

        while not glfw.window_should_close(win):
            glfw.poll_events()

            if on_tick is not None:
                interaction.tick()

            renderer.draw(
                view,
                glfw.get_framebuffer_size(win),
            )

            glfw.swap_buffers(win)

    finally:
        if renderer is not None:
            renderer.close()

        if win is not None:
            glfw.destroy_window(win)

        glfw.terminate()
