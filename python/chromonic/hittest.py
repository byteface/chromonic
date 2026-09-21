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


#: CSS `cursor` keywords this browser can show a distinct native shape for
#: (GLFW's own standard-cursor set) -- returned verbatim so callers (see
#: `native_browser.WindowInput._cursor_shapes`) can map straight to a GLFW
#: constant with no separate CSS-keyword-to-internal-name translation step.
_NATIVE_CURSOR_KEYWORDS = frozenset({
    "pointer", "text", "crosshair", "move", "not-allowed",
    "ew-resize", "ns-resize", "nwse-resize", "nesw-resize",
})


def cursor_for_element(element) -> str:
    """Return the cursor shape (a `_NATIVE_CURSOR_KEYWORDS` member, or
    'arrow') for a hovered element -- walking up through ancestors the same
    way `click()` already does to find an enclosing `<a href>`, so hovering
    text nested inside a link still shows the hand cursor. Any computed
    `cursor` the browser can actually show wins over tag-based defaults
    when the author set one explicitly."""

    node = element

    while node is not None and _is_element(node):
        cursor = _style_keyword(node, "cursor")

        if cursor in _NATIVE_CURSOR_KEYWORDS:
            return cursor

        tag = (getattr(node, "tagName", "") or "").lower()

        if tag == "a" and node.getAttribute("href"):
            return "pointer"

        if tag in ("button", "select"):
            return "pointer"

        if tag == "input":
            input_type = (node.getAttribute("type") or "text").lower()

            if input_type in ("button", "submit", "reset", "checkbox", "radio"):
                return "pointer"

            return "text"

        if tag == "textarea":
            return "text"

        # `getAttribute` returning `None` (attribute absent) must not match
        # here -- `(None or "").lower()` is `""`, the same string a bare
        # `contenteditable` (valid shorthand for `contenteditable="true"`)
        # produces, so checking the *value* alone made every element with
        # no `contenteditable` attribute at all report a text cursor too.
        if node.hasAttribute("contenteditable") and (node.getAttribute("contenteditable") or "").lower() in ("", "true"):
            return "text"

        node = getattr(node, "parentElement", None)

    return "arrow"


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