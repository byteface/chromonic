# chromonic

**Experimental.** Chromonic is a standalone project proving that Domonic can
be the DOM/CSSOM behind a native rendering pipeline:

<img src="https://raw.githubusercontent.com/byteface/chromonic/master/chromonic.png" alt="Chromonic rendering preview" width="900">


## Build & run

```bash
cd chromonic
make venv
make develop                         # compiles the Rust extension and installs chromonic editable
python examples/poc.py     # -> examples/poc.png, examples/poc_mutated.png
make test
```

Needs a Rust toolchain (`cargo`/`rustc`) on `PATH`; `skia-python` installs
from a prebuilt wheel (no C++ build needed on macOS/Linux/Windows x86_64 or
macOS arm64).


## Direct GPU browser (`browse2.py`)

```bash
python examples/browse2.py https://example.com/
python -m pytest tests
python chromonic/benchmarks/smoke_native.py
python chromonic/examples/browse2.py https://example.com/ --frames 2
```


## Web fonts

The browser loads `@font-face` URL sources in the background, resolving paths
relative to their stylesheet (or the page for inline CSS). OTF, TTF, WOFF and
WOFF2 are decoded into the same font bytes for Parley/fontique layout and Skia
painting. Arrival triggers relayout and repaint; fonts are never installed
into the OS. Family, numeric weight and normal/italic/oblique style select a
document-private face before system fonts and generic fallbacks.

This first implementation supports static faces in top-level `@font-face`
rules. `local()` sources, `@import`, conditional font-face rules, variable-font
descriptor ranges, `unicode-range` and `font-display` policies are not yet
implemented. Failed downloads retain fallback text; diagnostic errors are
available on `page.document._chromonic_webfonts.errors`.

## Chrome layout conformance

The permanent numeric correctness harness runs the focused fixtures in
`tests/layout/fixtures` through both installed headless Chrome and chromonic:

```sh
make layout-conformance
```

It compares every `data-layout` element's `getBoundingClientRect()` geometry
with a 0.5 CSS-pixel tolerance and reports exact Chrome, chromonic, and delta
values. Each fixture also writes focused computed styles plus `chrome.png`,
`ours.png`, and an amplified `diff.png`. Geometry controls the exit status;
screenshots and style serialization remain diagnostic. See
[`tests/layout/README.md`](tests/layout/README.md) for fixture conventions,
individual commands, and the current conformance baseline.

Chromonic depends on Domonic 1.8.1 or newer and uses the released DOM/CSSOM
implementation directly.


## Executable Python inside HTML

There's no limit on what a `<script type="text/python">` can do — it runs with the same power as a normal Python `exec()` (no import allowlist, no resource limits). Treat a `.py`-in-HTML page exactly like trusted application code you'd run yourself (`python app.py`) — **never** point this at arbitrary, remote, or user-supplied HTML/Python; there is no isolation here to protect against it.

```html
<script type="text/python">
button = document.querySelector("#hello")

def clicked(event):
    button.textContent = "Clicked"

button.addEventListener("click", clicked)
</script>
```

```bash
python examples/pyscript_demo.py        # the inline form above
python examples/pyscript_src_demo.py    # the src="app.py" form
```
