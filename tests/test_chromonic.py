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
from chromonic import hittest, paint, style_bridge, tree  # noqa: E402
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


def test_style_bridge_treats_bootstrap_prefixed_flex_as_flex():
    element = div(_style="display:-ms-flexbox; align-items:center;")
    style = style_bridge.to_dict(layout_style(element))

    assert style["display"] == "flex"
    assert style["align_items"] == "center"


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


def test_root_percentage_height_resolves_against_viewport_height():
    child = div(_style="height:100%;")
    root = div(child, _style="height:100%;")

    tree.layout(root, width=800.0, height=None, viewport_height=600.0)

    assert root.get_layout_box().height == 600.0
    assert child.get_layout_box().height == 600.0


def test_display_none_clears_stale_layout_box_instead_of_freezing_it():
    """`display:none` gives an element (and everything inside it) no box
    at all in real CSS -- `tree.py`'s own build/adjust pipeline already
    knew to exclude such an element from `node_map` and never touch it
    again, but never actually cleared the `_layout_box` a *previous* pass
    published for it back when it still rendered. `paint_tree` walks the
    real DOM, not `node_map`, so it never itself learns an element was
    excluded -- it kept drawing a hidden element at that frozen, stale
    position forever, and `getBoundingClientRect()`/hit-testing read the
    same stale box for the same reason. Found via a live `chromonic.App`
    run (`examples/kanban.py`): clicking a `.filter` button set matching
    cards' own `display` to `none`, layout heights changed accordingly,
    but the hidden cards never actually disappeared from the screen."""
    child = div("hi", _style="height:20px;")
    element = div(child, _style="height:40px;")
    root = div(element, _style="width:200px;")

    tree.layout(root, width=200.0)
    assert element.get_layout_box() is not None
    assert child.get_layout_box() is not None

    element.style.display = "none"
    tree.layout(root, width=200.0)
    assert element.get_layout_box() is None
    assert child.get_layout_box() is None

    element.style.display = ""
    tree.layout(root, width=200.0)
    assert element.get_layout_box() is not None
    assert element.get_layout_box().height == 40.0
    assert child.get_layout_box() is not None


def test_block_in_inline_split_marker_reports_the_blocks_border_box_not_margin_box():
    """CSS 2.1 9.2.1.1: an inline (`<a>`) whose only content is a genuine
    in-flow block child splits around it, generating one extra `getClient
    Rects()` marker rect at the block's own position -- but that marker
    must report the block's *border box*, not a margin-inflated union of
    it. A block's margin still participates in ordinary block-flow spacing
    (it is why the marker's own `y` sits below the line it would otherwise
    start at), but is not part of any box's own border-box geometry or hit
    region -- generated marker included. Confirmed against real Chrome on
    `wpt/css/CSS2/normal-flow/block-in-inline-hittest-margin.html`: a
    `100px`-margin, `100px`-square block reported this marker at `y=100,
    height=100` (its own border box) inside the block's own `784px`-wide
    containing block, not `y=0, height=300` (its full margin box, with a
    `100px` margin on every side unioned in)."""
    from domonic.dom import DOMParser

    from chromonic import ua_style

    document = DOMParser().parseFromString(
        "<html><body><a href='#'><div style="
        "'width:100px;height:100px;margin:100px;'></div></a></body></html>",
        "text/html",
    )
    ua_style.apply(document)
    tree.layout(document.body, width=800.0)

    anchor = document.getElementsByTagName("a")[0]
    rects = anchor.__dict__.get("_chromonic_inline_boxes")
    marker = rects[1]
    assert marker == (8.0, 108.0, 784.0, 100.0)


