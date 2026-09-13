"""chromonic phase 5, the `src="app.py"` form:

    <script type="text/python" src="pyscript_app.py"></script>

The same demo as `pyscript_demo.py`, except the Python lives in its own file
(`pyscript_app.py`) next to the HTML (`pyscript_page.html`) and is loaded by
`src=`, resolved relative to the page's own file -- the same resolution rule
`myjs.html.Page._resolve` uses for `<script src>`/`<link href>`.

Needs a real display -- run it by hand:

    .venv/bin/python chromonic/examples/pyscript_src_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[0] / "python"))

import chromonic  # noqa: E402


def main() -> int:
    html = (_HERE / "pyscript_page.html").read_text(encoding="utf-8")
    document, _scope = chromonic.pyscript.parse_and_run(html, base_dir=_HERE)
    chromonic.window.run(document.body, width=320, title="chromonic -- python script (src=)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
