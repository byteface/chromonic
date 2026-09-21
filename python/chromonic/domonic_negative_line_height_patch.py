"""CSS 2.1 10.8.1 says `line-height` "does not allow negative values" --
a negative `<length>`/`<number>`/`<percentage>` makes the whole declaration
invalid, so the property keeps whatever it would otherwise be (here, the
inherited/initial `normal`). `ComputedStyleDeclaration._to_used_length`
never enforces this: its `line-height` branch converts a unitless
multiplier, a percentage, or an absolute length straight to a used pixel
value with no sign check, so `line-height: -1pc` used-value-resolves to
`"-16px"` -- a real, negative used value -- instead of `"normal"`.

Confirmed directly with `tests/wpt/css/CSS2/linebox/line-height-023.xht`
(`#div3 { line-height: -1pc }`): real Chrome reports `getComputedStyle
(div3).lineHeight === "normal"` (the declaration was thrown out as
invalid), chromonic/domonic reports `"-16px"`. That negative value then
reaches `chromonic.tree._resolved_line_height` (`tree.py`'s only consumer
of `computed.lineHeight`) as a real negative float, driving the line box's
strut height negative and collapsing the element's own content height to
`0` -- not just a cosmetic `getComputedStyle` mismatch, a real layout bug
cascading to every element positioned after it.

Patched by wrapping `_to_used_length`: run the original resolution
unchanged, and only when it was asked for `line-height` and the result
parses as a negative `"Npx"` string, substitute `"normal"` instead --
covering all three of the property's value forms (multiplier/percentage/
absolute length) in one place, matching how the original resolves each of
them to the same `"Npx"` shape before returning."""
from __future__ import annotations

import re
import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

_style = sys.modules["domonic.style"]
_ComputedStyleDeclaration = _style.ComputedStyleDeclaration

_INSTALLED = False
_ORIGINAL_TO_USED_LENGTH = _ComputedStyleDeclaration._to_used_length
_NEGATIVE_PX_RE = re.compile(r"^\s*-\d")


def _to_used_length_with_line_height_guard(self, target, value):
    result = _ORIGINAL_TO_USED_LENGTH(self, target, value)
    if target == "line-height" and isinstance(result, str) and _NEGATIVE_PX_RE.match(result):
        return "normal"
    return result


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _ComputedStyleDeclaration._to_used_length = _to_used_length_with_line_height_guard
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _ComputedStyleDeclaration._to_used_length = _ORIGINAL_TO_USED_LENGTH
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