def test_block_in_inline_survives_through_nested_empty_inline_wrappers():
    """CSS 2.1 9.2.1.1's "anonymous block box" split must trigger for an
    in-flow block reachable *transitively* through a chain of genuine
    inline wrappers, not only a block that's a wrapper's own *direct*
    child -- `_split_wrapping_inline_element`'s per-child loop used to
    treat any inline-level child (including one that itself, one or more
    levels deeper, wraps a real block) as ordinary inline content headed
    for `_build_text_runs_from_nodes`, which has no notion of a block
    anywhere inside a nested element and silently dropped it -- and
    everything inside it -- instead of generating a real subtree. Found on
    `wpt/css/CSS2/normal-flow/block-in-inline-hittest-001.html`: `<div>
    <span><span style="outline:...">` (both empty, no content of their
    own) `<div id=target><div style="width:64px;height:26px;"></div>`
    measured `target` as `0x0`/absent from layout entirely instead of
    Chrome's real `x=8, y=8, width=784, height=26` (the containing block's
    own content width, `26px` tall -- `target`'s own inner `26px`-tall
    child)."""
    from domonic.dom import DOMParser

    from chromonic import ua_style

    document = DOMParser().parseFromString(
        "<html><body><div><span><span style='outline: 1px solid blue'>"
        "<div id='target'><div style='width: 64px; height: 26px;'></div></div>"
        "</span></span></div></body></html>",
        "text/html",
    )
    ua_style.apply(document)
    tree.layout(document.body, width=800.0)

    target = document.getElementById("target")
    box = target.get_layout_box()
    assert box is not None
    assert (box.x, box.y, box.width, box.height) == (8.0, 8.0, 784.0, 26.0)


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


def test_flex_toolbar_auto_height_is_not_shortened_by_bfc_float_correction():
    """`_fix_nested_bfc_float_auto_height` (CSS 2.1 10.6.3/10.6.7: an
    auto-height BFC's own bottom must include a descendant float that
    escaped a non-BFC wrapper) treated every flex container as eligible
    too, since `_establishes_bfc` correctly reports that a flex container
    establishes a BFC -- but CSS Flexbox computes `float` to `none` on
    every flex item regardless of its author value, so a flex container
    can never actually contain a floated child for this pass to find. It
    still recomputed the row's height from `max(child bottom margin
    edge)`, ordinary block-flow style, instead of leaving Taffy's own
    (already-correct) native flex cross-size alone -- children kept at
    their own height via `align-items:flex-start` (not the default
    `stretch`, which would mask this) land short of the row's real height,
    so the "correction" came out smaller than Taffy's own number and
    shifted every later sibling up to match.

    Two flex rows stacked directly, each followed only by pixel-height
    children (no font metrics involved), give an unambiguous expected
    answer any spec-compliant browser (Chrome included) would also
    produce: each row's height is its tallest child, and each later
    element sits exactly at the previous one's real border-box bottom
    edge. Mirrors `examples/kanban.py`'s two `.toolbar { display:flex }`
    rows, where this previously shifted the `.board` below both of them
    (and everything painted inside it) up by 8px -- 4px per row -- on
    every single relayout."""
    toolbar_a = div(
        div(_style="height:24px;"),
        div(_style="height:40px;"),
        _style="display:flex; align-items:flex-start; height:auto;",
    )
    toolbar_b = div(
        div(_style="height:10px;"),
        div(_style="height:30px;"),
        _style="display:flex; align-items:flex-start; height:auto;",
    )
    board = div(_style="height:5px;")
    root = div(toolbar_a, toolbar_b, board, _style="width:200px;")

    shifted = []
    original = tree._shift_later_siblings_for_height_delta

    def recording(element, delta):
        shifted.append(element)
        original(element, delta)

    tree._shift_later_siblings_for_height_delta = recording
    try:
        tree.layout(root, width=200.0)
    finally:
        tree._shift_later_siblings_for_height_delta = original

    assert toolbar_a.get_layout_box().height == 40.0
    assert toolbar_b.get_layout_box().height == 30.0
    assert toolbar_b.get_layout_box().y == toolbar_a.get_layout_box().y + 40.0
    assert board.get_layout_box().y == toolbar_b.get_layout_box().y + 30.0
    # Neither flex row should ever reach `_shift_later_siblings_for_height_
    # delta` at all -- not "corrected once instead of twice", corrected
    # zero times, since Taffy's own native flex sizing already owns it.
    assert toolbar_a not in shifted
    assert toolbar_b not in shifted


