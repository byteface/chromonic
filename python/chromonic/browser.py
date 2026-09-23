"""A simple, navigable browser on top of chromonic.

Fetch/parse/CSS handling lives here. The actual OS window is provided by
native_browser.py using GLFW + Skia directly -- no pywebview, HTML host,
JS bridge, PNG transport, or base64 frame swapping.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import mimetypes
import re
import sys
from types import SimpleNamespace
import urllib.parse
from pathlib import Path
import urllib.request

import domonic.style  # noqa: F401 -- ensures the real submodule is registered in `sys.modules`

_log = logging.getLogger(__name__)

from . import (
    domonic_ch_unit_patch,
    domonic_ex_unit_patch,
    # Domonic 1.8.4 expands var() in `_font_size_px` itself. This remaining
    # patch only supplies the browser-specific monospace default-size rule.
    domonic_monospace_font_size_patch,
    domonic_font_size_keywords_patch,
    domonic_logical_size_patch,
    hittest,
    netlog,
    tree,
    window,
)

netlog.install()

# Presentational-attribute hints (`bgcolor`, an `<img>`'s `width`/`height`,
# ...) used to be folded into the cascade via `domonic_presentational_hint_
# patch`, which monkeypatched `ComputedStyleDeclaration._collect_author_
# declarations` -- a private method, and a previous version of that same
# patch already silently regressed twice when domonic rewrote `_resolve`
# out from under it. Domonic 1.8.4 added a real extension point for exactly
# this (`set_presentational_hint_resolver`), consulted natively inside
# `_collect_author_declarations` at the correct cascade priority (weaker
# than any author rule, stronger than the initial value) -- so registering
# through it instead means this can never drift out of sync with `_resolve`
# again. `_apply_presentational_attributes` below still populates the same
# `element._chromonic_presentational_hints` dict; this just hands it to
# domonic through the supported channel instead of a monkeypatch.
#
# `domonic.style` (plain attribute access) is *not* the submodule here --
# `domonic/__init__.py` does `from domonic.html import ..., style, ...`
# (the `<style>` tag class), which shadows the real `domonic.style`
# submodule on the package object itself, the same "only a `sys.modules`
# lookup by dotted name reaches the real submodule" caveat every
# `domonic_*_patch` module in this package already works around. Plain
# `domonic.style.set_presentational_hint_resolver(...)` here resolved to
# the `<style>` tag class instead and broke every import of chromonic
# outright (`AttributeError: type object 'style' has no attribute
# 'set_presentational_hint_resolver'`).
sys.modules["domonic.style"].set_presentational_hint_resolver(
    lambda element: getattr(element, "_chromonic_presentational_hints", None)
)


def _is_url(s: str) -> bool:
    return (
        isinstance(s, str)
        and s.split(":", 1)[0].lower() in ("http", "https")
    )


def _is_internal_url(s: str) -> bool:
    """A `chromonic://...` page -- the built-in start/settings page `homepage.py`
    generates on the fly, never fetched or read from disk."""
    return isinstance(s, str) and s.split(":", 1)[0].lower() == "chromonic"


def _normalize_address(value: str) -> str:
    """Normalize common browser address-bar shorthand."""
    value = (value or "").strip()

    if not value:
        return value

    if value.startswith("//"):
        return "https:" + value

    if value.startswith(":") and value[1:].isdigit():
        return "http://127.0.0.1" + value

    lowered = value.lower()

    # An absolute/relative filesystem path or an explicit `file:` URI --
    # left alone rather than falling through to the "looks like a domain"
    # branch below, which would otherwise mangle e.g. `/Users/x/photo.png`
    # into `https:///Users/x/photo.png` (it has a dot and no space, so it
    # matched that check too) instead of loading the local file.
    if value.startswith(("/", "~/", "./", "../")) or lowered.startswith("file:"):
        return value

    if lowered.startswith(
        (
            "localhost:",
            "127.0.0.1:",
            "0.0.0.0:",
            "[::1]:",
        )
    ):
        return "http://" + value

    # A typo'd single slash (`http:/example.com`, missing the second `/`)
    # parses as scheme="http", netloc="" -- indistinguishable, to the
    # `parsed.scheme in (...) and parsed.netloc` check below, from a bare
    # host string that never had a scheme at all. Falling through to the
    # generic "add a scheme" branch then prepends a *second* scheme in
    # front of the one already there (`https://http:/example.com`), not
    # fixing the missing slash. Repaired directly here, before that check,
    # by inserting the missing slash rather than layering another scheme
    # on top -- found happening twice to a real user typing quickly in the
    # address bar.
    typo_match = re.match(r"^(https?):/(?!/)(.+)$", value, re.I)
    if typo_match:
        return f"{typo_match.group(1)}://{typo_match.group(2)}"

    parsed = urllib.parse.urlparse(value)

    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value

    # An internal `chromonic://` page (the settings form's own "save"
    # action carries the new homepage URL, dots and all, in its query
    # string) -- left alone rather than falling into the "looks like a
    # domain" heuristic below, which would otherwise see that embedded
    # dot and prepend a *second*, wrong scheme
    # (`https://chromonic://save-settings?...`).
    if parsed.scheme.lower() == "chromonic":
        return value

    if "." in value and " " not in value:
        return "https://" + value

    return value


def _local_path(value: str) -> Path:
    """Convert a plain filename or file:// URI to a filesystem path."""
    parsed = urllib.parse.urlparse(value)

    if parsed.scheme.lower() != "file":
        return Path(value).expanduser().resolve()

    path = urllib.request.url2pathname(parsed.path)

    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"

    return Path(path).expanduser().resolve()


