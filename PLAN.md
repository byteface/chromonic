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

CSS2/box/
CSS2/positions/ - 50%
CSS2/box-display/
CSS2/margin-padding-clear/
CSS2/linebox/
CSS2/normal-flow/auto-margins-used-values(-with-floats), block-formatting-contexts-016,
  block-formatting-context-height-002, block-formatting-contexts-010, `ch` unit resolution
  (fixed block-in-inline-align-*), block-in-inline-empty-001..004,
  block-formatting-contexts-010 (real fix this time) + 011, block-in-inline-empty-001..004
  (done: RTL edge assignment + zero-extent fragments), block-in-inline-client-rects-001,
  block-in-inline-after-block-in-inline-with-margin-collapse (already fixed as a side
  effect of the above, verified, no new code needed), block-in-inline-empty-001..004
  (flow-height vs visual-bounds split -- body now 36px, span union stays 41px)

left todo next: RTL text-align (block-in-inline-empty RTL text placement) -- tree.py
  never resolves `text-align`/RTL-default `start` for text *geometry* at all (only
  paint.py applies it, and only visually) -- real fix needed in `_InlineFormattingPlan`,
  out of scope for a quick pass, not attempted.
  block-in-inline-client-rects-001 (inline fragment height around a 1px atomic inline-
  block should be ~17px line-box height, not the child's own 1px -- not attempted, needs
  the same edge-fragment machinery to measure a real line strut around atomic content).
  block-in-inline-first-line-001/002 (::first-line pseudo + text-indent
  inside split-inline pieces -- not started)

WPT `block-in-inline-append-001/002` (and any other `flags="dom"` fixture): SKIPPED, not
fixed -- they need real onload JS DOM mutation, and chromonic/domonic have no JS engine at
all. `run_wpt.run_folder` now detects `<meta name="flags" content="dom">` and skips these
instead of comparing chromonic's pre-mutation layout against Chrome's post-mutation one.

left todo: rest of margin-collapse-*, rest of CSS2/, css-box/, css-display/, css-position/, css-flexbox/, css-text/

Agent should NOT run full suite of tests between fixes. It takes too long and waiting ages per fix is not productive. Instead run full verification between batches of fixes.

## domonic version

Pinned to `domonic>=1.8.2` (bumped from 1.8.1). 1.8.2 fixed several things
natively that chromonic was previously patching around; those patches were
removed (`domonic_border_width_keyword_patch.py`, `domonic_cdata_style_
patch.py`, `domonic_layout_calc_var_patch.py`, `domonic_logical_properties_
patch.py`, `domonic_pseudo_inheritance_patch.py`, `domonic_shorthand_
cascade_order_patch.py` -- border-width keywords, XHTML `<style>` CDATA
wrapping, `calc(var())`, logical properties, `::before`/`::after`
inheriting from the right element, and same-rule shorthand-vs-longhand
cascade order, respectively, all verified fixed natively post-upgrade).
`domonic_media_query_patch.py` and `domonic_presentational_hint_patch.py`
were rewritten (not removed) -- 1.8.2 fixed `@media` matching natively too,
so the first now only patches in `@supports` (still unfixed) instead of
reimplementing `@media` itself; the second now wraps `_collect_author_
declarations` instead of replacing `_resolve()` outright, since the old
full-replacement approach was silently regressing the shorthand-order and
pseudo-inheritance fixes 1.8.2 added to `_resolve()`. `domonic_ch_unit_
patch.py`/`domonic_ex_unit_patch.py` are still needed as-is (`ch`/`ex`
remain unresolved in 1.8.2, see below). `LayoutStyle._from_computed()` was
renamed to `LayoutStyle.from_computed()` (no underscore) -- a real breaking
change, fixed at both `tree.py` call sites.

## log domonic issues here to be fixed upstream

`@supports` conditions are still never evaluated for real --
`_condition_rule_matches` (1.8.2's replacement for the old `_iter_style_
rules`-inline check) now correctly evaluates `@media` via a real
`MediaQueryList`, but explicitly keeps the old always-match fallback for
`CSSSupportsRule` ("no evaluator here yet" per its own docstring), even
though `CSS.supports()` (correct, `not`/`and`/`or`/nested parens) already
exists and just isn't wired in for this case. Worked around in `domonic_
media_query_patch.py` (patches only `_condition_rule_matches` now, not the
whole rule-traversal function `_iter_style_rules` the original patch
replaced -- that part is domonic's own correct code since 1.8.2).

Also found while re-verifying that patch: `CSS.supports()` itself doesn't
validate a property's *value*, only that the declaration's syntax parses
-- `CSS.supports("(display: bogus-value-xyz)")` returns `True`. Minor,
pre-existing (not something chromonic's patch introduces or can fix from
outside), not chased further.

`domonic`'s CSS type-selector matching compares against an element's full
`tagName` including namespace prefix (`"SVG:SVG"` for `<svg:svg>`), not
local name -- so a bare `svg { ... }` author rule never matches, though
per CSS Namespaces L3 ??3 it should (no default namespace declared means
type selectors match by local name, any prefix). Confirmed via
`block-replaced-width-003/004.xht`: `svg { height: 100px; ... }` silently
dropped, element fell back to its own attribute-derived size instead.
Worked around in `tree.py` only, by also matching the literal `"svg:svg"`
string at chromonic's own tag-dispatch sites -- fixes chromonic's layout
branching, not domonic's cascade, so real author CSS on a namespaced SVG
root still never applies. Not patched upstream.

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

Attribute selectors never match anything in domonic's cascade --
confirmed directly: `[dir="rtl"] { ... }` matches zero elements even when
`dir="rtl"` is a literal attribute on the target. This is why HTML's own
`dir` attribute (real UA behavior: `[dir=rtl] { direction: rtl }`) has no
effect in domonic at all. Worked around in `tree.py`'s `_element_direction`
by walking the DOM for a `dir` attribute directly instead of relying on
CSS inheritance for it. Not patched upstream -- likely a real gap in
domonic's own selector-matching (attribute selectors generally, not just
this one rule), not investigated further.

`ComputedStyleDeclaration.getPropertyValue("text-indent")`: **partially
fixed upstream in domonic 1.8.2** -- `text-indent` is now in
`_USED_LENGTH_PROPERTIES`, so an `em`/`rem`/`px` value resolves correctly
(confirmed: `text-indent: 2em` -> `"32px"`, was returned as the literal
unresolved `"2em"` before). Still unresolved for `ch`/`ex` specifically
though (confirmed: `text-indent: 5ch` still comes back as literal
`"5ch"`) -- `getComputedStyle`'s own unit resolver (`_length_string_to_px`)
has never supported either unit at all, unlike `domonic.layout`'s separate
`_parse_length_or_percent` (which chromonic's own `domonic_ch_unit_patch.py`/
`domonic_ex_unit_patch.py` already patch). `tree.py`'s workaround --
calling `_parse_length_or_percent` directly instead of going through
`getPropertyValue` -- remains necessary for `ch`/`ex` values.