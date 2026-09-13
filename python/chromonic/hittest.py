"""Coordinate -> the topmost domonic element painted at that point.

Pure Python over the `LayoutBox`es `tree.layout()` already wrote back onto
the domonic elements -- no Rust, no Skia. "Topmost" = deepest in the DOM
tree among the elements whose border box contains the point, matching how a
real browser resolves a click: children paint over their parent, so a click
inside a child's box should resolve to the child, not the ancestor.
"""

from __future__ import annotations

ELEMENT_NODE = 1


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _contains(box, x: float, y: float) -> bool:
    return box.x <= x <= box.x + box.width and box.y <= y <= box.y + box.height


def hit_test(root_element, x: float, y: float):
    """The innermost laid-out element under `(x, y)`, or `None`."""
    box = root_element.get_layout_box()
    if box is None or not _contains(box, x, y):
        return None
    for child in reversed([c for c in (root_element.childNodes or []) if _is_element(c)]):
        found = hit_test(child, x, y)
        if found is not None:
            return found
    return root_element
