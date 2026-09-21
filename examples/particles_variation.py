"""Chromonic Node Connection Topology.

Demonstrates correct Domonic element instantiation, low-overhead Taffy layout
patching, and combined Skia vector + Chromonic DOM painting.

Controls:
  - Left-Click + Drag: Move attractor force origin
  - Space: Toggle pause state
  - +/-: Add or remove nodes

Run:
  .venv/bin/python chromonic/examples/node_connections.py [count]
"""
from __future__ import annotations

import argparse
import math
import random
import time

import glfw
import skia
from chromonic import paint, tree
from chromonic.native_browser import GLRenderer

# Correct domonic import: tags are functions/classes imported from domonic.html
from domonic.html import div

WIDTH = 900
HEIGHT = 650
BAR_HEIGHT = 44
MAX_NODES = 500
LINK_DISTANCE = 120.0


class Node:
    """Tracks position and momentum for an individual layout element."""

    def __init__(self, element, x: float, y: float):
        self.element = element
        self.x = x
        self.y = y
        self.vx = random.uniform(-1.5, 1.5)
        self.vy = random.uniform(-1.5, 1.5)


class TopologyView:
    def __init__(self, count: int = 100, width: int = WIDTH, height: int = HEIGHT):
        self.width = width
        self.height = height
        self.paused = False
        self.fps = 0.0
        self.dirty = True
        self.pending_count = None
        
        # Force attractor target (initialized to center)
        self.attractor_x = width / 2.0
        self.attractor_y = (height - BAR_HEIGHT) / 2.0
        self.dragging = False

        # Build initial DOM tree
        self.root = div(
            _id="topology-root",
            style=f"position: relative; width: {width}px; height: {height - BAR_HEIGHT}px;",
        )

        self.nodes: list[Node] = []
        self._rebuild_nodes(count)

        # Setup Taffy projection engine & initial display list
        self.layout_projection = tree.LayoutProjection()
        self.layout_projection.layout(
            self.root, width=self.width, height=self.height - BAR_HEIGHT
        )
        self.display_list = paint.build_display_list(self.root)

    def _rebuild_nodes(self, count: int):
        target_count = max(10, min(MAX_NODES, count))
        self.root.content = []  # Clear domonic children
        self.nodes.clear()

        sim_w = self.width
        sim_h = self.height - BAR_HEIGHT

        for _ in range(target_count):
            x = random.uniform(10, sim_w - 10)
            y = random.uniform(10, sim_h - 10)

            # Instantiating div directly using domonic's kwarg attribute syntax
            elem = div(
                _class="topology-node",
                style=(
                    f"position: absolute; "
                    f"top: {y:.1f}px; left: {x:.1f}px; "
                    f"width: 10px; height: 10px; "
                    f"background-color: #63b3ed; "
                    f"border-radius: 5px;"
                ),
            )
            self.root.append(elem)
            self.nodes.append(Node(elem, x, y))

    def set_count(self, count: int):
        self.pending_count = max(10, min(MAX_NODES, int(count)))
        self.dirty = True

    def resize(self, width: int, height: int):
        if width > 0 and height > BAR_HEIGHT and (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.layout_projection.layout(
                self.root, width=self.width, height=self.height - BAR_HEIGHT
            )
            self.display_list = paint.build_display_list(self.root)
            self.dirty = True

    def tick(self):
        # Handle node count changes safely before stepping layout
        if self.pending_count is not None:
            self._rebuild_nodes(self.pending_count)
            self.pending_count = None
            self.layout_projection.layout(
                self.root, width=self.width, height=self.height - BAR_HEIGHT
            )
            self.display_list = paint.build_display_list(self.root)

        elif not self.paused:
            sim_w = self.width
            sim_h = self.height - BAR_HEIGHT
            inset_updates = []

            for node in self.nodes:
                # Apply attractor force if mouse dragging
                if self.dragging:
                    dx = self.attractor_x - node.x
                    dy = self.attractor_y - node.y
                    dist = math.hypot(dx, dy) + 1e-4
                    if dist < 250:
                        force = (250 - dist) / 250 * 0.4
                        node.vx += (dx / dist) * force
                        node.vy += (dy / dist) * force

                # Movement step with velocity damping
                node.x += node.vx
                node.y += node.vy
                node.vx *= 0.99
                node.vy *= 0.99

                # Boundary collision bouncing
                if node.x <= 0 or node.x >= sim_w - 10:
                    node.vx *= -1.0
                    node.x = max(0, min(sim_w - 10, node.x))
                if node.y <= 0 or node.y >= sim_h - 10:
                    node.vy *= -1.0
                    node.y = max(0, min(sim_h - 10, node.y))

                # Patch Taffy layout insets: (element, top, right, bottom, left)
                inset_updates.append((
                    node.element,
                    round(node.y, 1),
                    0.0,
                    0.0,
                    round(node.x, 1),
                ))

            self.layout_projection.patch_insets(inset_updates)
            self.layout_projection.compute(
                self.root, width=self.width, height=self.height - BAR_HEIGHT
            )

        self.dirty = True

    def draw(self, canvas: skia.Canvas):
        canvas.clear(skia.Color(0x0f, 0x17, 0x2a, 0xff))

        # 1. Main Viewport Rendering
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(0, BAR_HEIGHT, self.width, self.height - BAR_HEIGHT))
        canvas.translate(0, BAR_HEIGHT)

        # Draw topology connection lines between close nodes directly in Skia
        num_nodes = len(self.nodes)
        for i in range(num_nodes):
            n1 = self.nodes[i]
            for j in range(i + 1, num_nodes):
                n2 = self.nodes[j]
                dx = n2.x - n1.x
                dy = n2.y - n1.y
                dist = math.hypot(dx, dy)

                if dist < LINK_DISTANCE:
                    alpha = int((1.0 - (dist / LINK_DISTANCE)) * 180)
                    line_paint = skia.Paint(
                        Color=skia.ColorSetARGB(alpha, 99, 179, 237),
                        StrokeWidth=1.0,
                        AntiAlias=True,
                    )
                    # Offsets (+5) center the lines on the 10x10 DOM elements
                    canvas.drawLine(n1.x + 5, n1.y + 5, n2.x + 5, n2.y + 5, line_paint)

        # Paint Chromonic Taffy Display List (Nodes) over the connection lines
        paint.paint_display_list(
            canvas, self.display_list, top=0.0, bottom=self.height - BAR_HEIGHT
        )
        canvas.restore()

        # 2. Status Header Bar
        canvas.drawRect(skia.Rect.MakeWH(self.width, BAR_HEIGHT), skia.Paint(Color=0xff1e293b))

        font = paint._font(14)
        ink = skia.Paint(Color=0xffe2e8f0, AntiAlias=True)
        accent = skia.Paint(Color=0xff38bdf8, AntiAlias=True)

        canvas.drawString("Chromonic Topology", 12, 28, paint._font(15), accent)
        canvas.drawString(f"Nodes: {len(self.nodes)}", 190, 28, font, ink)

        status_str = "Paused" if self.paused else f"{self.fps:.1f} fps"
        canvas.drawString(f"Status: {status_str}", 300, 28, font, ink)
        canvas.drawString("Controls: [Drag] Attract | [Space] Pause | [+/-] Node Count", self.width - 430, 28, font, ink)

        self.dirty = False


