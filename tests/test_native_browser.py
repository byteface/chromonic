"""Direct browser controller tests; GPU/display smoke test is separate."""
import skia
import pytest
from types import SimpleNamespace

from myjs import Page
from chromonic import browser, paint, tree
from chromonic.native_browser import TOOLBAR, View
from chromonic import ua_style


def loader(url):
    return Page('''<html><body style="display:block;margin:0">
<div id="box" style="display:block;width:100%;height:600px;background-color:rgb(255,0,0)"></div>
<a href="/next" style="display:block;height:40px">Next</a>
</body></html>''', run=False)


def test_resize_reflows_document_and_updates_viewport():
    view = View(300, 200, loader=loader)
    assert view.navigate('https://example.com/')
    el = view.page.document.getElementById('box')
    assert el.get_layout_box().width == 300
    view.resize(500, 400)
    assert el.get_layout_box().width == 500
    assert view.page.session.window._own['innerWidth'] == 500
    assert view.page.session.window._own['innerHeight'] == 400 - TOOLBAR


def test_set_viewport_updates_domonic_attached_window_without_myjs_own_dict():
    class Window:
        def resizeTo(self, width, height):
            self.innerWidth = width
            self.innerHeight = height

    window = Window()
    page = SimpleNamespace(session=SimpleNamespace(window=window), document=SimpleNamespace(defaultView=window))

    browser.set_viewport(page, 640, 480)

    assert window.innerWidth == 640
    assert window.innerHeight == 480


def test_ua_form_controls_keep_intrinsic_sizes_in_one_inline_run():
    page = Page('<html><body><main><h1>Controls</h1><button id="b">Button</button><input id="i"></main></body></html>', run=False)
    ua_style.apply(page.document)
    tree.LayoutProjection().layout(page.document.body, width=800, viewport_height=600)
    button = page.document.getElementById('b').get_layout_box()
    input_box = page.document.getElementById('i').get_layout_box()
    assert button.width == pytest.approx(54.546875, abs=0.01)
    assert button.height == pytest.approx(21.0, abs=0.01)
    assert (input_box.width, input_box.height) == pytest.approx((153.0, 21.0), abs=0.01)
    assert input_box.x == pytest.approx(button.x + button.width)
    assert input_box.y == pytest.approx(button.y)


def test_repeated_fractional_grid_tracks_do_not_expand_past_container():
    page = Page('''<html><body style="margin:0"><div id="grid" style="display:grid;width:740px;grid-template-columns:repeat(3,1fr);gap:18px">
<article id="a"><span>one</span></article><article id="b"><span>two</span></article><article id="c"><span>three</span></article>
</div></body></html>''', run=False)
    ua_style.apply(page.document)
    tree.LayoutProjection().layout(page.document.body, width=800, viewport_height=600)
    boxes = [page.document.getElementById(name).get_layout_box() for name in ('a', 'b', 'c')]
    assert [box.width for box in boxes] == pytest.approx([704 / 3] * 3, abs=0.001)
    assert boxes[-1].x + boxes[-1].width == pytest.approx(740.0)


def test_basic_table_rows_project_cells_horizontally():
    page = Page('''<html><body style="margin:0"><table id="table" style="display:block;width:360px">
<tbody><tr><td id="a" style="padding:7px;border:1px solid">A</td><td id="b" style="padding:7px;border:1px solid">B</td></tr>
<tr><td id="c" style="padding:7px;border:1px solid">C</td><td id="d" style="padding:7px;border:1px solid">D</td></tr></tbody></table></body></html>''', run=False)
    ua_style.apply(page.document)
    tree.LayoutProjection().layout(page.document.body, width=800, viewport_height=600)
    a, b, c, d = [page.document.getElementById(name).get_layout_box() for name in ('a', 'b', 'c', 'd')]
    assert (a.x, a.width, b.x, b.width) == (0.0, 180.0, 180.0, 180.0)
    assert a.y == b.y
    assert c.y == d.y == a.y + a.height


def test_settled_glfw_size_is_applied_before_the_next_draw():
    from chromonic.native_browser import sync_window_size

    view = View(300, 200, loader=loader)
    assert view.navigate('https://example.com/')
    box = view.page.document.getElementById('box')

    class FakeGlfw:
        @staticmethod
        def get_window_size(_window):
            return (640, 360)

    assert sync_window_size(view, object(), FakeGlfw)
    assert (view.width, view.height) == (640, 360)
    assert box.get_layout_box().width == 640
    assert not sync_window_size(view, object(), FakeGlfw)


def test_scroll_and_paint_do_not_layout_or_encode_png(monkeypatch):
    view = View(300, 200, loader=loader)
    view.navigate('https://example.com/')
    def forbidden(*a, **kw):
        raise AssertionError('Unexpected layout or PNG encoding')
    monkeypatch.setattr(tree, 'layout', forbidden)
    monkeypatch.setattr(paint, 'render_png', forbidden)
    view.scroll(90)
    assert view.scroll_y == 90
    surface = skia.Surface(300, 200)
    view.draw(surface.getCanvas())
    assert not view.dirty
    view.scroll(10000)
    assert view.scroll_y == view.content_height - view.viewport_height
    view.scroll(-10000)
    assert view.scroll_y == 0


