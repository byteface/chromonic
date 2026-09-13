"""Tests for the chromonic POC (see ../PLAN.md).

Skipped cleanly wherever the compiled `chromonic._native` extension isn't
built (this package is deliberately outside the main repo's `pytest`/CI
path -- run these with `maturin develop` done first):

    maturin develop && .venv/bin/python -m pytest tests/
"""

import subprocess
import sys

import pytest

pytest.importorskip("chromonic._native", reason="chromonic's Rust extension isn't built -- run `maturin develop` in chromonic/")

import skia  # noqa: E402
from domonic.html import div, img, p  # noqa: E402

import chromonic  # noqa: E402
from chromonic import hittest, style_bridge, tree  # noqa: E402
from chromonic._native import Tree  # noqa: E402
from domonic.layout import layout_style  # noqa: E402


def test_native_tree_computes_a_flex_row():
    t = Tree()
    a = t.new_leaf({"width": 100.0, "height": 50.0})
    b = t.new_leaf({"width": 100.0, "height": 50.0})
    root = t.new_with_children(
        {"display": "flex", "flex_direction": "row", "gap": (0.0, 10.0), "width": 500.0, "height": 100.0},
        [a, b],
    )
    boxes = t.compute(root, 500.0, 100.0)

    assert boxes[root][:4] == (0.0, 0.0, 500.0, 100.0)
    assert boxes[a][:4] == (0.0, 0.0, 100.0, 50.0)
    assert boxes[b][:4] == (110.0, 0.0, 100.0, 50.0)  # 100 (a's width) + the 10px gap


def test_native_tree_rejects_a_bad_style_value():
    t = Tree()
    with pytest.raises(ValueError):
        t.new_leaf({"display": "not-a-real-display-value"})


def test_style_bridge_translates_lengths_and_keywords():
    element = div(_style="width:200px; height:50%; display:flex; flex-direction:row-reverse; gap:4px 8px;")
    style = style_bridge.to_dict(layout_style(element))

    assert style["width"] == 200.0
    assert style["height"] == ("pct", 0.5)
    assert style["display"] == "flex"
    assert style["flex_direction"] == "row-reverse"
    assert style["gap"] == (4.0, 8.0)


def test_style_bridge_expands_simple_grid_repeat_tracks():
    element = div(_style="display:grid;grid-template-columns:repeat(3, 1fr)")
    style = style_bridge.to_dict(layout_style(element))
    assert style["grid_template_columns"] == [("fr", 1.0)] * 3


def test_style_bridge_resolves_viewport_units_for_current_layout():
    element = div(_style="width:80vw;height:24vh;margin:5vh;padding:2vw")
    with style_bridge.viewport(800, 600):
        style = style_bridge.to_dict(layout_style(element))

    assert style["width"] == 640.0
    assert style["height"] == 144.0
    assert style["margin"] == [30.0] * 4
    assert style["padding"] == [16.0] * 4


def test_chromonic_uses_released_domonic_package():
    result = subprocess.run(
        [sys.executable, "-c",
         "import chromonic, domonic, pathlib; "
         "assert tuple(map(int, domonic.__version__.split('.')[:3])) >= (1, 8, 1); "
         "assert '_vendor/domonic' not in pathlib.Path(domonic.__file__).as_posix()"],
        check=True, capture_output=True, text=True,
    )
    assert result.returncode == 0


def test_content_box_size_includes_padding_and_border_in_layout_geometry():
    element = div(_style=(
        "display:block; box-sizing:content-box; width:100px; height:40px; "
        "padding:10px; border:2px solid black"
    ))
    tree.layout(element, width=300.0)

    box = element.get_layout_box()
    assert style_bridge.to_dict(layout_style(element))["box_sizing"] == "content-box"
    assert (box.width, box.height) == (124.0, 64.0)


def test_layout_writes_geometry_that_matches_getBoundingClientRect():
    root = div(
        div(_style="width:100px; height:40px; background-color:rgb(200,0,0);"),
        div(_style="width:100px; height:40px; background-color:rgb(0,0,200);"),
        _style="display:flex; flex-direction:row; gap:20px; width:400px;",
    )

    tree.layout(root, width=400.0)

    first, second = (c for c in root.childNodes if c.nodeType == 1)
    root_rect = root.getBoundingClientRect()
    assert (root_rect.x, root_rect.y, root_rect.width) == (0.0, 0.0, 400.0)

    first_rect, second_rect = first.getBoundingClientRect(), second.getBoundingClientRect()
    assert (first_rect.x, first_rect.width) == (0.0, 100.0)
    assert (second_rect.x, second_rect.width) == (120.0, 100.0)  # 100 + the 20px gap
    assert first.get_layout_box().x == first_rect.x  # the exact box painting reads from


def test_a_text_leaf_gets_a_real_intrinsic_size():
    root = div(p("hello", _style="font-size:20px;"), _style="width:300px;")
    tree.layout(root, width=300.0)

    para = root.childNodes[0]
    box = para.get_layout_box()
    assert box.height > 0  # domonic._fontmetrics.text_extent, not the SVG-only getBBox()
    assert box.width > 0


def test_mutating_style_and_relaying_out_changes_the_geometry():
    box_a = div(_style="width:100px; height:40px;")
    root = div(box_a, _style="display:flex; width:400px;")

    tree.layout(root, width=400.0)
    assert box_a.get_layout_box().width == 100.0

    box_a.style.width = "250px"
    tree.layout(root, width=400.0)
    assert box_a.get_layout_box().width == 250.0


def test_hit_test_resolves_to_the_innermost_element():
    inner = p("hi", _style="width:50px; height:20px;")
    outer = div(inner, _style="width:200px; height:100px; padding:10px;")
    tree.layout(outer, width=200.0)

    assert hittest.hit_test(outer, 15.0, 15.0) is inner
    assert hittest.hit_test(outer, 190.0, 90.0) is outer
    assert hittest.hit_test(outer, 500.0, 500.0) is None


def test_render_produces_a_png():
    root = div(p("chromonic", _style="color:rgb(0,0,0);"), _style="width:200px; background-color:rgb(255,255,255);")
    png = chromonic.render(root, width=200)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # the PNG magic bytes


# -- phase 2: interactivity (chromonic.window.Interaction) -----------------

def test_chromonic_window_does_not_import_webview():
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-c", "import chromonic.window, sys; assert 'webview' not in sys.modules"],
        check=True,
    )


def test_interaction_click_dispatches_a_real_bubbling_dom_event():
    from domonic.events import MouseEvent
    from chromonic.window import Interaction

    log = []
    inner_button = p("Click me", _style="width:100px; height:40px; background-color:rgb(0,0,255);")
    wrapper = div(inner_button, _style="width:300px; padding:10px;")
    wrapper.addEventListener("click", lambda event: log.append((event.type, isinstance(event, MouseEvent))))

    interaction = Interaction(wrapper, width=300.0)
    interaction.render()  # an initial layout, same as the window would do before showing anything

    button_box = inner_button.get_layout_box()
    hit = interaction.handle_click(button_box.x + 5, button_box.y + 5)

    assert hit is inner_button          # hit-tested to the innermost element
    assert log == [("click", True)]     # but the wrapper's listener still fired (bubbling)


def test_interaction_click_triggers_a_real_relayout():
    from chromonic.window import Interaction

    box_el = div(_style="width:100px; height:40px;")
    root = div(box_el, _style="width:300px;")

    def grow(event):
        box_el.style.width = "250px"

    root.addEventListener("click", grow)

    interaction = Interaction(root, width=300.0)
    interaction.render()
    assert box_el.get_layout_box().width == 100.0

    interaction.handle_click(10, 10)   # anywhere inside root
    assert box_el.get_layout_box().width == 250.0  # the mutation reached Taffy, for real


def test_interaction_click_outside_everything_is_a_no_op():
    from chromonic.window import Interaction

    root = div(_style="width:100px; height:40px;")
    interaction = Interaction(root, width=100.0)
    interaction.render()

    hit = interaction.handle_click(9999, 9999)
    assert hit is None  # nothing crashed, and nothing was hit


