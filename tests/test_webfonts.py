"""Web fonts use identical in-memory faces for measurement and painting."""
from concurrent.futures import Future
import http.server
from io import BytesIO
import threading

import pytest
import skia
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

import chromonic
from chromonic import browser, fonts, paint, webfonts
from chromonic._native import layout_text
from chromonic.native_browser import View


def font_bytes(kind='ttf', advance=900):
    builder = FontBuilder(1000, isTTF=kind != 'otf')
    names = ['.notdef', 'A', 'space']
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({65: 'A', 32: 'space'})
    if kind == 'otf':
        glyphs = {}
        for name in names:
            pen = T2CharStringPen(advance, None)
            if name != 'space':
                pen.moveTo((50, 0)); pen.lineTo((400, 700)); pen.lineTo((750, 0)); pen.closePath()
            glyphs[name] = pen.getCharString()
        builder.setupCFF('ChromonicTest', {}, glyphs, {})
    else:
        glyphs = {}
        for name in names:
            pen = TTGlyphPen(None)
            if name != 'space':
                pen.moveTo((50, 0)); pen.lineTo((400, 700)); pen.lineTo((750, 0)); pen.closePath()
            glyphs[name] = pen.glyph()
        builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (advance, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'Internal Test Name', 'styleName': 'Regular',
                            'uniqueFontIdentifier': 'ChromonicTest', 'fullName': 'Chromonic Test',
                            'psName': 'ChromonicTest'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    if kind in ('woff', 'woff2'):
        builder.font.flavor = kind
    out = BytesIO(); builder.save(out)
    return out.getvalue()


def serve_directory(root):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize('kind', ['ttf', 'otf', 'woff', 'woff2'])
def test_external_stylesheet_relative_font_used_by_layout_and_paint(tmp_path, kind):
    css_dir = tmp_path / 'css'; css_dir.mkdir()
    (css_dir / f'face.{kind}').write_bytes(font_bytes(kind))
    (css_dir / 'style.css').write_text(
        f'@font-face {{font-family: LogoFont; src: url("face.{kind}");}} '
        '.logo {font-family: LogoFont; font-size: 100px; display:inline-block;}')
    page_path = tmp_path / 'index.html'
    page_path.write_text('<html><head><link rel="stylesheet" href="css/style.css"></head>'
                         '<body><span class="logo">AAAA</span></body></html>')
    page = browser.load(str(page_path))
    reg = webfonts.registry(page.document.body)
    for face in reg.faces:
        face.future.result(timeout=5)
    chromonic.layout(page.document.body, width=1000)
    assert not reg.errors
    assert len(reg.faces) == 1
    el = page.document.querySelector('.logo')
    family = el._chromonic_paint_style['font_family']
    assert reg.faces[0].alias in family
    # The fixture's internal name is different from its CSS family alias.
    assert layout_text('AAAA', family, 100)[0] == pytest.approx(360)
    tf = fonts.resolve_typeface(family)
    assert tf.getFamilyName() == 'Internal Test Name'
    assert paint._font(100, family=family).getTypeface().uniqueID() == tf.uniqueID()
    assert skia.Font(tf, 100).measureText('AAAA') == pytest.approx(360)
    assert el.get_layout_box().width == pytest.approx(360)
    assert chromonic.paint.render_png(page.document.body, width=1000, height=200).startswith(b'\x89PNG')


def test_http_stylesheet_relative_font_uses_domonic_scrape_css_attach(tmp_path):
    css_dir = tmp_path / 'css'; css_dir.mkdir()
    (css_dir / 'face.ttf').write_bytes(font_bytes())
    (css_dir / 'style.css').write_text(
        '@font-face {font-family: LogoFont; src: url("face.ttf");} '
        '.logo {font-family: LogoFont; font-size: 100px; display:inline-block;}')
    (tmp_path / 'index.html').write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="css/style.css"></head>'
        '<body><span class="logo">AAAA</span></body></html>')
    server = serve_directory(tmp_path)
    try:
        page = browser.load(f'http://127.0.0.1:{server.server_address[1]}/index.html')
        reg = webfonts.registry(page.document.body)
        assert page.document.defaultView is page.session.window
        assert len(reg.faces) == 1
        reg.faces[0].future.result(timeout=5)
        chromonic.layout(page.document.body, width=1000)
        assert not reg.errors
        assert page.document.querySelector('.logo').get_layout_box().width == pytest.approx(360)
    finally:
        server.shutdown()


def test_arrival_relayouts_native_view_and_document_aliases_do_not_leak(tmp_path):
    page_path = tmp_path / 'index.html'
    page_path.write_text('<html><body><span style="font-family:LogoFont; font-size:100px; '
                         'display:inline-block">AAAA</span></body></html>')
    page = browser.load(str(page_path))
    reg = webfonts.registry(page.document.body)
    future = Future()
    reg.faces.append(webfonts.Face('LogoFont', 400, False, 'chromonic-webfont-delayed-test', future))
    view = View()
    view.commit_page(page, str(page_path))
    el = page.document.querySelector('span')
    before = el.get_layout_box().width
    view.dirty = False
    future.set_result(font_bytes())
    view.poll_images()
    assert view.dirty
    assert el.get_layout_box().width == pytest.approx(360)
    assert before != el.get_layout_box().width
    assert not reg.errors
    other = browser.load(str(page_path))
    chromonic.layout(other.document.body, width=1000)
    assert 'chromonic-webfont' not in other.document.querySelector('span')._chromonic_paint_style['font_family']


def test_face_matching_and_bad_source_fallback(tmp_path):
    font = tmp_path / 'valid.ttf'; font.write_bytes(font_bytes())
    reg = webfonts.Registry([(f'''@font-face {{font-family: LogoFont;
        src: url("missing.ttf"), url("valid.ttf"); font-weight:700; font-style:italic;}}''',
        (tmp_path / 'style.css').as_uri())])
    reg.faces[0].future.result(timeout=5)
    assert reg.poll()
    regular = Future(); regular.set_result(font_bytes(advance=500))
    reg.faces.append(webfonts.Face('LogoFont', 400, False, 'chromonic-webfont-regular-test', regular))
    assert reg.poll()
    bold = reg.family_list('LogoFont, serif', 700, True)
    normal = reg.family_list('LogoFont, serif', 400, False)
    assert layout_text('AAAA', bold, 100, font_weight=700, italic=True)[0] == pytest.approx(360)
    assert layout_text('AAAA', normal, 100)[0] == pytest.approx(200)
    assert fonts.resolve_typeface(bold).uniqueID() != fonts.resolve_typeface(normal).uniqueID()
    broken = Future(); broken.set_exception(ValueError('broken font'))
    reg.faces.append(webfonts.Face('Broken', 400, False, 'broken', broken))
    assert not reg.poll()
    assert reg.errors
    assert reg.family_list('Broken, serif', 400, False) == 'Broken, serif'
