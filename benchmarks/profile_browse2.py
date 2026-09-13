"""Profile the actual direct-GPU browse2 architecture.

Creates a hidden GLFW window, loads either a deterministic tall page or a URL,
and records initial layout, full DOM paint, viewport display-list paint, and
GPU submission.  GPU samples call glFinish so queued driver work is included.
"""
from __future__ import annotations

import argparse
import cProfile
import json
from pathlib import Path
import pstats
import statistics
import time

import glfw
from OpenGL import GL

from myjs import Page
from chromonic import paint, tree
from chromonic.native_browser import GLRenderer, View


def fixture(nodes: int, rules: int) -> str:
    css = "\n".join(
        f".item-{i} {{color: rgb({i % 255},30,40); padding: 2px}}"
        for i in range(rules)
    )
    rows = "".join(
        f'<p class="item-{i % max(1, rules)}">Row {i}: direct rendering profile</p>'
        for i in range(nodes)
    )
    return (
        f"<html><head><style>body{{display:block;margin:0}}"
        f"p{{display:block;height:24px;margin:0}}{css}</style></head><body>{rows}</body></html>"
    )


def median_samples(repeat, function):
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000)
    return {"median_ms": statistics.median(samples), "samples_ms": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--nodes", type=int, default=1000)
    parser.add_argument("--rules", type=int, default=200)
    parser.add_argument("--repeat", type=int, default=9)
    parser.add_argument("--output", type=Path, default=Path("/tmp/chromonic-browse2"))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")

    if not glfw.init():
        raise RuntimeError("A working display is required")
    window = renderer = None
    try:
        glfw.window_hint(glfw.VISIBLE, False)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        window = glfw.create_window(1000, 800, "browse2 profile", None, None)
        if not window:
            raise RuntimeError("Could not create hidden GPU window")
        glfw.make_context_current(window)
        glfw.swap_interval(0)
        renderer = GLRenderer()

        source = args.url or fixture(args.nodes, args.rules)
        loader = None if args.url else lambda _url: Page(source, run=False)
        view = View(1000, 800, loader=loader)
        started = time.perf_counter()
        if not view.navigate(args.url or "https://fixture.invalid/"):
            raise RuntimeError(view.status)
        initial_ms = (time.perf_counter() - started) * 1000
        middle = max(0, (view.content_height - view.viewport_height) / 2)

        full_surface = __import__("skia").Surface(1000, 800)
        timings = {}
        timings["full_dom_paint"] = median_samples(
            args.repeat, lambda: paint.paint_tree(full_surface.getCanvas(), view.page.document.body)
        )
        def visible_paint():
            canvas = full_surface.getCanvas()
            canvas.save()
            canvas.translate(0, -middle)
            paint.paint_display_list(
                canvas, view.display_list, top=middle,
                bottom=middle + view.viewport_height,
            )
            canvas.restore()
        timings["visible_display_list_paint"] = median_samples(args.repeat, visible_paint)
        view.scroll_y = middle
        def gpu_frame():
            view.dirty = True
            renderer.draw(view, glfw.get_framebuffer_size(window))
            GL.glFinish()
        timings["gpu_frame"] = median_samples(args.repeat, gpu_frame)
        timings["rebuild_fresh_style_relayout"] = median_samples(
            args.repeat,
            lambda: tree.layout(view.page.document.body, width=view.width, height=None),
        )
        timings["retained_fresh_style_relayout"] = median_samples(args.repeat, view.relayout)
        timings["retained_cached_style_relayout"] = median_samples(
            args.repeat, lambda: view.relayout(reuse_styles=True)
        )

        profiler = cProfile.Profile()
        profiler.runcall(view.relayout)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(str(args.output) + ".prof")
        with Path(str(args.output) + ".txt").open("w") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(50)
        result = {
            "source": args.url or {"nodes": args.nodes, "rules": args.rules},
            "initial_navigation_and_layout_ms": initial_ms,
            "document_height": view.content_height,
            "display_list_elements": len(view.display_list),
            "visible_elements": view.last_painted_elements,
            "timings": timings,
            "limits": "Hidden direct-GPU window; glFinish included; excludes visible composition/vsync/input latency.",
        }
        args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        if renderer is not None:
            renderer.close()
        if window is not None:
            glfw.destroy_window(window)
        glfw.terminate()


if __name__ == "__main__":
    main()
