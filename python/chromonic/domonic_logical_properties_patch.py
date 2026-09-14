"""CSS Logical Properties (`padding-inline`, `margin-block-start`, ...) support
for domonic's cascade -- see `PLAN.md`'s domonic issues log for the full writeup.

domonic's CSS declaration parser (`domonic.style._parse_css_declarations`)
has never heard of logical properties: a rule declaring only `padding-inline`/
`padding-block` (as Tailwind v4's generated CSS does pervasively -- these two
alone accounted for the bulk of one real site's spacing) produces a
`("padding-inline", ..., priority)` tuple that no downstream physical-longhand
lookup (`getPropertyValue("padding-top")`, Taffy's own style dict, ...) ever
finds, silently dropping every logical declaration from the cascade -- while
an *unrelated* physical reset rule (`* { padding: 0 }`, Tailwind's own
preflight) sets `padding-top`/etc. explicitly and wins by simply being the
only declaration either sees.

Fixed by expanding a logical property into its physical longhand(s) at the
declaration-parsing level (`_parse_css_declarations`, the single tokenizer
every caller -- stylesheet rule indexing, inline `style` parsing, CSSOM
enumeration -- already funnels through), each expanded tuple keeping the
same `!important` priority as its source. This is the one point where
expansion can piggyback on domonic's *existing*, already cascade/layer/
specificity-aware property-name competition (`ComputedStyleDeclaration.
_collect_author_declarations`'s `cascade[name] = (key, value, important)`)
instead of guessing at priority after the fact -- an earlier version of this
patch expanded post-hoc (`_cssom.expand_shorthand`/`SHORTHANDS`) and silently
lost to an already-resolved physical `padding-top` from an unrelated,
lower-priority reset rule the moment that rule happened to be visited first,
exactly the failure mode a real cascade must not have.

Only LTR, horizontal `writing-mode` is modelled (`inline-start`/`-end` -> the
literal `left`/`right`; `block-start`/`-end` -> `top`/`bottom`) -- matching
this project's existing, already-documented lack of RTL/bidi support
(`PLAN.md`'s `wpt/css/CSS2/margin-padding-clear` entry)."""
from __future__ import annotations

import sys

import domonic.style  # noqa: F401 -- ensures `domonic.style` is in `sys.modules`

# `domonic/__init__.py` overwrites its own `style` attribute with a `Style`
# class, so `domonic.style` (attribute access) and even `import domonic.style
# as _style` no longer reach the actual submodule -- only a `sys.modules`
# lookup by its full dotted name does.
_style = sys.modules["domonic.style"]

_INSTALLED = False
_ORIGINAL_PARSE_CSS_DECLARATIONS = _style._parse_css_declarations

# Each 2-value logical axis shorthand ("start end", or one value for both) ->
# its two physical longhands, in start-then-end order.
_AXIS_SHORTHANDS: dict[str, tuple[str, str]] = {
    "padding-inline": ("padding-left", "padding-right"),
    "padding-block": ("padding-top", "padding-bottom"),
    "margin-inline": ("margin-left", "margin-right"),
    "margin-block": ("margin-top", "margin-bottom"),
    "inset-inline": ("left", "right"),
    "inset-block": ("top", "bottom"),
}

# Each single-value logical longhand -> the one physical longhand it aliases.
_ALIAS_LONGHANDS: dict[str, str] = {
    "padding-inline-start": "padding-left",
    "padding-inline-end": "padding-right",
    "padding-block-start": "padding-top",
    "padding-block-end": "padding-bottom",
    "margin-inline-start": "margin-left",
    "margin-inline-end": "margin-right",
    "margin-block-start": "margin-top",
    "margin-block-end": "margin-bottom",
    "inset-inline-start": "left",
    "inset-inline-end": "right",
    "inset-block-start": "top",
    "inset-block-end": "bottom",
}


def _parse_css_declarations_with_logical_properties(css_text: str):
    expanded: list[tuple[str, str, str]] = []
    for name, value, priority in _ORIGINAL_PARSE_CSS_DECLARATIONS(css_text):
        axis = _AXIS_SHORTHANDS.get(name)
        if axis is not None:
            parts = value.split()
            if len(parts) == 1:
                expanded.append((axis[0], parts[0], priority))
                expanded.append((axis[1], parts[0], priority))
                continue
            if len(parts) == 2:
                expanded.append((axis[0], parts[0], priority))
                expanded.append((axis[1], parts[1], priority))
                continue
            expanded.append((name, value, priority))  # unrecognised shape -- keep verbatim
            continue
        alias = _ALIAS_LONGHANDS.get(name)
        expanded.append((alias, value, priority) if alias is not None else (name, value, priority))
    return expanded


def install() -> bool:
    """Install once and return whether this call changed domonic."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _style._parse_css_declarations = _parse_css_declarations_with_logical_properties
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    _style._parse_css_declarations = _ORIGINAL_PARSE_CSS_DECLARATIONS
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED


install()
