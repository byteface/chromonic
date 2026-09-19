"""Concurrent image resource pipeline for Chromonic.

This module owns image resource I/O and decode, not DOM/layout/paint.  Callers
continue to use ``load_image(url)`` from layout/paint, while page parsing can
call ``resolve_image_sources(document, base_url)`` to resolve *and preload*
images before the first layout reaches them.

Key properties:

* network fetch and Skia/GIF decode never block the UI/layout thread;
* fetch and decode use separate pools so slow sockets do not occupy decode
  capacity (and expensive decodes do not prevent more downloads starting);
* completed resources are exposed as generation-stamped ``ImageEvent`` objects,
  allowing the browser to react only to images used by its current document;
* transient failures are negative-cached for a short TTL rather than forever;
* decoded images live in a bounded LRU cache rather than growing without limit;
* downloads are size-capped, and large data: URIs are decoded off-thread;
* cache clears are epoch-safe: stale workers can finish, but cannot repopulate a
  cache that was cleared while they were running;
* all shared dictionaries/sets/counters are protected by one lock.  No worker
  ever touches DOM, Taffy or the GL context.

The old public API (``load_image``, ``generation``, ``has_pending``,
``advance_animations``, ``clear_cache``) remains valid.
"""

from __future__ import annotations

import base64
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

import skia

from .animated_gif import AnimatedGIF, decode_animated_gif

