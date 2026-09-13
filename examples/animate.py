"""chromonic phase 3: a continuous animation, not just click-driven repaints.

Seven bars in a flex row, each height following its own phase-shifted sine
wave -- a real-time equalizer, entirely from Python mutating
`element.style.height` ~30 times a second and chromonic relaying it through
Taffy (relayout) and Skia (repaint) every time. No pywebview thread, no
Rust-side timer -- see `window.py`'s `run(on_tick=...)`: the hosted page
itself is the clock, calling back into Python on the same bridge a click
already uses, self-paced (each tick scheduled only once the previous one
resolves, see `window._TICK_LOOP_JS`) rather than fired on a bare timer.

Needs a real display -- run it by hand:

    .venv/bin/pip install 'chromonic[window]'
    .venv/bin/python chromonic/examples/animate.py
"""

from __future__ import annotations

import math
import sys
import time
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
    start = time.perf_counter()

    def on_tick():
        t = time.perf_counter() - start
        for i, bar in enumerate(bars):
            phase = i * 0.7
            wave = 0.5 + 0.5 * math.sin(t * 2.4 + phase)  # 0..1
            bar.style.height = f"{10 + wave * (HEIGHT - 60):.1f}px"

    chromonic.window.run(stage, width=WIDTH, height=HEIGHT, title="chromonic -- live equalizer", on_tick=on_tick, fps=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
