# objective: use the real web-platform-tests suite as a source of already-written CSS2.1/WPT test markup, run it through the Chrome-vs-chromonic geometry harness, and fix whatever chromonic gets wrong. If the issue is with domonic itself patch it for now, log it here so it can be fixed upstream. If the issue is with chromonic's own layer, fix it in this repo.

Domonic 1.8.4 is now the pinned baseline. It incorporates the former
Chromonic compatibility fixes for `currentcolor`, `:dir()`, `flex-flow`,
`:link`/`:visited`, negative `line-height`, print media matching, selector
fallback, stylesheet disabling, and `var()` font sizes, so the obsolete local
patch modules were removed. It also separates cascade caching from layout-dependent used
values and caches parsed selectors; on the 5,001-node/200-rule layout fixture,
retained full-style relayout improved from 695 ms on 1.8.3 to 398 ms on 1.8.4,
and cached-style relayout from 652 ms to 369 ms (both about 43%).

Follow-up profiling on the same 5,001-node fixture removed two remaining
unchanged-pass costs: `reuse_styles=True` now reuses the retained anonymous
table/inline child projection instead of reclassifying the entire DOM, and
the combined feature scan skips RTL and used-auto-margin correction walks
when the page has neither feature. Cached relayout fell from 380 ms to 187 ms
(51%); retained full-style relayout is 372 ms. Native Taffy compute remains
about 10 ms, so the next material step is dirty-subtree reconciliation rather
than more work inside the native solver.

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
CSS2/positioning/ - 527/555 captured fixtures (95%) on a chromonic-only
  replay against the Chrome baseline (23 scripted `dynamic-*`/uncaptured
  fixtures excluded). Remaining: floats inside inline text, Chrome's
  per-item client rects for undecorated ("culled") inlines, relative
  offsets inside scrollable containers, a few inline-rect height details.
CSS2/box-display/
CSS2/margin-padding-clear/ - 86 failed / 69 errors (out of 739 total)
CSS2/linebox/ - sampled ~250 fixtures. Fixed: negative `line-height`
  rejected to `normal` (domonic bug, see log below), `ex`-unit
  `line-height` using real font x-height instead of a flat 0.5em guess
  (same domonic bug). Remaining failures dominated by numeric/percentage
  `vertical-align` offsets and inline-block baseline computation -- not
  attempted, same scope as the already-noted font-metric rendering noise.
CSS2/normal-flow/ - 524 passed / 230 failed / 37 errors
CSS2/table/ - 909/962 captured fixtures (94%) on a chromonic-only replay
  against the Chrome baseline (5 Chrome-timeout fixtures uncaptured).
  Remaining: script-toggled display and unexplained Chrome behaviour in
  table-anonymous-objects, inline-table/float inside inline text,
  abs-positioned cells, Chrome's per-word text rects in collapsed columns.
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
css-flexbox/ - 706/1203 captured fixtures (59%; 1448 eligible, 210
  `flags=dom` skipped, ~35 Chrome timeouts/untaggable). Full sweep in six
  batches of 250 (`/tmp/wpt_flexbox_f1..f6`). Built this pass: flex items
  are blockified (a container of inline/inline-block children no longer
  takes the inline path), `inline-flex`/`inline-grid` as atomic inlines,
  `order`, `direction: rtl` mirroring, abs-child static position per
  `justify-content`/`align-self` (incl. `safe`, `self-start`, rtl),
  `align-self: baseline` on real items, `safe` overflow fallback (cross
  axis), `min-width: auto` = real min-content (new `MinContent` sentinel
  in `src/lib.rs`), `width: min/max-content`, `flex-basis: content`,
  indefinite % basis, stretched/ratio-sized `<img>`/`<canvas>` items,
  `<br clear>` among floats, Box Alignment keyword normalisation, invalid
  negative flex values. Harness: testharness.js's `html { font-family }`
  and `#log` are mirrored into the tagged copy (the old "~11.9px offset"
  was Arial vs Times). Remaining: vertical writing modes (88 fixtures),
  `aspect-ratio`/img transfer sizes (~35), nested-container baselines,
  `flex-wrap: balance` (tentative), calc(), CSS nesting, pseudo-element
  items, `contain`. KNOWN CAPTURE BUG (found and fixed later, during
  css-grid, not yet re-applied here): `run_wpt.py`'s `#log`-hiding rule
  only inserted if the fixture had an explicit `<head>` or `<html>` tag;
  a bare `<!DOCTYPE html><title>...</title><style>...` fixture (common,
  valid HTML5) silently kept `#log` visible, so its Chrome capture has
  testharness.js's real per-assertion results table inflating the page
  height, corrupting every later element's `y`. 243 of the 372
  `flags=dom`-eligible-ish (testharness.js-using) css-flexbox fixtures
  lack a `<head>`/`<html>` tag and may be affected -- the 694/1203 figure
  above is against that possibly-corrupted baseline for some fraction of
  them. Re-capturing is a `--start/--limit` batch sweep away but not done
  this session (budget); flagged here rather than silently left wrong.
