# objective: use the real web-platform-tests suite as a source of already-written CSS2.1/WPT test markup, run it through the Chrome-vs-chromonic geometry harness, and fix whatever chromonic gets wrong. If the issue is with domonic itself patch it for now, log it here so it can be fixed upstream. If the issue is with chromonic's own layer, fix it in this repo.

tests/layout/harness, and run the whole suite through it. The harness is already written, but needs a local checkout of the real web-platform-tests repo to run against.

``` bash
git clone --depth=1 https://github.com/web-platform-tests/wpt.git tests/wpt
```

The WPT runner also needs a local static file server on port 8943 serving
`tests/wpt/` as its document root (many fixtures use absolute-root paths like
`/resources/testharness.js` that only resolve over real HTTP). Start it once,
in its own terminal, before running `harness.run_wpt` — see
`tests/layout/README.md#running-the-real-web-platform-tests-suite` for the
full command and troubleshooting.

## whats been mostly done so far

CSS2/box/ - 8/11 PASSING
CSS2/visudet/ - 7/40 PASSING (was 2/40 at the start of this folder). Built:
  `img`/`canvas`/`svg`/`iframe` added to `_USUALLY_INLINE_TAGS` (dropped
  whole containers out of inline flow otherwise), atomic-element bail in
  `_make_inline_formatting_plan` extended to replaced/control tags
  (silently vanished from layout otherwise), real `@font-face`
  `unicode-range` support (`webfonts.py`, subsets each font to its own
  range so ordinary glyph-fallback does the rest), `text-align` mapped to
  `justify-content` in the flex-row inline approximation (was always
  flush-left). Also fixed a harness bug: shared-plan text fragments
  reported full line-height as their own height instead of glyph height.
  Remaining: font-metric rendering noise, and `line-height:normal` only
  using the first font in a fallback list instead of the tallest actually
  used -- not attempted, `fonts.text_metrics` too hot/shared to change safely.
CSS2/positions/ - 50%
CSS2/box-display/
CSS2/margin-padding-clear/ - 86 failed / 69 errors (out of 739 total)
CSS2/linebox/
CSS2/normal-flow/ 524 passed / 230 failed / 37 errors


left todo: rest of CSS2/, css-box/, css-display/, css-position/, css-flexbox/, css-text/

Agent should NOT run full suite of tests between fixes. It takes too long and waiting ages per fix is not productive. Instead run full verification between batches of fixes.


## log domonic issues here to be fixed upstream

`CSS.supports()` doesn't validate a property's *value*, only that the
declaration's syntax parses -- `CSS.supports("(display: bogus-value-xyz)")`
returns `True`. Minor, pre-existing, not chromonic's to fix.


------


`CanvasRenderingContext2D._record` only stores `{name, args}` per drawing
command, no snapshot of the style state (`fillStyle`/`strokeStyle`/
`globalAlpha`/etc.) active when that command was called -- so replaying
recorded commands later (chromonic's own paint path does this) uses
whatever style is current at replay time instead of at record time,
silently wrong the moment two draws with different styles are interleaved
with a style mutation. Worked around in `domonic_canvas_patch.py`,
overriding `_record` to snapshot the full style state alongside each
command. Not patched upstream.


------


`MediaQueryList._evaluate` has no concept of a "current media type" at
all -- `all`/`screen`/`print` all just unconditionally return `True`
regardless of what's actually rendering. Confirmed directly: `@media
print { ... }` applied during chromonic's own (always screen/interactive)
rendering, hiding content and applying sizing rules real Chrome never
does outside an actual print preview. Worked around in `domonic_print_
media_patch.py`, wrapping `_evaluate` so `print` never matches (chromonic
has no print/paged-media mode at all, so this is correct for every real
use, not just a narrow fix). Not patched upstream.


------


`ComputedStyleDeclaration`'s cascade only matches a rule's selector via
`Element._matchElement` (a small hand-rolled matcher, no `:nth-child`/
`:nth-of-type`/`:hover`/etc. support) or `_matches_selector_chain` (combinator
chains only) -- a single-compound selector using any pseudo-class outside
that whitelist (e.g. `div:nth-of-type(2)`) is silently never matched during
cascade resolution, even though `Element.matches()` correctly matches the
same selector (via a further `querySelectorAll()` fallback the cascade never
calls). Confirmed directly: `div:nth-of-type(2) { line-height: 30px }` never
applied to any element. Worked around in `domonic_selector_fallback_patch.py`,
giving `_matchElement` the same per-compound bs4-backed fallback `_matches_
selector_chain` already uses for each compound of a combinator chain. Not
patched upstream.


------


NEEDS REVIEW:

1

REPORTED:
`ComputedStyleDeclaration.getPropertyValue`'s `_to_used_length` substitutes
the layout box's own size for `width`/`height` whenever the computed value
is `auto`, with no check for whether the property even applies to the
element -- CSS 2.1 10.3.3/10.6.3 exempt non-replaced inline elements
(`width`/`height` "does not apply"), so real Chrome reports `auto`
verbatim there, never a resolved pixel value. domonic returns the used
box size regardless of display type. Seen repeatedly across nearly every
block-in-inline fixture's `style_mismatches` (`"auto"` vs `"784px"`/a
resolved `ch` width) wherever the reported element is `display:inline`.
Not patched -- affects only the harness's own style comparison, not
chromonic's real layout, so left alone each time.

NOTE FROM UPSTREAM(CLAUDE) ON THE ABOVE:
I looked at that one but I'm not going to patch it, and here's why:

I tried the literal fix — skip resolving width/height for non-replaced inline elements — but it broke real tests immediately. The reason: domonic has no UA default stylesheet. A plain <div> with no explicit display computes as "inline" (CSS's actual initial value), since nothing in domonic ever sets div { display: block }. So a check like "is this element's computed display inline?" can't distinguish a genuinely inline element (<span>) from an ordinary <div> that a real layout engine has already sized as a block — both report display: inline from domonic's cascade alone.

Getting this right would require adding a UA default-display stylesheet to domonic first — a real, separate piece of work, not a one-line guard. And notably, the chromonic report itself ends with "Not patched ... left alone each time" — they already reached the same conclusion for their harness. So I left it as-is rather than trade a narrow harness-comparison nicety for breaking real auto-width resolution on every unstyled <div>.



2

------

REPORTED:
`Window.requestAnimationFrame()` passes `window.performance.now()` directly
to the callback, but domonic's `Performance.now()` currently returns seconds
(`time.time() - start`) rather than a DOMHighResTimeStamp in milliseconds.
That makes spec-style RAF code that divides by 1000 appear frozen because it
is running 1000x too slowly. Chromonic's GLFW hosts temporarily multiply the
timestamp by 1000 when flushing queued RAF callbacks; domonic should either
make `Performance.now()` return milliseconds or convert at RAF dispatch.
