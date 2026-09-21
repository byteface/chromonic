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
CSS2/positions/ - ~50%
CSS2/box-display/
CSS2/margin-padding-clear/ - 86 failed / 69 errors (out of 739 total)
CSS2/linebox/ - sampled ~250 fixtures. Fixed: negative `line-height`
  rejected to `normal` (domonic bug, see log below), `ex`-unit
  `line-height` using real font x-height instead of a flat 0.5em guess
  (same domonic bug). Remaining failures dominated by numeric/percentage
  `vertical-align` offsets and inline-block baseline computation -- not
  attempted, same scope as the already-noted font-metric rendering noise.
CSS2/normal-flow/ - 524 passed / 230 failed / 37 errors
CSS2/table/ - ~40% (of 1239 fixtures)
CSS2/backgrounds - 173/200 PASSING
CSS2/colors
CSS2/positioning/ - swept in full (~575 fixtures). 11 chromonic (tree.py)
  fixes, all CSS 2.1 10.3.7/10.4/10.6.4/10.7/9.7 box-model gaps: the
  negative-margin exception for over-constrained horizontal auto-margins,
  min/max-width and min/max-height clamping (both the real-containing-
  block-ancestor and root/viewport-anchored cases), vertical auto-margins,
  height-solving against the containing block, the fully over-constrained
  horizontal case (`left`/`width`/`right`/both margins all definite --
  every replaced element with an intrinsic width included), `_renders()`
  and table-row/cell/row-group classification not blockifying an
  absolutely/fixed positioned element's `display` per CSS 2.1 9.7,
  `position:fixed`'s containing block incorrectly walking up to a
  positioned ancestor instead of always being the viewport, and
  `_fix_rtl_block_positioning` incorrectly overwriting a genuine
  `position:relative` element's own correct offset. Remaining gaps, not
  attempted: inline `<svg>` with no width/height/viewBox not sized as a
  replaced element at all, shrink-to-fit width + a descendant's `max-width`
  interaction, `inherit`-carried percentages re-resolving against a
  different containing block, and an absolutely positioned element with a
  table-internal `display` not correctly escaping the table's normal flow
  (deeper than the blockification fix above -- traced into `build()`'s
  escapee mechanism, root cause not found).
css-position/ (the modern Positioning L3 suite, distinct from CSS2/
  positioning/) - sampled, mostly not tractable this session: dominated by
  entirely unimplemented features (animations, popover/`overlay`/
  `backdrop`, multicol) and dynamic-reflow tests that are pure JS
  assertions with no static geometry to compare. `position-absolute-
  center-*.html` needs CSS Position 3's flex/grid alignment-based abspos
  centering (a real, distinct new feature, not attempted).