# -- phase 3: continuous animation (Interaction.tick) --------------------

def test_tick_runs_on_tick_then_relayouts():
    from chromonic.window import Interaction

    bar = div(_style="width:20px; height:10px;")
    stage = div(bar, _style="display:flex; align-items:flex-end; width:100px; height:100px;")

    heights = iter([40.0, 90.0, 15.0])

    def on_tick():
        bar.style.height = f"{next(heights)}px"

    interaction = Interaction(stage, width=100.0, height=100.0, on_tick=on_tick)
    interaction.render()  # on_tick has not run yet -- this is the initial frame

    interaction.tick()
    assert bar.get_layout_box().height == 40.0
    interaction.tick()
    assert bar.get_layout_box().height == 90.0
    interaction.tick()
    assert bar.get_layout_box().height == 15.0


def test_tick_with_no_on_tick_still_relayouts_without_error():
    from chromonic.window import Interaction

    root = div(_style="width:100px; height:40px;")
    interaction = Interaction(root, width=100.0)  # on_tick defaults to None
    interaction.render()

    interaction.tick()  # must not raise just because there's nothing to animate
    assert root.get_layout_box().width == 100.0


# -- non-rendering tags / display:none (needed before real pages render) --

def test_non_rendering_tags_and_display_none_are_skipped():
    from domonic.html import script, style

    root = div(
        script("var x = 1;"),
        style("body { color: red; }"),
        div("hidden", _style="display:none;"),
        div("shown"),
        _style="width:300px;",
    )
    node_map = tree.layout(root, width=300.0)

    # only the root and the one visible child ever became Taffy nodes --
    # <script>/<style> and the display:none div (and its text) never did.
    assert len(node_map) == 2
    tags = {getattr(el, "tagName", "").lower() for el in node_map.values()}
    assert tags == {"div"}
    shown = next(el for el in node_map.values() if el is not root)
    assert shown.textContent == "shown"


# -- the browser feature (chromonic.browser) --------------------------------

def _serve(files: dict):
    """A tiny local HTTP server for browser-feature tests -- same pattern
    tests/test_myjs.py already uses for Page.load's network calls."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body, ctype = files.get(self.path, (b"not found", "text/plain"))
            self.send_response(200 if self.path in files else 404)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_browser_interaction_loads_a_page_with_css_applied():
    from chromonic.browser import BrowserInteraction

    files = {
        "/index.html": (
            b"<!doctype html><html><head>"
            b"<link rel='stylesheet' href='style.css'>"
            b"</head><body><div id='box'>hello</div></body></html>",
            "text/html",
        ),
        "/style.css": (b"#box { color: rgb(1, 2, 3); }", "text/css"),
    }
    srv = _serve(files)
    port = srv.server_address[1]
    try:
        interaction = BrowserInteraction(f"http://127.0.0.1:{port}/index.html", width=300.0, height=200.0)
        interaction.render()
        assert interaction.url.endswith("/index.html")
        box = interaction.root.getElementsByTagName("div")[0]
        assert box.textContent == "hello"
        from domonic.style import ComputedStyleDeclaration
        assert ComputedStyleDeclaration(box).color == "rgb(1, 2, 3)"
    finally:
        srv.shutdown()


def test_browser_interaction_clicking_a_link_navigates_and_back_returns():
    from chromonic.browser import BrowserInteraction

    files = {
        "/index.html": (
            b"<!doctype html><html><body>"
            b"<a href='page2.html' style='display:block; width:100px; height:20px;'>go</a>"
            b"</body></html>",
            "text/html",
        ),
        "/page2.html": (
            b"<!doctype html><html><body><div id='p2'>second page</div></body></html>",
            "text/html",
        ),
    }
    srv = _serve(files)
    port = srv.server_address[1]
    try:
        interaction = BrowserInteraction(f"http://127.0.0.1:{port}/index.html", width=300.0, height=200.0)
        interaction.render()

        anchor = interaction.root.getElementsByTagName("a")[0]
        box = anchor.get_layout_box()
        hit = interaction.handle_click(box.x + 2, box.y + 2)

        assert hit is anchor
        assert interaction.url.endswith("/page2.html")
        assert "second page" in interaction.root.textContent

        assert interaction.go_back() is True
        assert interaction.url.endswith("/index.html")
        assert interaction.go_back() is False  # nothing further back
    finally:
        srv.shutdown()


def test_browser_interaction_fragment_links_do_not_navigate():
    from chromonic.browser import BrowserInteraction

    files = {
        "/index.html": (
            b"<!doctype html><html><body>"
            b"<a href='#section' style='display:block; width:100px; height:20px;'>jump</a>"
            b"</body></html>",
            "text/html",
        ),
    }
    srv = _serve(files)
    port = srv.server_address[1]
    try:
        interaction = BrowserInteraction(f"http://127.0.0.1:{port}/index.html", width=300.0, height=200.0)
        interaction.render()

        anchor = interaction.root.getElementsByTagName("a")[0]
        box = anchor.get_layout_box()
        interaction.handle_click(box.x + 2, box.y + 2)

        assert interaction.url.endswith("/index.html")  # unchanged -- no page fetch for a #fragment
    finally:
        srv.shutdown()


def test_browser_module_does_not_import_webview_or_myjs_until_used():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import chromonic, sys; assert 'webview' not in sys.modules; assert 'myjs' not in sys.modules",
        ],
        check=True,
    )


# -- phase 5: <script type="text/python"> (chromonic.pyscript) ---------------

_PYSCRIPT_PAGE = """<!doctype html>
<html>
<body>
<button id="hello" style="width:100px; height:30px;">Click me</button>
<script type="text/python">
count = 0
button = document.querySelector("#hello")


def clicked(event):
    global count
    count += 1
    button.textContent = "Clicked"
    button.style.backgroundColor = "rgb(0, 128, 0)"