def test_scrolled_link_hit_testing_and_history():
    calls = []
    def load(url):
        calls.append(url)
        return loader(url)
    view = View(300, 200, loader=load)
    view.navigate('https://example.com/')
    anchor = view.page.document.getElementsByTagName('a')[0]
    box = anchor.get_layout_box()
    view.scroll(10000)
    view.click(box.x + 2, box.y - view.scroll_y + TOOLBAR + 2)
    assert view.url == 'https://example.com/next'
    assert len(calls) == 2
    assert view.scroll_y == 0
    assert view.back()
    assert view.url == 'https://example.com/'
    assert not view.back()


def test_navigation_failure_preserves_page_and_history():
    view = View(loader=loader)
    view.navigate('https://example.com/')
    page = view.page
    assert not view.navigate('file:///etc/passwd')
    assert view.page is page
    assert view.history == ['https://example.com/']
    assert view.status
    def fail(url):
        raise OSError('offline')
    view.loader = fail
    assert not view.navigate('https://example.com/next')
    assert view.page is page and view.status == 'offline'


def test_address_selection_and_unicode_input():
    view = View(loader=loader)
    view.navigate('https://example.com/')
    view.click(100, 15)
    view.type_text('https://example.org/')
    view.type_text('café')
    assert view.address == 'https://example.org/café'
    assert view.url == 'https://example.com/'


def test_fragment_navigation_scrolls_without_fetch():
    calls = []
    def load(url):
        calls.append(url)
        return Page('''<html><body style="margin:0;display:block">
<a href="#target" style="display:block;height:40px">Jump</a>
<div style="display:block;height:500px"></div>
<div id="target" style="display:block;height:300px">Here</div>
</body></html>''', run=False)
    view = View(300, 200, loader=load)
    view.navigate('https://example.com/')
    view.click(5, TOOLBAR + 5)
    assert len(calls) == 1
    assert view.scroll_y > 0


def test_click_listener_mutation_updates_geometry():
    view = View(300, 200, loader=loader)
    view.navigate('https://example.com/')
    el = view.page.document.getElementById('box')
    el.addEventListener('click', lambda event: el.setAttribute('style', 'width:100px;height:600px'))
    view.click(5, TOOLBAR + 5)
    assert el.get_layout_box().width == 100


def test_address_caret_insert_delete_and_escape():
    view = View(loader=loader)
    view.navigate('https://example.com/')
    view.focus_address()
    view.type_text('abcd')
    view.edit_key('left')
    view.type_text('X')
    assert view.address == 'abcXd'
    view.edit_key('backspace')
    view.edit_key('delete')
    assert view.address == 'abc'
    view.edit_key('home')
    view.type_text('é')
    assert view.address == 'éabc'
    view.edit_key('end')
    view.type_text('z')
    assert view.address == 'éabcz'
    view.edit_key('escape')
    assert view.address == view.url and not view.editing


class _ManualExecutor:
    def __init__(self):
        self.futures = []

    def submit(self, fn, url):
        from concurrent.futures import Future
        future = Future()
        future.set_running_or_notify_cancel()
        self.futures.append(future)
        return future

    def shutdown(self, **kw):
        pass


def test_navigation_does_not_block_typing_or_commit_stale_loads():
    from chromonic.native_browser import Navigation
    view = View(loader=loader)
    executor = _ManualExecutor()
    navigation = Navigation(view, executor)
    assert view.navigate('https://example.com/old')
    view.focus_address()
    view.type_text('https://example.com/new')
    navigation.poll()  # unfinished work must not wait or swallow input
    assert view.address.endswith('/new')
    assert view.navigate(view.address)
    executor.futures[0].set_result(loader('old'))
    navigation.poll()
    assert view.page is None
    executor.futures[1].set_result(loader('new'))
    navigation.poll()
    assert view.url.endswith('/new')
    assert len(view.history) == 1
    navigation.close()


def test_page_arrival_preserves_in_progress_address_edit():
    from chromonic.native_browser import Navigation
    view = View(loader=loader)
    executor = _ManualExecutor()
    navigation = Navigation(view, executor)
    view.navigate('https://example.com/')
    view.focus_address()
    view.type_text('in progress')
    executor.futures[0].set_result(loader('page'))
    navigation.poll()
    assert view.page is not None
    assert view.editing and view.address == 'in progress'
    assert view.caret == len('in progress')


def test_failed_background_load_keeps_current_document():
    from chromonic.native_browser import Navigation
    view = View(loader=loader)
    view.navigate('https://example.com/')
    page = view.page
    executor = _ManualExecutor()
    navigation = Navigation(view, executor)
    view.navigate('https://example.com/bad')
    executor.futures[0].set_exception(OSError('offline'))
    navigation.poll()
    assert view.page is page
    assert view.status == 'offline'