def _validate_navigable(url: str) -> None:
    """Allow an absolute http(s) URL, or a reference to a local file: a
    `file:` URI, a plain/`~`/relative filesystem path (what dropping a
    file onto the window, or typing its path into the address bar, produces
    after `_normalize_address` leaves it alone -- see its own docstring), or
    a `chromonic://` internal page. Any other scheme (`javascript:`,
    `data:`, `ftp:`, ...) is rejected as a navigation target, the same as
    before this allowed local files too."""
    if _is_url(url) or _is_internal_url(url):
        return
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("", "file"):
        return
    raise ValueError(
        "chromonic's browser only navigates absolute http(s) URLs or a local file, "
        f"got: {url!r}"
    )


def warm_interpreter() -> None:
    """Pay myjs' one-time interpreter setup cost at startup."""
    from myjs import Page

    Page(
        "<html></html>",
        run=False,
        css=False,
    )


def _apply_presentational_attributes(document) -> None:
    """Translate the old HTML attributes real legacy pages still use.

    Hacker News is the canonical small repro: the orange bar is
    ``<td bgcolor="#ff6600">`` and the logo is an SVG ``<img>`` whose layout
    size comes from ``width``/``height`` attributes. domonic exposes those as
    attributes, not computed CSS, so normalize the narrow set Chromonic needs
    before resolving styles.

    Recorded as ``element._chromonic_presentational_hints`` -- a plain
    ``{property: value}`` dict handed to domonic's ``set_presentational_
    hint_resolver`` (registered above) -- rather than written into the
    element's real ``style=""`` text. A presentational attribute is the
    *weakest* possible declaration
    per the real CSS cascade (conceptually the first rule of the document),
    so it must lose to *any* later author stylesheet rule for the same
    property regardless of specificity; real inline ``style=""`` text does
    not work that way (it beats every non-``!important`` author rule
    outright), so writing into it made a legacy ``width="85%"``-style
    attribute far stronger than real browsers ever make it. Confirmed
    directly on ``news.ycombinator.com``: `#hnmain`'s ``width="85%"``
    attribute was beating a real, later, higher-priority ``#hnmain {
    width: 100% }`` inside an author media query, rendering the whole
    table (and everything inside it) ~110px too narrow.
    """

    def length(value):
        value = (value or "").strip()
        if not value:
            return None
        if value.endswith("%"):
            return value
        try:
            float(value)
        except ValueError:
            return value
        return f"{value}px"

    # Every hint is a physical *longhand*: `_collect_author_declarations`
    # already has fully expanded longhands by the point it folds hints in,
    # and a hint is only merged for a property name with no declaration at
    # all -- a shorthand hint (`padding`) would never be blocked by an
    # author `padding-left`, nor expanded downstream.
    hints_by_id: dict = {}

    def hint(element, name, value):
        entry = hints_by_id.get(id(element))
        if entry is None:
            entry = hints_by_id[id(element)] = (element, {})
        entry[1][name] = value

    def nearest_table(element):
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and (getattr(ancestor, "tagName", "") or "").lower() != "table":
            ancestor = getattr(ancestor, "parentElement", None)
        return ancestor

    def attribute(element, name):
        value = element.getAttribute(name)
        return (value or "").strip().lower() if value is not None else None

    sides = ("top", "right", "bottom", "left")
    valigns = {"top", "middle", "bottom", "baseline"}
    aligns = {"left", "center", "right", "justify"}
    for element in document.getElementsByTagName("*"):
        tag = (getattr(element, "tagName", "") or "").lower()
        bgcolor = element.getAttribute("bgcolor")
        if bgcolor:
            hint(element, "background-color", bgcolor)
        if tag in {"img", "table", "td", "th"}:
            width = length(element.getAttribute("width"))
            height = length(element.getAttribute("height"))
            if width:
                hint(element, "width", width)
            if height:
                hint(element, "height", height)
        # HTML's own table rendering defaults (WHATWG HTML §15.3.11: `td, th
        # { padding: 1px; vertical-align: inherit }`, `tr/thead/tbody/tfoot
        # { vertical-align: middle }`, `table { border-spacing: 2px }`, `th
        # { text-align: center }`) live *here* as hints rather than in
        # `ua_style.py`, because the legacy attributes that override them
        # (`cellpadding`/`cellspacing`/`valign`/`align`) are themselves
        # hints, and a hint can't beat a UA-stylesheet rule in this
        # project's cascade (see `set_presentational_hint_resolver` above)
        # -- as hints, the attribute simply replaces the default, and either one
        # still loses to any real author rule, exactly as in a real UA.
        if tag in {"td", "th"}:
            table = nearest_table(element)
            cellpadding = length(table.getAttribute("cellpadding")) if table is not None else None
            for side in sides:
                hint(element, f"padding-{side}", cellpadding or "1px")
            valign = attribute(element, "valign")
            hint(element, "vertical-align", valign if valign in valigns else "inherit")
            align = attribute(element, "align")
            if align in aligns:
                hint(element, "text-align", align)
            elif tag == "th":
                hint(element, "text-align", "center")
            if element.getAttribute("nowrap") is not None:
                hint(element, "white-space", "nowrap")
        elif tag in {"tr", "thead", "tbody", "tfoot"}:
            valign = attribute(element, "valign")
            hint(element, "vertical-align", valign if valign in valigns else "middle")
            align = attribute(element, "align")
            if align in aligns:
                hint(element, "text-align", align)
        elif tag == "table":
            cellspacing = length(element.getAttribute("cellspacing"))
            hint(element, "border-spacing", cellspacing or "2px")
            if attribute(element, "align") == "center":
                hint(element, "margin-left", "auto")
                hint(element, "margin-right", "auto")
            rules = attribute(element, "rules")
            if rules in ("none", "groups", "rows", "cols", "all"):
                # HTML's `rules` attribute (rendering section): the table
                # collapses its borders, its own frame is hidden unless a
                # `frame` attribute says otherwise, and every cell gets 1px
                # solid rules on the sides the value names (table-columns-
                # example-001.xht: `rules="cols"` draws the lines between
                # columns only -- the outer cells stay 34.5px, no edge line).
                hint(element, "border-collapse", "collapse")
                if element.getAttribute("frame") is None:
                    for side in sides:
                        hint(element, f"border-{side}-style", "hidden")
                rule_sides = {"rows": ("top", "bottom"), "cols": ("left", "right"),
                              "all": tuple(sides), "none": (), "groups": ()}[rules]
                for cell_tag in ("td", "th"):
                    for cell in element.getElementsByTagName(cell_tag):
                        if nearest_table(cell) is element:
                            for side in rule_sides:
                                hint(cell, f"border-{side}-width", "1px")
                                hint(cell, f"border-{side}-style", "solid")
            border = element.getAttribute("border")
            if border is not None:
                # `<table border>`/`border="N"`: an N px outset frame on the
                # table and a 1px inset border on each of its own cells --
                # a bare/unparsable value means 1, `0` means no frame.
                try:
                    frame = max(0, int(float(border.strip()))) if border.strip() else 1
                except ValueError:
                    frame = 1
                for side in sides:
                    hint(element, f"border-{side}-width", f"{frame}px")
                    hint(element, f"border-{side}-style", "outset")
                if frame > 0:
                    for cell_tag in ("td", "th"):
                        for cell in element.getElementsByTagName(cell_tag):
                            if nearest_table(cell) is element:
                                for side in sides:
                                    hint(cell, f"border-{side}-width", "1px")
                                    hint(cell, f"border-{side}-style", "inset")
    for element, hints in hints_by_id.values():
        element._chromonic_presentational_hints = hints