def run(count: int = 100, frames: int | None = None):
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

        win = glfw.create_window(WIDTH, HEIGHT, "Chromonic — Node Topology", None, None)
        if not win:
            raise RuntimeError("Could not create GPU window")

        glfw.set_window_size_limits(win, 500, 300, glfw.DONT_CARE, glfw.DONT_CARE)
        glfw.make_context_current(win)
        glfw.swap_interval(1)

        renderer = GLRenderer()
        view = TopologyView(count, *glfw.get_window_size(win))

        def on_mouse_button(w, button, action, mods):
            if button == glfw.MOUSE_BUTTON_LEFT:
                x, y = glfw.get_cursor_pos(win)
                view.dragging = action == glfw.PRESS and y >= BAR_HEIGHT
                if view.dragging:
                    view.attractor_x = x
                    view.attractor_y = y - BAR_HEIGHT

        def on_cursor_pos(w, x, y):
            if view.dragging and y >= BAR_HEIGHT:
                view.attractor_x = x
                view.attractor_y = y - BAR_HEIGHT

        def on_key(w, key, code, action, mods):
            if action in (glfw.PRESS, glfw.REPEAT):
                if key == glfw.KEY_SPACE and action == glfw.PRESS:
                    view.paused = not view.paused
                    view.dirty = True
                elif key == glfw.KEY_EQUAL:
                    view.set_count(len(view.nodes) + 20)
                elif key == glfw.KEY_MINUS:
                    view.set_count(len(view.nodes) - 20)

        glfw.set_key_callback(win, on_key)
        glfw.set_mouse_button_callback(win, on_mouse_button)
        glfw.set_cursor_pos_callback(win, on_cursor_pos)
        glfw.set_window_size_callback(win, lambda w, x, y: view.resize(x, y))
        glfw.set_framebuffer_size_callback(win, lambda *a: setattr(view, "dirty", True))

        last_time = time.perf_counter()
        elapsed = 0.0
        samples = 0
        drawn_frames = 0

        while not glfw.window_should_close(win):
            glfw.poll_events()

            if not view.paused or view.dirty or frames is not None:
                view.tick()
                renderer.draw(view, glfw.get_framebuffer_size(win))
                glfw.swap_buffers(win)

                now = time.perf_counter()
                if not view.paused:
                    elapsed += now - last_time
                    samples += 1
                    if elapsed >= 0.5:
                        view.fps = samples / elapsed
                        elapsed = 0.0
                        samples = 0
                last_time = now

                drawn_frames += 1
                if frames is not None and drawn_frames >= frames:
                    break
            else:
                glfw.wait_events_timeout(0.05)
                last_time = time.perf_counter()

    finally:
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, nargs="?", default=100, help="Number of nodes")
    parser.add_argument("--frames", type=int, help="Optional bound on total frames to render")
    args = parser.parse_args()

    run(args.count, frames=args.frames)
