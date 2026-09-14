# Contributing: finding and fixing rendering bugs

Chromonic's biggest ongoing job is closing the gap between what it renders
and what a real browser renders. There are two ways to find bugs, and
they're both simple to run.

## Option A: test against a real website (usually the most valuable)

This is the quickest way to find a bug that actually affects real pages.

1. Open the site you want to test in Chrome.
2. Open DevTools Console (`Cmd+Opt+J` / `Ctrl+Shift+J`).
3. Paste the contents of [`tools/chrome_probe.js`](tools/chrome_probe.js) and
   press enter. It downloads a JSON file with Chrome's computed styles and
   element positions for that page.
4. Load the same URL through chromonic and compare:

   ```python
   import sys
   sys.path.insert(0, "tests/layout")
   from pathlib import Path
   from harness import native_runner

   native_runner.run(
       Path("some-name"), Path("/tmp/out"), Path("/tmp/out/ours.png"),
       load_url="https://example.com/", viewport=(1280, 800),  # match the probe's viewport
   )
   ```

   Open `/tmp/out/ours.png` and look at it. If it's visibly broken (overlapping
   text, wrong sizes, missing content), you've found something worth fixing.

5. To pin down *why*, cross-reference the Chrome probe JSON against chromonic
   element-by-element (this finds the biggest differences across the whole
   page at once, instead of guessing):

   ```python
   import json, sys
   sys.path.insert(0, "tests/layout")
   from chromonic import browser, tree

   page = browser.load("https://example.com/")
   doc = page.document
   browser.set_viewport(page, 1280, 800)  # match the probe's viewport
   tree.LayoutProjection().layout(doc.body, width=1280, height=None, viewport_height=800)

   probe = json.load(open("chrome-probe-....json"))  # the file DevTools downloaded
   for el in probe["elements"]:
       node = doc.querySelector(el["selector"])
       if node is None:
           print("MISSING:", el["selector"]); continue
       r = node.getBoundingClientRect()
       cr = el["rect"]
       if max(abs(r.x - cr["x"]), abs(r.y - cr["y"]), abs(r.width - cr["width"]), abs(r.height - cr["height"])) > 2:
           print(el["selector"], "OURS", (r.x, r.y, r.width, r.height), "CHROME", (cr["x"], cr["y"], cr["width"], cr["height"]))
   ```

6. Once you know *which* element is wrong, inspect it directly to find the
   root cause — usually one of: chromonic's own layout code
   (`python/chromonic/tree.py`, `style_bridge.py`), or domonic's CSS
   cascade/computed-style resolution (installed in `.venv`, inspect via
   `from domonic.style import ComputedStyleDeclaration`).

## Option B: run the real web-platform-tests (WPT) suite

WPT is the actual, official CSS test suite used by every real browser. It's a
huge source of small, focused test files that already know the correct
answer — useful when you want to check one specific CSS behavior rather than
a whole page.

**One-time setup:**

```sh
git clone --depth=1 https://github.com/web-platform-tests/wpt.git tests/wpt
cd tests/wpt && python3 -m http.server 8943 --bind 127.0.0.1 &
cd ../..
```

(`tests/wpt/` is gitignored — it's a local checkout, not part of this repo.
The HTTP server needs to stay running while you test; restart it each
session.)

**Pick which part of the suite to run** — this is "changing the suite": just
point at a different folder under `tests/wpt/css/`. Good starting points:

- `tests/wpt/css/CSS2/` — CSS 2.1 basics (box model, positioning, margins)
- `tests/wpt/css/css-flexbox/`
- `tests/wpt/css/css-position/`
- `tests/wpt/css/css-text/`

**Run one file through the comparison harness:**

```python
import sys
sys.path.insert(0, "tests/layout")
from pathlib import Path
from harness import chrome_runner, native_runner, compare

f = Path("tests/wpt/css/CSS2/positioning/top-004.xht")
url = "http://127.0.0.1:8943/css/CSS2/positioning/top-004.xht"
out = Path("/tmp/wpt-check")

chrome_result = chrome_runner.run(f, out, out / "chrome.png")
native_result = native_runner.run(f, out, out / "ours.png", load_url=url)
result = compare.compare_results(chrome_result, native_result, tolerance=0.5)
print(result["passed"], result["geometry_mismatches"])
```

A WPT file has no `id`/`data-layout` attributes by default (the harness needs
them to know which elements to check), so most files need those added to a
throwaway copy first — don't edit the real checkout. Ask in the Discord for
the current auto-tagging snippet if you get to this step, or check
`PLAN.md`'s "Roadmap" section, which documents it.

## Reporting / fixing what you find

- Log what you found (and whether you fixed it) in `PLAN.md` — that file is
  the project's running log of what's been tried, what's fixed, and what's a
  known gap. Follow the format of the existing entries (dated section, what
  was found, what was fixed, what's still open).
- If the bug is in chromonic's own code, fix it directly and add/update a
  fixture under `tests/layout/fixtures/` so it stays fixed.
- If the bug is in **domonic** (the DOM/CSSOM library chromonic sits on top
  of), don't edit the installed package — write a small patch module
  following the pattern in `python/chromonic/domonic_canvas_patch.py`
  (or the newer `domonic_logical_properties_patch.py` /
  `domonic_layout_calc_var_patch.py`): an `install()`/`uninstall()` pair that
  monkeypatches the specific broken function, applied automatically when
  `chromonic.browser` is imported. Log the bug in `PLAN.md`'s "domonic
  issues" section either way, so it can eventually go upstream.

## Before you're done: check you haven't broken anything

```sh
.venv/bin/python -m pytest tests --ignore=tests/wpt
make layout-conformance
```

Both should show the same handful of pre-existing failures as before your
change, nothing new.

**Important — don't run huge batches while you're still debugging.** Each
file in the WPT/layout suites launches a real headless Chrome instance,
which is slow. Verify individual fixes with a single file (as shown above),
and only run the full suite once, at the very end, after you're done.
