"""chromonic -- experimental POC (see PLAN.md).

domonic owns the live DOM/CSSOM; Taffy (Rust, via PyO3, `chromonic._native`)
computes layout; Skia (`skia-python`) paints it. Geometry is written back
onto the same domonic elements (`element.set_layout_box(...)`), so
`element.getBoundingClientRect()` reports exactly what got painted.

    import chromonic
    png_bytes = chromonic.render(document.documentElement, width=800, height=600)

    # or, to inspect/mutate before painting:
    node_map = chromonic.layout(document.documentElement, width=800, height=600)
    png_bytes = chromonic.paint(document.documentElement, width=800, height=600)
"""

from __future__ import annotations

from . import browser, browser_images, canvas2d, domonic_canvas_patch, fonts, hittest, paint, pyscript, style_bridge, tree, ua_style, window
from .tree import layout
from . import native_browser

__all__ = [
    "App", "Browser", "layout", "render", "hittest", "paint", "style_bridge", "tree", "window",
    "browser", "pyscript", "native_browser", "ua_style", "browser_images", "fonts",
    "canvas2d", "domonic_canvas_patch", "initialize",
]


def initialize() -> bool:
    """Install explicit compatibility hooks before page scripts run."""
    return domonic_canvas_patch.install()


def render(root_element, *, width: int = 800, height: "int | None" = None, background=None) -> bytes:
    """Lay `root_element` out at `width` x `height` (writing geometry back
    onto every element) and paint it, returning PNG bytes."""
    layout(root_element, width=float(width), height=float(height) if height is not None else None)
    box = root_element.get_layout_box()
    render_height = height if height is not None else int(round(box.height)) if box else 0
    return paint.render_png(root_element, width=width, height=render_height, background=background)


def _document_for(root, title: str):
    from domonic.dom import Document, DOMImplementation
    from domonic.window import Window

    document = root if isinstance(root, Document) else getattr(root, "ownerDocument", None)
    if not isinstance(document, Document):
        document = DOMImplementation().createHTMLDocument(title)
        if getattr(root, "tagName", "").lower() == "body":
            document.body.replaceWith(root)
        else:
            document.body.appendChild(root)
    if getattr(document, "defaultView", None) is None:
        Window(doc=document)
    return document


class App:
    """A native Chromonic application around an existing Domonic root."""

    def __init__(
        self,
        root,
        *,
        width: int = 800,
        height: int = 600,
        title: str = "chromonic",
        on_tick=None,
        fps: float = 30.0,
    ):
        self.width = width
        self.height = height
        self.title = title
        self.on_tick = on_tick
        self.fps = fps
        self.document = _document_for(root, title)
        ua_style.apply(self.document)
        self.window = self.document.defaultView
        self.root = self.document.body if root is self.document else root
        self.interaction = window.Interaction(
            self.root,
            width=float(width),
            height=float(height),
            on_tick=on_tick,
        )

    def on(self, selector: str, event_type: str, handler=None, **event_options):
        def decorate(callback):
            def delegated(event):
                target = getattr(event, "target", None)
                while target is not None:
                    matches = getattr(target, "matches", None)
                    if callable(matches) and matches(selector):
                        event.currentTarget = target
                        return callback(event)
                    if target is self.document:
                        break
                    target = getattr(target, "parentNode", None)

            self.document.addEventListener(event_type, delegated, **event_options)
            return callback

        return decorate(handler) if handler is not None else decorate

    def click(self, selector: str):
        return self.on(selector, "click")

    def key(self, selector: str, key: str | None = None):
        def decorate(handler):
            def filtered(event):
                if key is not None and getattr(event, "key", None) != key:
                    return None
                return handler(event)

            self.on(selector, "keydown", filtered)
            return handler

        return decorate

    def trigger(self, selector: str, event_type: str):
        from domonic.events import Event, MouseEvent

        target = self.document.querySelector(selector)
        if target is None:
            raise ValueError(f"no element matches {selector!r}")
        event_cls = MouseEvent if event_type == "click" else Event
        result = target.dispatchEvent(event_cls(event_type, {"bubbles": True}))
        self.interaction.relayout()
        return result

    def run(self) -> None:
        window.run(
            self.root,
            width=self.width,
            height=self.height,
            title=self.title,
            on_tick=self.on_tick,
            fps=self.fps,
        )

    def render(self, *, relayout: bool = True) -> bytes:
        return self.interaction.render(relayout=relayout)


class Browser:
    """A native Chromonic browser window for a URL."""

    def __init__(
        self,
        url: str,
        *,
        width: int = 1000,
        height: int = 800,
        title: str = "chromonic",
    ):
        self.url = url
        self.width = width
        self.height = height
        self.title = title

    def run(self):
        return browser.run(
            self.url,
            width=self.width,
            height=self.height,
            title=self.title,
        )
