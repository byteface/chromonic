"""Real <video> playback: decoded frames composited into the render tree.

Opens video_demo.html in the real native browser -- click a video to
play/pause it. The decode itself is ffmpeg (see video_backend.py's own
docstring for why, and what a real GStreamer/Rust backend would replace
here later); everything from there on (layout, sizing, paint, click
handling) is ordinary chromonic.

    .venv/bin/python examples/video_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from chromonic.native_browser import run  # noqa: E402

HTML_PATH = Path(__file__).resolve().parent / "video_demo.html"


if __name__ == "__main__":
    run(str(HTML_PATH), width=560, height=760, title="Chromonic — video demo")