def test_bfc_float_correction_still_applies_once_to_a_real_block_bfc():
    """Positive control for the fix above: excluding flex/grid containers
    from `_fix_nested_bfc_float_auto_height`'s eligibility must narrow
    *which* boxes it corrects, not disable the pass itself. A genuine
    `display:flow-root` block (no flex/grid involved) whose only content
    is a wrapper div holding one `float:left` child is exactly the shape
    this function's own docstring cites (found on `wpt/css/CSS2/normal-
    flow/block-formatting-context-height-002.xht`): Chrome's real answer
    is `48px` (float height) `+ 48px` (its escaped bottom margin) `=
    96px`, not the `0px` Taffy's native block layout gives a BFC with no
    ordinary in-flow content of its own (a float is never in-flow)."""
    wrapper = div(div(_style="float:left; height:48px; margin-bottom:48px;"))
    bfc = div(wrapper, _style="display:flow-root; height:auto;")
    root = div(bfc, _style="width:200px;")

    shifted = []
    original = tree._shift_later_siblings_for_height_delta

    def recording(element, delta):
        shifted.append(element)
        original(element, delta)

    tree._shift_later_siblings_for_height_delta = recording
    try:
        tree.layout(root, width=200.0)
    finally:
        tree._shift_later_siblings_for_height_delta = original

    assert bfc.get_layout_box().height == 96.0
    assert shifted.count(bfc) == 1


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


def test_public_app_and_browser_wrap_existing_runtime(monkeypatch):
    from domonic.html import body, button

    root = body(button("Increment", _id="inc"), p("0", _id="value"))
    calls = []
    monkeypatch.setattr(chromonic.window, "run", lambda *args, **kw: calls.append(("app", args, kw)))
    monkeypatch.setattr(chromonic.browser, "run", lambda *args, **kw: calls.append(("browser", args, kw)))

    app = chromonic.App(root, width=320, height=200, title="Settings")
    assert app.root is root
    assert app.document.body is root
    assert app.window is app.document.defaultView

    @app.click("#inc")
    def increment(event):
        app.document.querySelector("#value").textContent = "1"

    assert app.render().startswith(b"\x89PNG")
    button_box = app.document.querySelector("#inc").get_layout_box()
    app.interaction.handle_click(button_box.x + 1, button_box.y + 1)
    assert app.document.querySelector("#value").textContent == "1"
    app.run()

    browser = chromonic.Browser("https://eventual.technology", width=640, height=480)
    browser.run()

    assert calls[0] == ("app", (root,), {"width": 320, "height": 200, "title": "Settings", "on_tick": None, "fps": 30.0})
    assert calls[1] == ("browser", ("https://eventual.technology",), {"width": 640, "height": 480, "title": "chromonic"})


def test_public_todo_app_features():
    from domonic.html import body, button, div, h1, input, li, span, ul

    root = body(
        h1("Tasks"),
        div(input(_id="new-task", _placeholder="Add task..."), button("Add", _id="add")),
        ul(_id="tasks"),
    )
    app = chromonic.App(root, width=700, height=500)

    @app.click("#add")
    def add_task(event):
        field = app.document.querySelector("#new-task")
        text = field.value.strip()
        if not text:
            return
        app.document.querySelector("#tasks").appendChild(
            li(input(_type="checkbox"), span(text), button("Delete", _class="delete"))
        )
        field.value = ""

    @app.key("#new-task", "Enter")
    def add_with_enter(event):
        app.trigger("#add", "click")

    @app.click(".delete")
    def delete_task(event):
        event.currentTarget.parentNode.remove()

    assert app.render().startswith(b"\x89PNG")
    field = app.document.querySelector("#new-task")
    field_box = field.get_layout_box()
    app.interaction.handle_click(field_box.x + 1, field_box.y + 1)
    for char in "Ship":
        app.interaction.handle_text(char)
    assert field.value == "Ship"
    assert field._chromonic_text_lines == ["Ship"]

    app.interaction.handle_key("Enter")
    assert field.value == ""
    assert app.document.querySelector("#tasks span").textContent == "Ship"

    delete = app.document.querySelector(".delete")
    delete_box = delete.get_layout_box()
    app.interaction.handle_click(delete_box.x + 1, delete_box.y + 1)
    assert app.document.querySelector("#tasks span") is None

    field.value = "Again"
    app.trigger("#add", "click")
    checkbox = app.document.querySelector("#tasks input")
    assert checkbox is not None
    checkbox.checked = True
    assert checkbox.checked is True


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


