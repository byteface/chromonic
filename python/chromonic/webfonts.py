"""Document-scoped @font-face downloads shared by Parley and Skia.

Workers fetch/decompress bytes only. poll() registers them on the layout
thread; private family aliases prevent fonts leaking between documents.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from itertools import count
from pathlib import Path
import re
import threading
import urllib.parse
import urllib.request

import skia
from fontTools import subset
from fontTools.ttLib import TTFont
from domonic.style import CSSFontFaceRule, CSSStyleSheet

from . import fonts

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='chromonic-font')
_ids = count()
_local = threading.local()
# A pathological author `unicode-range` (e.g. `U+0-10FFFF`) would expand to
# a huge literal codepoint set below -- past this many codepoints, treat the
# face as unrestricted instead (same practical effect: nothing gets
# subsetted out, so ordinary glyph-coverage fallback is all that applies,
# which is correct anyway for a range that wide).
_MAX_UNICODE_RANGE_CODEPOINTS = 20_000


def read_resource(url):
    """Return bytes and final URL, preserving redirects for relative CSS URLs."""
    if not urllib.parse.urlsplit(url).scheme:
        url = Path(url).resolve().as_uri()
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read(), response.url


_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s][^)]*?))\s*\)""", re.I)


def _urls(value):
    for match in _URL_RE.finditer(value or ''):
        yield next(group.strip() for group in match.groups() if group is not None)


def _download(urls):
    errors = []
    for url in urls:
        try:
            data, _ = read_resource(url)
            # Decode WOFF/WOFF2 once; both engines receive the same SFNT bytes.
            with TTFont(BytesIO(data)) as font:
                font.flavor = None
                out = BytesIO()
                font.save(out)
            return out.getvalue()
        except Exception as error:
            errors.append(f'{url}: {error}')
    raise ValueError('; '.join(errors) or 'no supported URL sources')


def _weight(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 700.0 if value == 'bold' else 400.0


_URANGE_TOKEN_RE = re.compile(r'U\+([0-9A-Fa-f?]{1,6})(?:-([0-9A-Fa-f]{1,6}))?', re.I)


def _parse_unicode_range(value: str) -> 'set[int] | None':
    """A `unicode-range` descriptor's own codepoints, expanded to a plain
    `set` -- `None` for no descriptor at all (an unrestricted face, same as
    the CSS initial value `U+0-10FFFF`) or one covering more than
    `_MAX_UNICODE_RANGE_CODEPOINTS` (treated the same way; see that
    constant). Handles all three real `<urange>` token shapes: a single
    codepoint (`U+41`), an explicit range (`U+0-24F`), and a `?` wildcard
    range (`U+4??`, every hex digit `0-F` in each `?` position)."""
    if not value or not value.strip():
        return None
    codepoints: set[int] = set()
    for match in _URANGE_TOKEN_RE.finditer(value):
        start_token, end_token = match.group(1), match.group(2)
        if '?' in start_token:
            try:
                start = int(start_token.replace('?', '0'), 16)
                end = int(start_token.replace('?', 'F'), 16)
            except ValueError:
                continue
        else:
            try:
                start = int(start_token, 16)
                end = int(end_token, 16) if end_token else start
            except ValueError:
                continue
        if end < start:
            continue
        if len(codepoints) + (end - start + 1) > _MAX_UNICODE_RANGE_CODEPOINTS:
            return None
        codepoints.update(range(start, end + 1))
    return codepoints or None


def _subset_to_range(sfnt_data: bytes, codepoints: 'set[int]') -> bytes:
    """`sfnt_data`, with every glyph outside `codepoints` (CSS `unicode-
    range`) removed -- not a performance optimisation (these are already-
    decoded, already-small web fonts), but the mechanism this project uses
    to get real `unicode-range` *semantics* at all: real font-fallback
    (Parley's own, matching every other real text shaper) tries the next
    family in a `font-family` list only when the current one's glyph
    lookup for a codepoint comes back empty -- which an *unsubsetted* font
    never does merely because CSS says it shouldn't be used there, since
    the font file itself may well contain (or synthesize) a glyph for
    every character in this test's alphabet regardless. Subsetting first
    makes "not in this face's `unicode-range`" and "this face has no
    glyph for it" the same real fact, so ordinary coverage-based fallback
    -- which chromonic already relies on for its whole multi-family
    `font-family` fallback chain -- does the rest correctly, unprompted.
    Falls back to the original, unsubsetted bytes on any subsetting
    failure (a corrupt/unusual font table set some fixture wasn't built
    to survive): still renders, just without the `unicode-range` split."""
    try:
        font = TTFont(BytesIO(sfnt_data))
        options = subset.Options()
        options.notdef_glyph = True
        options.notdef_outline = False
        options.recalc_bounds = False
        options.recalc_timestamp = False
        options.name_IDs = []
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=codepoints)
        subsetter.subset(font)
        out = BytesIO()
        font.save(out)
        return out.getvalue()
    except Exception:
        return sfnt_data


@dataclass
class Face:
    family: str
    weight: float
    italic: bool
    alias: str
    future: object
    unicode_range: 'set[int] | None' = None
    data: bytes | None = None
    failed: bool = False


