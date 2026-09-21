"""Showcase the native GLFW window features exposed through domonic.window.Window.

Usage:

    python windowsdemo.py
    python windowsdemo.py transparent
    python windowsdemo.py overlay

The public window object is domonic.window.Window.

Browser-standard operations go through that object:

    window.resizeTo(...)
    window.moveTo(...)
    window.focus()
    window.close()

Chromonic/GLFW-only extensions live under:

    window.native
"""

from __future__ import annotations

import sys
import time

from domonic.html import button, div, h1, h2, p, span

from chromonic.window import WindowOptions, run


BUTTON_STYLE = """
display:inline-block;
padding:10px 14px;
margin:5px;
background:#eeeeee;
border:1px solid #999999;
border-radius:7px;
font-size:14px;
"""

DANGER_BUTTON_STYLE = """
display:inline-block;
padding:10px 14px;
margin:5px;
background:#ffdddd;
border:1px solid #aa5555;
border-radius:7px;
font-size:14px;
"""

PANEL_STYLE = """
padding:22px;
margin:20px;
background:#ffffff;
border:1px solid #bbbbbb;
border-radius:18px;
"""

SECTION_STYLE = """
margin-top:18px;
padding-top:12px;
border-top:1px solid #dddddd;
"""


def clickable(label, callback, *, danger=False):
    el = button(
        label,
        _style=DANGER_BUTTON_STYLE if danger else BUTTON_STYLE,
    )

    el.addEventListener(
        "click",
        lambda event: callback(),
    )

    return el


