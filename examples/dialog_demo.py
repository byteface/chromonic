"""<dialog>, opened -- forms_demo.html's own dialog section, pre-opened.

forms_demo.html ships its dialog closed (a full-page modal blocking
everything on load is a bad first impression, and there's no click-to-open
without script -- see its own note). This instead takes the exact same
markup and flips `open` on before handing it to the real browser, so you
can see the centred overlay/backdrop and try its script-free Cancel/Delete
`<form method="dialog">` buttons directly.

    .venv/bin/python examples/dialog_demo.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from chromonic.native_browser import run  # noqa: E402

SOURCE_HTML = Path(__file__).resolve().parent / "forms_demo.html"


if __name__ == "__main__":
    html = SOURCE_HTML.read_text().replace(
        '<dialog id="confirm-dialog">', '<dialog id="confirm-dialog" open>',
    )
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as tmp:
        tmp.write(html)
        tmp_path = tmp.name
    run(tmp_path, width=760, height=820, title="Chromonic — dialog demo")
