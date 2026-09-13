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

from . import _domonic_vendor

# This must precede every module below: several import domonic classes at module
# scope, and swapping them after that point would split live DOM class identity.
_domonic_vendor.install()

from . import browser, browser_images, canvas2d, domonic_canvas_patch, fonts, hittest, paint, pyscript, style_bridge, tree, ua_style, window
from .tree import layout
from . import native_browser

__all__ = [
    "layout", "render", "hittest", "paint", "style_bridge", "tree", "window",
    "browser", "pyscript", "native_browser", "ua_style", "browser_images", "fonts",
    "canvas2d", "domonic_canvas_patch", "initialize", "_domonic_vendor",
]


def initialize() -> bool:
    """Install explicit compatibility hooks before page scripts run."""
    return domonic_canvas_patch.install()


def render(root_element, *, width: int, height: "int | None" = None, background=None) -> bytes:
    """Lay `root_element` out at `width` x `height` (writing geometry back
    onto every element) and paint it, returning PNG bytes."""
    layout(root_element, width=float(width), height=float(height) if height is not None else None)
    box = root_element.get_layout_box()
    render_height = height if height is not None else int(round(box.height)) if box else 0
    return paint.render_png(root_element, width=width, height=render_height, background=background)