css-display/ - sampled (140 fixtures), not tractable this session: almost
  entirely `display: contents` (the element generates no box of its own,
  children render as if it weren't there) -- chromonic doesn't implement
  this at all, a real, sizeable feature (not a quick box-model fix), plus
  more unimplemented-feature/animation noise on top.
css-flexbox/ - sampled (150 fixtures). A large fraction of the `abspos/`
  subfolder is JS-testharness-only (no static geometry to compare) or the
  same modern flex/grid-alignment-based abspos-centering gap already noted
  under css-position/. Many of the remaining geometry mismatches across
  css-flexbox (and, on reflection, scattered through every other folder
  sampled this session) share one consistent ~11.9px per-element offset --
  traced to real Chrome's `getComputedStyle().fontFamily` reporting the
  literal string `"DejaVu Sans", "Bitstream Vera Sans", Arial, Sans` on
  plain unstyled text, where chromonic falls back to `Times`. Neither
  DejaVu Sans nor Bitstream Vera Sans (both Linux-only fonts) are
  resolvable on this macOS dev machine either, in domonic's cascade or
  presumably in headless Chrome's own font lookup -- this looks like
  Chromium's own internal, environment-specific last-resort fallback for
  a `sans-serif` generic family when no real font config is found, not
  something driven by the page's CSS at all. A real fix would mean
  matching Chrome's exact internal fallback font/metrics for this
  specific headless-without-fontconfig case -- a deep, environment-
  specific problem, not a narrow CSS logic bug; refines rather than
  replaces the existing "font-metric rendering noise" note above, but
  explains why it recurs so pervasively across unrelated folders/tests.
css-box/, css-text/ - both tiny (10 fixtures each), almost entirely CSS
  Animations tests (unimplemented feature, no static geometry).
css/selectors/ - swept properly (365 fixtures). 3 real domonic fixes (see
  log below: `:link`/`:visited`, `:dir()`, and a broader bug in chromonic's
  own existing single-compound cascade-matching fallback that was
  wrongly rejecting `:has()`/any functional pseudo-class whose own
  argument contains a space/`>`/`+`/`~`). Remaining failures dominated by
  categories needing real interactivity/JS execution chromonic doesn't
  have at all (`:focus-visible`/`:focus-within`/`:active`, ~40+ fixtures,
  need real focus state + `element.focus()`), pure JS-testharness
  `.matches()`/`querySelectorAll()` assertion tests with no visual
  component (geometry comparison is meaningless noise there), Shadow DOM
  (`:host`, `<template shadowrootmode>` -- an entirely separate,
  unimplemented rendering model), bidi/RTL text geometry (already-known
  gap), and a couple of niche Unicode grapheme-cluster edge cases in
  `::first-letter` (a flag emoji, U+FEFF) plus `::first-letter { float:
  left }` drop caps -- none attempted, all narrow/niche.
css-inline/ - fully sampled (all 385 fixtures), not tractable this
  session: fixtures 0-200 are overwhelmingly SVG-adjacent advanced
  typography (`alignment-baseline`, `baseline-shift`, `dominant-
  baseline`, `baseline-source`) and `initial-letter` (multi-line drop
  caps, its own large CSS Inline Layout 3 feature with dedicated block-
  position/ruby-interaction rules); fixtures 200-385 are almost entirely
  `text-box-trim` (a single, separate, recent CSS Text feature --
  trimming leading/trailing space above/below text based on real font
  metrics -- chromonic has no foundation for it at all). Both large,
  genuinely separate features, not quick fixes. A few fixtures are also
  scroll-/JS-driven (`scrollIntoView()`, `reftest-wait` + a mutation
  script) where chromonic's lack of scroll simulation makes the geometry
  comparison itself unreliable, independent of any real layout bug.
left todo: rest of CSS2/, css-box/, css-flexbox/, css-text/

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



3

------

Found fixing real-site rendering (Wikipedia), all patched in
`domonic_*_patch.py`:

- `_scrape._load_external_stylesheets`/`_fetch_one` fetch every `<link
  rel=stylesheet>` with no headers, so any site that 403s an anonymous
  `requests` UA (Wikipedia does) silently gets 0 rules from every external
  stylesheet. Patched by passing `_load_remote`'s own session headers
  through as `request_kwargs`.
- `_cssom.expand_shorthand`'s generic single-token branch broadcasts that
  token to every longhand -- wrong for `flex-flow`, whose two longhands
  have disjoint keyword sets (`flex-flow:row` set `flex-wrap:row`, which
  then crashed the Rust layout engine outright). Patched in
  `domonic_flex_flow_patch.py`.
- `ComputedStyleDeclaration._font_size_px` reads its raw `font-size`
  without expanding `var()` first (every other property already does this
  before `_to_used_length`), so `font-size: var(--x, 0.875rem)` -- common
  on any site using CSS custom-property design tokens -- silently computes
  as the inherited size instead. Patched in
  `domonic_var_font_size_patch.py`.



4

------

Found building the stylesheets on/off toggle (F9): `CSSStyleSheet.
disabled` is a plain, inert `bool` -- the cascade never checks it at all,
and even fixed, toggling it wouldn't invalidate either the per-document
rule-index cache or the per-element computed-style cache, both keyed on
`_cssom.stylesheet_epoch()`, which only `insertRule`/`deleteRule`/
`replace(Sync)` bump. Patched in `domonic_stylesheet_disabled_patch.py`:
`_build_rule_index` now skips disabled sheets, and `disabled` is a real
property that bumps the epoch when it actually changes.



5

------

Found running `tests/wpt/css/CSS2/colors/colors-007.xht`: `Computed
StyleDeclaration._to_used_color` resolves `currentcolor` via `re.sub(
r"...currentcolor...", current, value)`, passing the resolved color
straight through as `re.sub`'s *replacement* string -- a raw backslash-
digit sequence in it (`\45` survives unescaped from a CSS identifier hex
escape domonic's tokenizer doesn't resolve, e.g. `color: g\re\45n`) is
read by Python's `re` as a backreference to a nonexistent capture group,
raising `re.PatternError` and aborting the whole layout pass over one
CSS declaration. Patched in `domonic_currentcolor_replace_patch.py`: the
same substitution via `re.sub`'s function form, never interpreted as a
backreference template regardless of content. The escape-sequence gap
that leaves `\45` unresolved in the first place is separate and deeper
(domonic's CSS tokenizer), not fixed.



6

------

Found in `CSS2/linebox`: `ComputedStyleDeclaration._to_used_length`'s
`line-height` branch never rejects a negative value -- CSS 2.1 10.8.1
disallows negative `line-height`, so real Chrome throws the whole
declaration out (`getComputedStyle` reports `normal`), but domonic
used-value-resolves e.g. `line-height: -1pc` straight to `"-16px"`. That
negative value then reaches `chromonic.tree._resolved_line_height` as a
real number and collapses the element's line box. Patched in
`domonic_negative_line_height_patch.py`: wraps `_to_used_length`,
substituting `"normal"` whenever a `line-height` used-value resolves
negative. Same folder also showed `_to_used_length` never resolves `ex`
units for `line-height` at all (falls back to the generic `0.5em`
placeholder `_length_string_to_px` uses when no real font metrics are
available) -- extended the existing `domonic_ex_unit_patch.py` to also
wrap `_to_used_length`, reusing its real `x_height()`-backed resolution
for this one property/unit combination.



7

------

Found rendering `news.ycombinator.com`: the CSS `:link`/`:visited`
pseudo-classes (present since CSS1) are never matched during the cascade
at all -- `Element._STRUCTURAL_PSEUDO_CLASSES` (what `_parse_simple_
selector` accepts) has no `link`/`visited` case, so a rule like `a:link
{ color: #000 }` silently never applies; every link kept chromonic's own
UA-stylesheet blue instead of the page's real black. Patched in
`domonic_link_pseudo_patch.py`: chromonic never simulates navigation
history, so `:link` now matches any `<a>`/`<area>` with a non-empty
`href` (the honest, privacy-safe stance a fresh browser profile already
takes) and `:visited` never matches anything.



8

------

Found in `css/selectors`: the `:dir()` functional pseudo-class is never
matched either -- `domonic.bs4._strip_simple_pseudo` (the per-compound
parser both the combinator-chain matcher and the existing single-
compound `_matchElement` fallback share) has no `:dir()` case, so `:dir
(ltr) { color: blue }`-style rules never apply. Patched in `domonic_dir_
pseudo_patch.py`, resolving it via HTML's own directionality algorithm:
walk up from the element for the nearest `dir="ltr"`/`"rtl"` attribute
(an invalid value like `dir="foopy"` is skipped, same as a real browser),
defaulting to `ltr` at the root. Deliberately doesn't also consult the
element's *computed* `direction` CSS property (`chromonic.tree._element_
direction`, used elsewhere in this project for real layout, does) --
resolving a full cascade from inside a bare selector-matching utility
isn't a small change, and every real use of `:dir()` (this fixture
included) drives it from the `dir` attribute alone.