def _synthetic_local_page(path: Path) -> "tuple[str, str] | None":
    """A minimal HTML wrapper for a local file that isn't itself HTML, plus
    what `page.source` (view-source, F8) should show for it -- or `None` to
    fall through to parsing `path`'s own content as HTML, unchanged.

    A real browser doesn't try to parse a dropped/navigated-to image or
    plain-text file as markup; it synthesizes a trivial document around it
    instead (an image viewer page for the former, a monospace `<pre>` for
    the latter). `_load_local` needs the same distinction -- without it, an
    image's binary bytes either fail UTF-8 decoding outright or, for a
    plain-text file, render unstyled and with its whitespace/newlines
    collapsed by ordinary HTML flow instead of preserved.

    Detection is by extension only (`mimetypes.guess_type`) -- deliberately
    no content-sniffing; an unrecognized or missing extension keeps today's
    behavior (parse as HTML) rather than guessing further."""
    guessed, _encoding = mimetypes.guess_type(str(path))
    if guessed is None or guessed == "text/html":
        return None
    title = html.escape(path.name)
    if guessed.startswith("image/"):
        return (
            f"<!DOCTYPE html><html><head><title>{title}</title></head>"
            f'<body style="margin:0"><img src="{html.escape(path.resolve().as_uri())}"></body></html>',
            None,
        )
    if guessed.startswith("text/"):
        text = path.read_text(encoding="utf-8", errors="replace")
        wrapped = (
            f"<!DOCTYPE html><html><head><title>{title}</title></head>"
            f'<body style="margin:8px"><pre style="font-family:monospace;'
            f'white-space:pre-wrap;word-wrap:break-word;margin:0">'
            f"{html.escape(text)}</pre></body></html>"
        )
        return (wrapped, text)
    return None


