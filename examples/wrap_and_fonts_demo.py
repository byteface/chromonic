"""chromonic phase 9: real multi-line text wrapping + `font-family`/
`font-style`/`font-weight` painted, not just measured.

Renders a paragraph long enough to wrap across several lines inside a
narrow column, plus bold/italic/monospace lines, and saves a PNG.

    .venv/bin/python chromonic/examples/wrap_and_fonts_demo.py
    # -> examples/wrap_and_fonts.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div, p  # noqa: E402

import chromonic  # noqa: E402


def main() -> int:
    root = div(
        p(
            "This is a long paragraph that should wrap across multiple lines "
            "once it hits the edge of its container, instead of overflowing "
            "off to the right forever the way a single-line text box used to.",
            _style="font-size:16px;",
        ),
        p("Bold and italic:", _style="font-weight:bold;"),
        p("This text is italic.", _style="font-style:italic;"),
        p("This text is monospace.", _style="font-family:monospace;"),
        _style="width:300px; padding:10px;",
    )
    png = chromonic.render(root, width=320)
    out = Path(__file__).parent / "wrap_and_fonts.png"
    out.write_bytes(png)
    print(f"wrote {out} ({len(png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
