# objective: use the real web-platform-tests suite as a source of already-written CSS2.1/WPT test markup, run it through the Chrome-vs-chromonic geometry harness, and fix whatever chromonic gets wrong. If the issue is with domonic itself patch it for now, log it here so it can be fixed upstream. If the issue is with chromonic's own layer, fix it in this repo.


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
CSS2/visudet/ - 31/40 PASSING
CSS2/positioning/ - 527/555 PASSING (old full run); first 250 fixtures now 225/250
CSS2/box-display/ ??
CSS2/margin-padding-clear/ - 86 failed / 69 errors (out of 739 total)
CSS2/linebox/ - first 60 fixtures: 43 pass / 15 fail
CSS2/normal-flow/ - 524 passed / 230 failed / 37 errors (old full run); first 200 fixtures now 98 pass / 37 fail / 4 errors
CSS2/tables/ - 920/1137 PASSING with the table node (old code: 909 of the same 1137; 2 regressions left: row-visibility-003/004)
CSS2/backgrounds - 173/200 PASSING
CSS2/colors ??
CSS2/positioning/ - (575 fixtures). ?? COMPLETE?
css-position/ ? no idea
css-display/ - ??
css-flexbox/ - 706/1203
css-grid/ - 257/551 
CSS2/floats/ + CSS2/floats-clear/ - 110/366 (old); now floats first 120: 36 pass, floats-clear first 60: 46 pass
css-sizing/ - 205/608 
css-box/ - tiny (10 fixtures) and??
css-text/ + css-text-decor/ - 257/1538
css-overflow/ - 223/761
css-grid (257/551),
CSS2/positioning (527/555),
cssom-view/ - 43/153
css/selectors/ - (365 fixtures). ?? passing what?
css-inline/ - (all 385 fixtures); first 100 now 8 pass (mostly unimplemented CSS Inline 3)

left todo: everything still cos the agents get lazy and just get into habbits of running loads of fixtures and not fixing things. Today one ran a few hundred and fixed fuck all and then said what should i do next. gets a bit fucking dull.

Agent should NOT run full suite of tests between fixes. It takes too long and waiting ages per fix is not productive. Instead run full verification between batches of fixes.

## engine structure (after the inline/float rebuild)

- `src/lib.rs` owns the layout tree (`LayoutPartialTree`), not Taffy's built-in
  `TaffyTree`: block/flex/grid are Taffy's algorithms, floats included
  (`float`/`clear` reach Taffy's block formatting context); an `inline` node
  kind is chromonic's own inline formatting context (`compute_inline_layout`
  + `inline_formatting._InlineFormattingPlan.measure`). Text leaves report
  first/last baselines to Taffy.
- Inline content is one path: the plan places text, atomic inline-level
  boxes (inline-block/replaced/inline-table, built as real children of the
  inline node, sized by their own formatting context) and floats (placed
  through the block formatting context, so later siblings and line boxes
  flow around them). Line boxes are shortened per float band over their full
  height; too-narrow lines shift down. The flex-wrap approximations
  (`_approximate_inline_flow`, `_build_inline_flex_row`,
  `_group_inline_element_runs`) and every post-layout float/strut/flex-row
  repair pass (`floats.py`, `_fix_flex_row_baseline_alignment`,
  `_apply_linebox_strut_height`, ...) are gone.
- Tree roots: a viewport-sized initial containing block holds `<html>` (a
  real box, a BFC) and every `fixed`/root-absolute box; `<body>` is an
  ordinary block child. Static positions of escaped positioned boxes come
  from zero-size placeholders left in the flow (`src/lib.rs` anchors).
- `direction`, `safe` alignment, sizing keywords (`min-content` ...) and
  legacy `<center>`/`align=` alignment are passed to Taffy, not corrected
  afterwards. Taffy is vendored (`vendor/taffy`, patches listed in
  `vendor/taffy/CHROMONIC_PATCHES.md`).