def test_display_list_culls_offscreen_elements_without_relayout(monkeypatch):
    import skia
    from chromonic import paint, tree

    html = '<html><body style="margin:0;display:block">' + ''.join(
        f'<p style="display:block;height:30px;margin:0">row {i}</p>'
        for i in range(200)
    ) + '</body></html>'
    view = View(300, 200, loader=lambda _url: Page(html, run=False))
    assert view.navigate('https://example.com/')
    assert len(view.display_list) == 201

    monkeypatch.setattr(tree, 'layout', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('scroll painting must not relayout')))
    calls = []
    original = paint.paint_element
    monkeypatch.setattr(paint, 'paint_element', lambda canvas, element, box=None: (
        calls.append(element), original(canvas, element))[1])
    view.scroll(3000)
    view.draw(skia.Surface(300, 200).getCanvas())

    assert 0 < view.last_painted_elements < 12
    assert len(calls) == view.last_painted_elements


def test_display_list_tests_children_independently_of_parent_box():
    import skia
    from domonic.html import div
    from domonic.layout import LayoutBox
    from chromonic import paint

    parent = div(div('visible'))
    child = parent.childNodes[0]
    parent.set_layout_box(LayoutBox(x=0, y=-100, width=20, height=10))
    child.set_layout_box(LayoutBox(x=0, y=10, width=20, height=10))
    parent._chromonic_paint_style = child._chromonic_paint_style = {
        'background_color': 'transparent', 'border_top_color': 'black',
        'color': 'black', 'font_size': '16px', 'font_weight': '400',
        'font_style': 'normal', 'font_family': 'sans-serif',
    }
    display = paint.build_display_list(parent)
    assert display == [parent, child]
    assert paint.paint_display_list(
        skia.Surface(50, 50).getCanvas(), display, top=0, bottom=50,
    ) == 1


def test_relayout_retains_native_nodes_and_updates_dirty_style_and_measurement():
    page = Page('''<html><body style="display:block;margin:0">
<p id="text" style="display:block;width:80px;font-size:12px">short text</p>
</body></html>''', run=False)
    view = View(300, 200, loader=lambda _url: page)
    assert view.navigate('https://example.com/')
    body = page.document.body
    text = page.document.getElementById('text')
    projection = view.layout_projection
    first_nodes = {id(body): projection.nodes[id(body)], id(text): projection.nodes[id(text)]}
    first_height = text.get_layout_box().height

    text.setAttribute('style', 'display:block;width:40px;font-size:24px')
    text.textContent = 'longer text that must wrap onto several lines'
    view.relayout()

    assert projection.nodes[id(body)] == first_nodes[id(body)]
    assert projection.nodes[id(text)] == first_nodes[id(text)]
    assert text.get_layout_box().height > first_height


def test_body_rect_excludes_collapsed_edge_margins_but_scroll_extent_keeps_them():
    page = Page('''<html><body style="display:block;margin:0">
<div id="first" style="display:block;height:40px;margin:10px 20px"></div>
<div id="second" style="display:block;height:40px;margin:10px 20px"></div>
</body></html>''', run=False)
    view = View(800, 600, loader=lambda _url: page)
    assert view.navigate('https://example.com/')

    body = page.document.body.get_layout_box()
    first = page.document.getElementById('first').get_layout_box()
    second = page.document.getElementById('second').get_layout_box()
    assert (body.x, body.y, body.width, body.height) == (0.0, 10.0, 800.0, 90.0)
    assert (first.x, first.y, first.width, first.height) == (20.0, 10.0, 760.0, 40.0)
    assert (second.x, second.y, second.width, second.height) == (20.0, 60.0, 760.0, 40.0)
    assert view.content_height == 110.0


def test_projection_reconciles_domonic_structure_without_replacing_survivors():
    from domonic.html import div

    page = Page('<html><body style="display:block"><div id="keep">keep</div></body></html>', run=False)
    view = View(300, 200, loader=lambda _url: page)
    assert view.navigate('https://example.com/')
    body = page.document.body
    keep = page.document.getElementById('keep')
    projection = view.layout_projection
    body_node = projection.nodes[id(body)]
    keep_node = projection.nodes[id(keep)]

    added = div('added', _id='added', style='display:block;height:25px')
    body.appendChild(added)
    view.relayout()
    added_node = projection.nodes[id(added)]
    assert projection.nodes[id(body)] == body_node
    assert projection.nodes[id(keep)] == keep_node

    body.removeChild(keep)
    view.relayout()
    assert id(keep) not in projection.nodes
    assert projection.nodes[id(body)] == body_node
    assert projection.nodes[id(added)] == added_node


def test_projection_detects_in_place_cached_native_style_changes():
    page = Page('<html><body style="display:block"><div id="box" style="display:block;width:20px;height:10px"></div></body></html>', run=False)
    view = View(300, 200, loader=lambda _url: page)
    assert view.navigate('https://example.com/')
    box = page.document.getElementById('box')
    node = view.layout_projection.nodes[id(box)]

    box._chromonic_native_style['width'] = 125.0
    view.relayout(reuse_styles=True)

    assert view.layout_projection.nodes[id(box)] == node
    assert box.get_layout_box().width == 125
