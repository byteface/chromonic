"""Every form control chromonic supports, live and interactive.

Opens forms_demo.html in the real native browser (toolbar, address bar,
the works) -- click a checkbox, open the dropdown, drag the slider, open
the dialog. Not a static render.

    .venv/bin/python examples/forms_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from chromonic.native_browser import run  # noqa: E402

HTML_PATH = Path(__file__).resolve().parent / "forms_demo.html"


if __name__ == "__main__":
    run(str(HTML_PATH), width=760, height=820, title="Chromonic — form controls demo")
