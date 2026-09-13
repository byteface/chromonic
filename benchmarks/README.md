# Browse performance audit — 2026-09-12

## Direct GPU browse2 results

`profile_browse2.py` measures the current GLFW/Skia path used by
`examples/browse2.py`; it does not encode PNGs or cross a webview bridge. Run:

```sh
.venv/bin/python chromonic/benchmarks/profile_browse2.py --nodes 1000 --rules 200 --repeat 9 --output /tmp/chromonic-browse2
```

On the same environment documented below, a 1,000-paragraph, 24,000 px-tall
page produced these medians after the direct renderer changes:

| Direct operation | Median |
| --- | ---: |
| Recursive full-DOM paint | 5.30 ms |
| Viewport-culled display-list paint | 0.35 ms |
| Complete hidden GPU frame, including `glFinish` | 0.89 ms |
| Rebuilt Taffy tree, fresh styles | 82.09 ms |
| Retained Taffy tree, fresh styles | 69.20 ms |
| Retained Taffy tree, cached styles | 10.41 ms |

The layout pass now constructs a stable paint-order display list. Scroll and
expose frames test each entry's bounds and paint only visible elements (33 of
1,001 in this fixture), while independently testing descendants so overflow
is not lost when a parent's own box is offscreen. This removes full DOM walks
and offscreen drawing from repeated frames. Selector parsers are bounded and
memoized because stylesheet selector text is immutable within a match; fresh
relayout improved from a 134.63 ms baseline to roughly 128 ms before native
retention.

`LayoutProjection` retains one native Taffy node per live Domonic element.
Every reconciliation still walks Domonic as the authoritative tree, including
its containing-block reparenting rules, then compares immutable style,
measurement, and child snapshots. It calls native `set_style`, `set_measure`,
or `set_children` only when the corresponding snapshot changed; removed DOM
elements remove their detached native nodes. Surviving elements keep their
native IDs across text/style changes and insertions/removals. Cached-style
relayout is about **2.5× faster** than the prior 24.6–26.0 ms rebuild path;
retention also saves about 11% when all CSS must be resolved freshly.

The same projection exposes explicit `patch_style` and batched `patch_insets`
operations for mutation paths that know the exact translated field changed.
`particles2.py` uses the latter after writing each particle's authoritative
Domonic inline style, reducing its 1,000-particle direct GPU frame from the
previous 110.78 ms to 8.74 ms. The batch crosses Python/Rust once; native
inset updates and Taffy compute together remain below 0.4 ms per frame under
cProfile. Unknown CSS changes continue through normal reconciliation.

Paint now memoizes pure CSS-colour conversion and consumes tag/leaf metadata
recorded during layout instead of rebuilding Domonic `childNodes` views. It
reads the layout box already published in element state and passes that box
through culling into painting, avoiding duplicate geometry lookups. Geometry
publication writes the same immutable Domonic `LayoutBox` directly to the
element state that Domonic's two wrapper functions ultimately target.

The benchmark writes JSON, cumulative cProfile text, and a `.prof` file. Its
GPU number includes queued driver work through `glFinish`, but excludes window
composition, vsync, and input latency. Initial navigation also includes Page
construction and is reported separately.

The first fixes remove duplicate event-frame layout and share ancestor style
resolution within each layout pass. The next substantial costs are Python
style conversion, full-frame PNG encoding, and network loading. Replacing
Taffy or optimizing base64 is not supported by these measurements.

## Reproduce

From the repository root, with chromonic built and installed:

```sh
.venv/bin/python chromonic/benchmarks/profile_browse.py --nodes 500 --rules 200 --repeat 7 --output /tmp/chromonic-profile
.venv/bin/python chromonic/benchmarks/profile_browse.py --url https://google.com/ --repeat 5 --output /tmp/chromonic-google
.venv/bin/python chromonic/benchmarks/profile_browse.py --html saved-page.html --repeat 5
.venv/bin/python -m pstats /tmp/chromonic-profile.prof
```

The command writes JSON samples, cumulative cProfile text, and a `.prof` file.
It warms fonts/imports before rendering measurements. Load timings are first-use
wall times; frame figures below are medians, without cProfile enabled. The
separate profile captures a bridge tick to a headless sink. It includes frame
script construction but **excludes real pywebview IPC, WebKit PNG decoding,
composition, display latency, and user-perceived FPS**. Fetch timing depends on
the network and page response; this is not a browser benchmark against Chrome.

Environment: macOS 14.3.1 arm64, Python 3.13.12, skia-python 144.0.post2.
The installed domonic reports runtime version 1.8.0, while its distribution
metadata reports 1.7.2; both are recorded to make that discrepancy explicit.
The deterministic fixture has 500 text paragraphs and 200 class rules at a
1000 × 800 viewport. It intentionally includes content outside the viewport.
Raw samples are in [results/](results/). Do not add stage medians together as
if they came from the same frame; stages are measured separately.

## Measurements

| Headless operation | 500 paragraphs, after fixes | Google, after fixes |
| --- | ---: | ---: |
| Layout: style, tree build, native compute, geometry writeback | 45.39 ms | 9.17 ms |
| Raster + image snapshot | 15.73 ms | 1.48 ms |
| PNG encoding alone | 29.59 ms | 23.08 ms |
| Base64 conversion | 0.23 ms | 0.022 ms |
| Complete bridge tick to a sink | 91.47 ms | 34.38 ms |
| Same code with an extra layout deliberately restored | 140.33 ms | 43.26 ms |

