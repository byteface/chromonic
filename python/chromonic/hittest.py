"""Coordinate -> the topmost Domonic element painted at that point."""

from __future__ import annotations


ELEMENT_NODE = 1


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _contains(box, x: float, y: float) -> bool:
    return (
        box.x <= x <= box.x + box.width
        and box.y <= y <= box.y + box.height
    )


def _contains_rect(rect, x: float, y: float) -> bool:
    try:
        left, top, width, height = rect
    except (TypeError, ValueError):
        return False

    return (
        left <= x <= left + width
        and top <= y <= top + height
    )


def _contains_element(element, x: float, y: float) -> bool:
    """Prefer real inline fragments over their union bounding box."""

    fragments = element.__dict__.get("_chromonic_inline_boxes")

    if fragments:
        return any(
            _contains_rect(fragment, x, y)
            for fragment in fragments
        )

    box = element.get_layout_box()

    return box is not None and _contains(box, x, y)


def _style_keyword(element, name: str) -> str:
    """Read an already-computed style without causing another resolution."""

    style = element.__dict__.get("_chromonic_computed_style")

    if style is None:
        return ""

    value = getattr(style, name, None)
    value = getattr(value, "value", value)

    return str(value or "").strip().lower()


def _accepts_pointer(element) -> bool:
    if _style_keyword(element, "visibility") == "hidden":
        return False

    if _style_keyword(element, "pointerEvents") == "none":
        return False

    return True


def hit_test(root_element, x: float, y: float):
    """Return the innermost painted element under (x, y)."""

    if root_element is None:
        return None

    if not _contains_element(root_element, x, y):
        return None

    children = [
        child
        for child in (root_element.childNodes or [])
        if _is_element(child)
    ]

    # Later siblings paint over earlier siblings.
    for child in reversed(children):
        found = hit_test(child, x, y)

        if found is not None:
            return found

    if not _accepts_pointer(root_element):
        return None

    return root_element