"""chromonic phase 12: `position:absolute`'s real containing block, and
`<select>` as a closed dropdown -- both found comparing chromonic's own
rendering of `https://www.wikipedia.org/` against Chrome, side by side.

1. A `position:absolute` element used to be positioned relative to its
   literal DOM parent -- correct only when that parent happens to be a CSS
   containing block (`position` other than `static`) itself. Real CSS
   resolves it against the *nearest ancestor* with non-static position, or
   the page itself if there is none. `tree.py`'s `build()` now does this
   properly (`is_containing_block`/`escapees`) -- see its own docstring.

2. `<select>` is a native, *closed* dropdown -- a real browser shows only
   its current value, never every `<option>` stacked as visible content.
   Without special handling, `tree.py` recursed into a `<select>`'s
   `<option>` children like any other block content -- harmless for three
   options, but wikipedia.org's real language picker has 250+, which
   rendered as a wall of text overlapping everything below it.

    .venv/bin/python chromonic/examples/containing_block_and_select_demo.py
    # -> examples/containing_block_and_select.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div, option, p, select  # noqa: E402

import chromonic  # noqa: E402


def main() -> int:
    # A badge absolutely positioned against a *distant* ancestor, not its
    # literal (static) parent -- exactly the wikipedia.org search-box shape.
    badge = div("NEW", _style=(
        "position:absolute; top:10px; right:10px; background-color:#e53e3e; "
        "color:#ffffff; padding:4px 10px;"
    ))
    static_wrapper = div(
        p("This wrapper is an ordinary position:static <div> -- it is NOT the badge's containing block."),
        _style="width:400px;",
    )
    card = div(
        static_wrapper, badge,
        _style="position:relative; width:440px; padding:20px; background-color:#f0f4f8;",
    )

    # A <select> with several options -- only the selected one should ever
    # be visible, not all of them stacked.
    picker = select(
        option("Afrikaans", _value="af"),
        option("Deutsch", _value="de", _selected="selected"),
        option("English", _value="en"),
        option("Français", _value="fr"),
        _style="width:200px; padding:6px; margin-top:16px; border:1px solid rgb(150,150,150);",
    )
    label = p(
        "The <select> below has 4 options -- only the selected one ('Deutsch') should be visible:",
        _style="margin-top:16px;",
    )

    root = div(card, label, picker, _style="width:460px; padding:10px; background-color:#ffffff;")
    png = chromonic.render(root, width=460)
    out = Path(__file__).parent / "containing_block_and_select.png"
    out.write_bytes(png)
    print(f"wrote {out} ({len(png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
