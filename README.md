# chromonic

**Experimental.** Chromonic is a standalone project proving that Domonic can
be the DOM/CSSOM behind a native rendering pipeline:

<img src="https://raw.githubusercontent.com/byteface/chromonic/master/chromonic.png" alt="Chromonic rendering preview" width="900">

> Browser compatibility is incomplete and the rendering engine is still under active development. You can help evolve Chromonic by contributing to conformance and layout tests. Check the repo for open issues to join in.


## Install

Install with pipx to use the command line:

```bash
chromonic https://csszengarden.com/

# local page
chromonic ./test.html
chromonic .                    # serves ./index.html

# run an example file
chromonic example interactive_canvas
```


## Build & run

Pull the repo to build and run the Python API examples:

```bash
cd chromonic
make venv
make develop # compiles the Rust extension and installs chromonic editable
python examples/poc.py # -> examples/poc.png, examples/poc_mutated.png
make test
```

Needs a Rust toolchain (`cargo`/`rustc`) on `PATH`; `skia-python` installs
from a prebuilt wheel (no C++ build needed on macOS/Linux/Windows x86_64 or
macOS arm64).


## Public API

Chromonic exposes a small layered surface. The engine functions are useful for
headless rendering and tests; `App` is the native desktop application wrapper;
`Browser` opens a URL in the native browser shell.

```bash
pip install chromonic
```

```python
from domonic.html import body, h1
import chromonic

root = body(h1("Headless render"))
chromonic.layout(root)
png = chromonic.render(root)
```

```python
from chromonic import App
from domonic.html import body, button, h1, p

root = body(
    h1("Counter"),
    button("Increment", _id="inc"),
    p("0", _id="value"),
)

app = App(root, width=700, height=500)

@app.click("#inc")
def increment(event):
    app.document.querySelector("#value").textContent = "1"

app.run()
```

`App` keeps the Domonic DOM as the application state. Event handlers can use
normal Domonic APIs such as `querySelector`, `appendChild`, `remove`,
`textContent`, `value`, `checked`, and `addEventListener`. The convenience
decorators delegate through the document, so they also match elements created
after startup.

```python
@app.click(".delete")
def delete_task(event):
    event.currentTarget.parentNode.remove()

@app.key("#new-task", "Enter")
def add_with_enter(event):
    app.trigger("#add", "click")
```

The public attributes are:

```python
app.document  # Domonic Document
app.window    # Domonic defaultView
```

The native GLFW window and Skia renderer stay internal for now. See
`examples/counter_app.py` and `examples/todo_app.py` for small apps that only
import Domonic HTML tags and `chromonic.App`.

```python
from chromonic import Browser

browser = Browser("https://eventual.technology")
browser.run()
```


## Direct GPU browser (`browse2.py`)

```bash
python examples/browse2.py https://example.com/
python -m pytest tests
python chromonic/benchmarks/smoke_native.py
python chromonic/examples/browse2.py https://example.com/ --frames 2
```

Press `F12` for a devtools-style console (green-on-black, drops down from the
toolbar). It evaluates input as Python against the loaded page's `document`/
`window`, so JS-style one-liners like `document.getElementById('x').textContent`
work as-is since domonic's DOM mirrors the real API.


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


## Comparing a live page with Chrome

For real sites, first capture what Chrome actually rendered. Open the page in
Chrome, then paste `tools/chrome_probe.js` into DevTools Console. It downloads
a JSON file containing loaded stylesheets, accessible CSS rules, resource
timing, web font status, image natural sizes, visible element rectangles, and
focused computed styles.

That dump is the quickest way to answer whether Chromonic missed an external
stylesheet, missed a background image/font resource, or parsed the CSS but
resolved a different computed value. For fixture-sized cases, use the permanent
Chrome-vs-Chromonic harness below.

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