def _load_local(url: str):
    from myjs import Page
    from myjs._engine import JSError

    from . import webfonts

    class FontAwarePage(Page):
        def _read_resource(self, href):
            data, final_url = webfonts.read_resource(
                self._resolve(href)
            )

            css = data.decode(
                "utf-8-sig",
                errors="replace",
            )

            self.__dict__.setdefault(
                "_font_stylesheet_sources",
                [],
            ).append(
                (css, final_url)
            )

            return css

        def _apply_stylesheets(self):
            """myjs >=0.0.5 rewrote `Page._apply_stylesheets` to fetch every
            `<link rel=stylesheet>` through its own inlined, concurrent
            fetch helper (`_batch_fetch_text`/`Path.read_text`) instead of
            `self._read_resource` -- a real behaviour change for this
            subclass specifically: `_read_resource`'s own side effect
            (appending every stylesheet's text to `_font_stylesheet_sources`,
            which `browser.load()` later hands to `webfonts.prepare()` to
            discover `@font-face` rules) silently stopped firing for any
            *external* stylesheet the moment myjs was bumped past 0.0.4,
            even though 0.0.4's identical-looking method still worked fine.
            Overridden back to the simpler, sequential 0.0.4 behaviour
            (call `self._read_resource(href)` per link) so font discovery
            keeps working regardless of which myjs version is installed --
            trades away 0.0.5's concurrent-fetch speedup for stylesheets,
            not a correctness concern for this project's own fixture-sized
            pages."""
            for link in list(self.document.getElementsByTagName("link")):
                rel = (link.getAttribute("rel") or "").strip().lower()
                href = link.getAttribute("href")
                if "stylesheet" not in rel.split() or not href:
                    continue
                try:
                    css = self._read_resource(href)
                except Exception as exc:  # noqa: BLE001 -- a bad sheet must not abort the page
                    self.errors.append(JSError(f"failed to load stylesheet {href!r}: {exc}",
                                               name="NetworkError"))
                    continue
                try:
                    style_el = self.document.createElement("style")
                    style_el.textContent = css
                    head = (self.document.getElementsByTagName("head") or [None])[0]
                    (head or self.document.documentElement or self.document).appendChild(style_el)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(JSError(f"failed to apply stylesheet {href!r}: {exc}",
                                               name="Error"))

    source = _local_path(url)
    synthetic = _synthetic_local_page(source)

    if synthetic is not None:
        wrapped_html, view_source_text = synthetic
        page = FontAwarePage(wrapped_html, base_dir=source.parent, url=source.as_uri(), run=False)
        page.source = view_source_text
        return page

    page = FontAwarePage.load(
        source,
        run=False,
    )

    # Keep a URL-shaped base for relative CSS/images/fonts.
    page.url = source.as_uri()
    # The raw bytes as fetched, before myjs/domonic parsed them -- what
    # `native_browser.py`'s view-source (F8) shows. Best-effort: a page this
    # far into loading successfully has already been read once, so this
    # essentially never fails, but view-source itself isn't worth failing
    # the whole navigation over if it somehow does.
    try:
        page.source = source.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- view-source unavailable is not a load failure
        page.source = None

    return page


