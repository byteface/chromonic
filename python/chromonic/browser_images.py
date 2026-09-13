"""Fetch, decode, and cache `<img>` images for chromonic's Taffy tree and Skia
painter. Not a new architecture: an image's decoded `skia.Image` is fetched
once (URL-keyed, in-process cache) and supplies exactly two things the rest
of chromonic already knows how to use -- an intrinsic size for `tree.py`'s
Taffy leaf (the same `measure`-callback mechanism `_make_measure` already
gives text leaves, see `tree.py`), and the pixels `paint.py` draws for that
leaf's box.

Scope, deliberately: a plain `<img src="...">` only -- no `<picture>`/
`srcset`, no `object-fit` (an image with an explicit CSS size just stretches
to fill it, which happens to match a real browser's own default
`object-fit: fill` behaviour for a plain `<img>`), no SVG images
(`skia.Image.MakeFromEncoded` doesn't decode SVG -- it returns `None`
exactly like a broken/failed fetch does, so an SVG `<img>` quietly paints as
an empty box rather than erroring). See "Known limitations" in README.md.

**Fetches happen in the background, not on the layout/paint thread.**
`load_image()` used to fetch synchronously the first time a URL was seen --
meaning the very first `tree.layout()` call touching an `<img>` blocked on a
real network round-trip before the page could finish laying out at all, and
a page with several never-before-seen images visibly froze until *all* of
them arrived. Fixed the same way `native_browser.py`'s own page navigation
already avoids blocking on network I/O (`ThreadPoolExecutor` + a poll from
the main thread, never touching the DOM/Taffy tree from a background
thread): `load_image()` kicks a fetch off in a background thread pool and
returns `None` immediately if the image isn't ready yet (so the element
just lays out at its CSS size, or 0x0 if it has none -- the same "no size
reserved" behaviour a real browser gives an `<img>` with no `width`/
`height` while it's still loading). `generation()` increments every time a
background fetch finishes (success or failure); a browser's own event loop
polls it once per frame/iteration (see `native_browser.py`'s `View.poll_images`
and `browser.py`'s tick loop) and triggers a relayout+repaint only when it
actually changed -- so images now pop in as they arrive instead of the
whole page waiting for the slowest one.
"""

from __future__ import annotations

import base64
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import skia

_UA = "chromonic/images (+https://github.com/byteface/domonic-libs)"
_TIMEOUT = 10.0

# URL -> decoded skia.Image, or None for a URL that failed to fetch/decode --
# caching the failure too, so a broken image URL is retried at most once per
# process, not once per relayout.
_cache: "dict[str, skia.Image | None]" = {}
_pending: "set[str]" = set()
_lock = threading.Lock()
_generation = 0
_executor: "ThreadPoolExecutor | None" = None


def _get_executor() -> ThreadPoolExecutor:
    # Created lazily, not at import time -- most chromonic consumers
    # (poc.py, particles.py, the pyscript demos, ...) never load an image
    # at all, and a thread pool is real (if small) setup cost.
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chromonic-image")
    return _executor


def _is_url(value) -> bool:
    return isinstance(value, str) and value.split(":", 1)[0].lower() in ("http", "https")


def _decode_data_uri(uri: str) -> bytes:
    header, _, payload = uri.partition(",")
    if ";base64" in header:
        return base64.b64decode(payload)
    return urllib.parse.unquote_to_bytes(payload)


def _fetch_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        return _decode_data_uri(url)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310 - page-declared image URL
        return response.read()


def _fetch_and_decode(url: str) -> None:
    """Runs on a background thread -- network I/O and Skia decoding only,
    nothing touching the DOM or a Taffy tree (neither is safe to touch off
    the main thread; see `native_browser.py`'s `Navigation` for the same
    rule already established for page loads)."""
    global _generation
    try:
        data = _fetch_bytes(url)
        image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    except Exception:
        image = None
    _cache[url] = image
    with _lock:
        _pending.discard(url)
        _generation += 1


def resolve_image_sources(document, base_url: str) -> None:
    """Rewrite every `<img src="...">` in `document` to an absolute URL
    against `base_url` (the page's own URL) -- called once, right after a
    page is fetched+parsed (see `browser.load`), so `tree.py`/`paint.py`
    never need to know what page an `<img>` came from: by the time they see
    it, `src` is already whatever `load_image` below can fetch directly."""
    for img in document.getElementsByTagName("img"):
        src = img.getAttribute("src")
        if not src or src.startswith("data:") or _is_url(src):
            continue
        img.setAttribute("src", urllib.parse.urljoin(base_url, src))


def load_image(url: str) -> "skia.Image | None":
    """The decoded image for `url` if it's already been fetched, `None`
    otherwise -- and if `None`, a background fetch is started (unless one
    is already in flight) so a *later* call (the next relayout/repaint,
    triggered once `generation()` changes -- see the module docstring)
    will have it. Never blocks on network I/O itself. `None` is also the
    permanent answer for a missing `url`, a failed fetch, or anything
    Skia's decoders don't understand (SVG, an unsupported format, corrupt
    data) -- a failure is cached exactly like a success, so a broken image
    URL is fetched at most once per process, not once per relayout.

    A `data:` URI is decoded immediately, synchronously, on this call --
    there's no network round-trip to move off-thread, only Skia decoding
    of bytes already in hand, so there's nothing to gain (and a first-frame
    delay to lose) by routing it through the background pool too."""
    if not url:
        return None
    if url in _cache:
        return _cache[url]
    if url.startswith("data:"):
        try:
            image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(_decode_data_uri(url)))
        except Exception:
            image = None
        _cache[url] = image
        return image
    with _lock:
        already_fetching = url in _pending
        if not already_fetching:
            _pending.add(url)
    if not already_fetching:
        _get_executor().submit(_fetch_and_decode, url)
    return None


def generation() -> int:
    """Increments every time a background image fetch finishes (success or
    failure). A browser's event loop polls this once per iteration/frame,
    comparing against the value it last saw -- a change means it's worth
    doing a relayout+repaint (some `<img>`'s intrinsic size, or its
    pixels, just became available); no change means nothing did."""
    return _generation


def has_pending() -> bool:
    """Whether any image fetch is currently in flight -- a browser's event
    loop can poll more eagerly while this is true (see
    `native_browser.py`'s `run()`), rather than waiting its normal idle
    interval before it would next notice `generation()` changing."""
    return bool(_pending)


def clear_cache() -> None:
    """Forget every cached (and pending) image -- tests use this so one
    test's fetch doesn't silently satisfy another's from the shared
    process-wide cache."""
    _cache.clear()
    with _lock:
        _pending.clear()
