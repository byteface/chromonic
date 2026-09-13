"""Real text layout via Parley -- font matching, shaping, and genuine
Unicode line-breaking, replacing the old fixed Helvetica-shaped table.
Renders the same paragraph in three different font-families side by side:
each one now wraps and measures using its *own* real metrics, not one
shared approximation.

    .venv/bin/python chromonic/examples/parley_demo.py
    # -> examples/parley_demo.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div, p  # noqa: E402

import chromonic  # noqa: E402

TEXT = "The quick brown fox jumps over the lazy dog, again and again."


def main() -> int:
    root = div(
        p(f"Georgia: {TEXT}", _style="font-family:Georgia, serif; font-size:16px;"),
        p(f"Monospace: {TEXT}", _style="font-family:monospace; font-size:16px;"),
        p(f"Bold sans-serif: {TEXT}", _style="font-family:sans-serif; font-weight:bold; font-size:16px;"),
        p(f"Wide letter-spacing: {TEXT}", _style="font-size:16px; letter-spacing:2px;"),
        _style="width:280px; padding:10px;",
    )
    png = chromonic.render(root, width=300)
    out = Path(__file__).parent / "parley_demo.png"
    out.write_bytes(png)
    print(f"wrote {out} ({len(png)} bytes)")
    for para in root.childNodes:
        print(f"  {len(para._chromonic_text_lines)} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