_HTTP_SESSION = None


def _shared_http_session():
    """A single, process-wide `requests.Session` reused for every remote
    load unless a caller supplies its own -- gives ordinary navigation (and
    form submission, see `_load_remote`'s `method`/`data`) a real, persistent
    cookie jar (plus connection reuse) across the whole browsing session,
    the same way a real browser's cookie store outlives any one request.
    `domonic.scrape()`'s own `fetch()` calls the stateless `requests.request`
    directly, so nothing before this ever kept cookies between navigations
    at all -- logging into a site would "work" (the response set a session
    cookie) but every subsequent page acted logged-out again."""
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        import requests
        _HTTP_SESSION = requests.Session()
        # `requests`' own default `User-Agent` (`python-requests/x.y.z`)
        # gets a bare `403` from Wikipedia (and plenty of other real sites)
        # -- not a real browser-fingerprint check, just a common heuristic
        # against unlabeled script traffic; any self-identifying, non-
        # generic string clears it (confirmed directly: `curl -A
        # "chromonic/1.0" https://www.wikipedia.org/` -> `200`, no other
        # header needed). Matches `browser_images.py`'s own convention of
        # identifying honestly rather than spoofing a real browser's UA.
        _HTTP_SESSION.headers["User-Agent"] = "chromonic/1.0 (+https://github.com/byteface/domonic-libs)"
    return _HTTP_SESSION


class _RequestsResponseAdapter:
    """Adapts a `requests.Response` to the `.text()`/`.url` surface
    `domonic._scrape._parse` expects from its own `fetch.Response` -- lets
    `_load_remote` reuse that function's parsing/CSS-loading/window-attach
    logic verbatim instead of duplicating it, while doing the actual HTTP
    fetch itself (through `_shared_http_session()`, not domonic's stateless
    `fetch()`) so cookies and `method`/`data` (form submission) are
    available to it at all."""
    __slots__ = ("_response", "url")

    def __init__(self, response):
        self._response = response
        self.url = response.url

    def text(self):
        # `requests.Response.text` decodes a `text/*` body with no declared
        # charset as ISO-8859-1 (the HTTP/1.1 default), so a UTF-8 file's
        # BOM came through as the three characters `ï»¿` before the
        # `<!DOCTYPE>` -- domonic's parser then treated that as body text
        # (a whole first line, with `<title>`/`<link>`/`<style>` demoted
        # into `<body>` after it, 26px below Chrome on every one of the 22
        # BOM-prefixed `css-flexbox/*.htm` fixtures). Chrome sniffs the
        # BOM first, then the declared charset, then falls back to UTF-8
        # for a well-formed body; the same order here. The BOM itself is
        # dropped: domonic keeps a leading U+FEFF as a text node (logged
        # in PLAN.md).
        content = self._response.content
        content_type = (self._response.headers.get("content-type") or "").lower()
        if content.startswith(b"\xef\xbb\xbf"):
            text = content[3:].decode("utf-8", errors="replace")
        elif "charset=" in content_type and self._response.encoding:
            text = self._response.text.lstrip("﻿")
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = self._response.text
        if "xhtml+xml" in content_type or _looks_like_xhtml(text):
            text = _expand_xhtml_self_closing_tags(text)
        return _strip_html_comments_in_style(text)


