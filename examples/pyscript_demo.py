"""chromonic phase 5: `<script type="text/python">` -- a click handler written
in Python, registered via an ordinary `addEventListener` call *from inside
the HTML*, mutating the live DOM's content and style, and visibly redrawn --
with zero new plumbing in chromonic's window/click/relayout/repaint pipeline
(see `chromonic/python/chromonic/pyscript.py`'s module docstring for why).

Needs a real display -- run it by hand:

    .venv/bin/python chromonic/examples/pyscript_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import chromonic  # noqa: E402

PAGE = """<!doctype html>
<html>
<body>
<div style="width:320px; padding:24px; font-family:sans-serif; background-color:#f7fafc;">
  <p id="status" style="margin:0 0 16px 0; color:#2d3748;">Not clicked yet</p>
  <button id="hello" style="padding:12px 24px; background-color:#3182ce; color:#ffffff; border:none;">
    Click me
  </button>
</div>
<script type="text/python">
count = 0
button = document.querySelector("#hello")
status = document.querySelector("#status")


def clicked(event):
    global count
    count += 1
    status.textContent = f"Clicked {count} time{'s' if count != 1 else ''}"
    button.textContent = "Click me again" if count else "Click me"
    # alternate the button's background colour each click -- a real style
    # mutation, not just text, so the redraw proves both paths.
    button.style.backgroundColor = "#38a169" if count % 2 == 0 else "#3182ce"


button.addEventListener("click", clicked)
</script>
</body>
</html>"""


def main() -> int:
    # `parse_and_run` is the lightest tier: parse the HTML, exec every
    # <script type="text/python"> against the real resulting DOM, no myjs/
    # network dependency needed for this inline-only page.
    document, _scope = chromonic.pyscript.parse_and_run(PAGE)

    # From here it's an ordinary chromonic window: the button's Python-written
    # listener is a completely normal domonic event listener now, so
    # Interaction.handle_click's existing hit-test -> dispatchEvent ->
    # relayout -> repaint loop drives it with no special-casing at all.
    chromonic.window.run(document.body, width=320, title="chromonic -- python script")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