The same-process comparison isolates the duplicate-layout cost: about **35%
less frame work** on the 500-paragraph fixture and **21% less** on Google.
It uses the new ancestor cache in both paths. It does not claim those numbers
as on-screen FPS improvements. Direct `tick(); render()` still deliberately
recomputes twice: `render()` defaults to fresh geometry for external mutations.
The actual event bridge uses `render(relayout=False)` after its event layout.

Before either fix, measured standalone layout was 56.98 ms on the larger
fixture and 25.31 ms on Google. The earlier larger-fixture bridge median was
193.69 ms, with appreciable variance; the paired comparison above is the more
useful estimate of the duplicate pass's benefit. Live Google responses can
change between runs, so its cross-run comparison is indicative.

The final 500-element cProfile recorded **501 style resolutions**, compared
with **2,002** before the fixes (the earlier frame did two layouts). Native
`Tree.compute` took about 1.63 ms **under profiling**, including Python text
measurement callbacks; `_from_computed` accumulated about 101 ms, style-dict
translation 8.9 ms, and geometry writeback under 1 ms. These instrumented
numbers show where calls concentrate; they must not be mixed with unprofiled
wall timings. The native call is not separately instrumented inside Rust.

Google's measured first load was 379 ms: HTML fetch 203 ms, parsing 45 ms,
Session setup 66 ms, stylesheet batch 64 ms. Stylesheets already fetch
concurrently and duplicate remote targets already collapse within a page.
A separate four-load tiny-page probe measured first Session setup at 80 ms,
then 0.12, 0.09, and 0.26 ms: most of that cost is cold imports/setup.

## Implemented and verified

- Click, tick, navigation, and back navigation reuse their just-computed
  layout when delivering the frame. Initial and direct renders remain fresh.
  The particle bridge follows the same rule. Back with no history sends no
  redundant frame.
- One per-layout style cache serves the whole traversal. Siblings reuse
  their parent's resolved cascade. Each next layout starts with a fresh cache,
  so inherited colour and geometry mutations remain visible.
- Pixel-equivalence and call-count tests cover event delivery, navigation,
  external mutations, and cache lifetime. The chromonic suite passes 38 tests,
  including its real local-HTTP navigation tests.

The style cache uses domonic's private `_chain_cache` hook, alongside the
existing private `LayoutStyle._from_computed` call. A public per-pass computed
style context is the upstream API improvement; avoid global or cross-frame
caches until there is reliable mutation invalidation.

## Remaining architectural priorities

| Priority | Evidence / current path | Next change and validation |
| --- | --- | --- |
| 1. Add mutation-driven invalidation above the retained projection | Native node identity and dirty native updates are retained now, but a fresh-style pass still resolves CSS across the full Domonic tree | Track DOM, stylesheet, inherited-style, viewport and resource revisions so reconciliation can resolve only affected subtrees. Preserve a conservative full-style fallback for unobservable mutations and media-query changes. |
| 2. Avoid PNG compression for every interactive frame | Encoding alone costs 23–30 ms here; an `<img>` data URI forces encode → base64 → JS bridge → decode | Benchmark a lossless faster encoder or raw pixel/canvas transport against a native Skia surface. Measure transport bytes and real display completion as well as CPU. Keep PNG for export. Do not trade away text quality through an unmeasured JPEG switch. |
| 3. Reduce style conversion work | `_from_computed` dominates profiled layout; properties repeatedly normalize names, parse lengths, and wrap style getters | Provide domonic with a public computed-style snapshot/typed layout view. Reuse immutable conversion results within a pass where inputs match. Profile before moving conversion into Rust; native layout itself is already small. Author rules are already indexed by selector candidates. |
| 4. Extend retained render data to hit testing and true paint bounds | browse2 now retains a display list and culls independent layout boxes; hit testing still walks the DOM and layout boxes do not include all visual overflow | Reuse retained entries for hit testing and add conservative paint bounds for shadows/transforms and overflowing text. Handle `display:none` transitions and stale boxes explicitly. |
| 5. Resource and back-navigation caching | Back stores URLs and refetches/reparses pages; no cross-navigation resource cache in this path | Add bounded history/page or HTTP resource caches with clear invalidation/revalidation policy. Keep concurrent stylesheet fetching. Measure cold/warm navigation separately. |
| 6. Serialize and coalesce UI work | Animation already self-paces, but toolbar navigation/click calls can overlap; fetch is synchronous before frame delivery | Use one owner for DOM/layout/render operations, generation IDs for navigation responses, and a bounded pending-frame queue. Show loading status promptly. Do not share PyO3's unsendable Tree across threads. |
| 7. Measure the actual display boundary | The headless sink cannot time pywebview, PNG decoding, or composition | Add event IDs and Python stage timestamps plus JS image `load`/`decode()` and `requestAnimationFrame` acknowledgements. Measure input-to-present separately from Python render time. |
| 8. Defer interpreter startup for render-only pages | `Page(run=False)` still constructs Session, costing about 66–80 ms cold but sub-ms warm | Consider a lazy Session or DOM/CSS-only loader if startup matters. Preserve `Page`'s existing eval/script API; do not prioritize this as a repeated-frame optimization. |

No persistent dirty-tree system, alternate image transport, or resource cache
is claimed as implemented by this audit. These are the measured follow-on
work, with the correctness constraints that make each reviewable.
