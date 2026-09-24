"""A radio's group -- which other `<input type=radio name=X>` elements
share `checked` exclusivity with it -- is scoped to its `<form>` owner if
it has one, or the *whole document* (tree order) if it doesn't (HTML: "a
radio button's radio button group"). domonic's own `_radio_group_members`
(dom.py) gets the no-form case wrong: `root = form if form is not None
else getattr(control, "parentNode", None)` falls back to the radio's
*immediate* parent, not the document -- so three radios each individually
wrapped in their own `<label>` (a very common real-world pattern, not
just this project's own form demo) are never found as siblings of each
other at all, and `checked`'s exclusivity logic silently has nothing to
clear: every radio in the group can end up checked at once. Confirmed
directly against `examples/forms_demo.html`'s own radio group. Not
patched upstream; logged in PLAN.md.

Fixed by replacing the module-level function in place, falling back to
`ownerDocument` instead of `parentNode` -- `HTMLInputElement.checked`'s
setter calls it as a bare name, resolved against `domonic.dom`'s own
module globals at *call* time, so reassigning it here is picked up by
that already-defined method with no further wiring, the same technique
`domonic_details_element_patch.py` uses for a different gap."""
from __future__ import annotations

import domonic.dom as _dom

_INSTALLED = False


def _radio_group_members(control, *, include_disabled: bool = True):
    if not isinstance(control, _dom.Element):
        return []
    name = control.getAttribute("name")
    if not name:
        return [control] if isinstance(control, _dom.HTMLInputElement) else []
    form = _dom._form_owner(control)
    root = form if form is not None else getattr(control, "ownerDocument", None)
    if root is None or not hasattr(root, "querySelectorAll"):
        return [control] if isinstance(control, _dom.HTMLInputElement) else []
    radios = [
        candidate for candidate in root.querySelectorAll("input")
        if (
            isinstance(candidate, _dom.HTMLInputElement)
            and (include_disabled or not candidate.hasAttribute("disabled"))
            and (candidate.getAttribute("type") or "").lower() == "radio"
            and candidate.getAttribute("name") == name
        )
    ]
    return radios or ([control] if isinstance(control, _dom.HTMLInputElement) else [])


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return False
    _dom._radio_group_members = _radio_group_members
    _INSTALLED = True
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