_UA = "chromonic/images (+https://github.com/byteface/domonic-libs)"
_TIMEOUT = float(os.getenv("CHROMONIC_IMAGE_TIMEOUT", "10"))
_FETCH_WORKERS = max(1, int(os.getenv("CHROMONIC_IMAGE_FETCH_WORKERS", "8")))
_DECODE_WORKERS = max(
    1,
    int(
        os.getenv(
            "CHROMONIC_IMAGE_DECODE_WORKERS",
            str(min(4, max(2, (os.cpu_count() or 2) // 2))),
        )
    ),
)
_MAX_IMAGE_BYTES = max(1, int(os.getenv("CHROMONIC_MAX_IMAGE_BYTES", str(32 * 1024 * 1024))))
_CACHE_BUDGET_BYTES = max(
    1,
    int(os.getenv("CHROMONIC_IMAGE_CACHE_BYTES", str(256 * 1024 * 1024))),
)
_FAILURE_TTL = max(0.0, float(os.getenv("CHROMONIC_IMAGE_FAILURE_TTL", "30")))
# Small inline resources are cheap enough to decode synchronously when first
# encountered by layout.  Large ones are work too, despite having no network.
_DATA_URI_SYNC_LIMIT = max(
    0,
    int(os.getenv("CHROMONIC_DATA_URI_SYNC_BYTES", str(256 * 1024))),
)
_EVENT_HISTORY = max(64, int(os.getenv("CHROMONIC_IMAGE_EVENT_HISTORY", "2048")))
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class ImageEvent:
    """One completed background image request.

    ``generation`` is monotonically increasing.  A view can retain the last
    generation it consumed and call :func:`events_since` without draining a
    global queue, so multiple views/debuggers can observe the same events.
    """

    generation: int
    url: str
    success: bool
    width: int | None
    height: int | None
    animated: bool
    encoded_bytes: int
    fetch_ms: float
    decode_ms: float
    error: str | None = None


@dataclass(slots=True)
class _CacheEntry:
    image: skia.Image
    width: int
    height: int
    decoded_bytes: int
    loaded_at: float


@dataclass(slots=True)
class _Failure:
    retry_at: float
    error: str


# URL -> decoded first/static frame.  OrderedDict gives us an inexpensive LRU.
_cache: "OrderedDict[str, _CacheEntry]" = OrderedDict()
_animations: dict[str, AnimatedGIF] = {}
_failures: dict[str, _Failure] = {}
_pending: dict[str, int] = {}  # URL -> cache epoch that owns this request
_events: "deque[ImageEvent]" = deque(maxlen=_EVENT_HISTORY)
_futures: "set[Future]" = set()

_lock = threading.RLock()
_generation = 0
_epoch = 0
_cache_bytes = 0
_fetch_executor: "ThreadPoolExecutor | None" = None
_decode_executor: "ThreadPoolExecutor | None" = None
_shutdown_requested = False

# Lightweight cumulative counters used by the F10 HUD / diagnostics.
_metrics = {
    "requests": 0,
    "cache_hits": 0,
    "fetches": 0,
    "successes": 0,
    "failures": 0,
    "evictions": 0,
    "network_bytes": 0,
    "fetch_completed": 0,
    "decode_completed": 0,
    "fetch_ms": 0.0,
    "decode_ms": 0.0,
}


def _executor(kind: str) -> ThreadPoolExecutor:
    global _fetch_executor, _decode_executor
    with _lock:
        if _shutdown_requested:
            raise RuntimeError("Chromonic image pipeline is shut down")
        if kind == "fetch":
            if _fetch_executor is None:
                _fetch_executor = ThreadPoolExecutor(
                    max_workers=_FETCH_WORKERS,
                    thread_name_prefix="chromonic-img-fetch",
                )
            return _fetch_executor
        if _decode_executor is None:
            _decode_executor = ThreadPoolExecutor(
                max_workers=_DECODE_WORKERS,
                thread_name_prefix="chromonic-img-decode",
            )
        return _decode_executor


def _track_future(future: Future) -> None:
    with _lock:
        _futures.add(future)

    def done(completed: Future) -> None:
        with _lock:
            _futures.discard(completed)

    future.add_done_callback(done)


def _submit(kind: str, fn, *args) -> Future:
    future = _executor(kind).submit(fn, *args)
    _track_future(future)
    return future


def _is_url(value) -> bool:
    return isinstance(value, str) and value.split(":", 1)[0].lower() in ("http", "https")


def _supported_source(url: str) -> bool:
    return url.startswith("data:") or _is_url(url)


def _decode_data_uri(uri: str) -> bytes:
    header, separator, payload = uri.partition(",")
    if not separator:
        raise ValueError("malformed data URI")
    if ";base64" in header.lower():
        # Refuse an obviously enormous base64 payload before allocating the
        # decoded bytes (4 encoded chars represent at most 3 decoded bytes).
        estimated = (len(payload) * 3) // 4
        if estimated > _MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {_MAX_IMAGE_BYTES} byte limit")
        data = base64.b64decode(payload, validate=False)
    else:
        data = urllib.parse.unquote_to_bytes(payload)
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {_MAX_IMAGE_BYTES} byte limit")
    return data


def _fetch_bytes(url: str) -> bytes:
    """Fetch one encoded image with a hard upper bound on response size."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310 - page-declared image URL
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                declared = 0
            if declared > _MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {_MAX_IMAGE_BYTES} byte limit")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {_MAX_IMAGE_BYTES} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)


def _decode_image(data: bytes) -> "skia.Image | None":
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    if image is not None:
        return image

    # Skia's encoded raster path does not handle SVG, but SVGDOM does.  Render
    # it once to a bitmap because the rest of Chromonic already consumes
    # skia.Image and should not need a second SVG-specific paint architecture.
    try:
        stream = skia.MemoryStream.MakeCopy(data)
        svg = skia.SVGDOM.MakeFromStream(stream)
        if svg is None:
            return None
        size = svg.containerSize()
        width = max(1, int(round(size.width())))
        height = max(1, int(round(size.height())))
        # Avoid accidentally allocating a pathological SVG surface.  This is a
        # decoded-memory guard, independent of the encoded network size limit.
        if width * height * 4 > _CACHE_BUDGET_BYTES:
            return None
        svg.setContainerSize(skia.Size(width, height))
        surface = skia.Surface(width, height)
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorTRANSPARENT)
        svg.render(canvas)
        return surface.makeImageSnapshot()
    except Exception:
        return None


def _decode_resource(data: bytes):
    animation = decode_animated_gif(data)
    if animation is not None:
        return animation.frames[0], animation
    return _decode_image(data), None


def _image_size(image: skia.Image) -> tuple[int, int]:
    return int(image.width()), int(image.height())


def _decoded_size(image: skia.Image, animation: AnimatedGIF | None) -> int:
    """Approximate resident decoded pixel bytes for cache budgeting."""
    if animation is not None:
        frames = getattr(animation, "frames", None)
        if frames:
            total = 0
            for frame in frames:
                try:
                    total += int(frame.width()) * int(frame.height()) * 4
                except Exception:
                    pass
            if total:
                return total
    width, height = _image_size(image)
    return width * height * 4


def _evict_locked() -> None:
    global _cache_bytes
    # Keep at least the newest entry even if one unusually large decoded image
    # exceeds the nominal budget by itself; otherwise every paint would refetch
    # it and turn memory pressure into a network loop.
    while _cache_bytes > _CACHE_BUDGET_BYTES and len(_cache) > 1:
        url, entry = _cache.popitem(last=False)
        _animations.pop(url, None)
        _cache_bytes = max(0, _cache_bytes - entry.decoded_bytes)
        _metrics["evictions"] += 1


def _still_owned(url: str, epoch: int) -> bool:
    with _lock:
        return epoch == _epoch and _pending.get(url) == epoch


def _finish(
    url: str,
    epoch: int,
    image: "skia.Image | None",
    animation: "AnimatedGIF | None",
    *,
    encoded_bytes: int,
    fetch_ms: float,
    decode_ms: float,
    error: str | None = None,
) -> None:
    """Publish a worker result atomically, or discard it if it became stale."""
    global _generation, _cache_bytes

    with _lock:
        if epoch != _epoch or _pending.get(url) != epoch:
            return
        _pending.pop(url, None)
        _metrics["fetch_ms"] += fetch_ms
        _metrics["decode_ms"] += decode_ms
        if _is_url(url):
            _metrics["network_bytes"] += encoded_bytes
            _metrics["fetch_completed"] += 1
        if decode_ms > 0.0:
            _metrics["decode_completed"] += 1

        success = image is not None
        width = height = None
        animated = animation is not None

        if success:
            width, height = _image_size(image)
            decoded_bytes = _decoded_size(image, animation)
            old = _cache.pop(url, None)
            if old is not None:
                _cache_bytes = max(0, _cache_bytes - old.decoded_bytes)
            _cache[url] = _CacheEntry(
                image=image,
                width=width,
                height=height,
                decoded_bytes=decoded_bytes,
                loaded_at=time.monotonic(),
            )
            _cache_bytes += decoded_bytes
            if animation is not None:
                _animations[url] = animation
            else:
                _animations.pop(url, None)
            _failures.pop(url, None)
            _metrics["successes"] += 1
            _evict_locked()
        else:
            message = error or "unsupported or corrupt image"
            _failures[url] = _Failure(
                retry_at=time.monotonic() + _FAILURE_TTL,
                error=message,
            )
            _metrics["failures"] += 1

        _generation += 1
        _events.append(
            ImageEvent(
                generation=_generation,
                url=url,
                success=success,
                width=width,
                height=height,
                animated=animated,
                encoded_bytes=encoded_bytes,
                fetch_ms=fetch_ms,
                decode_ms=decode_ms,
                error=None if success else (error or "unsupported or corrupt image"),
            )
        )


def _decode_stage(url: str, epoch: int, data: bytes, fetch_ms: float) -> None:
    if not _still_owned(url, epoch):
        return
    started = time.perf_counter()
    try:
        image, animation = _decode_resource(data)
        error = None if image is not None else "unsupported or corrupt image"
    except Exception as exc:
        image = animation = None
        error = f"{type(exc).__name__}: {exc}"
    decode_ms = (time.perf_counter() - started) * 1000.0
    _finish(
        url,
        epoch,
        image,
        animation,
        encoded_bytes=len(data),
        fetch_ms=fetch_ms,
        decode_ms=decode_ms,
        error=error,
    )


def _fetch_stage(url: str, epoch: int) -> None:
    if not _still_owned(url, epoch):
        return
    started = time.perf_counter()
    try:
        data = _fetch_bytes(url)
    except Exception as exc:
        fetch_ms = (time.perf_counter() - started) * 1000.0
        _finish(
            url,
            epoch,
            None,
            None,
            encoded_bytes=0,
            fetch_ms=fetch_ms,
            decode_ms=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    fetch_ms = (time.perf_counter() - started) * 1000.0
    if not _still_owned(url, epoch):
        return
    try:
        _submit("decode", _decode_stage, url, epoch, data, fetch_ms)
    except RuntimeError as exc:  # executor shut down between stages
        _finish(
            url,
            epoch,
            None,
            None,
            encoded_bytes=len(data),
            fetch_ms=fetch_ms,
            decode_ms=0.0,
            error=str(exc),
        )


def _data_stage(url: str, epoch: int) -> None:
    started = time.perf_counter()
    try:
        data = _decode_data_uri(url)
    except Exception as exc:
        _finish(
            url,
            epoch,
            None,
            None,
            encoded_bytes=0,
            fetch_ms=0.0,
            decode_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
        return
    _decode_stage(url, epoch, data, 0.0)


def request_image(url: str) -> bool:
    """Ensure ``url`` is queued; return True only when this call queued it."""
    global _epoch
    if not url or not _supported_source(url):
        return False

    now = time.monotonic()
    with _lock:
        if _shutdown_requested:
            return False
        if url in _cache or url in _pending:
            return False
        failed = _failures.get(url)
        if failed is not None and now < failed.retry_at:
            return False
        if failed is not None:
            _failures.pop(url, None)
        epoch = _epoch
        _pending[url] = epoch
        _metrics["requests"] += 1
        if not url.startswith("data:"):
            _metrics["fetches"] += 1

    try:
        if url.startswith("data:"):
            _submit("decode", _data_stage, url, epoch)
        else:
            _submit("fetch", _fetch_stage, url, epoch)
    except Exception as exc:
        _finish(
            url,
            epoch,
            None,
            None,
            encoded_bytes=0,
            fetch_ms=0.0,
            decode_ms=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True


def resolve_image_sources(document, base_url: str, *, preload: bool = True) -> list[str]:
    """Resolve ``<img src>`` URLs and opportunistically start loading them.

    This runs immediately after parse in ``browser.load``.  Starting requests
    here overlaps image I/O/decode with the rest of page preparation instead of
    waiting until Taffy or paint first happens to encounter each image.

    ``loading=lazy`` images are resolved but not proactively queued; if layout
    or paint later calls :func:`load_image`, they still load normally.
    """
    sources: list[str] = []
    for img in document.getElementsByTagName("img"):
        src = img.getAttribute("src")
        if not src:
            continue
        if not src.startswith("data:") and not _is_url(src):
            src = urllib.parse.urljoin(base_url, src)
            img.setAttribute("src", src)
        sources.append(src)
        if preload and (img.getAttribute("loading") or "").lower() != "lazy":
            request_image(src)
    return sources


def load_image(url: str) -> "skia.Image | None":
    """Return a decoded image/frame if ready; otherwise queue it and return None.

    Tiny data URIs retain the old first-frame optimisation and may decode
    synchronously when not already preloaded.  Large inline images go through
    the decode pool so a megabyte-sized data URI cannot freeze layout.
    """
    global _cache_bytes
    if not url:
        return None

    now = time.monotonic()
    with _lock:
        entry = _cache.get(url)
        if entry is not None:
            _cache.move_to_end(url)
            _metrics["cache_hits"] += 1
            animation = _animations.get(url)
            return animation.frame() if animation is not None else entry.image

        failed = _failures.get(url)
        if failed is not None and now < failed.retry_at:
            return None
        already_pending = url in _pending

    # Preserve immediate inline rendering for genuinely small data URIs when
    # nothing has preloaded them yet.  This decode happens on whichever caller
    # invoked load_image; normally that is initial layout/paint.
    if (
        not already_pending
        and url.startswith("data:")
        and len(url) <= max(128, _DATA_URI_SYNC_LIMIT * 2)
    ):
        try:
            data = _decode_data_uri(url)
            if len(data) <= _DATA_URI_SYNC_LIMIT:
                image, animation = _decode_resource(data)
                if image is not None:
                    width, height = _image_size(image)
                    decoded_bytes = _decoded_size(image, animation)
                    with _lock:
                        existing = _cache.pop(url, None)
                        if existing is not None:
                            _cache_bytes = max(0, _cache_bytes - existing.decoded_bytes)
                        _cache[url] = _CacheEntry(
                            image=image,
                            width=width,
                            height=height,
                            decoded_bytes=decoded_bytes,
                            loaded_at=time.monotonic(),
                        )
                        _cache_bytes += decoded_bytes
                        if animation is not None:
                            _animations[url] = animation
                        _failures.pop(url, None)
                        _metrics["successes"] += 1
                        _evict_locked()
                    return animation.frame() if animation is not None else image
        except Exception:
            # Let the normal background pipeline record/cache a useful failure
            # rather than giving synchronous data URIs a separate error path.
            pass

    request_image(url)
    return None


def generation() -> int:
    """Monotonic completion generation retained for backwards compatibility."""
    with _lock:
        return _generation


def events_since(last_generation: int) -> tuple[int, tuple[ImageEvent, ...]]:
    """Return ``(current_generation, events_after_last_generation)``.

    Unlike a drain queue this is multi-consumer safe.  Event history is bounded;
    a very stale consumer may receive only the newest ``_EVENT_HISTORY`` events,
    which is still enough to repaint/reconcile its current document.
    """
    with _lock:
        current = _generation
        if last_generation >= current:
            return current, ()
        return current, tuple(event for event in _events if event.generation > last_generation)


def _selected_animations(urls: Iterable[str] | None):
    with _lock:
        if urls is None:
            return tuple(_animations.values())
        wanted = set(urls)
        return tuple(animation for url, animation in _animations.items() if url in wanted)


def advance_animations(urls: Iterable[str] | None = None) -> bool:
    """Advance GIF clocks; optionally only those referenced by a given page."""
    now = time.monotonic()
    return any(animation.advance(now) for animation in _selected_animations(urls))


def has_active_animations(urls: Iterable[str] | None = None) -> bool:
    return any(animation.active for animation in _selected_animations(urls))


def has_pending(urls: Iterable[str] | None = None) -> bool:
    with _lock:
        if urls is None:
            return bool(_pending)
        wanted = set(urls)
        return any(url in wanted for url in _pending)


def cache_info() -> dict[str, int | float]:
    """Cheap diagnostics for the browser HUD and tests."""
    with _lock:
        return {
            "generation": _generation,
            "entries": len(_cache),
            "cache_bytes": _cache_bytes,
            "cache_budget_bytes": _CACHE_BUDGET_BYTES,
            "pending": len(_pending),
            "negative_cache": len(_failures),
            "requests": int(_metrics["requests"]),
            "fetches": int(_metrics["fetches"]),
            "successes": int(_metrics["successes"]),
            "failures": int(_metrics["failures"]),
            "cache_hits": int(_metrics["cache_hits"]),
            "evictions": int(_metrics["evictions"]),
            "network_bytes": int(_metrics["network_bytes"]),
            "avg_fetch_ms": (_metrics["fetch_ms"] / _metrics["fetch_completed"]) if _metrics["fetch_completed"] else 0.0,
            "avg_decode_ms": (_metrics["decode_ms"] / _metrics["decode_completed"]) if _metrics["decode_completed"] else 0.0,
            "fetch_workers": _FETCH_WORKERS,
            "decode_workers": _DECODE_WORKERS,
        }


def clear_cache(*, cancel_pending: bool = True, reset_metrics: bool = False) -> None:
    """Forget cached resources without allowing stale workers to repopulate them.

    Running urllib calls cannot be forcibly interrupted safely, so the epoch is
    advanced.  Any old worker result is simply discarded when it finishes.
    Queued futures are cancelled where possible.
    """
    global _epoch, _cache_bytes
    with _lock:
        _epoch += 1
        _cache.clear()
        _animations.clear()
        _failures.clear()
        _pending.clear()
        _events.clear()
        _cache_bytes = 0
        futures = tuple(_futures) if cancel_pending else ()
        if reset_metrics:
            for key in _metrics:
                _metrics[key] = 0.0 if key.endswith("_ms") else 0
    for future in futures:
        future.cancel()


def shutdown(*, wait: bool = False) -> None:
    """Stop worker pools. Primarily useful for tests/embedded Chromonic hosts."""
    global _fetch_executor, _decode_executor, _epoch, _shutdown_requested
    with _lock:
        _shutdown_requested = True
        _epoch += 1
        _pending.clear()
        fetch, decode = _fetch_executor, _decode_executor
        _fetch_executor = _decode_executor = None
    if fetch is not None:
        fetch.shutdown(wait=wait, cancel_futures=True)
    if decode is not None:
        decode.shutdown(wait=wait, cancel_futures=True)
