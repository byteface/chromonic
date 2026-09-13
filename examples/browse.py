"""chromonic as a simple browser: fetch a real URL, render it through the
Taffy+Skia pipeline, and click around it in a real window with a working
address bar. Built for visually testing how domonic's DOM/CSSOM handles
real-world pages -- not a general-purpose browser.

Needs a real display -- run it by hand:

    make develop
    .venv/bin/python chromonic/examples/browse.py [url]

Defaults to example.com if no URL is given.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import chromonic  # noqa: E402


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://google.com/"
    chromonic.browser.run(url, width=1000, height=800, title="chromonic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