def showcase():
    state = {
        "window": None,
        "floating": False,
        "decorated": True,
        "opacity": 1.0,
        "big": False,
        "hidden_until": None,
        "passthrough_until": None,
    }

    status = p(
        "Waiting for native window...",
        _style="font-family:monospace;padding:8px;background:#f5f5f5;",
    )

    scale_status = span(
        "unknown",
        _style="font-family:monospace;",
    )

    dimensions_status = span(
        "unknown",
        _style="font-family:monospace;",
    )

    position_status = span(
        "unknown",
        _style="font-family:monospace;",
    )

    def window():
        return state["window"]

    def native():
        win = window()
        return None if win is None else win.native

    def message(text):
        status.textContent = text

    def update_window_status():
        win = window()
        if win is None:
            return

        dimensions_status.textContent = (
            f"inner={win.innerWidth}×{win.innerHeight}, "
            f"outer={win.outerWidth}×{win.outerHeight}"
        )

        position_status.textContent = (
            f"{win.screenX}, {win.screenY}"
        )

        scale_status.textContent = (
            f"{getattr(win, 'devicePixelRatio', 1.0):.2f}"
        )

    # ------------------------------------------------------------------
    # Browser-standard Window API
    # ------------------------------------------------------------------

    def move_left():
        win = window()
        win.moveTo(
            win.screenX - 80,
            win.screenY,
        )
        message("window.moveTo(...)")
        update_window_status()

    def move_right():
        win = window()
        win.moveTo(
            win.screenX + 80,
            win.screenY,
        )
        message("window.moveTo(...)")
        update_window_status()

    def move_down():
        win = window()
        win.moveTo(
            win.screenX,
            win.screenY + 80,
        )
        message("window.moveTo(...)")
        update_window_status()

    def resize():
        win = window()
        state["big"] = not state["big"]

        if state["big"]:
            size = (1000, 720)
        else:
            size = (720, 600)

        win.resizeTo(*size)

        message(
            f"window.resizeTo({size[0]}, {size[1]})"
        )

        update_window_status()

    def focus():
        window().focus()
        message("window.focus()")

    def close():
        window().close()

    def animation_frame():
        win = window()

        win.requestAnimationFrame(
            lambda timestamp: message(
                f"requestAnimationFrame fired at {timestamp:.2f}ms"
            )
        )

        message("requestAnimationFrame scheduled")

    # ------------------------------------------------------------------
    # Native/GLFW extensions
    # ------------------------------------------------------------------

    def toggle_opacity():
        state["opacity"] = (
            0.55
            if state["opacity"] == 1.0
            else 1.0
        )

        native().set_opacity(
            state["opacity"]
        )

        message(
            f"window.native opacity: {state['opacity']}"
        )

    def toggle_floating():
        state["floating"] = not state["floating"]

        native().set_floating(
            state["floating"]
        )

        message(
            f"window.native floating: {state['floating']}"
        )

    def toggle_decorated():
        state["decorated"] = not state["decorated"]

        native().set_decorated(
            state["decorated"]
        )

        message(
            f"window.native decorated: {state['decorated']}"
        )

    def attention():
        native().request_attention()
        message("window.native.request_attention()")

    def cursor(name):
        native().set_cursor(name)
        message(f"Native cursor: {name}")

    def hide_temporarily():
        state["hidden_until"] = (
            time.monotonic() + 1.5
        )

        message(
            "Native window hidden for 1.5 seconds..."
        )

        native().hide()

    def passthrough_temporarily():
        state["passthrough_until"] = (
            time.monotonic() + 2.0
        )

        native().set_mouse_passthrough(True)

        message(
            "Mouse passthrough ON for 2 seconds."
        )

    # ------------------------------------------------------------------
    # DOM
    # ------------------------------------------------------------------

    root = div(
        div(
            h1("Chromonic Window Demo"),

            p(
                "This page is Domonic DOM + Chromonic layout/paint. "
                "Browser-standard operations use domonic.window.Window; "
                "GLFW-only behaviour lives under window.native."
            ),

            status,

            div(
                h2("Domonic Window API"),

                p(
                    "Dimensions: ",
                    dimensions_status,
                ),

                p(
                    "Position: ",
                    position_status,
                ),

                p(
                    "devicePixelRatio: ",
                    scale_status,
                ),

                clickable(
                    "← move",
                    move_left,
                ),

                clickable(
                    "move →",
                    move_right,
                ),

                clickable(
                    "move ↓",
                    move_down,
                ),

                clickable(
                    "Toggle size",
                    resize,
                ),

                clickable(
                    "Focus",
                    focus,
                ),

                clickable(
                    "requestAnimationFrame",
                    animation_frame,
                ),

                _style=SECTION_STYLE,
            ),

            div(
                h2("Native GLFW Extensions"),

                clickable(
                    "55% opacity",
                    toggle_opacity,
                ),

                clickable(
                    "Always on top",
                    toggle_floating,
                ),

                clickable(
                    "Toggle decoration",
                    toggle_decorated,
                ),

                clickable(
                    "Request attention",
                    attention,
                ),

                clickable(
                    "Arrow cursor",
                    lambda: cursor("arrow"),
                ),

                clickable(
                    "Pointer cursor",
                    lambda: cursor("pointer"),
                ),

                clickable(
                    "Text cursor",
                    lambda: cursor("text"),
                ),

                clickable(
                    "Crosshair cursor",
                    lambda: cursor("crosshair"),
                ),

                clickable(
                    "Hide for 1.5 sec",
                    hide_temporarily,
                ),

                clickable(
                    "Click-through for 2 sec",
                    passthrough_temporarily,
                ),

                _style=SECTION_STYLE,
            ),

            div(
                h2("Native events"),

                p(
                    "Drop a file from Finder onto this window."
                ),

                clickable(
                    "Close window",
                    close,
                    danger=True,
                ),

                _style=SECTION_STYLE,
            ),

            _style=PANEL_STYLE,
        ),

        _style="""
        width:100%;
        height:100%;
        background:#e8edf3;
        """,
    )

    # ------------------------------------------------------------------
    # Native lifecycle callbacks
    # ------------------------------------------------------------------

    def ready(win):
        # This is now domonic.window.Window.
        state["window"] = win

        update_window_status()

        message(
            "Ready: domonic.window.Window is attached to its GLFW host."
        )

    def dropped(paths):
        message(
            "Dropped: " + ", ".join(paths)
        )

    def scale_changed(x, y):
        win = window()

        if win is not None:
            scale_status.textContent = (
                f"{getattr(win, 'devicePixelRatio', x):.2f}"
            )

        message(
            f"Content scale changed: {x:.2f} × {y:.2f}"
        )

    def tick():
        win = window()

        if win is None:
            return

        now = time.monotonic()

        hidden_until = state["hidden_until"]

        if (
            hidden_until is not None
            and now >= hidden_until
        ):
            state["hidden_until"] = None

            win.native.show()
            win.focus()

            message(
                "Window shown again."
            )

        passthrough_until = state[
            "passthrough_until"
        ]

        if (
            passthrough_until is not None
            and now >= passthrough_until
        ):
            state["passthrough_until"] = None

            win.native.set_mouse_passthrough(False)

            message(
                "Mouse passthrough OFF."
            )

        update_window_status()

    run(
        root,
        width=720,
        height=600,
        title="Chromonic GLFW showcase",
        fps=30,
        on_tick=tick,
        on_window_ready=ready,
        on_drop=dropped,
        on_content_scale=scale_changed,
        window=WindowOptions(
            decorated=True,
            resizable=True,
            floating=False,
            position=(120, 120),
            min_size=(500, 400),
            max_size=(1200, 900),
        ),
    )