css-grid/ - 257/551 captured fixtures (47%). `grid-lanes` (CSS Grid 3
  masonry, a different `display` value, 1707 fixtures) deliberately never
  captured -- unimplemented feature, out of scope. Swept: all seven core
  layout folders (grid-definition/grid-model/layout-algorithm/parsing/
  placement/implicit-grids/grid-items, 503 eligible) plus one 250-fixture
  batch of `alignment` (430 eligible); `abspos` (215) and `subgrid` (135)
  not started. Before this pass grid was essentially unexercised --
  `justify_items`/`justify_self`, `grid_auto_rows`/`grid_auto_columns`,
  `minmax()`, `fit-content()`, and `repeat()` (literal count and
  `auto-fill`/`auto-fit`, via a real Taffy `GridTemplateComponent::Repeat`
  now, not flattened) were entirely unwired in `src/lib.rs`/
  `style_bridge.py`; all added this pass. Also fixed: a grid item's own
  `min-width: auto` was unconditionally forced to `0` (predated this
  session's flex `width:auto`-substitution exclusion that made it
  unnecessary; killed every grid item's real automatic-minimum
  contribution), `grid-area` shorthand not expanded by domonic (worked
  around), a table row's/cell's *specified* height being included in the
  proportional-surplus-height split instead of held fixed (CSS 2.1
  17.5.3), and `overflow != visible` wrongly blocking a block's own
  top-margin collapse with its first child's (should only block a
  *grandchild's* margin escaping past it) -- the last three are general
  engine fixes, not grid-specific, and lifted CSS2/table to 909/962,
  CSS2/positioning to 527/555, and css-flexbox to 699/1203 too (numbers
  above already updated). SEPARATELY, an unrelated regression was found
  and fixed this session: `browser.py`'s new `set_presentational_hint_
  resolver` call used plain `domonic.style.` attribute access, which
  `domonic/__init__.py`'s own `from domonic.html import ..., style, ...`
  shadows (the `<style>` tag class, not the submodule) -- this broke
  *every* chromonic import outright until fixed via the established
  `sys.modules["domonic.style"]` workaround every other `domonic_*_patch`
  module already uses. Remaining: an unresolved, unreproduced-in-
  isolation bug where a *later* occurrence of an otherwise-identical
  `repeat(auto-fill/auto-fit, ...)` grid (same CSS, same available width)
  sometimes gets a completely different track count than an earlier one
  (`grid-definition/grid-auto-fill-columns-001.html`; several minimal
  repros with matching structure did not reproduce it); a parallel
  column-width version of the row-height fix above (an explicit column
  width still gets redistributed into by extra table width) in the same
  fixture's reference table; `aspect-ratio` (already logged, unmodelled);
  named lines/`grid-template-areas` (unmodelled); `calc()` (unmodelled).
CSS2/floats/ + CSS2/floats-clear/ - 110/366 captured fixtures (30%; 25
  dom-skipped). Previously floats were a heuristic on top of Taffy's
  `flex-wrap` with one real structural gap: a float that is a *direct
  element child of a block that also has real text* (the single most
  common float pattern -- `<p>text <img style="float:left"> more
  text</p>`) made `_inline_mixed_content`'s own child-qualification gate
  reject the whole container outright (a float was simply never an
  accepted child there), which fell through to a fallback with *no
  notion of direct text at all* -- not just the float's position, the
  entire surrounding paragraph's text silently vanished. Fixed: a float
  now qualifies there (mirroring how an absolutely positioned child
  already did), and `build()`'s own text/element flex-row approximation
  now tracks any float it contains separately from its ordinary row
  members (never baseline-aligned) and hands them to a new pass,
  `_fix_inline_float_position`, which corrects only each float's own `x`
  -- flush to the container's left/right content edge (CSS 2.1 9.5.1),
  dropped below an earlier same-container float it would otherwise
  overlap (rule 7) -- leaving Taffy's own row-wrap `y` and every other
  row member's position untouched. Deliberately narrow to keep the
  change low-risk: does *not* yet narrow surrounding text around the
  float's rectangle (real "inline layout consults active floats" line-
  shortening is a substantially bigger feature); a float mixed with an
  *other, non-floated inline-level element* sibling (not text) still
  isn't real-packed, since `_fix_float_flow_after_block_sibling` bails
  its whole container whenever a qualifying-but-not-floated sibling is
  present (`floats-clear/floats-001.xht`: an inline-block next to a
  float on the same line) -- a natural next extension of the same
  pattern, not attempted this pass. Verified zero regressions across
  CSS2/table (909/962), CSS2/positioning (527/555), css-flexbox
  (699/1203), and css-grid (254/551) before and after. Remaining, by
  family: `clear`/`float` applying to table-internal displays
  (`clear-applies-to-*`, `float-applies-to-*`), various margin-collapse-
  with-float interactions (`margin-collapse-clear-*`, `margin-collapse-
  *`), multicol (`floats-clear-multicol-*`, unimplemented feature).
css-sizing/ - 205/608 captured fixtures (34%; 0 errors). Swept the whole
  folder in three 250-fixture batches (not split by subfolder). `aspect-
  ratio` was entirely unwired going in: added `Style.aspect_ratio` ->
  `style_bridge._aspect_ratio()` (a domonic `Ratio` -> plain `width/height`
  float, guarded against a CSS-invalid zero-length ratio component -- see
  domonic log below), a declared ratio overriding an image's *natural*
  ratio in `_apply_image_intrinsic_size`, and a `_has_ratio_derived_height`
  guard stopping `_fix_nested_bfc_float_auto_height`/`_fix_float_flow_
  container_auto_height` from silently overwriting Taffy's own correct
  ratio-derived height with a summed-content one. Also fixed a real crash
  (`OverflowError: cannot convert float infinity to integer` on a `0`-
  length ratio component). Separately, `position:absolute` boxes with an
  `aspect-ratio` were fully ignoring it in both of CSS 2.1 10.3.7/10.6.4's
  inset-constraint-equation solvers (`_fix_absolute_width_against_
  containing_block`/`_fix_absolute_height_against_containing_block`),
  always stretching to fill both axes via insets regardless of the ratio;
  fixed per CSS Position 3's abspos-auto-size rule (block axis is ratio-
  dependent unless only the inline axis has an auto inset, in which case
  inline is) for the two unambiguous, narrow cases: all four insets
  definite (height derives from the already-inset-solved width), and one
  definite dimension + the other auto with a ratio (the auto one derives
  from the ratio, overriding the insets equation) -- 14/22 `aspect-ratio/
  abspos-*` fixtures now pass, up from a handful. Confirmed as a genuine
  cross-cutting improvement: css-flexbox +7 (699->706/1203), css-grid +3
  (254->257/551), zero regressions elsewhere. Deliberately deferred (all
  narrow, spec-advanced, or unimplemented-feature gaps): the CSS Sizing 4
  "aspect-ratio minimum size contribution" (a box whose content would
  overflow its ratio-derived size gets an automatic-minimum clamp that
  respects content size -- the same underlying mechanism as the already-
  known flex automatic-minimum-size gap, just also reachable from plain
  abspos boxes, e.g. `abspos-012`/`abspos-013`); the mirrored inline-axis-
  has-the-lone-auto-inset case (would need a coordinated two-axis pre-pass
  since the two insets-equation functions currently run and mutate
  independently); re-deriving the *stretch* axis from the ratio after the
  *ratio* axis gets min/max-clamped (`abspos-021`); percentage `height`/
  `width` on an abspos ratio box (`abspos-009`); `contain-intrinsic-size`/
  `contain:size` (unimplemented CSS Containment feature, unrelated to
  aspect-ratio); vertical writing modes (no support at all); the `auto
  <ratio>` combined-syntax domonic gap (already logged). Also deferred,
  unrelated to aspect-ratio: `stretch-fit`/`stretch-quirk`/`stretch-table`
  sizing keywords, SVG intrinsic sizing (`svg-intrinsic-size-*`, 8
  fixtures), and a large `vert-block-size-*` fixture (247 fragment
  mismatches -- likely a distinct, bigger bug, not investigated).
css-box/ - tiny (10 fixtures), almost entirely CSS Animations tests
  (unimplemented feature, no static geometry).
css-text/ + css-text-decor/ - in progress (2927 + 665 fixtures; an
  earlier "10 fixtures, mostly animations" note here was simply wrong --
  that was some other, smaller folder). `word-break`/`overflow-wrap` were
  entirely unwired going in -- both map close to 1:1 onto Parley's own
  `WordBreak`/`OverflowWrap` style properties (`src/lib.rs`'s
  `layout_text` binding now takes and forwards both), so this was a
  wiring job, not new line-breaking logic: Parley already does real
  Unicode-aware intra-word breaking. Also handled: CSS Sizing 3's min-
  content carve-out for `overflow-wrap: break-word` specifically (must
  *not* shrink min-content, unlike `word-break: break-all`/`overflow-
  wrap: anywhere`, which do) -- confirmed directly (`break-word` and
  `normal` both report the same min-content width; `break-all`/`anywhere`
  correctly shrink it). Capture batches running; the `_InlineFormattingPlan`
  fallback path (mixed text+inline-element content, its own token-by-
  token placement rather than Parley's paragraph layout) doesn't have
  this yet -- only the single-text-leaf `_make_measure` path -- deferred
  pending real fixture results showing whether it's needed.
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


`_ABSOLUTE_FONT_SIZE_KEYWORDS` in `domonic/style.py` scales the `font-size`
keywords by CSS 2's 1.2 ratio (`small` 13.333px, `large` 18.667px); browsers
use the HTML font-size table for a 16px medium: xx-small 9, x-small 10, small
13, medium 16, large 18, x-large 24, xx-large 32, xxx-large 48 (Blink
`FontSizeFunctions::FontSizeForKeyword`). Seen on `tables/table-height-
algorithm-012.xht`. Worked around in `domonic_font_size_keywords_patch.py`
(updates the table in place).


------


`CanvasRenderingContext2D._record` only stores `{name, args}` per drawing
command, no snapshot of the style state (`fillStyle`/`strokeStyle`/
`globalAlpha`/etc.) active when that command was called -- so replaying
recorded commands later (chromonic's own paint path does this) uses
whatever style is current at replay time instead of at record time,
silently wrong the moment two draws with different styles are interleaved
with a style mutation. Domonic 1.8.4 now snapshots the style state itself,
but still stringifies `Path2D` arguments and loses their commands; the narrowed
`domonic_canvas_patch.py` remains only to preserve replayable path/gradient/
pattern arguments for Chromonic's Skia command replay.


------


`MediaQueryList._evaluate` has no concept of a "current media type" at
all -- `all`/`screen`/`print` all just unconditionally return `True`
regardless of what's actually rendering. Confirmed directly: `@media
print { ... }` applied during chromonic's own (always screen/interactive)
rendering, hiding content and applying sizing rules real Chrome never
does outside an actual print preview. Fixed upstream in Domonic 1.8.4;
the former local workaround was removed.


------


`ComputedStyleDeclaration`'s cascade only matches a rule's selector via
`Element._matchElement` (a small hand-rolled matcher, no `:nth-child`/
`:nth-of-type`/`:hover`/etc. support) or `_matches_selector_chain` (combinator
chains only) -- a single-compound selector using any pseudo-class outside
that whitelist (e.g. `div:nth-of-type(2)`) is silently never matched during
cascade resolution, even though `Element.matches()` correctly matches the
same selector (via a further `querySelectorAll()` fallback the cascade never
calls). Confirmed directly: `div:nth-of-type(2) { line-height: 30px }` never
applied to any element. Fixed upstream in Domonic 1.8.4; the former local
workaround was removed.


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
  then crashed the Rust layout engine outright). Fixed upstream in Domonic
  1.8.4; the former local workaround was removed.
- `ComputedStyleDeclaration._font_size_px` reads its raw `font-size`
  without expanding `var()` first (every other property already does this
  before `_to_used_length`), so `font-size: var(--x, 0.875rem)` -- common
  on any site using CSS custom-property design tokens -- silently computes
  as the inherited size instead. Fixed upstream in Domonic 1.8.4; the former
  local workaround was removed.



4

------

Found building the stylesheets on/off toggle (F9): `CSSStyleSheet.
disabled` is a plain, inert `bool` -- the cascade never checks it at all,
and even fixed, toggling it wouldn't invalidate either the per-document
rule-index cache or the per-element computed-style cache, both keyed on
`_cssom.stylesheet_epoch()`, which only `insertRule`/`deleteRule`/
`replace(Sync)` bump. Fixed upstream in Domonic 1.8.4; the former local
workaround was removed.



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
CSS declaration. Fixed upstream in Domonic 1.8.4; the former local workaround
was removed. The escape-sequence gap
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
real number and collapses the element's line box. Fixed upstream in Domonic
1.8.4; the former local workaround was removed. Same folder also showed
`_to_used_length` never resolves `ex`
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
UA-stylesheet blue instead of the page's real black. Fixed upstream in
Domonic 1.8.4; the former local workaround was removed. Domonic never
simulates navigation history, so `:link` matches any `<a>`/`<area>` with a non-empty
`href` (the honest, privacy-safe stance a fresh browser profile already
takes) and `:visited` never matches anything.



8

------

Found in `CSS2/tables` (`border-spacing-001.xht`): `border-spacing: -1px`
is accepted as-is by the cascade (`getComputedStyle` reports `"-1px"`) --
CSS 2.1 17.6.1 says lengths "may not be negative", so the declaration is
invalid and Chrome drops it, leaving the UA default `2px`. Worked around
in `tree.py`'s table-root branch (a negative spacing falls back to 2px).
Same for a percentage (`border-spacing-percentage-001.xht`: `20%` is
invalid, Chrome keeps the earlier `0px`; domonic keeps the percentage and
resolves it) -- read as 0 in `tree.py`. Not patched upstream.



9

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



10

------

Found in `CSS2/positioning` (`left-offset-percentage-002.xht`,
`relpos-calcs-005..007.xht`, `left-113.xht`): a percentage `top`/`right`/
`bottom`/`left` is resolved by `ComputedStyleDeclaration._to_used_length`
against the *viewport* width -- CSS 2.1 9.3.2 keeps the computed value
as the percentage (resolved against the containing block at use, and
`inherit` copies the percentage). Worked around in
`domonic_ex_unit_patch.py` (the percentage string passes through
unchanged). Not patched upstream.



11

------

Found in `css-flexbox` (`align-items-001.htm` and 21 other BOM-prefixed
`.htm` fixtures): a leading U+FEFF (UTF-8 byte-order mark) in the source
is kept as a text node before `<!DOCTYPE>`, so the parser treats it as
body text and demotes `<title>`/`<link>`/`<style>` into `<body>` after
it (a rendered first line, everything 26px lower than Chrome). Chrome
strips the BOM before parsing. Worked around in `browser.py`'s
`_RequestsResponseAdapter.text()` (which also decodes an undeclared-
charset body as UTF-8 rather than requests' ISO-8859-1 default). Not
patched upstream.



12

------

Found in `css-flexbox` (`align-items-baseline-row-horz.html`): the logical
size properties `inline-size`/`block-size` and their `min-`/`max-` forms
are not in `style.py`'s `_LOGICAL_ALIAS_LONGHANDS` table (which already
maps `margin-inline-start`, `padding-block-end`, `inset-inline-start`...),
so `inline-size: 100px` never becomes `width` and the element stays
`auto`. Same for the `border-inline-start`/`border-block-end` side
aliases. Patched in `domonic_logical_size_patch.py` (table extended in
place, LTR horizontal only like the rest of the table). Not patched
upstream.



13

------

Found in `css-flexbox` (`align-self-baseline-with-flex-wrap.html`): CSS
Nesting (`.row { width: 20px; .big { width: 30px } }`) is not parsed --
the nested `.big` rule never applies (computed `width: auto`, Chrome
`30px`). Not patched (a parser change); the fixture stays failing.



14

------

Found in `css-flexbox` (`aspect-ratio-intrinsic-size-001.html`, `flex-
aspect-ratio-cross-size-001.html`): the CSS Sizing 4 `aspect-ratio`
property is not modelled -- `ComputedStyleDeclaration` has no
`aspectRatio` and `LayoutStyle` carries nothing for it, so `aspect-ratio:
2 / 1` never reaches layout (Taffy itself supports an aspect ratio, and
`tree.py` already feeds it one for `<img>`). Not patched; the ~10
`aspect-ratio` fixtures stay failing.



15

------

Found in `css-flexbox` (the Mozilla `flexbox-*.xhtml` fixtures, e.g.
`flexbox-justify-content-horiz-001a.xhtml`): an XHTML document is parsed
with the HTML parser, so a self-closing non-void element (`<div class=
"a"/>`) is read as an open tag and every following sibling nests inside
it -- Chrome parses `application/xhtml+xml` as XML, where it is a
complete empty element. Worked around in `browser.py`
(`_expand_xhtml_self_closing_tags`: for an XHTML content type, XML
prolog or XHTML namespace, `<tag .../>` becomes `<tag ...></tag>` for
non-void tags before parsing). Not patched upstream.



16

------

Found in `css-flexbox` (`flexbox-min-width-auto-001.html`'s `width:
calc(10% + 50px)`, `auto-margins-001.html`'s `calc(100% - 4em)`,
`flex-item-compressible-001.html`): `calc()` is never evaluated -- the
value reaches `LayoutStyle` as an opaque `Keyword`, which
`style_bridge._len()` drops to `auto`. Not patched (needs a real
expression evaluator in the cascade, with percentages kept symbolic);
these fixtures stay failing.



17

------

Found in `css-grid` (`grid-items/grid-inline-order-property-painting-
*.html`, `grid-inline-z-axis-ordering-*.html`, and any other fixture
placing an item with `grid-area`): the `grid-area` shorthand (`<row-
start> / <column-start> / <row-end> / <column-end>`) is never expanded
into its four longhands -- `grid-row-start`/`grid-column-start`/etc. all
stay their own initial `auto`, so every `grid-area`-placed item falls
through to ordinary auto-placement instead (two items both declared
`grid-area: 1 / 1` landed in two separate auto-placed cells instead of
overlapping in the same one). Worked around in `tree.py`
(`_parse_grid_area`, reading the raw cascade the same way
`justify-items` above already needs to). Not patched upstream.



Found in `css-flexbox` (`flexbox_flex-0-0-1-unitless-basis.html`,
`flexbox_flex-0-0-N-unitless-basis.html`): `flex: 0 0 4` (a unitless,
non-zero third value) is an invalid declaration that Chrome drops
(leaving `flex: 0 1 auto`); domonic accepts it and reports `flex-basis:
0%` (the same computed value it gives a valid `flex: 1 2 0`), so the
items collapse to a 0 basis. Not patched (indistinguishable from a valid
`0%` at the computed level); the two fixtures stay failing.



18

------

Found in `css-flexbox` (`flexbox-mbp-horiz-003.xhtml` and its `-reverse`/
`v` variants): an HTML comment (`<!-- ... -->`) between rules inside a
`<style>` block makes the CSS parser drop the rule that follows it (the
`.borderA` rule never applied, so three containers lost their borders).
CSS Syntax 3 §5.4.1 ignores CDO/CDC tokens at the top level of a
stylesheet, as Chrome does. Worked around in `browser.py`
(`_strip_html_comments_in_style`, applied to fetched documents). Not
patched upstream.



19

------

Found in `css-flexbox` (`flexbox-safe-overflow-position-003.html`):
`justify-content: safe flex-start` computes to plain `flex-start` -- the
`safe` overflow-position prefix is dropped for `justify-content` (and
`align-content`), while `align-self`/`align-items` keep it (`"safe
center"`). `tree._fix_flex_safe_alignment` can therefore only honour
`safe` on the cross axis. Not patched upstream.



