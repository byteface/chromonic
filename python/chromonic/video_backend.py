"""`<video>` playback: decode real video files/streams and composite the
current frame into the render tree, the same way `browser_images.py`
already does for `<img>` -- `paint.py`'s `_paint_video` is the `<img>`
`_paint_image` of this module, and to layout a `<video>` is exactly as
intrinsically sized as an `<img>`, just with a *changing* bitmap instead
of a static one.

**Why ffmpeg, not GStreamer.** The natural Rust-side design here is
`gstreamer-rs` decoding into an `AppSink`, pulled straight into a Skia
texture -- and that's the right *eventual* architecture. But GStreamer
itself isn't installed on this machine (no `gstreamer-1.0` pkg-config,
no `gst-launch-1.0`), and standing up a new system dependency isn't
something to do silently mid-session. `ffmpeg`/`ffprobe` (Homebrew,
already on `PATH`) decode exactly as well for a first version: spawn
`ffmpeg ... -f rawvideo -pix_fmt rgba pipe:1`, read fixed-size RGBA
frames off its stdout, hand each one to `skia.Image.frombytes`. Same
"decode -> raw frame -> Skia composite" shape the Rust/GStreamer design
would have; this module is the part to swap out if/when GStreamer lands.

**What's deliberately not here yet:** audio playback (ffmpeg's raw
video pipe carries no audio; real audio output needs a second sink this
module doesn't build), frame-accurate seeking (`seek()` restarts decode
from ffmpeg's nearest keyframe via `-ss`, not exact), and GPU/zero-copy
frame delivery (every frame is a CPU-side `bytes` copy). All three are
real follow-ups, not correctness bugs in what's here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import weakref

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

#: DOM element -> VideoDecoder. Weak so a decoder (and its subprocess/
#: thread) is torn down once nothing else references the element anymore,
#: rather than living for the rest of the process.
_decoders: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _probe(source: str) -> "tuple[int, int, float, float] | None":
    """`(width, height, duration_s, fps)`, or None if ffprobe is missing,
    the source can't be read, or the metadata doesn't parse."""
    if not _FFPROBE:
        return None
    try:
        result = subprocess.run(
            [
                _FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json", source,
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        num, _, den = stream.get("r_frame_rate", "25/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 25.0
        duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
        if width <= 0 or height <= 0 or fps <= 0:
            return None
        return width, height, duration, fps
    except Exception:
        return None


class VideoDecoder:
    """One ffmpeg decode pipeline for one `<video>` element's current
    source. `play()`/`pause()`/`seek()` are called from the UI thread;
    the reader loop runs on its own daemon thread so a slow/laggy decode
    never blocks layout, paint, or input handling."""

    def __init__(self, source: str):
        self.source = source
        self.width = 0
        self.height = 0
        self.duration = 0.0
        self.fps = 25.0
        probed = _probe(source)
        if probed is not None:
            self.width, self.height, self.duration, self.fps = probed
        self._process: "subprocess.Popen | None" = None
        self._thread: "threading.Thread | None" = None
        self._lock = threading.Lock()
        self._latest_frame: "bytes | None" = None
        self._frame_version = 0
        self._frames_read = 0
        self._seek_offset = 0.0
        self._playing = False
        self._cached_image = None
        self._cached_image_version = -1

    @property
    def ready(self) -> bool:
        return self.width > 0 and self.height > 0 and _FFMPEG is not None

    @property
    def paused(self) -> bool:
        return not self._playing

    @property
    def current_time(self) -> float:
        if self._playing:
            return self._seek_offset + self._frames_read / self.fps
        return self._seek_offset

    def play(self) -> None:
        if self._playing or not self.ready:
            return
        self._start_process(self._seek_offset)
        self._playing = True

    def pause(self) -> None:
        if not self._playing:
            return
        self._seek_offset = self.current_time
        self._stop_process()
        self._playing = False

    def seek(self, seconds: float) -> None:
        was_playing = self._playing
        self._stop_process()
        self._seek_offset = max(0.0, min(seconds, self.duration or seconds))
        self._frames_read = 0
        with self._lock:
            self._latest_frame = None
        if was_playing:
            self._start_process(self._seek_offset)
            self._playing = True

    def __del__(self) -> None:
        # `_decoders` (video_backend.py module level) is a WeakKeyDictionary
        # keyed by DOM element -- once nothing else references the element
        # (removed from the DOM, or its whole page navigated away from),
        # this decoder is garbage-collected too. Without this, its ffmpeg
        # subprocess (if still playing) would keep running and pacing out
        # frames nobody reads, orphaned for the rest of the process's life.
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def _start_process(self, start_seconds: float) -> None:
        command = [
            # `-re` (native-frame-rate pacing) is an *input* option -- it
            # must precede `-i`, or ffmpeg rejects it outright as invalid
            # for the output side instead of silently ignoring it.
            _FFMPEG, "-loglevel", "error", "-re", "-ss", str(start_seconds),
            "-i", self.source, "-an", "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{self.width}x{self.height}", "pipe:1",
        ]
        self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._frames_read = 0
        self._thread = threading.Thread(target=self._read_loop, args=(self._process,), daemon=True)
        self._thread.start()

    def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.wait(timeout=2)
        self._thread = None

    def _read_loop(self, process: "subprocess.Popen") -> None:
        frame_size = self.width * self.height * 4
        stdout = process.stdout
        try:
            while process is self._process:
                chunk = stdout.read(frame_size)
                if len(chunk) < frame_size:
                    return  # EOF (video ended) or process was killed mid-read
                with self._lock:
                    self._latest_frame = chunk
                    self._frame_version += 1
                self._frames_read += 1
        except Exception:
            return

    def current_frame_image(self):
        """The latest decoded frame as a `skia.Image`, or None before the
        first frame has arrived / if decoding never started. Rebuilds the
        cached `skia.Image` only when a new frame has actually landed."""
        with self._lock:
            frame, version = self._latest_frame, self._frame_version
        if frame is None:
            return None
        if version != self._cached_image_version:
            import skia
            self._cached_image = skia.Image.frombytes(
                frame, skia.ISize(self.width, self.height), skia.ColorType.kRGBA_8888_ColorType,
            )
            self._cached_image_version = version
        return self._cached_image


def _effective_source(element) -> "str | None":
    src = element.getAttribute("src")
    if src:
        return src
    for child in element.getElementsByTagName("source"):
        child_src = child.getAttribute("src")
        if child_src:
            return child_src
    return None


def decoder_for(element) -> "VideoDecoder | None":
    """The `VideoDecoder` backing this `<video>` element, creating one
    (and re-creating it if `src` changed since) on first use. Cached on
    the element via a weak map -- `paint.py`/the DOM properties below both
    call this and must always see the same instance."""
    source = _effective_source(element)
    if not source:
        return None
    existing = _decoders.get(element)
    if existing is not None and existing.source == source:
        return existing
    decoder = VideoDecoder(source)
    _decoders[element] = decoder
    return decoder


def resolve_video_sources(document, base_url: str) -> None:
    """Resolve `<video src>` and `<video><source src>` URLs to absolute,
    the same way `browser_images.resolve_image_sources` does for `<img>`
    right after parse -- so every later reader (intrinsic sizing, paint,
    `decoder_for`) can trust `getAttribute('src')` is already absolute."""
    elements = list(document.getElementsByTagName("video")) + list(document.getElementsByTagName("source"))
    for el in elements:
        src = el.getAttribute("src")
        if src and not src.startswith("data:") and "://" not in src:
            el.setAttribute("src", urllib.parse.urljoin(base_url, src))


def _install_video_element_api() -> None:
    """Attach `currentTime`/`duration`/`paused`/`videoWidth`/`videoHeight`/
    `play()`/`pause()` onto the real `video` tag class -- domonic's own
    `HTMLVideoElement` is attribute plumbing only (no playback state at
    all, and doesn't even inherit `HTMLMediaElement`), so this isn't
    recovering something that already existed -- it's chromonic's own new
    API surface, attached onto the live class via `sys.modules`, since the
    `domonic.html` *attribute* is shadowed by the `<html>` tag class
    itself."""
    import domonic.html  # noqa: F401 -- ensures the real submodule is registered in `sys.modules`
    video_cls = sys.modules["domonic.html"].video

    def current_time_getter(self):
        decoder = decoder_for(self)
        return decoder.current_time if decoder is not None else 0.0

    def current_time_setter(self, value):
        decoder = decoder_for(self)
        if decoder is not None:
            decoder.seek(float(value))

    def duration_getter(self):
        decoder = decoder_for(self)
        return decoder.duration if decoder is not None else 0.0

    def paused_getter(self):
        decoder = decoder_for(self)
        return decoder.paused if decoder is not None else True

    def video_width_getter(self):
        decoder = decoder_for(self)
        return decoder.width if decoder is not None else 0

    def video_height_getter(self):
        decoder = decoder_for(self)
        return decoder.height if decoder is not None else 0

    def play(self):
        from domonic.events import Event
        decoder = decoder_for(self)
        if decoder is not None:
            decoder.play()
        self.dispatchEvent(Event("play", {"bubbles": False, "cancelable": False}))
        self.dispatchEvent(Event("playing", {"bubbles": False, "cancelable": False}))
        return None

    def pause(self):
        from domonic.events import Event
        decoder = decoder_for(self)
        if decoder is not None:
            decoder.pause()
        self.dispatchEvent(Event("pause", {"bubbles": False, "cancelable": False}))
        return None

    video_cls.currentTime = property(current_time_getter, current_time_setter)
    video_cls.duration = property(duration_getter)
    video_cls.paused = property(paused_getter)
    video_cls.videoWidth = property(video_width_getter)
    video_cls.videoHeight = property(video_height_getter)
    video_cls.play = play
    video_cls.pause = pause


_install_video_element_api()
