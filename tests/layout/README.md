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
PYTHONPATH=tests/layout .venv/bin/python -m harness.run_suite \
  tests/layout/fixtures/flex.html \
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

Known upstream or deliberately unfixed gaps live under `known_issues/`. Run
them explicitly with `harness.run_suite tests/layout/known_issues/<name>.html`
when working that issue, but they are not part of the default green baseline.

## Running the real web-platform-tests suite

`harness.run_wpt` (see `PLAN.md` for the objective) runs fixtures straight out
of a local `web-platform-tests` checkout at `tests/wpt/` instead of the
handwritten `tests/layout/fixtures/`:

```sh
git clone --depth=1 https://github.com/web-platform-tests/wpt.git tests/wpt
```

Both Chrome and chromonic load each test over real HTTP rather than from
disk, because many WPT fixtures reference absolute-root paths (`/fonts/
ahem.css`, `/resources/testharness.js`, etc.) that only resolve correctly
against a server whose document root is `tests/wpt/`. `harness.run_wpt`
does not start that server itself — start it first, in its own terminal,
and leave it running for the duration of the run:

```sh
make layout-wpt-server
# or directly:
.venv/bin/python -m http.server 8943 --directory tests/wpt --bind 127.0.0.1
```

Then, from a second terminal, run the WPT harness against any folder under
the checkout:

```sh
PYTHONPATH=tests/layout .venv/bin/python -m harness.run_wpt \
  tests/wpt/css/CSS2/linebox \
  --limit 20
```

If the harness prints `ConnectionError: ... Connection refused` on port
8943, the server above either isn't running or was stopped (e.g. by a
terminal restart) — start it again before re-running. `--base-url` overrides
the default `http://127.0.0.1:8943` if you serve `tests/wpt/` on a different
port.

The original eleven fixtures remain as regression coverage. The expanded
suite now has a zero-geometry-mismatch baseline across all 21 fixtures,
including the realistic pages, and should stay green unless a fixture is added
to pin a newly found rendering gap.

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

- extend table layout beyond equal auto-width cells (spans, intrinsic/explicit
  column negotiation, separate borders and captions), and paint list markers;
- broaden CSS paint order coverage for stacking, clipping, border radii, and
  backgrounds.

More complex grid functions (`minmax`, auto-fill/auto-fit, named tracks) still
require broader parsing and native representation.

Viewport-relative `vw`, `vh`, `vmin`, and `vmax` lengths are resolved by the
Chromonic style bridge against the exact harness/window viewport. Positioned
mixed content also retains direct text alongside out-of-flow children, and
root body geometry accounts for collapsed child margins with explicit heights.
The harness waits for Chrome fonts and Chromonic image/font downloads before
capturing, preserves stylesheet base URLs for relative assets, and writes
opaque amplified diffs so screenshot disagreements are visible.

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
