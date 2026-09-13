"""A minimal user-agent stylesheet -- domonic's CSS cascade has none (see
`docs/domonic-wrinkles.md` #11), so a freshly parsed page's elements sit at
CSS's raw initial values (`margin: 0`, `font-weight: normal`, `font-size:
16px` for every tag alike) unless the *page's own* author CSS says
otherwise. A real browser closes this gap with its own UA stylesheet before
a single author rule is ever applied; chromonic needs the same thing to render
an arbitrary real-world page recognisably (a `<h1>` that doesn't look like
a `<p>`, a `<body>` with its usual 8px margin, a `<ul>` that's actually
indented) rather than every demo page having to spell all of this out by
hand the way `examples/poc.py`/`native_browser.py`'s own tests still do.

**Applied as a real `@layer`, not a heuristic or plain source-order trick.**
It would be tempting to special-case "if this element's margin/font-weight
is still at its CSS initial value, substitute the tag's usual default" --
but that can't distinguish "the author never mentioned this property" from
"the author explicitly reset it to that same value" (`* { margin: 0 }` is
one of the single most common rules on the real web). It's also not enough
to merely insert this stylesheet *first* in the document and rely on
source-order: CSS resolves competing declarations by origin (user-agent <
author, regardless of specificity) *before* specificity/order ever come
into it, and a plain same-origin "first stylesheet" can still lose to a
later, lower-specificity author rule like `*` the wrong way round. CSS
`@layer` is the real mechanism for exactly this -- an unlayered author
declaration always wins over a layered one regardless of specificity or
which came first -- and domonic supports it. `apply()` wraps this whole
stylesheet in `@layer chromonic-ua { ... }`, so it behaves like a genuine UA
stylesheet: any author rule at all, however weak, beats it; nothing here
does when a page doesn't mention the property itself. Verified directly
against domonic: an unlayered `* { padding: 0 }` correctly clears this
file's `ul { padding: ... }` even though `*` has *lower* specificity.

**Every margin/padding rule below uses the full shorthand, never a
`margin-top`/`padding-left`-style longhand -- deliberately, on top of the
`@layer` fix above.** A second, independent domonic cascade bug found
writing this file: a longhand value for a property beats a *shorthand*
covering that same property regardless of layer, specificity, or source
order -- shorthand and longhand declarations appear to be tracked
separately and merged by an unconditional "longhand wins" rule rather than
competing in the normal cascade at all. Reproduced minimally and logged as
`docs/domonic-wrinkles.md` #15 -- unlike the origin issue above, `@layer`
does not paper over this one, so every rule here is written as a shorthand
even where a longhand would otherwise be the more natural way to say it
(`padding: 0 0 0 40px` instead of `padding-left: 40px`); an author page's
own longhand (of either origin) still correctly overrides it, since that
case was verified to already work correctly.

**`display: block` for the standard block-level tags, added in phase 11 --
not for layout geometry (`chromonic.style_bridge._display()` already collapses
everything that isn't `flex`/`grid`/`none` to Taffy's block layout, so
these rules are a geometry no-op) but so `tree.py`'s inline-flow
*approximation* (`_approximate_inline_flow`) can tell a `<p>`/`<div>` apart
from an `<a>`/`<span>` at all.** Without an explicit default, domonic's raw
CSS initial value for `display` is `inline` for *every* tag alike -- there
is no way to distinguish "an unstyled `<div>`, meant to stack" from "an
unstyled `<a>`, meant to sit in a line" without one of them saying so.
`display: list-item` (`<li>`'s real default) isn't attempted -- Taffy has
no such display mode, and this doesn't yet draw list markers/bullets (see
PLAN.md's "Fonts and further rendering" for what's still open); `block` is
the closest achievable and enough to make `_approximate_inline_flow` treat
a `<ul>`'s `<li>` children correctly as non-inline.

Values below are the common values shared by WHATWG's suggested UA
stylesheet and real browsers' `html.css`, trimmed to properties this POC's
`style_bridge`/`paint.py` actually consume.
"""

from __future__ import annotations

_RULES = """
html, body, div, section, article, header, footer, nav, main, aside,
figure, figcaption, address, blockquote, form, fieldset, table, dl, dd,
dt, pre, p, ul, ol, li, hr,
h1, h2, h3, h4, h5, h6 { display: block; }
body { margin: 8px; font: 16px Times; }
p, dl, form, hr,
h1, h2, h3, h4, h5, h6, ul, ol { margin: 1em 0; }
blockquote, figure { margin: 1em 40px; }
h1 { font-size: 2em; font-weight: bold; margin: 0.67em 0; }
h2 { font-size: 1.5em; font-weight: bold; margin: 0.83em 0; }
h3 { font-size: 1.17em; font-weight: bold; margin: 1em 0; }
h4 { font-size: 1em; font-weight: bold; margin: 1.33em 0; }
h5 { font-size: 0.83em; font-weight: bold; margin: 1.67em 0; }
h6 { font-size: 0.67em; font-weight: bold; margin: 2.33em 0; }
ul, ol { padding: 0 0 0 40px; }
b, strong { font-weight: bold; }
small { font-size: 0.83em; }
a { color: #0000ee; }
hr { margin: 0.5em 0; border: 1px solid; height: 0; }
button, input { display: inline-block; font: 13.3333px Arial; margin: 0; }
button { height: 21px; padding: 1px 6px; border: 2px solid; box-sizing: border-box; text-align: center; }
input { width: 145px; height: 15px; padding: 1px 2px; border: 2px solid; box-sizing: content-box; overflow: clip; }
""".strip()

STYLESHEET = f"@layer chromonic-ua {{\n{_RULES}\n}}"


def apply(document) -> None:
    """Insert the UA stylesheet, in its own `@layer`, as the first child of
    `document`'s `<head>` (creating one if the page somehow has none). Layer
    order, not source position, is what gives it the lowest priority any
    author rule can beat -- see the module docstring -- so *where* in
    `<head>` this lands doesn't actually matter for correctness; first is
    just tidy. Idempotent: a second call on the same document is a harmless
    no-op (checked via a marker attribute), since `browser.load()` calls
    this once per freshly parsed page but nothing stops a caller from
    calling it again on one it already touched."""
    if document.querySelector("style[data-chromonic-ua]") is not None:
        return

    head = document.getElementsByTagName("head")
    if head:
        head = head[0]
    else:
        head = document.createElement("head")
        document.documentElement.insertBefore(head, document.documentElement.firstChild)

    style_element = document.createElement("style")
    style_element.setAttribute("data-chromonic-ua", "")
    style_element.textContent = STYLESHEET
    head.insertBefore(style_element, head.firstChild)
