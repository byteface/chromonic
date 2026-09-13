"""chromonic phase 2: a real, interactive window. See ../PLAN.md and
python/chromonic/window.py for how the click -> hit-test -> real DOM event ->
relayout -> repaint loop works.

Needs a real display -- run it by hand, not from an automated check:

    .venv/bin/pip install 'chromonic[window]'   # adds pywebview
    .venv/bin/python chromonic/examples/live.py

Click the button. Its background colour and the counter above it are both
ordinary domonic DOM state, mutated by an ordinary `addEventListener`
handler -- chromonic's job stops at "relayout and repaint after something
changed"; the interactivity itself is just the DOM being the DOM.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from domonic.html import button, div, p  # noqa: E402

import chromonic  # noqa: E402

WIDTH = 420
COLORS = ["#2b6cb0", "#2f855a", "#c05621", "#805ad5"]


def build_page():
    count_label = p("Clicks: 0", _style="margin:0 0 16px; color:#1a202c; font-size:18px;")
    action_button = button(
        "Click me",
        _style=f"background-color:{COLORS[0]}; color:#ffffff; border:3px solid #1a202c; padding:14px 28px;",
    )
    state = {"clicks": 0}

    def on_click(event):
        # A real bubbling DOM event -- this listener is on the *wrapper*,
        # not the button itself, so this is delegation, exactly like a
        # browser: whatever was hit, the click bubbled up to here.
        state["clicks"] += 1
        count_label.textContent = f"Clicks: {state['clicks']}"
        action_button.style.backgroundColor = COLORS[state["clicks"] % len(COLORS)]

    wrapper = div(
        count_label,
        action_button,
        _style=f"display:flex; flex-direction:column; align-items:flex-start; padding:24px; width:{WIDTH}px; background-color:#ffffff;",
    )
    wrapper.addEventListener("click", on_click)
    return wrapper


def main() -> int:
    root = build_page()
    chromonic.window.run(root, width=WIDTH, height=160, title="chromonic -- click the button")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