_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_html_comments_in_style(text: str) -> str:
    """CSS Syntax 3 §5.4.1: `<!--`/`-->` (CDO/CDC) inside a stylesheet
    are ignored, so an HTML-style comment between rules in a `<style>`
    block is harmless to Chrome. domonic's CSS parser doesn't drop them
    and loses the rule that follows (`css-flexbox/flexbox-mbp-horiz-
    003.xhtml`: the `.borderA` rule after `<!-- customizations ... -->`
    never applied). Removed here before parsing; logged in PLAN.md."""
    return _STYLE_BLOCK_RE.sub(
        lambda m: m.group(1) + _HTML_COMMENT_RE.sub(" ", m.group(2)) + m.group(3), text)


_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_SELF_CLOSING_TAG_RE = re.compile(r"<([a-zA-Z][\w:.-]*)((?:\s+[^<>]*?)?)\s*/>")


def _looks_like_xhtml(text: str) -> bool:
    head = text[:2048]
    return head.lstrip().startswith("<?xml") or 'xmlns="http://www.w3.org/1999/xhtml"' in head


def _expand_xhtml_self_closing_tags(text: str) -> str:
    """An XHTML document (served as `application/xhtml+xml`, or an XML
    prolog / the XHTML namespace on its root) is parsed by Chrome as XML,
    where `<div class="a"/>` is a complete, empty element. domonic parses
    everything as HTML, where the `/` on a non-void start tag is ignored
    and every following sibling nests *inside* that `<div>` (the Mozilla
    `css-flexbox/flexbox-*.xhtml` fixtures: `<div class="a"/><div class=
    "b"/>` became one item containing the other, 200px wide instead of
    10px). Rewritten here into explicit `<div ...></div>` pairs, leaving
    HTML's void elements (`<br/>`, `<img/>`...) alone. Logged in PLAN.md."""
    def expand(match):
        tag = match.group(1)
        if tag.lower() in _VOID_HTML_TAGS:
            return match.group(0)
        return f"<{tag}{match.group(2)}></{tag}>"
    return _SELF_CLOSING_TAG_RE.sub(expand, text)


def _load_remote(url: str, *, method: str = "GET", data=None, http_session=None):
    from domonic._scrape import _parse
    from domonic.webapi.fetch import Request

    from . import netlog

    session = http_session or _shared_http_session()
    netlog.log("net", f"{method} {url}")
    response = session.request(method, url, data=data, timeout=30, allow_redirects=True)
    netlog.log("net", f"{response.status_code} {response.url} ({len(response.content)} bytes)")
    # Domonic 1.8.4 requires the source Request so external stylesheets can
    # inherit credentials/headers only when they are same-origin. Build it
    # from requests' actual prepared request (which includes session headers
    # and cookies), rather than reconstructing it from the caller's inputs.
    prepared = response.request
    source_request = Request(
        response.url,
        method=prepared.method,
        headers=dict(prepared.headers),
        redirect="follow",
    )
    document = _parse(
        _RequestsResponseAdapter(response), None,
        source_request=source_request,
        css=True,
        attach=True,
        request_kwargs={"timeout": 30, "allow_redirects": True},
    )
    default_view = getattr(document, "defaultView", None)
    page = SimpleNamespace(
        document=document,
        url=response.url,
        session=SimpleNamespace(window=default_view),
        # The raw response body, before domonic parsed it -- what
        # `native_browser.py`'s view-source (F8) shows.
        source=response.text,
    )
    return page


def _load_internal(url: str):
    """Build a `page` for a `chromonic://...` URL straight from `homepage.py`'s
    generated HTML -- no fetch, no file on disk. Mirrors `_load_local`'s
    synthetic-page construction (a bare `myjs.Page(html_string, url=...)`),
    just with domonic-generated markup instead of an f-string wrapper.

    `chromonic://save-settings?homepage=...` is not really a page: it
    persists the new preference to `homepage.save_prefs`, then serves the
    settings page back (with `page.url` set to the *settings* URL, not the
    save action, so the address bar lands on `chromonic://settings` after
    saving -- the same "the final `page.url` wins" redirect behaviour
    `native_browser.commit_page` already gives a real HTTP redirect)."""
    from myjs import Page

    from . import homepage

    parsed = urllib.parse.urlparse(url)
    name = (parsed.netloc or parsed.path).strip("/").lower() or "home"
    saved = False

    if name == "save-settings":
        query = urllib.parse.parse_qs(parsed.query)
        new_homepage = (query.get("homepage", [""])[0]).strip()
        prefs = homepage.load_prefs()
        if new_homepage:
            prefs["homepage"] = new_homepage
        else:
            prefs.pop("homepage", None)
        homepage.save_prefs(prefs)
        saved = True
        name = "settings"

    if name == "settings":
        source = homepage.build_settings_html(saved=saved)
        final_url = homepage.SETTINGS_URL
    else:
        source = homepage.build_homepage_html()
        final_url = homepage.HOME_URL

    return Page(source, url=final_url, run=False)


