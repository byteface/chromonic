"""chromonic phase 8: a UA stylesheet (`ua_style.py`) and real `<img>` loading
(`browser_images.py`) -- both wired into `chromonic.browser.load()`, so the
native browser gets them automatically.

Serves a tiny page (a heading, a paragraph, a list, two images) from a local
HTTP server -- no live network needed to run this -- fetches it through the
real `browser.load()` pipeline, and saves a PNG.

    .venv/bin/python chromonic/examples/ua_and_images_demo.py
    # -> examples/ua_and_images.png
"""

from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import skia  # noqa: E402

import chromonic  # noqa: E402

_PAGE = b"""<!doctype html><html><body>
<h1>Hello, chromonic</h1>
<p>This paragraph has real spacing above and below it, from the UA
stylesheet -- not the zero margin domonic's cascade gives every element by
default (see docs/domonic-wrinkles.md #11).</p>
<ul><li>an indented list item</li><li>and another one</li></ul>
<img id="natural" src="logo.png">
</body></html>"""


def _make_checker_png() -> bytes:
    surface = skia.Surface(64, 64)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorWHITE)
    canvas.drawRect(skia.Rect.MakeXYWH(0, 0, 32, 32), skia.Paint(Color=skia.ColorRED))
    canvas.drawRect(skia.Rect.MakeXYWH(32, 32, 32, 32), skia.Paint(Color=skia.ColorBLUE))
    return bytes(surface.makeImageSnapshot().encodeToData())


def _serve(png: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body, ctype = (_PAGE, "text/html") if self.path == "/" else (png, "image/png")
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    server = _serve(_make_checker_png())
    try:
        page = chromonic.browser.load(f"http://127.0.0.1:{server.server_address[1]}/")
        png = chromonic.render(page.document.body, width=500)
        out = Path(__file__).parent / "ua_and_images.png"
        out.write_bytes(png)

        img = page.document.getElementById("natural")
        box = img.get_layout_box()
        print(f"wrote {out} ({len(png)} bytes); <img> intrinsic size: {box.width:.0f}x{box.height:.0f}")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
