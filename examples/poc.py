"""chromonic POC -- see ../PLAN.md.

Builds one domonic page exercising nested elements, text, margin/padding,
flexbox, CSS grid, backgrounds, borders, and a button; lays it out through
Taffy and paints it through Skia; proves `getBoundingClientRect()` matches
what got painted; then mutates the live DOM and re-renders to show the
whole pipeline reacting.

    .venv/bin/python chromonic/examples/poc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import button, div, h1, p  # noqa: E402

import chromonic  # noqa: E402
from chromonic import hittest  # noqa: E402

WIDTH = 900
OUT_DIR = Path(__file__).resolve().parent


def build_page():
    """A page carrying every required feature in one screen:
    nested elements + text, margin/padding, a flex row, a CSS grid,
    backgrounds/borders, and a button."""

    def card(text, color):
        return div(
            p(text, _style="color:#ffffff; margin:0;"),
            _style=(
                f"background-color:{color}; border:3px solid #222222; "
                "border-radius:0; padding:16px; flex-grow:1; flex-basis:0;"
            ),
        )

    flex_row = div(
        card("Flexbox", "#2b6cb0"),
        card("owns", "#2f855a"),
        card("this row", "#c05621"),
        _style="display:flex; flex-direction:row; gap:16px; padding:16px; margin:16px 0;",
    )

    def grid_cell(text, color, *, column=None):
        style = f"background-color:{color}; border:2px solid #222222; padding:12px;"
        if column:
            style += f" grid-column:{column};"
        return div(p(text, _style="color:#ffffff; margin:0;"), _style=style)

    grid = div(
        grid_cell("1", "#805ad5"),
        grid_cell("2 (spans 2 cols)", "#d53f8c", column="span 2"),
        grid_cell("3", "#3182ce"),
        grid_cell("4", "#dd6b20"),
        _style=(
            "display:grid; grid-template-columns:100px 1fr 1fr; "
            "gap:12px; padding:16px; margin:16px 0; background-color:#edf2f7; border:1px solid #cbd5e0;"
        ),
    )

    action_button = button(
        "Click me",
        _style=(
            "background-color:#e53e3e; color:#ffffff; border:2px solid #742a2a; "
            "padding:10px 20px; margin:16px;"
        ),
    )

    root = div(
        h1("Domonic + Taffy + Skia", _style="margin:16px; color:#1a202c;"),
        p(
            "Every box below is a real domonic element. Taffy computed its "
            "geometry; Skia painted from that geometry; nothing here is a "
            "second DOM.",
            _style="margin:0 16px 16px; color:#4a5568;",
        ),
        flex_row,
        grid,
        action_button,
        _style="display:flex; flex-direction:column; width:%dpx; background-color:#ffffff;" % WIDTH,
    )
    return root, flex_row, grid, action_button


def prove_geometry_matches(root):
    """`element.getBoundingClientRect()` must equal the LayoutBox chromonic
    just painted from -- domonic 1.8.0's `set_layout_box` wiring, not
    anything chromonic fakes."""
    print("\n--- getBoundingClientRect() vs. the box chromonic painted from ---")
    for el in (root, *[c for c in root.childNodes if getattr(c, "nodeType", None) == 1][:4]):
        box = el.get_layout_box()
        rect = el.getBoundingClientRect()
        match = (rect.x, rect.y, rect.width, rect.height) == (box.x, box.y, box.width, box.height)
        tag = getattr(el, "tagName", "?")
        print(f"  <{tag}> rect=({rect.x:.1f},{rect.y:.1f},{rect.width:.1f}x{rect.height:.1f})"
              f"  layout_box=({box.x:.1f},{box.y:.1f},{box.width:.1f}x{box.height:.1f})  match={match}")
        assert match, f"getBoundingClientRect() disagreed with the painted box for <{tag}>"


def main() -> int:
    root, flex_row, grid, action_button = build_page()

    node_map = chromonic.layout(root, width=WIDTH)
    height = int(round(root.get_layout_box().height))
    png = chromonic.paint.render_png(root, width=WIDTH, height=height)
    out = OUT_DIR / "poc.png"
    out.write_bytes(png)
    print(f"wrote {out} ({WIDTH}x{height})")

    prove_geometry_matches(root)

    # hit-test: the middle of the second flex card should resolve to that
    # card's own <p>, not the row or the page.
    card_box = flex_row.childNodes[1].get_layout_box()
    hx, hy = card_box.x + card_box.width / 2, card_box.y + card_box.height / 2
    hit = hittest.hit_test(root, hx, hy)
    print(f"\nhit-test at ({hx:.0f},{hy:.0f}) -> <{hit.tagName}> {hit.textContent.strip()!r}")

    # --- live mutation: element.style.width = "400px" -----------------
    print("\n--- mutating grid.style.width = '400px' and re-rendering ---")
    before = grid.get_layout_box()
    grid.style.width = "400px"
    chromonic.layout(root, width=WIDTH)
    after = grid.get_layout_box()
    height2 = int(round(root.get_layout_box().height))
    png2 = chromonic.paint.render_png(root, width=WIDTH, height=height2)
    out2 = OUT_DIR / "poc_mutated.png"
    out2.write_bytes(png2)
    print(f"wrote {out2} ({WIDTH}x{height2})")
    print(f"  grid width before: {before.width:.1f}px   after: {after.width:.1f}px")
    assert round(after.width) == 400, "the mutation did not reach Taffy"
    print("\nmark layout dirty -> rerun Taffy -> update geometry -> repaint: proven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
