"""chromonic phase 11: an approximation of real CSS inline flow.

Taffy has no inline flow at all -- every element becomes a block box (see
`style_bridge.py`) -- so a nav bar built the way most real, unstyled HTML
nav bars actually are (`<div><a>Home</a><a>About</a>...</div>`, relying on
nothing but `display:inline` being every element's own CSS default) used
to render as one link per line instead of a row. Found comparing chromonic's
own output against a real browser's on a real page (suckless.org).

`tree.py`'s `_approximate_inline_flow` now treats a block container whose
children are mostly inline-tagged elements (`<a>`/`<span>`/`<b>`/...) as a
wrapping flex row instead -- not real inline layout, just close enough for
the extremely common "a run of links/badges/tags" case this was written
for. See its own docstring, and "Phase 11" in PLAN.md, for exactly what it
does and doesn't handle.

    .venv/bin/python chromonic/examples/inline_flow_demo.py
    # -> examples/inline_flow.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import a, div, p  # noqa: E402

import chromonic  # noqa: E402


def main() -> int:
    nav = div(
        *(a(name, _href="#") for name in ("home", "docs", "blog", "about", "contact")),
        _style="background-color:#1a73a8; color:#ffffff; padding:10px; width:500px;",
    )
    root = div(
        nav,
        p(
            "The nav bar above is a plain <div> around five entirely unstyled "
            "<a> tags -- no flexbox, no CSS at all for the layout, just the "
            "same HTML a real, minimal nav bar would use.",
            _style="width:500px; padding:10px; color:#c9d1d9;",
        ),
        _style="width:520px; background-color:#0d1117;",
    )
    png = chromonic.render(root, width=520)
    out = Path(__file__).parent / "inline_flow.png"
    out.write_bytes(png)
    print(f"wrote {out} ({len(png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