def test_generated_content_pseudo_elements_contribute_text_layout():
    from chromonic.browser import BrowserInteraction

    files = {
        "/index.html": (
            b"<!doctype html><html><head>"
            b"<style>"
            b".fa { font-family: Arial; font-size: 20px; }"
            b".fa-image::before { content: '\\\\f03e'; }"
            b"</style>"
            b"</head><body><i class='fa fa-image'></i></body></html>",
            "text/html",
        ),
    }
    srv = _serve(files)
    port = srv.server_address[1]
    try:
        interaction = BrowserInteraction(f"http://127.0.0.1:{port}/index.html", width=300.0, height=200.0)
        interaction.render()
        icon = interaction.root.getElementsByTagName("i")[0]

        # `::before` with non-empty `content` now gets a real, separately
        # laid-out box (its own font/position/background, not text merely
        # concatenated onto the owner's) -- see `tree._PseudoElement`.
        assert icon._chromonic_before_text == "\uf03e"
        fragments = icon._chromonic_inline_fragments
        assert len(fragments) == 1
        pseudo = fragments[0]
        assert pseudo.text == "\uf03e"
        assert pseudo.__dict__["_layout_box"].width > 0
        assert icon.get_layout_box().width > 0
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


def test_outline_never_perturbs_geometry_across_repeated_classlist_cycles():
    """CSS `outline` is paint-only (CSS2.1 8.5.4/CSS UI 4 -- it never
    participates in the box model, Taffy layout, or document flow, only
    drawn on top of the element's own already-final box). Regression test
    for the chromonic showcase's `.card.selected { outline: 2px solid
    #333 }` -- reported as the clicked card visually drifting ~4-5px per
    click (`inspect_card` toggles `.selected` on/off on every click).
    Extensive manual reproduction across both layout engines (`tree.
    layout()` and `LayoutProjection`, single- and multi-card documents,
    real dispatched click events, and a 40-step randomized stress
    sequence) found no drift with the code as it stands -- this test
    locks that invariant in going forward, exactly as specified: x/y/
    width/height must stay byte-for-byte identical through 20 add/remove
    cycles, whether or not `.selected` is currently applied."""
    from domonic.dom import DOMParser

    from chromonic import tree

    document = DOMParser().parseFromString(
        "<html><head><style>"
        ".card { background: white; border: 1px solid #ccc; margin-bottom: 10px; padding: 12px; }"
        ".card.selected { outline: 2px solid #333; }"
        "</style></head><body style='margin:0;display:block'>"
        "<div class='card' id='x'>hello</div>"
        "<div class='card' id='y'>world</div>"
        "</body></html>",
        "text/html",
    )
    el = document.getElementById("x")
    tree.layout(document.body, width=800.0, height=None, viewport_height=600.0)
    baseline = el.get_layout_box()
    expected = (baseline.x, baseline.y, baseline.width, baseline.height)

    for _ in range(20):
        el.classList.add("selected")
        tree.layout(document.body, width=800.0, height=None, viewport_height=600.0)
        selected_box = el.get_layout_box()
        assert (selected_box.x, selected_box.y, selected_box.width, selected_box.height) == expected

        el.classList.remove("selected")
        tree.layout(document.body, width=800.0, height=None, viewport_height=600.0)
        unselected_box = el.get_layout_box()
        assert (unselected_box.x, unselected_box.y, unselected_box.width, unselected_box.height) == expected


def test_supports_rule_applies_a_true_condition():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (display: grid) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(document.getElementById("test")).display == "grid"


def test_supports_rule_skips_a_false_condition():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (this-property-does-not-exist: 1) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(document.getElementById("test")).display != "grid"


def test_supports_rule_handles_not():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports not (this-property-does-not-exist: 1) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(document.getElementById("test")).display == "grid"