- Tables are a `table` node kind (`table_formatting.py`): columns, rows,
  rowspans, baselines and vertical-align are resolved during layout.
- Per-node layout state lives on one `Box` (`tree/box.py`, `box_of(node)`),
  not on `_chromonic_*` attributes.
- Post-layout passes left: publishing inline fragments, relative offsets of
  split inlines, `<col>` boxes, absolute boxes whose containing block is an
  inline, and static positions inside flex/grid parents.
- `white-space` is honoured in inline flow (`pre`/`pre-wrap`/`pre-line`/
  `break-spaces`/`nowrap`: preserved spaces, newlines as forced breaks, no
  wrapping) via `_runs_for_text`.
- Known remaining gaps: `<br>` with its own font-size, CSS Inline 3
  (`baseline-shift`, `alignment-baseline`, `initial-letter`), inline-level
  text next to a float when the *line* is taller than the band it starts
  in for text leaves (plan-based inline nodes re-lay such lines; leaves
  use the line's own uniform height). Taffy 0.14 quirks seen: a BFC box with a
  negative remaining width beside a float still "fits" (floats-wrap-bfc-with-
  margin-004), and a float wider than its containing block is dropped below
  earlier floats where Chrome keeps it at the top (floats-rule3-outside-*).
- The WPT harness reuses a stored `chrome.json` baseline for an unchanged
  fixture when its tagged id set matches (`--fresh-chrome` to force);
  `el.style = ...` scripted fixtures are skipped like `.style.x =` ones.

- Numbers after the rebuild (same harness, same samples; "before" is the
  pre-rebuild summary where one existed): CSS2/visudet 7 -> 31 of 40;
  CSS2/box 8 -> 8 of 11; CSS2/linebox (first 60) 43 pass; CSS2/floats
  (first 120) 19 -> 36 pass; CSS2/floats-clear (first 60) 46 pass;
  CSS2/positioning (first 150) 134 pass; CSS2/normal-flow (first 200)
  103 -> 98 (stored baselines for several of the "regressions" were from an
  older tagger/engine state, e.g. block-non-replaced-height-* lay out
  identically on the committed code); css-inline (first 100) 8 pass -- that
  folder is mostly CSS Inline 3 features (initial-letter, baseline-shift,
  alignment-baseline) chromonic doesn't implement.

## known chromonic engine gaps (found via real-world use, not WPT)


`inline_formatting.py`'s `_build_text_runs_from_nodes` silently drops a
replaced/control element (`<input>`, `<img>`, ...) nested inside a plain
`display:inline` wrapper (e.g. `<label><input type=radio></label>` with
no CSS at all -- label's UA-default display) -- confirmed directly: the
element gets no layout box whatsoever. Root cause: the function's
"flatten a nested genuine-inline-wrapper" branch (recursing into a
wrapper's children looking for text) has no case for "this specific
child is itself an atomic replaced box needing its own run" -- it
recurses into the replaced element's own (empty) children and gets
nothing back, so no run, no box. `_contains_in_flow_block`'s existing
9.2.1.1 split-trigger only checks for block-level descendants, not
replaced ones, so it doesn't catch this either. A bare replaced element
(not nested in another inline wrapper) is unaffected -- only this one
nesting shape. Real-world impact is plausibly wide (label-wrapped form
controls without explicit CSS are extremely common) but unconfirmed
beyond the direct repro. Not fixed yet -- found investigating the forms
demo, needs focused time given how large/fixture-tuned this file is.

A `@font-face` URL resolves to a malformed, doubled address on bbc.com --
`[font] GET https://www.bbc.com/https://static.files.bbci.co.uk/.../
Freight_Disp_Light.woff2`, page URL directly concatenated onto an already-
absolute URL. Confirmed broken (that fetch fails); NOT confirmed root
cause despite a thorough attempt -- `urllib.parse.urljoin` (used
throughout `webfonts.py`) correctly leaves an already-absolute URL
unchanged against any base in every variant tried directly (plain
absolute, protocol-relative `//host/...`, multi-source `local(),
url()...` declarations, a double-join of an already-resolved URL); a
repo-wide grep for naive `+`/f-string URL concatenation (the only other
shape that reproduces this exact "base directly prefixed onto a complete
second URL" result) found nothing. All ~19 other BBCReith font variants
on the same page resolved correctly, so whatever's different about this
one font specifically (JS-injected via `document.fonts`/`FontFace`
rather than a `<link>`/`<style>` `@font-face` rule? something particular
about how BBC declares it?) wasn't pinned down without fetching the live
page's actual served CSS to inspect, which this pass didn't do. Next
step: capture bbc.com's real HTML/CSS (e.g. via chromonic's own F8
view-source, or a saved HAR) and trace this one font specifically rather
than guessing at plausible CSS shapes.

Separately, but found the same session: fetching every declared
`@font-face` variant unconditionally, regardless of whether the page
currently needs it, is real (if lesser) inefficiency -- ~20 requests for
one BBC page load. Not incorrect, just not lazy the way `font-display`-
aware real browsers are. Not investigated further.


## log domonic issues here to be fixed upstream

PERF: Wikipedia's Python article: load (fetch+parse+CSS) ~0.5s, layout ~8.4s,
almost all of it domonic's cascade. `_collect_author_declarations` runs
19,861 times for 1,423 elements (~14x, about the tree depth: ancestors'
styles get re-cascaded instead of hitting `_computed_style_cache`), giving
709k `_matches_selector_chain` calls. Each of those also re-runs the
`from domonic.bs4 import (...)` at the top of `_matches_selector_chain`
(~1.1s of import machinery under cProfile). GC and streaming/progressive
parse are not the bottleneck (GC off saves ~0.25s total).

domonic doesn't drop an invalid declaration in favour of an earlier valid
one: python.org's `white-space: pre-wrap; white-space: -o-pre-wrap;` computes
to `-o-pre-wrap`. Worked around in `dom._white_space`.

domonic keeps an invalid keyword as a property's computed value (e.g.
`grid-auto-flow: columns`, a typo in css-inline/baseline-source/*, comes
through as `"columns"`) where CSS drops the declaration at parse time.
chromonic now falls back to the property's initial value in `src/lib.rs`'s
`parse_style` instead of failing the page.

domonic's Python API keeps a bare `str` child (`div("text")`) in `args`
rather than a `Text` node; layout wraps these in a text-node shim
(`dom._StringTextNode`) so inline formatting sees them.

------


`CSS.supports()` doesn't validate a property's *value*, only that the
declaration's syntax parses -- `CSS.supports("(display: bogus-value-xyz)")`
returns `True`. Minor, pre-existing, not chromonic's to fix.


------


Inline HTML event-handler attributes (`onclick="..."`, `onchange="..."`,
...) parse but never run: `element.onclick` after parsing `<button
onclick="foo()">` returns the literal *string* `"foo()"`, not a callable --
nothing compiles the attribute into a real listener or invokes it on
dispatch, confirmed with a direct `dispatchEvent(MouseEvent('click'))`
doing nothing. `<script>` tags execute fine (`js_sandbox.py`'s
`run_scripts()`); this is specifically the inline-attribute form. Not
chromonic's layer to fix (parsing/wiring, myjs and/or domonic). Not
patched -- `examples/forms_demo.html` uses `addEventListener` instead.


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
simulates navigation history, so `:link` matches any `<a>`/`<area>`/`<link>`
with an `href` (1.8.5) and `:visited` never matches anything.



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
`.htm` fixtures): `parseString(str)` keeps a leading U+FEFF as a text node
before `<!DOCTYPE>`, demoting `<title>`/`<link>`/`<style>` into `<body>`.
`parseString(bytes)` (1.8.5) strips it; chromonic now hands domonic the raw
bytes (`browser._RequestsResponseAdapter.bytes()`). The `str` path is still
unfixed upstream.



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