button.addEventListener("click", clicked)
</script>
</body>
</html>"""


def test_parse_and_run_executes_an_inline_python_script():
    from chromonic import pyscript

    document, scope = pyscript.parse_and_run(_PYSCRIPT_PAGE)

    assert scope["count"] == 0
    assert scope["button"] is document.querySelector("#hello")
    assert callable(scope["clicked"])


def test_a_python_registered_listener_is_an_ordinary_dom_event_listener():
    # the whole point: no chromonic-specific glue between "a python script ran"
    # and "clicking the button fires it" -- Interaction.handle_click (which
    # knows nothing about pyscript) drives it through a real dispatchEvent.
    from chromonic import pyscript
    from chromonic.window import Interaction

    document, _scope = pyscript.parse_and_run(_PYSCRIPT_PAGE)
    button = document.querySelector("#hello")

    interaction = Interaction(document.body, width=200.0)
    interaction.render()
    box = button.get_layout_box()

    interaction.handle_click(box.x + 5, box.y + 5)

    from domonic.style import ComputedStyleDeclaration
    assert button.textContent == "Clicked"
    assert ComputedStyleDeclaration(button).backgroundColor == "rgb(0, 128, 0)"


def test_pyscript_window_injects_document_and_a_forwarding_window():
    from chromonic import pyscript

    document, scope = pyscript.parse_and_run(
        '<html><body><script type="text/python">seen_doc = document\nseen_window = window</script></body></html>'
    )
    assert scope["seen_doc"] is document
    assert scope["seen_window"].document is document
    # anything not explicitly modelled forwards to domonic's real `window`
    # singleton (location/alert/atob/... -- see pyscript.Window)
    assert hasattr(scope["seen_window"], "btoa")


def test_a_script_src_is_read_relative_to_base_dir():
    from pathlib import Path

    from chromonic import pyscript

    app_dir = Path(__file__).resolve().parents[1] / "examples"
    html = (app_dir / "pyscript_page.html").read_text(encoding="utf-8")

    document, scope = pyscript.parse_and_run(html, base_dir=app_dir)

    assert scope["button"] is document.querySelector("#hello")


def test_run_python_scripts_shares_one_namespace_across_multiple_scripts():
    from chromonic import pyscript

    html = (
        '<html><body>'
        '<script type="text/python">shared = 1</script>'
        '<script type="text/python">shared += 1</script>'
        "</body></html>"
    )
    _document, scope = pyscript.parse_and_run(html)
    assert scope["shared"] == 2


def test_non_python_script_types_are_left_alone():
    from chromonic import pyscript

    html = (
        '<html><body>'
        '<script type="text/javascript">var x = 1;</script>'
        '<script type="text/python">ran = True</script>'
        "</body></html>"
    )
    _document, scope = pyscript.parse_and_run(html)
    assert scope["ran"] is True
    assert "x" not in scope  # the JS-typed script was never touched


def test_remote_script_src_without_a_hostname_is_refused():
    from chromonic import pyscript

    with pytest.raises(ValueError):
        pyscript._validate_remote_src("http:///no-hostname")


def test_a_relative_script_src_with_no_local_file_raises_a_plain_file_error():
    # a src that isn't http(s) falls to the local-path branch, same as any
    # other missing file -- no silent success, but also no chromonic-specific
    # exception type; ordinary Python I/O errors are enough here.
    from pathlib import Path

    from chromonic import pyscript

    with pytest.raises(OSError):
        pyscript._read_src("does-not-exist.py", base_dir=Path("/nonexistent-chromonic-test-dir"))


# -- phase 6: absolute positioning (`inset`) + the particle perf demo -----

def test_style_bridge_translates_inset():
    element = div(_style="position:absolute; top:10px; right:5%; bottom:auto; left:20px;")
    style = style_bridge.to_dict(layout_style(element))

    assert style["position"] == "absolute"
    assert style["inset"] == [10.0, ("pct", 0.05), "auto", 20.0]


def test_absolutely_positioned_child_is_placed_by_its_inset():
    stage = div(
        div(_style="position:absolute; top:15px; left:25px; width:10px; height:10px;"),
        _style="position:relative; width:200px; height:200px;",
    )
    tree.layout(stage, width=200.0, height=200.0)

    particle = stage.childNodes[0]
    box = particle.get_layout_box()
    assert (box.x, box.y) == (25.0, 15.0)


def test_particles_example_moves_and_bounces_particles():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import particles

    interaction = particles.ParticleInteraction(width=200.0, height=200.0, count=20)
    interaction.render()
    before = [(p.x, p.y) for p in interaction.particles]

    interaction.tick()
    after = [(p.x, p.y) for p in interaction.particles]

    assert before != after  # every particle actually moved
    # the layout box tree.layout() wrote back agrees with the particle's own
    # x/y (within Taffy's pixel rounding) -- inset is really driving layout,
    # not just being stored.
    box = interaction.particles[0].element.get_layout_box()
    assert abs(box.x - interaction.particles[0].x) < 1.0
    assert abs(box.y - interaction.particles[0].y) < 1.0


def test_particles_example_bounces_off_the_stage_edges():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import particles

    element = div(_style="position:absolute;")
    particle = particles.Particle(element, "position:absolute;", x=0.0, y=0.0, vx=-3.0, vy=-3.0)
    particle.step(width=100.0, height=100.0)

    assert particle.x == 0.0 and particle.vx == 3.0  # clamped + reflected off the left edge
    assert particle.y == 0.0 and particle.vy == 3.0  # ...and the top edge


def test_particles_example_set_count_rebuilds_the_stage():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import particles

    interaction = particles.ParticleInteraction(width=200.0, height=200.0, count=20)
    interaction.render()
    old_root = interaction.root

    interaction.set_count(5)

    assert len(interaction.particles) == 5
    assert interaction.root is not old_root
    png = interaction.render()  # still paints fine after a live resize
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_particles_example_uses_native_particle_runner():
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import particles

    source = inspect.getsource(particles.run)
    assert "particles2" in source
    assert "webview" not in source


def test_animate_example_produces_visibly_different_frames_over_time():
    # examples/animate.py's own on_tick, driven headlessly -- the same proof
    # of "a real, continuous animation" that a live window would show, minus
    # the window.
    import importlib
    import sys
    import time
    from pathlib import Path

    from chromonic.window import Interaction

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    animate = importlib.import_module("animate")

    stage, bars = animate.build_page()
    start = time.perf_counter() - 10.0  # backdate so consecutive ticks differ noticeably

    def on_tick():
        import math

        t = time.perf_counter() - start
        for i, bar in enumerate(bars):
            wave = 0.5 + 0.5 * math.sin(t * 2.4 + i * 0.7)
            bar.style.height = f"{10 + wave * (animate.HEIGHT - 60):.1f}px"

    interaction = Interaction(stage, width=animate.WIDTH, height=animate.HEIGHT, on_tick=on_tick)
    frame_a = interaction.render()
    interaction.tick()
    frame_b = interaction.render()

    assert frame_a != frame_b
    assert all(bar.get_layout_box().height > 0 for bar in bars)


class _FrameSink:
    def __init__(self):
        self.frames = []

    def evaluate_js(self, script):
        self.frames.append(script)


def test_bridge_tick_and_click_layout_once_and_show_current_pixels(monkeypatch):
    from chromonic import tree
    from chromonic.window import Interaction, _Api

    child = div(_style='width:20px;height:20px;background-color:rgb(255,0,0)')
    root = div(child, _style='width:100px;height:50px')
    interaction = Interaction(root, width=100, height=50,
                              on_tick=lambda: child.setAttribute('style', 'width:40px;height:20px;background-color:rgb(0,0,255)'))
    sink = _FrameSink()
    api = _Api(interaction)
    api.attach(sink)
    api.push_frame()
    before = sink.frames[-1]
    original = tree.layout
    calls = []
    def layout(*args, **kw):
        calls.append(1)
        return original(*args, **kw)
    monkeypatch.setattr(tree, 'layout', layout)
    api.tick()
    assert len(calls) == 1
    assert child.get_layout_box().width == 40
    assert sink.frames[-1] != before
    optimized = sink.frames[-1]
    api.push_frame()  # independently fresh layout must paint identical pixels
    assert sink.frames[-1] == optimized
    calls.clear()
    api.on_click(5, 5)
    assert len(calls) == 1
    child.style.width = '60px'
    interaction.render()  # arbitrary caller mutations still force fresh geometry
    assert child.get_layout_box().width == 60


def test_browser_navigation_bridge_layouts_once_and_noop_back_does_not_render(monkeypatch):
    from myjs import Page
    from chromonic import browser, tree
    monkeypatch.setattr(browser, 'load', lambda url: Page('<html><body><p>Hello</p></body></html>', run=False))
    interaction = browser.BrowserInteraction('https://example.com/', width=100, height=50)
    api = browser._Api(interaction)
    sink = _FrameSink()
    api.attach(sink)
    api.push_frame()
    api.go_back()
    assert len(sink.frames) == 1
    original = tree.layout
    calls = []
    def layout(*args, **kw):
        calls.append(1)
        return original(*args, **kw)
    monkeypatch.setattr(tree, 'layout', layout)
    api.navigate('https://example.com/next')
    assert len(calls) == 1
    calls.clear()
    api.go_back()
    assert len(calls) == 1
    assert interaction.url == 'https://example.com/'


def test_layout_shares_ancestor_styles_only_within_one_pass(monkeypatch):
    from domonic.style import ComputedStyleDeclaration
    from chromonic import tree
    first, second = p('one'), p('two')
    root = div(first, second, _style='color:rgb(255,0,0);width:100px')
    original = ComputedStyleDeclaration._resolve
    resolutions = {}
    def resolve(self):
        key = id(self._element)
        resolutions[key] = resolutions.get(key, 0) + 1
        return original(self)
    monkeypatch.setattr(ComputedStyleDeclaration, '_resolve', resolve)
    tree.layout(root, width=100)
    assert all(count == 1 for count in resolutions.values())
    before = first._chromonic_computed_style.color
    resolutions.clear()
    root.style.color = 'rgb(0,0,255)'
    tree.layout(root, width=100)
    assert all(count == 1 for count in resolutions.values())
    assert first._chromonic_computed_style.color != before
    assert second._chromonic_computed_style.color == first._chromonic_computed_style.color


# -- phase 7: a UA stylesheet + <img> loading -----------------------------

def test_ua_style_applies_defaults_when_the_page_has_no_opinion():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    from chromonic import ua_style

    document = DOMParser().parseFromString(
        "<html><body><ul id='list'><li>hi</li></ul><p id='p'>x</p><h2 id='h2'>Sub</h2></body></html>",
        "text/html",
    )
    ua_style.apply(document)

    assert ComputedStyleDeclaration(document.getElementById("list")).paddingLeft == "40px"
    assert ComputedStyleDeclaration(document.getElementById("p")).marginTop == "16px"
    h2 = document.getElementById("h2")
    assert ComputedStyleDeclaration(h2).fontSize == "24px"
    assert ComputedStyleDeclaration(h2).fontWeight == "700"
    body = document.getElementsByTagName("body")[0]
    assert ComputedStyleDeclaration(body).marginTop == "8px"


def test_ua_style_never_beats_even_a_low_specificity_author_reset():
    # the whole point of applying this as a real @layer, not just inserting
    # it first in the document: a `*` reset has LOWER specificity than the
    # UA stylesheet's own `ul`/`p` rules, and must still win, exactly as it
    # would in a real browser (user-agent origin always loses to author,
    # regardless of specificity).
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    from chromonic import ua_style

    document = DOMParser().parseFromString(
        "<html><head><style>* { margin: 0; padding: 0; }</style></head>"
        "<body><ul id='list'><li>hi</li></ul><h1 id='h'>T</h1><p id='p'>x</p></body></html>",
        "text/html",
    )
    ua_style.apply(document)

    assert ComputedStyleDeclaration(document.getElementById("list")).paddingLeft == "0px"
    assert ComputedStyleDeclaration(document.getElementById("h")).marginTop == "0px"
    assert ComputedStyleDeclaration(document.getElementById("p")).marginTop == "0px"
    # properties the reset never touched are still defaulted
    assert ComputedStyleDeclaration(document.getElementById("h")).fontWeight == "700"


def test_ua_style_apply_is_idempotent():
    from domonic.dom import DOMParser

    from chromonic import ua_style

    document = DOMParser().parseFromString("<html><body></body></html>", "text/html")
    ua_style.apply(document)
    ua_style.apply(document)
    assert len(document.getElementsByTagName("style")) == 1


def _tiny_png(color=skia.ColorRED, size=4) -> bytes:
    surface = skia.Surface(size, size)
    surface.getCanvas().clear(color)
    return bytes(surface.makeImageSnapshot().encodeToData())


def test_resolve_image_sources_makes_relative_srcs_absolute_only():
    from domonic.dom import DOMParser

    from chromonic import browser_images

    document = DOMParser().parseFromString(
        "<html><body>"
        "<img id='rel' src='pic.png'>"
        "<img id='abs' src='https://other.example/x.png'>"
        "<img id='data' src='data:image/png;base64,AAAA'>"
        "<img id='none'>"
        "</body></html>",
        "text/html",
    )
    browser_images.resolve_image_sources(document, "https://example.com/dir/page.html")

    assert document.getElementById("rel").getAttribute("src") == "https://example.com/dir/pic.png"
    assert document.getElementById("abs").getAttribute("src") == "https://other.example/x.png"
    assert document.getElementById("data").getAttribute("src") == "data:image/png;base64,AAAA"
    assert document.getElementById("none").getAttribute("src") is None


def _await_image(browser_images, url, timeout=5.0):
    """`load_image()` is asynchronous (see `browser_images.py`'s module
    docstring) -- it returns `None` immediately and starts a background
    fetch. Tests that need the *result* poll `generation()` (or the cache
    directly) rather than assume the first call already has it."""
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if url in browser_images._cache:
            return browser_images._cache[url]
        browser_images.load_image(url)  # a no-op once the fetch is already in flight or cached
        time.sleep(0.02)
    raise TimeoutError(f"image never resolved within {timeout}s: {url}")


def test_load_image_fetches_decodes_and_caches():
    from chromonic import browser_images

    browser_images.clear_cache()
    png = _tiny_png(skia.ColorBLUE, size=6)
    hits = []

    class H(__import__("http").server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *a):
            pass

    import http.server
    import threading

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/pic.png"
        assert browser_images.load_image(url) is None  # not ready on the very first call
        image = _await_image(browser_images, url)
        assert image is not None
        assert (image.width(), image.height()) == (6, 6)

        image_again = browser_images.load_image(url)
        assert image_again is image  # cached -- no second fetch
        assert hits == ["/pic.png"]
    finally:
        srv.shutdown()


def test_load_image_caches_a_failed_fetch_as_none():
    from chromonic import browser_images

    browser_images.clear_cache()
    url = "http://127.0.0.1:1/does-not-exist.png"
    assert browser_images.load_image(url) is None
    assert _await_image(browser_images, url) is None
    assert url in browser_images.__dict__["_cache"]


def test_load_image_decodes_a_data_uri():
    import base64

    from chromonic import browser_images

    browser_images.clear_cache()
    png = _tiny_png(skia.ColorGREEN, size=3)
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    image = browser_images.load_image(uri)
    assert image is not None
    assert (image.width(), image.height()) == (3, 3)


def test_an_img_with_no_css_size_gets_its_intrinsic_size_from_tree_layout(monkeypatch):
    from chromonic import browser_images, tree

    png_image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_tiny_png(size=20)))
    monkeypatch.setattr(browser_images, "load_image", lambda url: png_image)

    root = div(img(_src="pic.png"), _style="width:300px;")
    tree.layout(root, width=300.0)

    box = root.childNodes[0].get_layout_box()
    assert (box.width, box.height) == (20.0, 20.0)


def test_an_img_with_an_explicit_css_size_ignores_intrinsic_size(monkeypatch):
    from chromonic import browser_images, tree

    png_image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_tiny_png(size=20)))
    monkeypatch.setattr(browser_images, "load_image", lambda url: png_image)

    img_el = img(_src="pic.png", _style="width:50px;height:50px;")
    root = div(img_el, _style="width:300px;")
    tree.layout(root, width=300.0)

    box = img_el.get_layout_box()
    assert (box.width, box.height) == (50.0, 50.0)  # CSS size wins over the image's own 20x20


def test_paint_draws_the_decoded_image_pixels(monkeypatch):
    from chromonic import browser_images

    png_image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_tiny_png(skia.ColorBLUE, size=10)))
    monkeypatch.setattr(browser_images, "load_image", lambda url: png_image)

    img_el = img(_src="pic.png", _style="width:40px;height:40px;")
    root = div(img_el, _style="width:100px;background-color:rgb(255,255,255);")
    png_bytes = chromonic.render(root, width=100, height=100)

    result = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png_bytes))
    pixels = result.toarray()  # (height, width, 4) RGBA
    # a pixel well inside the painted <img>'s box (top-left, 40x40) is blue
    r, g, b, _a = pixels[10, 10]
    assert (r, g, b) == (0, 0, 255)
    # a pixel outside the <img> (the root's white background) is untouched
    r, g, b, _a = pixels[90, 90]
    assert (r, g, b) == (255, 255, 255)


# -- phase 9: text line-wrapping + fonts -----------------------------------

def test_long_text_wraps_across_multiple_lines_within_available_width():
    from domonic import _fontmetrics

    text = "This is a long paragraph that should wrap across several lines once it no longer fits on one."
    root = div(p(text, _style="font-size:16px;"), _style="width:150px;")
    tree.layout(root, width=150.0)

    para = root.childNodes[0]
    lines = para._chromonic_text_lines
    assert len(lines) > 1
    assert " ".join(lines).replace("  ", " ") == text  # no words dropped or duplicated
    for line in lines:
        assert _fontmetrics.advance_width(line, 16.0, False) <= 150.0 + 0.01

    box = para.get_layout_box()
    assert box.height > _fontmetrics.text_extent("", 16.0, False)[1]  # taller than one line


def test_text_rewraps_when_relaid_out_at_a_different_width():
    text = "This is a long paragraph that should wrap differently at different widths please."
    para = p(text, _style="font-size:16px;")
    root = div(para, _style="width:600px;")

    tree.layout(root, width=600.0)
    wide_lines = len(para._chromonic_text_lines)

    root.style.width = "120px"
    tree.layout(root, width=120.0)
    narrow_lines = len(para._chromonic_text_lines)

    assert narrow_lines > wide_lines  # the same text needs more lines in a narrower box


def test_an_unconstrained_measure_call_does_not_wrap():
    from chromonic._native import layout_text

    # available_width=None (an intrinsic-sizing pass, see lib.rs's
    # measure_via_python) must behave like "don't wrap" -- the single
    # natural line, same as a text leaf with no width constraint at all
    # always painted before line wrapping existed.
    _width, _height, lines = layout_text("several unbroken words here", "sans-serif", 16.0)
    assert [text for text, _w, _h in lines] == ["several unbroken words here"]


def test_short_text_is_not_wrapped():
    root = div(p("hi", _style="font-size:16px;"), _style="width:300px;")
    tree.layout(root, width=300.0)
    assert root.childNodes[0]._chromonic_text_lines == ["hi"]


# -- fonts (chromonic.fonts) --------------------------------------------------

def test_parse_family_list_strips_quotes_and_handles_the_unset_value():
    from chromonic import fonts

    assert fonts.parse_family_list('Georgia, "Helvetica Neue", sans-serif') == [
        "Georgia", "Helvetica Neue", "sans-serif",
    ]
    assert fonts.parse_family_list("none") == []  # domonic's own "not set" value
    assert fonts.parse_family_list(None) == []
    assert fonts.parse_family_list("") == []


def test_is_italic_recognises_italic_and_oblique_only():
    from chromonic import fonts

    assert fonts.is_italic("italic") is True
    assert fonts.is_italic("oblique") is True
    assert fonts.is_italic("normal") is False
    assert fonts.is_italic(None) is False


def test_resolve_typeface_caches_by_name_and_style():
    from chromonic import fonts

    a = fonts.resolve_typeface("Georgia", bold=False, italic=False)
    b = fonts.resolve_typeface("Georgia", bold=False, italic=False)
    c = fonts.resolve_typeface("Georgia", bold=True, italic=False)
    assert a is b  # same key -> the cached instance, not a fresh lookup
    assert a is not c  # bold is part of the cache key


def test_resolve_typeface_maps_generic_families():
    from chromonic import fonts

    serif = fonts.resolve_typeface("serif")
    default = fonts.resolve_typeface(None)
    # not a strong assertion on *which* font ("serif" -> a concrete platform
    # name that may not even be installed, see the module docstring) -- just
    # that generic keywords are actually translated to something, not left
    # as the literal string "serif" for skia to fail to interpret.
    assert isinstance(serif, skia.Typeface)
    assert isinstance(default, skia.Typeface)


def test_resolve_typeface_falls_through_an_unavailable_first_name():
    # the actual bug found live: an earlier version only ever tried the
    # *first* family in a stack, because `skia.Typeface(name, style)` never
    # returns null even for a name nothing provides -- so a stack like
    # `-apple-system, "Segoe UI", sans-serif` (this repo's own UA default)
    # silently resolved to whatever Typeface() falls back to for
    # "-apple-system" specifically, never giving "Segoe UI" or "sans-serif"
    # a chance. `resolve_typeface` must walk the whole list, using each
    # name only if it's *actually installed* (`FontMgr().matchFamily`).
    from chromonic import fonts

    fonts._typeface_cache.clear()
    typeface = fonts.resolve_typeface("NoSuchFontAtAllXYZ, Georgia, sans-serif")
    assert typeface.getFamilyName() == "Georgia"  # skipped the fake name, landed on the real one


def test_resolve_typeface_maps_browser_internal_system_font_keywords():
    # "-apple-system"/"-webkit-system-font"/"BlinkMacSystemFont" are
    # browser-internal "use the OS UI font" keywords, not real family names
    # -- no font manager lists a family literally called that, so these must
    # go straight to the platform default (`None`) rather than wasting a
    # guaranteed-failing lookup (or worse, silently accepting whatever
    # Typeface() falls back to for a name that was never real -- the same
    # class of bug as the fallback-chain one above).
    from chromonic import fonts

    assert fonts.resolve_typeface("-apple-system") is fonts.resolve_typeface(None)
    assert fonts.resolve_typeface("-webkit-system-font") is fonts.resolve_typeface(None)
    assert fonts.resolve_typeface("BlinkMacSystemFont") is fonts.resolve_typeface(None)


def test_paint_uses_bold_and_italic_typefaces_for_matching_text():
    # a real, if soft, proof that bold/italic actually reach Skia's Font,
    # not just domonic's layout-side is_bold(): a bold line's glyphs render
    # with more filled (darker-appearing under anti-aliasing) pixels than
    # the same text set normally, at the same size/colour/position.
    def render_weight(style):
        root = div(p("Sample", _style=f"font-size:24px; {style}"), _style="width:200px;")
        png = chromonic.render(root, width=200, height=40)
        image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
        pixels = image.toarray()
        return int((255 - pixels[:, :, 0]).sum())  # more "ink" (darker) = more/heavier glyph coverage

    normal_ink = render_weight("")
    bold_ink = render_weight("font-weight:bold;")
    assert bold_ink > normal_ink


# -- phase 9: paint-style caching (measured on native_browser.py) ---------

def test_paint_style_is_extracted_once_per_layout_not_once_per_paint(monkeypatch):
    from domonic.style import ComputedStyleDeclaration

    from chromonic import paint

    root = div(p("hello", _style="color:rgb(1,2,3);"), _style="width:200px; background-color:rgb(9,9,9);")
    tree.layout(root, width=200.0)

    calls = []
    original = ComputedStyleDeclaration.__getattribute__

    def spy(self, name):
        if name in ("backgroundColor", "color", "fontSize", "fontWeight", "fontStyle", "fontFamily", "borderTopColor"):
            calls.append(name)
        return original(self, name)

    monkeypatch.setattr(ComputedStyleDeclaration, "__getattribute__", spy)
    surface = skia.Surface(200, 100)
    canvas = surface.getCanvas()
    paint.paint_tree(canvas, root)
    paint.paint_tree(canvas, root)  # a second repaint, no relayout in between

    assert calls == []  # paint never touched ComputedStyleDeclaration -- it read the cached extract instead


def test_paint_style_falls_back_when_painted_without_a_prior_layout():
    from chromonic import paint

    # an element that never went through tree.layout() (no
    # _chromonic_paint_style yet) must still paint correctly, just without the
    # caching benefit.
    element = p("hi", _style="color:rgb(4,5,6);")
    style = paint._paint_style(element)
    assert style["color"] == "rgb(4, 5, 6)"


def test_fonts_warm_cache_populates_the_common_combinations():
    from chromonic import fonts

    fonts._typeface_cache.clear()
    fonts.warm_cache()
    for bold in (False, True):
        for italic in (False, True):
            assert (None, bold, italic) in fonts._typeface_cache


# -- phase 10: startup warm-up + head/body style-placement --------------

def test_warm_interpreter_does_no_network_io_and_populates_the_globals_cache():
    from domonic_libs.acorn import interpret

    import chromonic.browser as browser_module

    interpret._DOMONIC_GLOBALS = None  # force a fresh collection to prove this call is what populates it
    browser_module.warm_interpreter()
    assert interpret._DOMONIC_GLOBALS is not None


def test_native_browser_view_construction_warms_both_caches(monkeypatch):
    from chromonic import browser, fonts, native_browser

    calls = []
    monkeypatch.setattr(fonts, "warm_cache", lambda: calls.append("fonts"))
    monkeypatch.setattr(browser, "warm_interpreter", lambda: calls.append("interpreter"))

    native_browser.View(200, 200)
    assert calls == ["fonts", "interpreter"]


def _serve_style_placement_pages():
    import http.server
    import threading

    pages = {
        "/head-style.html": (
            b"<!doctype html><html><head><style>#x{color:rgb(1,2,3);}</style></head>"
            b'<body><div id="x">a</div></body></html>'
        ),
        "/body-style.html": (
            b"<!doctype html><html><head></head><body>"
            b'<div id="x">a</div><style>#x{color:rgb(4,5,6);}</style></body></html>'
        ),
        "/body-link.html": (
            b"<!doctype html><html><head></head><body>"
            b'<link rel="stylesheet" href="a.css"><div id="x">a</div></body></html>'
        ),
        "/a.css": (b"#x{color:rgb(7,8,9);}"),
        "/inline.html": (
            b"<!doctype html><html><body>"
            b'<div id="x" style="color:rgb(10,11,12);">a</div></body></html>'
        ),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, b"not found")
            ctype = "text/css" if self.path.endswith(".css") else "text/html"
            self.send_response(200 if self.path in pages else 404)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_styles_apply_the_same_regardless_of_head_or_body_placement():
    # a concrete answer to "not sure if inline and loaded styles are being
    # applied or lost, specifically if they're in the head or body" --
    # exercised through the real chromonic.browser.load() pipeline (fetch,
    # parse, UA stylesheet, image resolution), not a synthetic shortcut.
    from domonic.style import ComputedStyleDeclaration

    from chromonic import browser

    server = _serve_style_placement_pages()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        expected = {
            "/head-style.html": "rgb(1, 2, 3)",
            "/body-style.html": "rgb(4, 5, 6)",
            "/body-link.html": "rgb(7, 8, 9)",
            "/inline.html": "rgb(10, 11, 12)",
        }
        for path, color in expected.items():
            page = browser.load(base + path)
            element = page.document.getElementById("x")
            assert ComputedStyleDeclaration(element).color == color, path
    finally:
        server.shutdown()


# -- phase 11: an inline-flow approximation for runs of inline elements --

def test_a_run_of_links_lays_out_horizontally_not_one_per_line():
    from domonic.html import a

    # a nav bar exactly like suckless.org's own: a block wrapper around
    # several plain, entirely unstyled <a> tags -- real inline flow needs
    # no CSS at all for this, since `inline` is already every element's own
    # default; chromonic has no inline flow, so this is what the approximation
    # (`tree._approximate_inline_flow`) is for.
    links = [a(f"link{i}", _href="#") for i in range(4)]
    nav = div(*links, _style="width:600px;")
    tree.layout(nav, width=600.0)

    boxes = [link.get_layout_box() for link in links]
    # side by side (increasing x, same row) -- not stacked (increasing y)
    assert all(boxes[i + 1].x > boxes[i].x for i in range(3))
    assert len({round(box.y) for box in boxes}) == 1


def test_inline_flow_approximation_tolerates_a_minority_of_exceptions():
    # the real bug found on suckless.org: one <span> among nine <a>s had its
    # own `display:block` (the site's own CSS, for unrelated dropdown/JS
    # behaviour) -- requiring *every* child to qualify let that one
    # exception veto the whole nav back to one-link-per-line. A majority
    # must still be enough.
    from domonic.html import a, span

    children = [a(f"link{i}", _href="#") for i in range(8)] + [span("x", _style="display:block;")]
    nav = div(*children, _style="width:900px;")
    tree.layout(nav, width=900.0)

    first, second = children[0].get_layout_box(), children[1].get_layout_box()
    assert second.x > first.x  # still laid out as a row, not one-per-line


def test_inline_flow_approximation_does_not_fire_on_ordinary_block_content():
    # the regression this heuristic caused and had to be fixed to avoid:
    # domonic's raw CSS initial value for `display` is "inline" for *every*
    # tag with no UA stylesheet applied (wrinkle #11) -- an early version
    # trusted that alone and turned an entirely ordinary <div><p>...</p>
    # <p>...</p></div> into a flex row. `div`/`p` are never in
    # `_USUALLY_INLINE_TAGS`, so this must stay stacked regardless.
    paragraphs = [p(f"paragraph {i}") for i in range(3)]
    root = div(*paragraphs, _style="width:300px;")
    tree.layout(root, width=300.0)

    boxes = [para.get_layout_box() for para in paragraphs]
    assert boxes[0].y < boxes[1].y < boxes[2].y  # stacked, top to bottom -- not a row
    assert all(round(box.x) == round(boxes[0].x) for box in boxes)  # all flush left, not side by side


def test_inline_flow_approximation_requires_at_least_two_children():
    from domonic.html import a

    root = div(a("only", _href="#"), _style="width:300px;")
    tree.layout(root, width=300.0)
    # nothing to prove a row vs a stack with a single child -- just must not crash,
    # and the lone link still gets a sane box.
    box = root.childNodes[0].get_layout_box()
    assert box.width > 0 and box.height > 0


def test_an_explicit_block_display_on_an_inline_tag_is_respected():
    from domonic.html import a

    # a { display: block } is a real, common pattern (button-like nav
    # links) -- it must count *against* the majority the same way
    # suckless.org's stray <span> does, not be silently overridden.
    links = [a(f"link{i}", _href="#", _style="display:block;") for i in range(3)]
    root = div(*links, _style="width:300px;")
    tree.layout(root, width=300.0)

    boxes = [link.get_layout_box() for link in links]
    assert boxes[0].y < boxes[1].y < boxes[2].y  # every child opted out -- stays stacked


def test_ua_style_sets_display_block_for_common_tags_but_not_inline_ones():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    from chromonic import ua_style

    document = DOMParser().parseFromString(
        "<html><body><div id='d'>x</div><p id='p'>x</p>"
        "<a id='a' href='#'>x</a><span id='s'>x</span></body></html>",
        "text/html",
    )
    ua_style.apply(document)

    assert ComputedStyleDeclaration(document.getElementById("d")).display == "block"
    assert ComputedStyleDeclaration(document.getElementById("p")).display == "block"
    assert ComputedStyleDeclaration(document.getElementById("a")).display == "inline"
    assert ComputedStyleDeclaration(document.getElementById("s")).display == "inline"


# -- phase 12: position:absolute's real containing block, <select>, float --

def test_absolute_positioning_resolves_against_the_real_containing_block():
    # the bug found on wikipedia.org: a position:absolute search box with no
    # positioned ancestor anywhere landed relative to its literal (static)
    # DOM parent instead of the page -- reproduced minimally.
    inner = div(_style="position:absolute; top:50px; left:50px; width:20px; height:20px;")
    middle = div(inner, _style="width:200px; height:200px; margin:100px;")  # static -- not a containing block
    outer = div(middle, _style="width:500px; height:500px;")

    tree.layout(outer, width=500.0, height=500.0)

    box = inner.get_layout_box()
    assert (box.x, box.y) == (50.0, 50.0)  # relative to outer (the root), NOT middle (100,100) + (50,50)


def test_absolute_positioning_still_uses_a_direct_positioned_parent():
    # the common case (particles.py's own architecture) must keep working:
    # a direct position:relative parent is already the correct containing
    # block, no reparenting needed.
    inner = div(_style="position:absolute; top:10px; left:10px; width:5px; height:5px;")
    stage = div(inner, _style="position:relative; width:100px; height:100px;")

    tree.layout(stage, width=100.0, height=100.0)

    box = inner.get_layout_box()
    assert (box.x, box.y) == (10.0, 10.0)


def test_absolute_positioning_uses_the_nearest_positioned_ancestor_not_root():
    # a positioned ancestor two levels up (not the root, not the literal
    # parent) must still be preferred over either alternative -- each of
    # root/middle/positioned is given its own distinct offset (padding/
    # margin) specifically so the three possible (wrong, wrong, right)
    # answers land at three different, unambiguous coordinates.
    inner = div(_style="position:absolute; top:5px; left:5px; width:5px; height:5px;")
    middle = div(inner, _style="width:50px; height:50px; margin:7px;")  # static
    positioned = div(middle, _style="position:relative; width:300px; height:300px; margin:20px;")
    root = div(positioned, _style="width:500px; height:500px; padding:100px;")

    tree.layout(root, width=500.0, height=500.0)

    box = inner.get_layout_box()
    # positioned's own content origin is (100+20, 100+20) = (120,120) --
    # correct: (125,125). Attaching to root instead would give (105,105);
    # attaching to middle (the pre-fix bug -- the literal DOM parent)
    # would give (132,132).
    assert (box.x, box.y) == (125.0, 125.0)


def test_select_shows_only_its_selected_option_not_every_option_stacked():
    from domonic.html import option, select

    # the bug found on wikipedia.org: a 250-option language <select> was
    # rendered as 250 stacked, fully visible block boxes -- a real <select>
    # is a closed dropdown showing only its current value.
    picker = select(
        option("Afrikaans", _value="af"),
        option("Deutsch", _value="de", _selected="selected"),
        option("English", _value="en"),
        _style="width:200px;",
    )
    root = div(picker, _style="width:300px;")
    node_map = tree.layout(root, width=300.0)

    tags = {getattr(el, "tagName", "").lower() for el in node_map.values()}
    assert "option" not in tags  # no <option> ever became its own laid-out node
    box = picker.get_layout_box()
    assert box.height > 0  # still a real, visible box itself


def test_select_falls_back_to_the_first_option_with_none_marked_selected():
    from domonic.html import option, select

    picker = select(option("Afrikaans", _value="af"), option("Deutsch", _value="de"))
    root = div(picker, _style="width:300px;")
    tree.layout(root, width=300.0)

    from chromonic import tree as tree_module
    assert tree_module._select_display_text(picker) == "Afrikaans"


def test_a_floated_grid_wraps_horizontally_like_an_inline_run():
    from domonic.html import a

    # float: left is unambiguous author intent (unlike display:inline,
    # float's initial value is always "none" regardless of tag) -- no tag
    # gate needed the way _is_inline_level needs one.
    cards = [a(f"card{i}", _href="#", _style="float:left; width:50px; height:20px;") for i in range(4)]
    root = div(*cards, _style="width:220px;")
    tree.layout(root, width=220.0)

    boxes = [card.get_layout_box() for card in cards]
    assert boxes[0].y == boxes[1].y  # side by side, not stacked
    assert boxes[1].x > boxes[0].x


def test_paint_draws_the_selects_own_text_not_its_options():
    from domonic.html import option, select

    picker = select(
        option("Afrikaans", _value="af"),
        option("Deutsch", _value="de", _selected="selected"),
        _style="width:200px; height:30px; background-color:rgb(255,255,255);",
    )
    root = div(picker, _style="width:220px; background-color:rgb(255,255,255);")
    png = chromonic.render(root, width=220, height=40)

    result = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    pixels = result.toarray()
    # some non-white ink was painted inside the select's own box (its text)
    # -- not asserting exact glyph positions, just that *something* other
    # than a blank white rect got drawn where <option>s used to stack.
    region = pixels[5:25, 5:195]
    assert (region[:, :, :3] < 250).any()


# -- phase 13: real text layout via Parley (chromonic._native.layout_text) --

def test_layout_text_uses_real_per_font_metrics_not_one_fixed_table():
    from chromonic._native import layout_text

    # a fixed Helvetica-shaped table can't tell monospace from sans-serif;
    # Parley resolves an actual font via fontique and measures with it.
    mono_width, _h, _lines = layout_text("iiiiiiiiii", "monospace", 16.0)
    sans_width, _h2, _lines2 = layout_text("iiiiiiiiii", "sans-serif", 16.0)
    assert mono_width > sans_width  # monospace forces equal-width glyphs; "i" is narrow in sans-serif


def test_layout_text_bold_measures_wider_than_normal():
    from chromonic._native import layout_text

    normal_width, _h, _lines = layout_text("Bold Text", "sans-serif", 16.0, font_weight=400.0)
    bold_width, _h2, _lines2 = layout_text("Bold Text", "sans-serif", 16.0, font_weight=700.0)
    assert bold_width > normal_width


def test_layout_text_wraps_to_max_width_and_reports_line_metrics():
    from chromonic._native import layout_text

    text = "This is a long paragraph that should wrap across several lines once it no longer fits."
    width, height, lines = layout_text(text, "sans-serif", 16.0, max_width=150.0)
    assert len(lines) > 1
    assert width <= 150.0 + 0.5
    assert height > lines[0][2]  # taller than a single line
    # no words dropped or duplicated -- each line's text_range includes its
    # own trailing whitespace (the break point itself), so concatenating
    # them directly (not re-joining with a space) reconstructs the original
    assert "".join(line_text for line_text, _w, _h in lines) == text


def test_layout_text_unconstrained_is_a_single_line():
    from chromonic._native import layout_text

    _width, _height, lines = layout_text("a short line", "sans-serif", 16.0, max_width=None)
    assert len(lines) == 1


def test_tree_measure_uses_the_elements_own_font_family():
    # the actual bug this closes: text used to be *measured* as Helvetica
    # regardless of font-family, then *painted* in the real font (fonts.py)
    # -- now both measurement and painting agree on the same family. A
    # block <p>'s own box always stretches to fill its container (that's
    # normal block layout, nothing to do with text measurement -- see
    # tree.py's `_apply_image_intrinsic_size` docstring for the same
    # "measure() doesn't own a block child's width" point made for <img>),
    # so the font actually affects the *content*, not the box -- checked
    # here as "a narrower font wraps the same text into fewer lines".
    text = "The quick brown fox jumps over the lazy dog again and again"
    serif = p(text, _style="font-family:Georgia, serif; font-size:16px;")
    mono = p(text, _style="font-family:monospace; font-size:16px;")
    root = div(serif, mono, _style="width:200px;")
    tree.layout(root, width=200.0)

    # different fonts wrap at different points -- proof the actual
    # font-family (not one fixed table) drove the line-break decisions,
    # regardless of whether the two happen to produce the same line count
    assert serif._chromonic_text_lines != mono._chromonic_text_lines


def test_tree_measure_respects_letter_spacing():
    text = "spaced text wraps differently depending on how far apart its letters are"
    tight = p(text, _style="font-size:16px;")
    wide = p(text, _style="font-size:16px; letter-spacing:6px;")
    root = div(tight, wide, _style="width:200px;")
    tree.layout(root, width=200.0)

    assert len(wide._chromonic_text_lines) > len(tight._chromonic_text_lines)


def test_tree_measure_respects_an_explicit_line_height():
    normal = p("one line of text", _style="font-size:16px;")
    tall = p("one line of text", _style="font-size:16px; line-height:3;")
    root = div(normal, tall, _style="width:600px;")
    tree.layout(root, width=600.0)

    assert tall.get_layout_box().height > normal.get_layout_box().height


def test_paint_uses_parleys_real_line_height_for_baseline_spacing():
    text = "This paragraph is long enough to wrap across a couple of lines here"
    para = p(text, _style="font-size:16px;")
    root = div(para, _style="width:150px;")
    tree.layout(root, width=150.0)

    assert para._chromonic_line_height > 0
    lines = len(para._chromonic_text_lines)
    assert lines > 1
    # a per-line number, not the whole box's height -- the box is
    # (roughly) `lines` of these stacked, not equal to a single one
    assert para._chromonic_line_height < para.get_layout_box().height


def test_warm_text_layout_does_not_crash_and_is_idempotent():
    from chromonic.tree import warm_text_layout

    warm_text_layout()
    warm_text_layout()  # a second call must be cheap and harmless, not an error


def test_native_browser_view_construction_warms_text_layout_too(monkeypatch):
    from chromonic import browser, fonts, native_browser

    calls = []
    monkeypatch.setattr(fonts, "warm_cache", lambda: calls.append("fonts"))
    monkeypatch.setattr(native_browser, "warm_text_layout", lambda: calls.append("text_layout"))
    monkeypatch.setattr(browser, "warm_interpreter", lambda: calls.append("interpreter"))

    native_browser.View(200, 200)
    assert calls == ["fonts", "text_layout", "interpreter"]


# -- lazy/async image loading, so the page doesn't wait for every <img> --

def test_load_image_does_not_block_and_starts_a_background_fetch():
    import http.server
    import threading
    import time

    from chromonic import browser_images

    browser_images.clear_cache()
    started = threading.Event()
    server_may_respond = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            server_may_respond.wait(timeout=5.0)  # held open until the test says go
            body = b"not a real image"
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/slow.png"
        t0 = time.perf_counter()
        result = browser_images.load_image(url)
        elapsed = time.perf_counter() - t0

        assert result is None  # not ready yet -- the fetch is still "in flight" on the server
        assert elapsed < 1.0  # did NOT block waiting for the (deliberately held-open) response
        assert browser_images.has_pending() is True
        started.wait(timeout=2.0)  # the background thread really did start the request
    finally:
        server_may_respond.set()
        srv.shutdown()


def test_generation_increments_only_once_a_background_fetch_finishes():
    from chromonic import browser_images

    browser_images.clear_cache()
    before = browser_images.generation()
    assert browser_images.load_image("data:image/png;base64,not-valid-base64===") is None
    # a data: URI is decoded synchronously (see the module docstring) -- no
    # polling needed, but generation() is only for the async (network) path
    assert browser_images.generation() == before

    server_generation_before = browser_images.generation()
    url = "http://127.0.0.1:1/unreachable.png"  # nothing listens on port 1 -- fails fast
    browser_images.load_image(url)
    _await_image(browser_images, url)
    assert browser_images.generation() > server_generation_before


def test_an_img_still_loading_lays_out_as_a_zero_sized_box_not_a_blocked_one(monkeypatch):
    from chromonic import browser_images, tree

    monkeypatch.setattr(browser_images, "load_image", lambda url: None)  # "still loading"

    root = div(img(_src="pic.png"), _style="width:300px;")
    tree.layout(root, width=300.0)

    box = root.childNodes[0].get_layout_box()
    # width still stretches to fill the container -- ordinary block layout,
    # unrelated to whether the image has arrived (see `_apply_image_
    # intrinsic_size`'s docstring for the same point made about <img> in
    # general); height is the part that reflects "nothing to reserve space
    # for yet", same as a real browser gives an <img> with no width/height
    # attributes while it's still loading.
    assert box.width == 300.0
    assert box.height == 0.0


def test_native_browser_view_poll_images_relayouts_only_when_generation_changes(monkeypatch):
    from chromonic import browser_images, native_browser
    from myjs import Page

    def loader(url):
        return Page("<html><body><p>hi</p></body></html>", run=False, css=False)

    view = native_browser.View(200, 200, loader=loader)
    view.navigate("https://example.com/")

    relayouts = []
    monkeypatch.setattr(view, "relayout", lambda **kwargs: relayouts.append(1))

    monkeypatch.setattr(browser_images, "generation", lambda: 0)
    view.poll_images()
    assert relayouts == []  # unchanged -- no relayout triggered

    monkeypatch.setattr(browser_images, "generation", lambda: 1)
    view.poll_images()
    assert relayouts == [1]  # changed -- exactly one relayout

    view.poll_images()
    assert relayouts == [1]  # still 1 -- generation() didn't change again


def test_native_browser_view_throttles_a_burst_of_image_arrivals_into_one_relayout(monkeypatch):
    # the real regression this fixes: a page with many images completing
    # close together (a real thread pool draining a real queue) used to
    # relayout the *entire* tree once per arrival -- measured, a 40-image
    # page produced 34 separate relayouts for one burst, spiking CPU enough
    # to beachball the window. A burst within one throttle window must
    # collapse into a single relayout.
    from chromonic import browser_images, native_browser
    from myjs import Page

    def loader(url):
        return Page("<html><body><p>hi</p></body></html>", run=False, css=False)

    view = native_browser.View(200, 200, loader=loader)
    view.navigate("https://example.com/")

    relayouts = []
    monkeypatch.setattr(view, "relayout", lambda **kwargs: relayouts.append(1))
    generation = [0]
    monkeypatch.setattr(browser_images, "generation", lambda: generation[0])

    # ten "arrivals" in immediate succession (no real time passing) --
    # simulating several images finishing within the same loop tick
    for _ in range(10):
        generation[0] += 1
        view.poll_images()
    assert len(relayouts) == 1  # the first one relayouts immediately; the rest are within the throttle window

    # advancing real time past the throttle window lets the next arrival through
    view._last_image_relayout -= native_browser._IMAGE_RELAYOUT_INTERVAL + 0.01
    generation[0] += 1
    view.poll_images()
    assert len(relayouts) == 2


def test_browser_api_tick_pushes_a_frame_only_when_an_image_arrived(monkeypatch):
    from chromonic import browser, browser_images
    from chromonic.browser import BrowserInteraction

    def loader(url):
        from myjs import Page

        return Page("<html><body><p>hi</p></body></html>", run=False, css=False)

    interaction = BrowserInteraction.__new__(BrowserInteraction)
    interaction.width, interaction.height = 200.0, 200.0
    interaction.url = "https://example.com/"
    from domonic.html import div as _div

    interaction.root = _div()
    api = browser._Api(interaction)

    pushes = []
    monkeypatch.setattr(api, "push_frame", lambda **kw: pushes.append(kw))
    monkeypatch.setattr(browser_images, "generation", lambda: 0)
    api.tick()
    assert pushes == []

    monkeypatch.setattr(browser_images, "generation", lambda: 1)
    api.tick()
    assert len(pushes) == 1


# -- reuse_styles: skip domonic's CSS cascade on a relayout that can't have
# changed any element's styling inputs (see tree.py's `_describe` docstring
# for the real-page finding -- a single relayout of bbc.co.uk cost 6.7s,
# almost all of it re-parsing CSS selectors domonic never caches) ---------

def test_reuse_styles_skips_css_resolution_for_an_already_resolved_element(monkeypatch):
    from domonic.style import ComputedStyleDeclaration
    from chromonic import tree

    root = div(p('hello'), _style='color:rgb(255,0,0);width:100px')
    tree.layout(root, width=100)  # first pass: real resolution, populates _chromonic_resolved_style

    resolutions = []
    original = ComputedStyleDeclaration._resolve

    def counting_resolve(self):
        resolutions.append(id(self._element))
        return original(self)

    monkeypatch.setattr(ComputedStyleDeclaration, '_resolve', counting_resolve)
    tree.layout(root, width=100, reuse_styles=True)
    assert resolutions == []  # nothing re-resolved -- every element's prior style was reused


def test_reuse_styles_still_applies_a_newly_arrived_images_real_size(monkeypatch):
    from chromonic import browser_images, tree

    monkeypatch.setattr(browser_images, "load_image", lambda url: None)  # not arrived yet
    img_el = img(_src="pic.png")
    root = div(img_el, _style="width:300px;")
    tree.layout(root, width=300.0)
    assert img_el.get_layout_box().height == 0.0  # nothing to reserve space for yet

    png_image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_tiny_png(size=20)))
    monkeypatch.setattr(browser_images, "load_image", lambda url: png_image)  # "arrived" -- generation changed
    tree.layout(root, width=300.0, reuse_styles=True)
    assert img_el.get_layout_box().height == 20.0  # picked up even though CSS wasn't re-resolved


def test_reuse_styles_false_still_sees_a_real_style_mutation():
    from chromonic import tree

    root = div(p('hello'), _style='color:rgb(255,0,0);width:100px')
    tree.layout(root, width=100)  # populates the per-element cache reuse_styles=True would reuse
    before = root.childNodes[0]._chromonic_computed_style.color

    root.style.color = 'rgb(0,0,255)'
    tree.layout(root, width=100)  # default reuse_styles=False -- must not serve the stale cached colour
    after = root.childNodes[0]._chromonic_computed_style.color
    assert after != before
