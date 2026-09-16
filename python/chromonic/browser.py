"""A simple, navigable browser on top of chromonic.

Fetch/parse/CSS handling lives here. The actual OS window is provided by
native_browser.py using GLFW + Skia directly -- no pywebview, HTML host,
JS bridge, PNG transport, or base64 frame swapping.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
import urllib.parse
from pathlib import Path
import urllib.request


from . import (
    domonic_cdata_style_patch,
    domonic_layout_calc_var_patch,
    domonic_logical_properties_patch,
    hittest,
    tree,
    window,
)


def _is_url(s: str) -> bool:
    return (
        isinstance(s, str)
        and s.split(":", 1)[0].lower() in ("http", "https")
    )


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

    if lowered.startswith(
        (
            "localhost:",
            "127.0.0.1:",
            "0.0.0.0:",
            "[::1]:",
        )
    ):
        return "http://" + value

    parsed = urllib.parse.urlparse(value)

    if parsed.scheme in ("http", "https") and parsed.netloc:
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
    """Only allow absolute http(s) URLs."""
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            "chromonic's browser only navigates absolute "
            f"http(s) URLs, got: {url!r}"
        )


def warm_interpreter() -> None:
    """Pay myjs' one-time interpreter setup cost at startup."""
    from myjs import Page

    Page(
        "<html></html>",
        run=False,
        css=False,
    )


def _append_presentational_style(element, declarations: list[str]) -> None:
    if not declarations:
        return
    existing = element.getAttribute("style") or ""
    prefix = "; ".join(declarations) + ";"
    # Put presentational attributes first so authored inline style text that
    # follows still wins when both mention the same property.
    element.setAttribute("style", f"{prefix} {existing}".strip())


def _apply_presentational_attributes(document) -> None:
    """Translate the old HTML attributes real legacy pages still use.

    Hacker News is the canonical small repro: the orange bar is
    ``<td bgcolor="#ff6600">`` and the logo is an SVG ``<img>`` whose layout
    size comes from ``width``/``height`` attributes. domonic exposes those as
    attributes, not computed CSS, so normalize the narrow set Chromonic needs
    before resolving styles.
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

    for element in document.getElementsByTagName("*"):
        declarations = []
        bgcolor = element.getAttribute("bgcolor")
        if bgcolor:
            declarations.append(f"background-color:{bgcolor}")
        if (getattr(element, "tagName", "") or "").lower() in {"img", "table"}:
            width = length(element.getAttribute("width"))
            height = length(element.getAttribute("height"))
            if width:
                declarations.append(f"width:{width}")
            if height:
                declarations.append(f"height:{height}")
        _append_presentational_style(element, declarations)


def _load_local(url: str):
    from myjs import Page

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

    source = _local_path(url)

    page = FontAwarePage.load(
        source,
        run=False,
    )

    # Keep a URL-shaped base for relative CSS/images/fonts.
    page.url = source.as_uri()

    return page


def _load_remote(url: str):
    from domonic import domonic

    document = domonic.scrape(
        url,
        css=True,
        attach=True,
    )
    default_view = getattr(document, "defaultView", None)
    page = SimpleNamespace(
        document=document,
        url=url,
        session=SimpleNamespace(window=default_view),
    )
    return page


def load(url: str):
    """Fetch + parse `url` with domonic 1.8.1 for HTTP(S), myjs for local files."""
    from . import browser_images, ua_style, webfonts

    page = _load_remote(url) if _is_url(url) else _load_local(url)
    page.document._chromonic_base_url = page.url

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
        elif hasattr(window_obj, "resizeTo"):
            window_obj.resizeTo(width, height)
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