20

------

Found in `css-sizing` (`aspect-ratio/zero-or-infinity-001.html`): CSS
Sizing 4's `<ratio>` requires both numbers to be non-zero (a `0` on
either side makes the whole declaration invalid, falling back to the
property's initial `auto`) -- domonic's `_parse_aspect_ratio` accepts
`aspect-ratio: 0/1` as a real `Ratio(0.0, 1.0)` regardless. Worked around
in `style_bridge._aspect_ratio` (a non-positive component reads as `auto`
instead). Not patched upstream.



21

------

Found in `css-sizing` (`aspect-ratio/block-aspect-ratio-004.html`/
`-006.html`): the `auto <ratio>` combined `aspect-ratio` syntax (CSS
Sizing 4 -- "use the box's own natural ratio if it has one, else fall
back to `<ratio>`") is indistinguishable from a bare `<ratio>` once
parsed -- `_parse_aspect_ratio` strips the `auto` token before matching,
so `aspect-ratio: auto 4/1` and `aspect-ratio: 4/1` both resolve to the
same `Ratio(4.0, 1.0)`, with no way to tell whether `auto` was also
written. Immaterial for a replaced element with its own natural ratio
(the common case this syntax exists for), but wrong for a non-replaced
box, where Chrome's actual resolution of the `auto`-combined form
doesn't match using the ratio outright either (still unexplained --
`-004.html`'s second `<div>` measured a height matching neither its own
`auto 1/1` ratio nor its sibling's plain `2/1`). Not patched upstream;
narrow in practice.



------

22

------

Found running a real page's JS (`readthedocs-addons.js`, ordinary docs
site): the JS interpreter's auto-bound-globals collection (`domonic_libs.
acorn.interpret._collect_domonic_globals`) is missing a wide swath of
real DOM/CSSOM interfaces, for two different reasons. (1)
`_GLOBAL_DENYLIST` deliberately excludes `Document`/`Node`/`Element`/
`Window`/`Text`/`Comment`/`CharacterData`/`Attr`, even though `domonic.
dom`/`domonic.window` have a real class for every one of them. (2) The
whole CSSOM (`CSSStyleSheet`/`CSSRule`/`CSSStyleDeclaration`/`MediaList`/
...) lives in `domonic.style`, a module `_collect_domonic_globals` never
scans at all (only `domonic.javascript`/`domonic.webapi.*`/`domonic.dom`
are). A real browser exposes every one of these as a bare global for
ordinary `instanceof`/`typeof` feature-detection, so a script checking
`typeof Document` or `typeof CSSStyleSheet` hits `ReferenceError: ... is
not defined` instead. Worked around in `js_sandbox._dom_interface_globals`/
`_cssom_interface_globals` (chromonic's own sandboxed scope override for
remote-page scripts, not the vendored package). Not patched upstream.



------

23

------

Found running a real page's JS (same `readthedocs-addons.js`, past the
`Document`/`CSSStyleSheet` gaps above): `domonic_libs.acorn.interpret`
has no `Proxy`/`Reflect` support -- an explicit, stated scope boundary,
not an oversight: its own module docstring says plainly "Not covered --
real prototype chains, `Proxy` / `Symbol`, `with`." (`Symbol` is the same
boundary and likely to surface the same way on some other real site.)
Unlike entry 22, this is not a missing-global fix -- `Proxy` is a core
ECMAScript language feature
requiring the interpreter itself to intercept every property get/set/
has/delete on an object and route it through trap functions; there is no
existing domonic class to point a global at, the way `Document`/
`CSSStyleSheet` had one. A real implementation is a genuine interpreter
feature, upstream `domonic_libs`/`myjs` work, not a `js_sandbox.py`-side
workaround -- and not attempted as a partial/fake shim, since a `Proxy`
that exists but doesn't actually trap property access would fail in more
confusing ways than today's clean `ReferenceError`. Expect this on other
real sites too (`Proxy` is common in modern reactive-framework code --
Vue 3, MobX, plain "reactive object" patterns); each occurrence is this
same gap, not a new bug. Not patched upstream.