def _ensure_window(page) -> None:
    """Every loaded page gets a real `domonic.window.Window` as its
    `document.defaultView` -- a remote page already gets one from
    `domonic._scrape._parse(..., attach=True)` (see `_load_remote`), but a
    local (myjs) page never does, so a plain `<script>` reading `window.*`,
    the devtools console (`native_browser.View.console_submit`), or a
    native browser host (`native_browser.run`'s `GLFWWindowHost.attach`)
    would otherwise have nothing real to attach to. `Window(doc=...)` sets
    `document.defaultView` itself as a side effect of construction."""
    from domonic.window import Window

    if getattr(page.document, "defaultView", None) is None:
        Window(doc=page.document)


def load(url: str, *, method: str = "GET", data=None, http_session=None):
    """Fetch + parse `url` with domonic 1.8.4 for HTTP(S), myjs for local files.

    `method`/`data` (ignored for local files -- forms don't target them in
    practice) let a caller submit a real HTML form: `method="POST"` with
    `data` as the `application/x-www-form-urlencoded` pairs/string a
    `<form>`'s fields serialize to. `http_session` overrides the shared
    session (see `_shared_http_session`) for callers that want an isolated
    cookie jar; almost nothing needs this."""
    from . import browser_images, ua_style, webfonts

    is_remote = _is_url(url)
    page = (_load_internal(url) if _is_internal_url(url) else
            _load_remote(url, method=method, data=data, http_session=http_session)
            if is_remote else _load_local(url))
    page.document._chromonic_base_url = page.url
    _ensure_window(page)

    _apply_presentational_attributes(page.document)

    webfonts.prepare(
        page,
        page.__dict__.get(
            "_font_stylesheet_sources",
            [],
        ),
    )

    ua_style.apply(page.document)

    browser_images.resolve_image_sources(
        page.document,
        page.url,
    )

    # Run the page's own `<script>` elements -- ordinary browser behaviour,
    # same as any other browser does on every site -- against a sandboxed
    # `myjs` session (see `js_sandbox`'s own docstring for why: unlike
    # `pyscript.py`'s deliberately unsandboxed first-party model, a remote
    # page's script could be anything, so it never gets a real filesystem,
    # shell, or `require()` of an arbitrary Python module). Local files and
    # chromonic's own internal pages don't run scripts at all yet -- out of
    # scope here, and neither needs the remote case's sandboxing anyway.
    if is_remote:
        from . import js_sandbox
        session = http_session or _shared_http_session()
        try:
            script_page = js_sandbox.run_scripts(page.document, url=page.url, http_session=session)
        except Exception:  # noqa: BLE001 -- a broken script environment must not fail the whole load
            _log.exception("chromonic: script execution failed for %s", page.url)
        else:
            page.session = script_page.session
            page.js_errors = script_page.errors

    return page


def set_viewport(page, width, height) -> None:
    windows = [
        getattr(getattr(page, "session", None), "window", None),
        getattr(getattr(page, "document", None), "defaultView", None),
    ]
    try:
        from domonic.window import window as domonic_window
        windows.append(domonic_window)
    except Exception:
        pass
    for window_obj in {id(obj): obj for obj in windows if obj is not None}.values():
        state = getattr(window_obj, "_own", None)
        if isinstance(state, dict):
            state["innerWidth"] = width
            state["innerHeight"] = height
        elif hasattr(window_obj, "_set_viewport"):
            # Not `resizeTo()`: that's the *outer* browser-window-resizing
            # API (CSS 2.1's `window.resizeTo` == moving the real OS
            # window), and once a `domonic.window.Window` has a real host
            # attached (see `native_browser.run`), calling it here actually
            # commands the native window to shrink to this *inner* content
            # height -- which then changes the real window size, which
            # `native_browser.sync_window_size` notices next frame and
            # resizes the view to match, triggering another relayout that
            # calls this again with an even smaller height: a runaway
            # feedback loop that shrinks the window to nothing. This call
            # only means "the page's own viewport is now this size" (for
            # `window.innerWidth`/`matchMedia()`), so it must only update
            # that state, never touch the host/native window.
            window_obj._set_viewport(width, height, dispatch=True)
        else:
            window_obj.innerWidth = width
            window_obj.innerHeight = height


