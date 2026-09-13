# Layout correctness harness

This suite compares chromonic's real Domonic/Taffy geometry with installed
Chrome or Chromium at a fixed 800 × 600 CSS-pixel viewport. Geometry is the
pass/fail signal. Focused computed-style differences are recorded alongside
it, and screenshots are diagnostic artifacts rather than pixel-perfect gates.

Run every fixture from the repository root:

```sh
make layout-conformance
```

Run selected fixtures or change the numeric tolerance:

```sh
PYTHONPATH=chromonic/tests/layout .venv/bin/python -m harness.run_suite \
  chromonic/tests/layout/fixtures/flex.html \
  --tolerance 0.5 --output /tmp/layout-results
```

Set `CHROME=/path/to/chrome` when Chrome is not installed in a standard
location. The command returns zero only when every measured rectangle agrees
within the tolerance. Style mismatches are reported and written to JSON, but
are not yet fatal because Domonic and Chrome can serialize equivalent computed
values differently.

Every fixture output directory contains:

```text
chrome.json       Chrome rectangles and focused computed styles
ours.json         chromonic rectangles and focused computed styles
comparison.json   numeric deltas and style differences
chrome.png        fixed-viewport Chrome screenshot
ours.png          fixed-viewport Skia screenshot
diff.png          amplified absolute pixel difference
overlay.png       50/50 Chrome and chromonic overlay
```

The output root also contains `summary.json`, which ranks the 50 largest
geometry/fragment deltas across the suite and sorts pages by visual change.

Elements are included by adding both a stable ID and `data-layout`:

```html
<div id="target" data-layout>Hello</div>
```

For realistic fixtures, put `data-layout-root` on a container. Every
descendant with an ID is captured automatically. Results include the outer
bounding rectangle, `getClientRects()`-equivalent element fragments, direct
text-node range fragments, and the focused computed-style set.

The `pages/` directory contains complete editorial, documentation, and
catalog pages. Whole-page cases additionally fail when more than 8% of pixels
differ materially after ignoring small antialiasing changes. Geometry and text
fragments remain the primary signal; `diff.png` and `overlay.png` explain broad
paint disagreement.

Fixtures stay small and independent. Add a fixture for one behavior, run the
suite to capture its exact mismatch, fix the renderer, then keep the fixture as
the regression test. Generated `artifacts/` can be removed and recreated at
any time; Chrome references are generated from the current fixture every run.

The original eleven fixtures remain as regression coverage. The expanded
suite intentionally exposes many more failures; its pass count should only
rise through general renderer fixes that also improve the realistic pages.

## Current ownership

Chromonic now forwards `box-sizing` to Taffy instead of silently using Taffy's
border-box default. That correction fixed the numeric box-model, percentage,
flex, overflow, and typography failures. Chromonic also publishes Chrome-like
body geometry for the narrowly eligible root block whose first and last child
margins collapse through it, while retaining the full document scroll extent.
Root UA margins and viewport-anchored fixed/absolute boxes are also corrected
after native layout. Taffy rounding is disabled so DOM geometry retains CSS
subpixels until Skia rasterization.

The current simple table projection maps rows to retained flex rows and equal
auto-width cells, including collapsed-border geometry. The combined lists and
table fixture now has zero element-geometry mismatches. Integer
`repeat(N, <px|%|fr|auto>)` grid tracks expand at the bridge and grid items no
longer feed a containing block's full width back as their automatic minimum;
the catalog whole-page fixture now passes geometry, text-fragment, and visual
comparison. Basic UA buttons and inputs retain intrinsic dimensions and share
an anonymous inline row.

The current high-priority geometry/paint work belongs in chromonic:

- replace the retained anonymous mixed-text fragments' flex approximation with
  real line boxes and place inline text/elements in one formatting context;
- preserve text fragment metrics, wrapping, baseline alignment, and inline
  padding/borders when projecting the resulting geometry to Taffy/Skia.
- extend table layout beyond equal auto-width cells (spans, intrinsic/explicit
  column negotiation, separate borders and captions), and paint list markers;
- provide UA intrinsic sizing for form controls and paint stacking, clipping,
  border radii, and backgrounds in CSS paint order.

More complex grid functions (`minmax`, auto-fill/auto-fit, named tracks) still
require broader parsing and native representation.

Viewport-relative `vw`, `vh`, `vmin`, and `vmax` lengths are resolved by the
Chromonic style bridge against the exact harness/window viewport. Positioned
mixed content also retains direct text alongside out-of-flow children, and
root body geometry accounts for collapsed child margins with explicit heights.

The remaining non-fatal style diagnostics belong primarily in Domonic's
computed-style/CSSOM layer. Geometry already uses the correct numeric values,
but `getComputedStyle()` still commonly returns authored or initial tokens
where Chrome returns a normalized used value. The high-value cases are:

- report an inactive border width as `0px` when its border style is `none`;
- normalize initial `min-width` and `min-height` to Chrome-compatible values;
- expose used pixel values for resolved percentages and `auto` dimensions when
  a layout box is available.

These CSSOM differences remain diagnostic so they do not obscure renderer
geometry regressions.
