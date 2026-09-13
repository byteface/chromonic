# chromonic — Domonic Native Browser POC

Experimental, isolated, not wired into the main test suite or CI. Proves one
architecture end to end:

> domonic owns the live DOM and CSS cascade. Taffy (Rust, via PyO3) computes
> box layout (block/flex/grid/absolute positioning). Skia paints it. Python
> can mutate the same live DOM driving the client, and
> `element.getBoundingClientRect()` reports exactly the geometry painted.

The code and the examples are the documentation now — this file is kept
short on purpose (it used to be a blow-by-blow changelog of every phase;
that grew unreadable and stopped being worth maintaining). For what a
specific piece does and why, read its own module docstring — every file
under `python/chromonic/` explains itself, including the real domonic/Taffy/
Skia limitations it's working around.

## Architecture

```
domonic DOM + CSSOM  ->  Taffy (block/flex/grid/absolute)  ->  layout
       |                                                          |
       v                                                          v
  live Elements  <---------- geometry written back --------  LayoutBox
       |
       v
    Skia paints from that geometry
```

`python/chromonic/tree.py` walks the DOM, asks `domonic.layout.layout_style()`
for each element's cascaded style, and builds a mirroring Taffy tree
(`chromonic._native`, `src/lib.rs`) — `style_bridge.py` is the narrow
translator between domonic's `LayoutStyle` and Taffy's plain FFI vocabulary.
`paint.py` walks the same DOM again after layout and draws backgrounds,
borders, text, and `<img>`s via `skia-python`, reading exactly the
`LayoutBox`/paint-relevant style `tree.py` already resolved (never
re-resolving domonic's cascade itself — see `tree.py`'s module docstring
for why that used to be a real perf bug).

`browser.py`/`native_browser.py` are two front ends over the same pipeline —
a `pywebview`-hosted PNG-swapping window and a direct GLFW+OpenGL Skia
surface, respectively — both fetching real pages via `myjs.Page.load()`.
`window.py`/`pyscript.py` are the interaction layer: real DOM event
dispatch, `<script type="text/python">` execution, and a self-pacing
animation clock.

## Where to look

- `python/chromonic/tree.py` — DOM → Taffy tree, text wrapping, the
  `position:absolute` containing-block resolution, the inline/float-flow
  approximation, `<select>`/`<img>` special-casing.
- `python/chromonic/paint.py` — Taffy geometry → Skia drawing.
- `python/chromonic/style_bridge.py` — the domonic↔Taffy style vocabulary.
- `python/chromonic/fonts.py` — CSS `font-family`/weight/style → `skia.Typeface`.
- `python/chromonic/ua_style.py` — the UA-stylesheet defaults domonic doesn't ship.
- `python/chromonic/browser_images.py` — `<img>` fetch/decode/cache.
- `python/chromonic/browser.py`, `native_browser.py` — the two window front ends.
- `python/chromonic/pyscript.py` — `<script type="text/python">` execution.
- `examples/` — one runnable, working demo per feature; run any of them.
- `tests/test_chromonic.py` — the real spec; every fixed bug has a regression test.
- `docs/domonic-wrinkles.md` — real domonic gaps found building this (not
  chromonic's own bugs — those live as comments/tests in this package instead).

## Known limitations (current, not historical)

- **No real inline formatting context.** Mixed inline content (text
  interleaved with `<a>`/`<span>`, real line-breaking around inline boxes)
  isn't implemented — `tree.py`'s `_approximate_inline_flow` heuristic
  (flex-wrap for a majority-inline/floated run of *whole elements*) covers
  the common nav-bar/badge-row case but isn't real inline layout.
- **No CSS floats.** The approximation above catches simple floated rows;
  float *grids* that rely on a percentage width sized for a float context
  (common) don't reflow correctly once reinterpreted as flex items.
- No `display: list-item` markers/bullets, no CSS Grid named lines/areas or
  `repeat()`/`minmax()`, no `@font-face`, no CSS transforms/shadows/gradients,
  no incremental/dirty-bit layout (`tree.layout()` always rebuilds the whole
  tree).
- Painting still doesn't share Parley's shaping: `skia-python` does its own
  font resolution and glyph shaping to draw each line (see `chromonic.fonts`),
  so `letter-spacing`/`word-spacing` affect *measurement* but not yet what
  gets drawn.

## Parley

[Parley](https://github.com/linebender/parley) (the Rust text-layout engine
`Blitz` pairs with Taffy + Vello) now does real text layout for `tree.py`:
font matching via `fontique`, real Unicode line-breaking, and
`letter-spacing`/`word-spacing`/`line-height` — replacing the old fixed
Helvetica-shaped table that measured every font identically. See
`layout_text` in `src/lib.rs` and `tree.py`'s `_make_measure`. Not adopting
Blitz itself or its DOM — domonic stays the only DOM/CSSOM; Parley is used
purely as a layout library, the same role Taffy has. Painting is still
Skia's own text shaping (see the limitation above); real inline boxes
(mixed text + inline elements, replacing `_approximate_inline_flow`) and
`white-space`/`word-break`/`overflow-wrap` are the natural next steps, not
yet done.