class Registry:
    def __init__(self, sources):
        self.faces = []
        self.errors = []
        self.generation = 0
        for sheet, base_url in sources:
            for rule in _font_face_rules(sheet):
                style = rule.style
                if style is None:
                    continue
                family = (style.getPropertyValue('font-family') or '').strip().strip('\"\'')
                urls = [urllib.parse.urljoin(base_url, u) for u in _urls(style.getPropertyValue('src'))]
                if not family or not urls:
                    continue
                weight = _weight((style.getPropertyValue('font-weight') or '').strip())
                font_style = (style.getPropertyValue('font-style') or '').strip()
                unicode_range = _parse_unicode_range(style.getPropertyValue('unicode-range') or '')
                self.faces.append(Face(family, weight, font_style.startswith(('italic', 'oblique')),
                                       f'chromonic-webfont-{next(_ids)}', _executor.submit(_download, urls),
                                       unicode_range=unicode_range))

    def poll(self):
        """Install completed fonts on the calling layout thread. Return changed."""
        from ._native import register_font
        registered = getattr(_local, 'registered', None)
        if registered is None:
            registered = _local.registered = set()
        changed = False
        for face in self.faces:
            if face.failed or not face.future.done() or face.alias in registered:
                continue
            try:
                data = face.data or face.future.result()
                if face.unicode_range is not None:
                    data = _subset_to_range(data, face.unicode_range)
                typeface = skia.Typeface.MakeFromData(skia.Data.MakeWithCopy(data))
                if typeface is None:
                    raise ValueError('Skia rejected font')
                register_font(data, face.alias, face.weight, face.italic)
                fonts._web_typefaces[face.alias.lower()] = typeface
                face.data = data
                registered.add(face.alias)
                self.generation += 1
                changed = True
            except Exception as error:
                face.failed = True
                self.errors.append(f'{face.family}: {error}')
        return changed

    def pending(self):
        # A completed future still needs poll() to register it before a bounded
        # native-browser run may close its window.
        return any(face.data is None and not face.failed for face in self.faces)

    def family_list(self, value, weight, italic):
        """Select one face per CSS family, using the same alias for both engines."""
        result = []
        replaced = False
        for name in fonts.parse_family_list(value):
            candidates = [f for f in self.faces if f.data is not None and f.family.casefold() == name.casefold()]
            if candidates:
                # Prefer the requested slope, then CSS's weight search order.
                def rank(face):
                    w = face.weight
                    if 400 <= weight <= 500:
                        order = (0, w) if weight <= w <= 500 else ((1, -w) if w < weight else (2, w))
                    elif weight < 400:
                        order = (0, -w) if w <= weight else (1, w)
                    else:
                        order = (0, w) if w >= weight else (1, -w)
                    return (face.italic != italic, *order)
                name = min(candidates, key=rank).alias
                replaced = True
            result.append(name if name.lower() in fonts._GENERIC_FAMILIES else
                          '"' + name.replace('"', '\\"') + '"')
        return ', '.join(result) if replaced else value


def _font_face_rules(sheet_or_css):
    if isinstance(sheet_or_css, str):
        sheet = CSSStyleSheet()
        sheet.replaceSync(sheet_or_css)
    else:
        sheet = sheet_or_css
    for rule in getattr(sheet, 'cssRules', []) or []:
        if isinstance(rule, CSSFontFaceRule):
            yield rule
        else:
            yield from _font_face_rules(rule)


def _stylesheet_sources(document, base, external_sources=()):
    external = dict(external_sources)
    external_css = set(external)
    for css, href in external_sources:
        yield css, href
    for sheet in getattr(document, 'styleSheets', []) or []:
        if external_sources and not getattr(sheet, 'href', None):
            continue
        if str(sheet) in external_css:
            continue
        href = getattr(sheet, 'href', None) or base
        yield sheet, urllib.parse.urljoin(base, href)
    for el in document.getElementsByTagName('style'):
        css = el.textContent or ''
        if css in external_css:
            continue
        yield css, external.get(css, base)


def prepare(page, external_sources=()):
    """Collect @font-face rules from domonic CSSOM sheets and legacy inline CSS."""
    base = page.url
    base_elements = page.document.getElementsByTagName('base')
    if base_elements:
        base = urllib.parse.urljoin(base, base_elements[0].getAttribute('href') or '')
    sources = list(_stylesheet_sources(page.document, base, external_sources))
    page.document._chromonic_webfonts = Registry(sources)


def registry(element):
    doc = getattr(element, 'ownerDocument', None)
    return getattr(doc, '__dict__', {}).get('_chromonic_webfonts')


def prepare_layout(root):
    reg = registry(root)
    if reg is None:
        return False
    reg.poll()
    version = (id(reg), reg.generation)
    changed = root.__dict__.get('_chromonic_font_generation') != version
    root.__dict__['_chromonic_font_generation'] = version
    return changed


def resolve_style(element, style):
    reg = registry(element)
    if reg is not None:
        style['font_family'] = reg.family_list(style['font_family'], _weight(style['font_weight']),
                                               fonts.is_italic(style['font_style']))
