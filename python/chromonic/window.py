"""Native Chromonic window and GLFW host for a Domonic browsing context.

Domonic owns the public browser ``Window`` object and DOM events.
GLFW owns the OS window/input.
GLRenderer owns the Skia/OpenGL surface.
Chromonic owns hit-testing/layout/paint and adapts GLFW to Domonic through
``GLFWWindowHost``.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Callable

from . import hittest, paint, tree
from .native_browser import GLRenderer


@dataclass(slots=True)
class WindowOptions:
    """Creation-time configuration for a GLFW window."""

    decorated: bool = True
    resizable: bool = True
    floating: bool = False
    visible: bool = True
    focused: bool = True
    focus_on_show: bool = True
    maximized: bool = False
    transparent: bool = False
    mouse_passthrough: bool = False
    opacity: float = 1.0
    position: tuple[int, int] | None = None
    min_size: tuple[int, int] | None = None
    max_size: tuple[int, int] | None = None
    aspect_ratio: tuple[int, int] | None = None
    scale_to_monitor: bool = False
    scale_framebuffer: bool = True
    clear_color: int | None = None


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
        self.relayout()

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
    """Legacy adapter retained for callers that still use the PNG/webview path."""

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

    def __init__(self, interaction, *, clear_color: int = 0xFFFFFFFF):
        self.interaction = interaction
        self.width = interaction.width
        self.height = interaction.height
        self.clear_color = clear_color
        self.content_scale = (1.0, 1.0)

    def resize(self, width, height):
        self.width = self.interaction.width = float(width)
        self.height = self.interaction.height = float(height)
        tree.layout(
            self.interaction.root,
            width=self.width,
            height=self.height,
        )

    def set_content_scale(self, xscale: float, yscale: float):
        self.content_scale = (float(xscale), float(yscale))

    @property
    def device_pixel_ratio(self) -> float:
        return self.content_scale[0]

    def draw(self, canvas):
        canvas.clear(self.clear_color)
        paint.paint_tree(canvas, self.interaction.root)

    def record_frame_time(self, elapsed_ms):
        pass


def _bool(glfw, value: bool):
    return glfw.TRUE if value else glfw.FALSE


def _window_hint_if_available(glfw, name: str, value) -> bool:
    hint = getattr(glfw, name, None)
    if hint is None:
        return False
    glfw.window_hint(hint, value)
    return True


class GLFWWindowHost:
    """GLFW backend attached to a :class:`domonic.window.Window`.

    The Domonic object stays the public browser-facing ``window``.  This host
    only performs native operations and reports native state changes back via
    the Window's ``_host_*`` hooks.

    ``viewport_insets`` are ``(left, top, right, bottom)`` logical pixels.
    They let the full Chromonic browser exclude its own toolbar from
    ``window.innerHeight`` while preserving the real native outer size.
    """

    def __init__(
        self,
        glfw_module,
        native_window,
        *,
        viewport_insets: tuple[int, int, int, int] = (0, 0, 0, 0),
    ):
        self.glfw = glfw_module
        self.handle = native_window
        self.viewport_insets = tuple(int(v) for v in viewport_insets)
        self.window = None
        self._cursors = {}
        self._next_animation_frame_id = 1
        self._animation_frames: dict[int, Callable[[float], object]] = {}
        self.on_resize = None
        self.on_framebuffer_resize = None
        self.on_refresh = None
        self.on_drop = None
        self.on_content_scale = None

    @property
    def native(self):
        return self.handle

    @property
    def has_animation_frames(self) -> bool:
        return bool(self._animation_frames)

    def viewport_size(self, outer_size: tuple[int, int] | None = None) -> tuple[int, int]:
        width, height = outer_size or self.glfw.get_window_size(self.handle)
        left, top, right, bottom = self.viewport_insets
        return max(0, width - left - right), max(0, height - top - bottom)

    def attach(self, window) -> None:
        previous = self.window
        if previous is not None and previous is not window:
            # A navigation replaces the browsing context. RAF callbacks belong
            # to the old document/window and must not leak into the new one.
            self._animation_frames.clear()
            if getattr(previous, "_host", None) is self:
                previous._host = None
        self.window = window
        self.sync_state()

    def detach(self, window=None) -> None:
        if window is not None and self.window is not window:
            return
        current = self.window
        self.window = None
        self._animation_frames.clear()
        if current is not None and getattr(current, "_host", None) is self:
            current._host = None

    def sync_state(self) -> None:
        self._notify_size(*self.glfw.get_window_size(self.handle))
        self._notify_position(*self.glfw.get_window_pos(self.handle))
        self._notify_content_scale()
        if self.window is not None:
            focused = bool(self.glfw.get_window_attrib(self.handle, self.glfw.FOCUSED))
            if focused:
                self.window._host_focused(dispatch=False)
            else:
                self.window._host_blurred(dispatch=False)

    def install_callbacks(self) -> None:
        g = self.glfw
        w = self.handle
        g.set_window_size_callback(w, self._on_window_size)
        g.set_framebuffer_size_callback(w, self._on_framebuffer_size)
        g.set_window_pos_callback(w, self._on_window_pos)
        g.set_window_focus_callback(w, self._on_window_focus)
        g.set_window_close_callback(w, self._on_window_close)
        g.set_window_refresh_callback(w, self._on_window_refresh)
        g.set_drop_callback(w, self._on_drop)
        callback = getattr(g, "set_window_content_scale_callback", None)
        if callback is not None:
            callback(w, self._on_content_scale)

    def _notify_size(self, outer_width: int, outer_height: int) -> None:
        inner_width, inner_height = self.viewport_size((outer_width, outer_height))
        if self.window is not None:
            self.window._host_resized(
                inner_width,
                inner_height,
                outer_width=outer_width,
                outer_height=outer_height,
            )
        if self.on_resize is not None:
            self.on_resize(inner_width, inner_height, outer_width, outer_height)

    def _notify_position(self, x: int, y: int) -> None:
        if self.window is not None:
            self.window._host_moved(x, y)

    def _notify_content_scale(self, xscale=None, yscale=None) -> None:
        getter = getattr(self.glfw, "get_window_content_scale", None)
        if xscale is None or yscale is None:
            if getter is None:
                xscale = yscale = 1.0
            else:
                xscale, yscale = getter(self.handle)
        if self.window is not None:
            self.window._host_scale_changed(float(xscale), float(yscale))
        if self.on_content_scale is not None:
            self.on_content_scale(float(xscale), float(yscale))

    def _on_window_size(self, _window, width, height):
        self._notify_size(width, height)

    def _on_framebuffer_size(self, _window, width, height):
        if self.on_framebuffer_resize is not None:
            self.on_framebuffer_resize(width, height)

    def _on_window_pos(self, _window, x, y):
        self._notify_position(x, y)

    def _on_window_focus(self, _window, focused):
        if self.window is None:
            return
        if focused:
            self.window._host_focused()
        else:
            self.window._host_blurred()

    def _on_window_close(self, _window):
        if self.window is not None:
            self.window._host_closed()

    def _on_window_refresh(self, _window):
        if self.on_refresh is not None:
            self.on_refresh()

    def _on_drop(self, _window, paths):
        if self.on_drop is not None:
            self.on_drop(list(paths))

    def _on_content_scale(self, _window, xscale, yscale):
        self._notify_content_scale(xscale, yscale)

    # ---- browser Window backend -------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        # Window.resizeTo() is defined in terms of the outer browser window.
        # The host subtracts any Chromonic-owned chrome only when reporting
        # innerWidth/innerHeight back to Domonic.
        outer_width = max(1, int(width))
        outer_height = max(1, int(height))
        self.glfw.set_window_size(self.handle, outer_width, outer_height)
        self._notify_size(*self.glfw.get_window_size(self.handle))

    def move_to(self, x: int, y: int) -> None:
        self.glfw.set_window_pos(self.handle, int(x), int(y))
        self._notify_position(*self.glfw.get_window_pos(self.handle))

    def focus(self) -> None:
        self.glfw.focus_window(self.handle)
        if self.window is not None:
            self.window._host_focused()

    def close(self) -> None:
        self.glfw.set_window_should_close(self.handle, True)
        if self.window is not None:
            self.window._host_closed()

    def request_animation_frame(self, callback: Callable[[float], object]) -> int:
        request_id = self._next_animation_frame_id
        self._next_animation_frame_id += 1
        self._animation_frames[request_id] = callback
        return request_id

    def cancel_animation_frame(self, request_id: int) -> None:
        self._animation_frames.pop(request_id, None)

    def flush_animation_frames(self, timestamp: float) -> int:
        if not self._animation_frames:
            return 0
        callbacks = list(self._animation_frames.items())
        self._animation_frames.clear()
        for _request_id, callback in callbacks:
            callback(timestamp)
        return len(callbacks)

    # ---- native extensions exposed as window.native -----------------------------

    def show(self) -> None:
        self.glfw.show_window(self.handle)

    def hide(self) -> None:
        self.glfw.hide_window(self.handle)

    def request_attention(self) -> None:
        fn = getattr(self.glfw, "request_window_attention", None)
        if fn is not None:
            fn(self.handle)

    def set_title(self, title: str) -> None:
        self.glfw.set_window_title(self.handle, str(title))

    def set_opacity(self, opacity: float) -> None:
        self.glfw.set_window_opacity(self.handle, max(0.0, min(1.0, float(opacity))))

    def set_floating(self, enabled: bool) -> None:
        self.glfw.set_window_attrib(self.handle, self.glfw.FLOATING, _bool(self.glfw, enabled))

    def set_decorated(self, enabled: bool) -> None:
        self.glfw.set_window_attrib(self.handle, self.glfw.DECORATED, _bool(self.glfw, enabled))

    def set_resizable(self, enabled: bool) -> None:
        self.glfw.set_window_attrib(self.handle, self.glfw.RESIZABLE, _bool(self.glfw, enabled))

    def set_mouse_passthrough(self, enabled: bool) -> None:
        attrib = getattr(self.glfw, "MOUSE_PASSTHROUGH", None)
        if attrib is None:
            raise RuntimeError("Mouse passthrough requires GLFW 3.4+")
        self.glfw.set_window_attrib(self.handle, attrib, _bool(self.glfw, enabled))

    def set_position(self, x: int, y: int) -> None:
        self.move_to(x, y)

    def get_position(self) -> tuple[int, int]:
        return self.glfw.get_window_pos(self.handle)

    def set_size(self, width: int, height: int) -> None:
        self.resize(width, height)

    def get_size(self) -> tuple[int, int]:
        return self.glfw.get_window_size(self.handle)

    def get_viewport_size(self) -> tuple[int, int]:
        return self.viewport_size()

    def get_outer_size(self) -> tuple[int, int]:
        return self.get_size()

    def get_framebuffer_size(self) -> tuple[int, int]:
        return self.glfw.get_framebuffer_size(self.handle)

    def get_content_scale(self) -> tuple[float, float]:
        getter = getattr(self.glfw, "get_window_content_scale", None)
        return getter(self.handle) if getter is not None else (1.0, 1.0)

    def set_cursor(self, name: str | None) -> None:
        if name is None:
            self.glfw.set_cursor(self.handle, None)
            return

        aliases = {
            "default": "ARROW_CURSOR",
            "arrow": "ARROW_CURSOR",
            "text": "IBEAM_CURSOR",
            "ibeam": "IBEAM_CURSOR",
            "crosshair": "CROSSHAIR_CURSOR",
            "pointer": "HAND_CURSOR",
            "hand": "HAND_CURSOR",
            "ew-resize": "HRESIZE_CURSOR",
            "hresize": "HRESIZE_CURSOR",
            "ns-resize": "VRESIZE_CURSOR",
            "vresize": "VRESIZE_CURSOR",
            "nwse-resize": "RESIZE_NWSE_CURSOR",
            "nesw-resize": "RESIZE_NESW_CURSOR",
            "move": "RESIZE_ALL_CURSOR",
            "not-allowed": "NOT_ALLOWED_CURSOR",
        }
        constant_name = aliases.get(name.lower(), name)
        shape = getattr(self.glfw, constant_name, None)
        if shape is None:
            raise ValueError(f"Cursor {name!r} is not supported by this GLFW version")
        cursor = self._cursors.get(shape)
        if cursor is None:
            cursor = self.glfw.create_standard_cursor(shape)
            self._cursors[shape] = cursor
        self.glfw.set_cursor(self.handle, cursor)

    def set_cursor_mode(self, mode: str) -> None:
        modes = {
            "normal": self.glfw.CURSOR_NORMAL,
            "hidden": self.glfw.CURSOR_HIDDEN,
            "disabled": self.glfw.CURSOR_DISABLED,
        }
        captured = getattr(self.glfw, "CURSOR_CAPTURED", None)
        if captured is not None:
            modes["captured"] = captured
        try:
            value = modes[mode.lower()]
        except KeyError:
            raise ValueError(f"Unsupported cursor mode: {mode!r}") from None
        self.glfw.set_input_mode(self.handle, self.glfw.CURSOR, value)

    def destroy(self) -> None:
        for cursor in self._cursors.values():
            if cursor is not None:
                self.glfw.destroy_cursor(cursor)
        self._cursors.clear()
        self._animation_frames.clear()
        self.detach()


# Compatibility for the very short-lived API introduced while this host layer
# was being designed.  New code should use ``GLFWWindowHost`` and access it via
# ``domonic_window.native``.
WindowControls = GLFWWindowHost


def _domonic_window_for(root_element, title: str):
    from domonic.dom import Document, DOMImplementation
    from domonic.window import Window

    document = root_element if isinstance(root_element, Document) else getattr(root_element, "ownerDocument", None)
    if not isinstance(document, Document):
        document = DOMImplementation().createHTMLDocument(title)
        if getattr(root_element, "tagName", "").lower() == "body":
            document.body.replaceWith(root_element)
        else:
            document.body.appendChild(root_element)
    dom_window = getattr(document, "defaultView", None)
    if dom_window is None:
        dom_window = Window(doc=document)
    return dom_window


def run(
    root_element,
    *,
    width: int,
    height: int = 600,
    title: str = "chromonic",
    on_tick=None,
    fps: float = 30.0,
    window: WindowOptions | None = None,
    on_window_ready: Callable[[object], None] | None = None,
    on_drop: Callable[[list[str]], None] | None = None,
    on_content_scale: Callable[[float, float], None] | None = None,
) -> None:
    import glfw

    options = window or WindowOptions()
    if not glfw.init():
        raise RuntimeError("GLFW initialization failed")

    win = None
    renderer = None
    host = None
    dom_window = None

    try:
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        glfw.window_hint(glfw.DECORATED, _bool(glfw, options.decorated))
        glfw.window_hint(glfw.RESIZABLE, _bool(glfw, options.resizable))
        glfw.window_hint(glfw.FLOATING, _bool(glfw, options.floating))
        glfw.window_hint(glfw.FOCUSED, _bool(glfw, options.focused))
        glfw.window_hint(glfw.FOCUS_ON_SHOW, _bool(glfw, options.focus_on_show))
        glfw.window_hint(glfw.MAXIMIZED, _bool(glfw, options.maximized))
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, _bool(glfw, options.transparent))

        initially_visible = options.visible and options.position is None
        glfw.window_hint(glfw.VISIBLE, _bool(glfw, initially_visible))
        _window_hint_if_available(glfw, "SCALE_TO_MONITOR", _bool(glfw, options.scale_to_monitor))
        _window_hint_if_available(glfw, "SCALE_FRAMEBUFFER", _bool(glfw, options.scale_framebuffer))

        if options.mouse_passthrough:
            if not _window_hint_if_available(glfw, "MOUSE_PASSTHROUGH", glfw.TRUE):
                raise RuntimeError("Mouse passthrough requires GLFW 3.4+")

        win = glfw.create_window(width, height, title, None, None)
        if not win:
            raise RuntimeError("Could not create GPU window")

        glfw.make_context_current(win)
        glfw.swap_interval(1)

        if options.position is not None:
            glfw.set_window_pos(win, int(options.position[0]), int(options.position[1]))

        if options.min_size is not None or options.max_size is not None:
            dont_care = glfw.DONT_CARE
            min_w = options.min_size[0] if options.min_size else dont_care
            min_h = options.min_size[1] if options.min_size else dont_care
            max_w = options.max_size[0] if options.max_size else dont_care
            max_h = options.max_size[1] if options.max_size else dont_care
            glfw.set_window_size_limits(win, min_w, min_h, max_w, max_h)

        if options.aspect_ratio is not None:
            glfw.set_window_aspect_ratio(win, int(options.aspect_ratio[0]), int(options.aspect_ratio[1]))

        if options.opacity != 1.0:
            glfw.set_window_opacity(win, max(0.0, min(1.0, float(options.opacity))))

        interaction = Interaction(
            root_element,
            width=float(width),
            height=float(height),
            on_tick=on_tick,
        )
        clear_color = options.clear_color
        if clear_color is None:
            clear_color = 0x00000000 if options.transparent else 0xFFFFFFFF
        view = _View(interaction, clear_color=clear_color)
        renderer = GLRenderer()
        tree.layout(root_element, width=width, height=height)

        host = GLFWWindowHost(glfw, win)
        host.on_resize = lambda inner_w, inner_h, _outer_w, _outer_h: view.resize(inner_w, inner_h)
        host.on_drop = on_drop

        def scale_changed(xscale, yscale):
            view.set_content_scale(xscale, yscale)
            if on_content_scale is not None:
                on_content_scale(xscale, yscale)

        host.on_content_scale = scale_changed
        host.install_callbacks()

        dom_window = _domonic_window_for(root_element, title)
        dom_window.attach_host(host)

        def mouse_button(_win, button, action, mods):
            if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS:
                interaction.handle_click(*glfw.get_cursor_pos(win))

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

        glfw.set_mouse_button_callback(win, mouse_button)
        glfw.set_char_callback(win, char_callback)
        glfw.set_key_callback(win, key_callback)

        if options.visible and not initially_visible:
            glfw.show_window(win)

        if on_window_ready is not None:
            on_window_ready(dom_window)

        interval = 1.0 / fps if fps > 0 else 0.0
        last_frame = time.perf_counter()

        while not glfw.window_should_close(win):
            glfw.poll_events()

            if host.flush_animation_frames(dom_window.performance.now() * 1000.0):
                interaction.relayout()

            if on_tick is not None:
                interaction.tick()

            if interval:
                elapsed = time.perf_counter() - last_frame
                if elapsed < interval:
                    time.sleep(interval - elapsed)
                last_frame = time.perf_counter()

            renderer.draw(view, glfw.get_framebuffer_size(win))
            glfw.swap_buffers(win)

    finally:
        if dom_window is not None and host is not None and not dom_window.closed:
            dom_window._host_closed()
        if host is not None:
            host.destroy()
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()