class BrowserInteraction(window.Interaction):
    """Interaction plus browser navigation.

    Kept as the framework/headless interaction layer used by tests.
    The real displayed browser is native_browser.py.
    """

    def __init__(
        self,
        url: str,
        *,
        width: float,
        height: "float | None" = None,
    ):
        super().__init__(
            None,
            width=width,
            height=height,
        )

        self._history: list[str] = []

        self._load(
            url,
            record=True,
        )

    def _load(
        self,
        url: str,
        *,
        record: bool,
    ) -> None:
        _validate_navigable(url)

        page = load(url)

        self.url = url
        self.page = page
        self.root = page.document.body

        if record:
            self._history.append(url)

    def relayout(self) -> None:
        self._prepare_viewport()
        super().relayout()

    def render(self, *, relayout: bool = True, reuse_styles: bool = False) -> bytes:
        if relayout or self.root.get_layout_box() is None:
            self._prepare_viewport()
        return super().render(relayout=relayout, reuse_styles=reuse_styles)

    def _prepare_viewport(self) -> None:
        page = getattr(self, "page", None)
        if page is None:
            return
        set_viewport(page, self.width, self.height)
        doc = page.document
        if hasattr(doc, "_cssom_rule_index"):
            doc._cssom_rule_index = None

    def navigate(self, href: str) -> None:
        url = urllib.parse.urljoin(
            self.url,
            href,
        )

        self._load(
            url,
            record=True,
        )

        self.relayout()

    def go_back(self) -> bool:
        if len(self._history) < 2:
            return False

        self._history.pop()

        self._load(
            self._history[-1],
            record=False,
        )

        self.relayout()

        return True

    def handle_click(
        self,
        x: float,
        y: float,
    ):
        """Navigate links; otherwise dispatch normal DOM click handling."""

        element = hittest.hit_test(
            self.root,
            x,
            y,
        )

        anchor = element

        while (
            anchor is not None
            and (
                getattr(
                    anchor,
                    "tagName",
                    "",
                )
                or ""
            ).lower()
            != "a"
        ):
            anchor = getattr(
                anchor,
                "parentElement",
                None,
            )

        href = (
            anchor.getAttribute("href")
            if anchor is not None
            else None
        )

        if (
            href
            and not href.startswith("#")
            and href.split(":", 1)[0].lower()
            not in (
                "javascript",
                "mailto",
                "tel",
            )
        ):
            try:
                self.navigate(href)

            except ValueError as error:
                print(
                    f"chromonic: {error}"
                )

            return element

        return super().handle_click(
            x,
            y,
        )


class _Api(window._Api):
    def __init__(self, interaction: BrowserInteraction):
        super().__init__(interaction)
        self._image_generation = 0

    def navigate(self, url: str) -> None:
        try:
            self._interaction.navigate(_normalize_address(url))
        except ValueError as error:
            print(f"chromonic: {error}")
            return
        self.push_frame(relayout=False)

    def go_back(self) -> None:
        if self._interaction.go_back():
            self.push_frame(relayout=False)

    def tick(self) -> None:
        from . import browser_images

        current = browser_images.generation()
        if current != self._image_generation:
            self._image_generation = current
            self.push_frame(reuse_styles=True)

    def push_frame(self, *, relayout: bool = True, reuse_styles: bool = False) -> None:
        if self._window is None:
            return
        png = self._interaction.render(relayout=relayout, reuse_styles=reuse_styles)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        address = json.dumps(self._interaction.url)
        self._window.evaluate_js(
            f"document.getElementById('frame').src = {data_uri!r};"
            f"document.getElementById('address').value = {address};"
        )


def run(
    url: str,
    *,
    width: int = 1000,
    height: int = 800,
    title: str = "chromonic",
) -> None:
    """Open a real GLFW/Skia Chromonic browser window."""

    # Lazy import is important:
    #
    # native_browser imports browser for load()/helpers,
    # so importing it at module import time would create a cycle.
    from .native_browser import run as native_run

    native_run(
        url,
        width=width,
        height=height,
        title=title,
    )
