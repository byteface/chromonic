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

## log domonic issues here to be fixed upstream

`domonic.style._iter_style_rules` never evaluated `@supports` conditions
(`CSSSupportsRule.conditionText`) at all -- fell through its own generic
media-condition fallback, coincidentally always-true for non-media
syntax. Should call `CSS.supports()` (already correct, just never wired
into traversal) instead. Worked around in `domonic_media_query_patch.py`
(same patch already fixing `@media`'s identical always-true bug) for now.