def test_supports_rule_handles_and():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    both_real = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (display: grid) and (display: flex) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(both_real.getElementById("test")).display == "grid"

    one_fake = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (display: grid) and (this-property-does-not-exist: 1) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(one_fake.getElementById("test")).display != "grid"


def test_supports_rule_handles_or():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration

    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (this-property-does-not-exist: 1) or (display: grid) { #test { display: grid; } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    assert ComputedStyleDeclaration(document.getElementById("test")).display == "grid"


def test_supports_rule_nested_inside_media_applies():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration
    from domonic.window import Window

    # `style_bridge.viewport(...)` is a chromonic-internal contextvar used
    # only to resolve `vw`/`vh` CSS units during *layout* -- domonic's own
    # cascade (`_collect_author_declarations`, what actually evaluates
    # `@media`) reads the viewport from the document's real `window.
    # innerWidth`/`innerHeight` instead, entirely unrelated to that
    # contextvar. `Window(doc=...).resizeTo(...)` is the real mechanism
    # (`browser.set_viewport` uses the same one for real pages).
    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@media (min-width: 500px) { @supports (display: grid) { #test { display: grid; } } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    Window(doc=document).resizeTo(1000, 800)
    assert ComputedStyleDeclaration(document.getElementById("test")).display == "grid"

    narrow = DOMParser().parseFromString(
        "<html><head><style>"
        "@media (min-width: 5000px) { @supports (display: grid) { #test { display: grid; } } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    Window(doc=narrow).resizeTo(1000, 800)
    assert ComputedStyleDeclaration(narrow.getElementById("test")).display != "grid"


def test_media_rule_nested_inside_supports_applies():
    from domonic.dom import DOMParser
    from domonic.style import ComputedStyleDeclaration
    from domonic.window import Window

    document = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (display: grid) { @media (min-width: 500px) { #test { display: grid; } } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    Window(doc=document).resizeTo(1000, 800)
    assert ComputedStyleDeclaration(document.getElementById("test")).display == "grid"

    unsupported = DOMParser().parseFromString(
        "<html><head><style>"
        "@supports (this-property-does-not-exist: 1) { @media (min-width: 500px) "
        "{ #test { display: grid; } } }"
        "</style></head><body><div id='test'></div></body></html>",
        "text/html",
    )
    Window(doc=unsupported).resizeTo(1000, 800)
    assert ComputedStyleDeclaration(unsupported.getElementById("test")).display != "grid"


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


def test_browser_load_applies_legacy_presentational_attributes(tmp_path):
    from domonic.style import ComputedStyleDeclaration

    from chromonic import browser, tree

    page_path = tmp_path / "legacy.html"
    page_path.write_text(
        "<!doctype html><html><body>"
        "<table id='t' width='85%' bgcolor='#f6f6ef'><tr>"
        "<td id='bar' bgcolor='#ff6600'>"
        "<img id='logo' src='y18.svg' width='18' height='18'>"
        "</td></tr></table></body></html>"
    )

    page = browser.load(page_path.as_uri())
    table = page.document.getElementById("t")
    bar = page.document.getElementById("bar")
    logo = page.document.getElementById("logo")

    assert ComputedStyleDeclaration(table).backgroundColor == "rgb(246, 246, 239)"
    assert ComputedStyleDeclaration(bar).backgroundColor == "rgb(255, 102, 0)"
    tree.layout(page.document.body, width=800.0)
    assert logo.get_layout_box().width == 18.0
    assert logo.get_layout_box().height == 18.0


def _await_image(browser_images, url, timeout=5.0):
    """`load_image()` is asynchronous (see `browser_images.py`'s module
    docstring) -- it returns `None` immediately and starts a background
    fetch. Tests that need the *result* poll `generation()` (or the cache
    directly) rather than assume the first call already has it."""
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if url in browser_images._cache:
            # `_cache[url]` is the internal `_CacheEntry` record (its own
            # `.width`/`.height` are plain ints, used for cache-size
            # accounting, not a `skia.Image`/animation frame) -- a cache
            # hit through `load_image()` itself already unwraps that to
            # the real decoded image (or the current animation frame),
            # exactly what a caller actually wants back here.
            return browser_images.load_image(url)
        if url in browser_images._failures:
            # A failed fetch is never added to `_cache` at all -- it's
            # recorded in the separate `_failures` dict instead (with its
            # own retry-backoff timing), so a permanently-failing URL
            # would otherwise never satisfy the check above and this
            # helper would just spin until `timeout` even though
            # `load_image()` has already resolved (to `None`).
            return None
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
    # A failed fetch is never added to `_cache` (only a successful decode
    # goes there) -- it's remembered in the separate `_failures` dict
    # instead, with its own retry-backoff timing (`_finish`'s own `else`
    # branch), so a request for the same URL doesn't hammer an
    # unreachable/broken source on every relayout.
    assert url in browser_images.__dict__["_failures"]


def test_load_image_decodes_a_data_uri():
    import base64

    from chromonic import browser_images

    browser_images.clear_cache()
    png = _tiny_png(skia.ColorGREEN, size=3)
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    image = browser_images.load_image(uri)
    assert image is not None
    assert (image.width(), image.height()) == (3, 3)


def test_load_image_decodes_an_svg_data_uri():
    from chromonic import browser_images

    browser_images.clear_cache()
    uri = (
        "data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' width='18' height='18'>"
        "<rect width='18' height='18' fill='%23ff6600'/></svg>"
    )
    image = browser_images.load_image(uri)

    assert image is not None
    assert (image.width(), image.height()) == (18, 18)


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




def test_text_transform_changes_layout_text_and_text_align_offsets_paint():
    root = div(
        p("welcome", _style="width:200px; text-transform:uppercase; text-align:center; color:rgb(0,0,0);"),
        _style="width:200px; background-color:rgb(255,255,255);",
    )
    tree.layout(root, width=200.0)
    child = root.childNodes[0]

    assert child._chromonic_text_lines == ["WELCOME"]
    assert child._chromonic_text_line_widths[0] < child.get_layout_box().client_width

    png = paint.render_png(root, width=200, height=40)
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    pixels = image.toarray()
    left_band_has_ink = bool((pixels[5:30, 0:25, :3] < 250).any())
    center_band_has_ink = bool((pixels[5:30, 70:130, :3] < 250).any())

    assert left_band_has_ink is False
    assert center_band_has_ink is True

def test_paint_draws_css_background_image(monkeypatch):
    from chromonic import browser_images

    browser_images.clear_cache()
    png_image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_tiny_png(skia.ColorBLUE, size=10)))
    seen = []

    def load_image(url):
        seen.append(url)
        return png_image

    monkeypatch.setattr(browser_images, "load_image", load_image)
    root = div(_style="width:20px; height:20px; background-image:url('assets/bg.png')")
    from domonic.dom import DOMImplementation
    document = DOMImplementation().createHTMLDocument("bg")
    document._chromonic_base_url = "https://example.com/pages/index.html"
    document.body.replaceWith(root)
    tree.layout(root, width=20.0, height=20.0)
    png = paint.render_png(root, width=20, height=20)
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    pixel = image.toarray()[5, 5]

    assert seen == ["https://example.com/pages/assets/bg.png"]
    assert pixel[2] > 200  # BGRA array, blue channel

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


def test_inline_display_var_fallback_participates_in_inline_text_layout():
    from domonic.html import h5, i

    icon = i("x", _style="display:var(--fa-display, inline-block); font-size:20px; margin-right:8px;")
    heading = h5(icon, "Media pipelines", _style="width:700px; font-size:20px;")

    tree.layout(heading, width=700.0)

    icon_box = icon.get_layout_box()
    heading_box = heading.get_layout_box()
    assert 0 < icon_box.width < 40
    assert icon_box.x == heading_box.x
    assert icon_box.y == heading_box.y


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
    # A *successful* small data: URI decodes synchronously (see the module
    # docstring) with no `_generation` bump at all -- but a *failed* one
    # (this URI's base64 payload is deliberately invalid) still falls
    # through to the same background pipeline every other failure uses
    # (`load_image`'s own comment: "let the normal background pipeline
    # record/cache a useful failure rather than giving synchronous data
    # URIs a separate error path"), so `_generation` does still advance
    # for it, just asynchronously -- not immediately inline, the way a
    # bare `generation() == before` right after the call would assume.
    bad_uri = "data:image/png;base64,not-valid-base64==="
    assert browser_images.load_image(bad_uri) is None
    assert _await_image(browser_images, bad_uri) is None

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
    # `poll_images()` no longer reads `browser_images.generation()` (or
    # calls `view.relayout()`) directly at all -- it diffs `events_since()`
    # against the generation *this view* last consumed, filters for events
    # that are (a) successful, (b) for a URL this page's own `<img>`
    # actually references, and (c) intrinsic-size-dependent, and only then
    # *schedules* a relayout via `request_relayout()` (coalesced, not
    # immediate -- `poll_deferred_work()`/the throttle test below cover
    # that separately). This test now drives `events_since()` directly
    # instead of the no-longer-consulted `generation()`.
    from chromonic import browser_images, native_browser
    from myjs import Page

    def loader(url):
        return Page('<html><body><img src="pic.png"></body></html>', run=False, css=False)

    view = native_browser.View(200, 200, loader=loader)
    view.navigate("https://example.com/")
    assert "pic.png" in view._page_image_urls

    scheduled = []
    monkeypatch.setattr(view, "request_relayout", lambda **kwargs: scheduled.append(1))

    state = {"generation": 0, "events": ()}
    monkeypatch.setattr(browser_images, "events_since", lambda _last: (state["generation"], state["events"]))

    view.poll_images()
    assert scheduled == []  # unchanged -- no relayout scheduled

    event = browser_images.ImageEvent(
        generation=1, url="pic.png", success=True, width=10, height=10,
        animated=False, encoded_bytes=64, fetch_ms=1.0, decode_ms=1.0,
    )
    state["generation"], state["events"] = 1, (event,)
    view.poll_images()
    assert scheduled == [1]  # changed, relevant, and intrinsic-size-dependent -- exactly one schedule

    view.poll_images()
    assert scheduled == [1]  # still 1 -- this view already consumed generation 1


def test_native_browser_view_throttles_a_burst_of_image_arrivals_into_one_relayout(monkeypatch):
    # the real regression this fixes: a page with many images completing
    # close together (a real thread pool draining a real queue) used to
    # relayout the *entire* tree once per arrival -- measured, a 40-image
    # page produced 34 separate relayouts for one burst, spiking CPU enough
    # to beachball the window. The throttling now lives entirely in
    # `request_relayout()`'s own deadline coalescing (shared with every
    # other relayout trigger, not image-specific): each `poll_images()`
    # call in the burst *schedules* one (pushing the same deadline out
    # further), but only one *actual* `relayout()` execution happens once
    # that deadline is finally reached via `poll_deferred_work()`.
    from chromonic import browser_images, native_browser
    from myjs import Page

    def loader(url):
        return Page('<html><body><img src="pic.png"></body></html>', run=False, css=False)

    view = native_browser.View(200, 200, loader=loader)
    view.navigate("https://example.com/")
    assert "pic.png" in view._page_image_urls

    relayouts = []
    monkeypatch.setattr(view, "relayout", lambda **kwargs: relayouts.append(1))

    state = {"generation": 0, "events": ()}
    monkeypatch.setattr(browser_images, "events_since", lambda _last: (state["generation"], state["events"]))

    # ten "arrivals" in immediate succession (no real time passing) --
    # simulating several images finishing within the same loop tick
    for i in range(10):
        state["generation"] = i + 1
        state["events"] = (
            browser_images.ImageEvent(
                generation=i + 1, url="pic.png", success=True, width=10, height=10,
                animated=False, encoded_bytes=64, fetch_ms=1.0, decode_ms=1.0,
            ),
        )
        view.poll_images()
    assert view.poll_deferred_work() is False  # still within the throttle window -- not due yet
    assert relayouts == []

    # advancing real time past the throttle window lets the coalesced
    # relayout fire, exactly once, regardless of how many arrivals fed it
    view._deferred_layout_at -= native_browser._IMAGE_RELAYOUT_INTERVAL + 0.01
    assert view.poll_deferred_work() is True
    assert len(relayouts) == 1


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