def transparent_demo():
    state = {
        "window": None,
    }

    status = p(
        "Waiting for native host...",
        _style="""
        font-family:monospace;
        padding:8px;
        """,
    )

    def close():
        if state["window"] is not None:
            state["window"].close()

    root = div(
        div(
            h1(
                "Transparent Chromonic"
            ),

            p(
                "This is an undecorated GLFW window with a "
                "transparent framebuffer."
            ),

            p(
                "The public object is still domonic.window.Window. "
                "Transparency is a native creation-time option."
            ),

            status,

            clickable(
                "Close",
                close,
                danger=True,
            ),

            _style="""
            margin:40px;
            padding:28px;

            background:#ffffff;

            border:3px solid #333333;
            border-radius:36px;
            """,
        ),

        _style="""
        width:100%;
        height:100%;
        background:transparent;
        """,
    )

    def ready(win):
        state["window"] = win

        status.textContent = (
            "window.native attached; transparent framebuffer active."
        )

    run(
        root,
        width=620,
        height=420,
        title="Chromonic transparent demo",
        on_window_ready=ready,
        window=WindowOptions(
            decorated=False,
            transparent=True,
            resizable=True,
            floating=True,
            position=(180, 160),
        ),
    )


def overlay_demo():
    state = {
        "window": None,
    }

    started = time.monotonic()

    clock = h1(
        "Chromonic HUD",
        _style="""
        margin:0;
        font-size:32px;
        """,
    )

    detail = p(
        "",
        _style="""
        font-family:monospace;
        margin-top:8px;
        """,
    )

    root = div(
        div(
            clock,

            p(
                "Floating + transparent + mouse passthrough"
            ),

            detail,

            _style="""
            padding:20px;
            margin:20px;

            background:#ffffff;

            border:2px solid #333333;
            border-radius:18px;
            """,
        ),

        _style="""
        width:100%;
        height:100%;
        background:transparent;
        """,
    )

    def ready(win):
        state["window"] = win

    def tick():
        elapsed = (
            time.monotonic() - started
        )

        detail.textContent = (
            f"alive for {elapsed:0.1f}s"
        )

    run(
        root,
        width=460,
        height=170,
        title="Chromonic HUD",
        fps=30,
        on_tick=tick,
        on_window_ready=ready,
        window=WindowOptions(
            decorated=False,
            transparent=True,
            floating=True,
            mouse_passthrough=True,
            resizable=False,
            focus_on_show=False,
            focused=False,
            position=(80, 80),
        ),
    )


def main():
    mode = (
        sys.argv[1].lower()
        if len(sys.argv) > 1
        else "showcase"
    )

    if mode in {
        "showcase",
        "normal",
        "demo",
    }:
        showcase()

    elif mode in {
        "transparent",
        "shape",
        "shaped",
    }:
        transparent_demo()

    elif mode in {
        "overlay",
        "hud",
    }:
        overlay_demo()

    else:
        print("Usage:")
        print("  python windowsdemo.py")
        print("  python windowsdemo.py transparent")
        print("  python windowsdemo.py overlay")


if __name__ == "__main__":
    main()
