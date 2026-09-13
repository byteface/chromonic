"""Images load in the background now, not on the layout/paint thread --
the page appears immediately and each `<img>` pops in as its own fetch
completes, instead of the whole page waiting for the slowest one before
it can be shown at all. See `browser_images.py`'s module docstring for how
(a background thread pool + a `generation()` counter a browser's event loop
polls, the same "poll on the main thread, never touch the DOM off it" shape
`native_browser.py`'s own page-navigation already used).

Serves a page with a handful of deliberately slow, server-held-open images
from a local HTTP server (no live network needed to run this), and saves a
"before" and "after" PNG so the difference is visible without needing a
real window.

    .venv/bin/python chromonic/examples/lazy_images_demo.py
    # -> examples/lazy_images_before.png, examples/lazy_images_after.png
"""

from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import skia  # noqa: E402

import chromonic  # noqa: E402

_PAGE = b"""<!doctype html><html><body>
<h1>Lazy image loading</h1>
<p>Three images below are held open by the server for a moment, on purpose,
to simulate a slow connection.</p>
<img src="a.png"> <img src="b.png"> <img src="c.png">
</body></html>"""


def _make_png(color) -> bytes:
    surface = skia.Surface(80, 80)
    surface.getCanvas().clear(color)
    return bytes(surface.makeImageSnapshot().encodeToData())


def _serve(delay_seconds: float):
    images = {"/a.png": _make_png(skia.ColorRED), "/b.png": _make_png(skia.ColorGREEN), "/c.png": _make_png(skia.ColorBLUE)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body, ctype = _PAGE, "text/html"
            else:
                time.sleep(delay_seconds)  # simulate a slow image host
                body, ctype = images.get(self.path, (b"not found", "text/plain")), "image/png"
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
    server = _serve(delay_seconds=0.8)
    try:
        from chromonic.native_browser import View

        view = View(400, 300)
        t0 = time.perf_counter()
        view.navigate(f"http://127.0.0.1:{server.server_address[1]}/")
        print(f"navigate() returned after {(time.perf_counter() - t0) * 1000:.0f}ms (images still loading)")

        surface = skia.Surface(400, 300)
        canvas = surface.getCanvas()
        view.draw(canvas)
        before = Path(__file__).parent / "lazy_images_before.png"
        before.write_bytes(bytes(surface.makeImageSnapshot().encodeToData()))
        print(f"wrote {before} -- the page, before any image arrived")

        from chromonic import browser_images

        while browser_images.has_pending():
            view.poll_images()
            time.sleep(0.05)
        view.poll_images()  # catch completions that landed after the last has_pending() check
        view.draw(canvas)
        after = Path(__file__).parent / "lazy_images_after.png"
        after.write_bytes(bytes(surface.makeImageSnapshot().encodeToData()))
        print(f"wrote {after} -- once all three arrived")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
