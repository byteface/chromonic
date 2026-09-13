from harness.compare import compare_results


def _result(width):
    return {"fixture": "tiny.html", "elements": {"target": {
        "rect": {"x": 10, "y": 20, "width": width, "height": 40},
        "style": {},
    }}}


def test_geometry_tolerance_and_exact_delta():
    comparison = compare_results(_result(100), _result(99.4), tolerance=0.5)
    assert not comparison["passed"]
    assert comparison["geometry_mismatches"] == [{
        "element": "target", "field": "width", "chrome": 100.0,
        "ours": 99.4, "delta": -0.5999999999999943, "kind": "geometry",
    }]


def test_geometry_inside_tolerance_passes():
    assert compare_results(_result(100), _result(99.75), tolerance=0.5)["passed"]


def test_text_fragment_mismatch_fails_even_when_parent_rect_matches():
    chrome = _result(100)
    ours = _result(100)
    chrome["elements"]["target"]["fragments"] = {
        "element": [], "text": [{"x": 10, "y": 20, "width": 80, "height": 18}],
    }
    ours["elements"]["target"]["fragments"] = {
        "element": [], "text": [{"x": 10, "y": 20, "width": 60, "height": 18}],
    }
    comparison = compare_results(chrome, ours)
    assert not comparison["passed"]
    assert comparison["fragment_mismatches"][0]["field"] == "text[0].width"


def test_invalid_numeric_geometry_cannot_pass():
    assert not compare_results(_result(100), _result(float('nan')))['passed']


def test_different_viewports_are_rejected():
    import pytest
    chrome, ours = _result(100), _result(100)
    chrome['viewport'] = {'width': 800, 'height': 600}
    ours['viewport'] = {'width': 800, 'height': 599}
    with pytest.raises(ValueError, match='viewports'):
        compare_results(chrome, ours)


def test_native_capture_matches_root_selection_and_resolves_external_css(tmp_path):
    from harness.native_runner import run
    (tmp_path / 'style.css').write_text('#target {width:123px; height:45px}')
    fixture = tmp_path / 'fixture.html'
    fixture.write_text('<html><head><link rel="stylesheet" href="style.css"></head>'
                       '<body><main data-layout-root><div id="target"></div>'
                       '<span>unmarked descendant</span></main></body></html>')
    capture = run(fixture, tmp_path / 'out', tmp_path / 'screens' / 'ours.png')
    assert set(capture['elements']) == {'target'}
    assert capture['elements']['target']['rect']['width'] == 123
    assert capture['elements']['target']['rect']['height'] == 45


def test_different_screenshot_sizes_are_rejected(tmp_path):
    import pytest
    import skia
    from harness.compare import write_image_diff
    for name, width in [('chrome', 10), ('ours', 9)]:
        surface = skia.Surface(width, 10)
        (tmp_path / f'{name}.png').write_bytes(bytes(surface.makeImageSnapshot().encodeToData()))
    with pytest.raises(ValueError, match='dimensions'):
        write_image_diff(tmp_path / 'chrome.png', tmp_path / 'ours.png',
                         tmp_path / 'diff.png', tmp_path / 'overlay.png')


def test_difference_image_is_visible(tmp_path):
    import skia
    from harness.compare import write_image_diff
    for name, color in [('chrome', skia.ColorWHITE), ('ours', skia.ColorBLACK)]:
        surface = skia.Surface(2, 2)
        surface.getCanvas().clear(color)
        (tmp_path / f'{name}.png').write_bytes(bytes(surface.makeImageSnapshot().encodeToData()))
    stats = write_image_diff(tmp_path / 'chrome.png', tmp_path / 'ours.png',
                             tmp_path / 'diff.png', tmp_path / 'overlay.png')
    assert stats['changed_fraction'] == 1
    image = skia.Image.MakeFromEncoded((tmp_path / 'diff.png').read_bytes()).toarray()
    assert (image[:, :, 3] == 255).all()


def test_repeated_projection_keeps_inline_fragments_and_updates_line_height():
    import chromonic
    from myjs import Page
    from chromonic import tree, ua_style
    page = Page('<html><body><p style="font:16px Arial;line-height:24px">before '
                '<span>inside</span> after</p></body></html>', run=False)
    ua_style.apply(page.document)
    el = page.document.querySelector('p')
    projection = tree.LayoutProjection()
    projection.layout(page.document.body, width=300)
    first = [(f._layout_box.x, f._layout_box.y, f._layout_box.width)
             for f in el._chromonic_inline_fragments]
    assert len(first) == 3
    projection.layout(page.document.body, width=300)
    assert first == [(f._layout_box.x, f._layout_box.y, f._layout_box.width)
                     for f in el._chromonic_inline_fragments]
    el.style.lineHeight = '40px'
    projection.layout(page.document.body, width=300)
    assert el.get_layout_box().height == 40
    assert len(el._chromonic_inline_fragments) == 3


def test_preformatted_text_keeps_newline_and_range_break_fragment(tmp_path):
    from harness.native_runner import run
    fixture = tmp_path / 'pre.html'
    fixture.write_text('<html><body><pre id="code" data-layout>first\nsecond</pre></body></html>')
    capture = run(fixture, tmp_path / 'out', tmp_path / 'out' / 'ours.png')
    fragments = capture['elements']['code']['fragments']['text']
    assert len(fragments) == 3
    assert fragments[1]['width'] == 0
    assert fragments[2]['y'] > fragments[0]['y']
