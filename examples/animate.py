"""chromonic phase 3: requestAnimationFrame-driven animation.

Seven bars in a flex row, each height following its own phase-shifted sine
wave -- a real-time equalizer, entirely from Python mutating
`element.style.height` from `window.requestAnimationFrame` callbacks and
chromonic relaying it through Taffy (relayout) and Skia (repaint) every frame.

Needs a real display -- run it by hand:

    make develop
    .venv/bin/python chromonic/examples/animate.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import div  # noqa: E402

import chromonic  # noqa: E402

WIDTH = 420
HEIGHT = 200
BAR_WIDTH = 40
BAR_COUNT = 7
COLORS = ["#e53e3e", "#dd6b20", "#d69e2e", "#38a169", "#3182ce", "#5a67d8", "#805ad5"]


def build_page():
    bars = [
        div(_style=f"width:{BAR_WIDTH}px; height:10px; background-color:{COLORS[i]}; border-radius:0;")
        for i in range(BAR_COUNT)
    ]
    stage = div(
        *bars,
        _style=(
            "display:flex; flex-direction:row; align-items:flex-end; justify-content:center; "
            f"gap:10px; width:{WIDTH}px; height:{HEIGHT}px; background-color:#1a202c; padding:16px;"
        ),
    )
    return stage, bars


def main() -> int:
    stage, bars = build_page()
    state = {"window": None}

    def animate(timestamp_ms: float):
        t = timestamp_ms / 1000.0
        for i, bar in enumerate(bars):
            phase = i * 0.7
            wave = 0.5 + 0.5 * math.sin(t * 2.4 + phase)  # 0..1
            bar.style.height = f"{10 + wave * (HEIGHT - 60):.1f}px"
        state["window"].requestAnimationFrame(animate)

    def start_animation(dom_window):
        state["window"] = dom_window
        dom_window.requestAnimationFrame(animate)

    chromonic.window.run(
        stage,
        width=WIDTH,
        height=HEIGHT,
        title="chromonic -- requestAnimationFrame equalizer",
        on_window_ready=start_animation,
        fps=60,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
