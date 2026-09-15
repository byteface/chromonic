"""Animated GIF decoding/playback using skia.Codec.

No extra dependency: skia-python already exposes GIF frame decoding,
durations, disposal/dependency handling, and repetition counts.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import time

import skia


_MIN_FRAME_SECONDS = 0.02  # cap pathological/zero-delay GIFs at 50 fps


@dataclass
class AnimatedGIF:
    frames: tuple["skia.Image", ...]
    durations: tuple[float, ...]
    repetition_count: int
    started: float = field(default_factory=time.monotonic)
    current_index: int = 0

    def __post_init__(self) -> None:
        self._ends = []
        total = 0.0
        for duration in self.durations:
            total += duration
            self._ends.append(total)
        self.duration = total

    @property
    def active(self) -> bool:
        if self.repetition_count < 0:
            return True
        playthroughs = self.repetition_count + 1
        return (time.monotonic() - self.started) < self.duration * playthroughs

    def index_at(self, now: float | None = None) -> int:
        if len(self.frames) <= 1 or self.duration <= 0:
            return 0

        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self.started)

        if self.repetition_count >= 0:
            playthroughs = self.repetition_count + 1
            if elapsed >= self.duration * playthroughs:
                return len(self.frames) - 1

        position = elapsed % self.duration
        return min(bisect_right(self._ends, position), len(self.frames) - 1)

    def frame(self, now: float | None = None) -> "skia.Image":
        self.current_index = self.index_at(now)
        return self.frames[self.current_index]

    def advance(self, now: float | None = None) -> bool:
        index = self.index_at(now)
        if index == self.current_index:
            return False
        self.current_index = index
        return True


def _looks_like_gif(data: bytes) -> bool:
    return data.startswith((b"GIF87a", b"GIF89a"))


def decode_animated_gif(data: bytes) -> AnimatedGIF | None:
    """Decode a multi-frame GIF into immutable Skia images.

    Returns None for non-GIF or single-frame GIF data.
    """
    if not _looks_like_gif(data):
        return None

    codec = skia.Codec.MakeFromData(data)
    if codec is None:
        return None

    frame_count = codec.getFrameCount()
    if frame_count <= 1:
        return None

    infos = codec.getFrameInfo()
    dimensions = codec.dimensions()
    width = int(dimensions.width())
    height = int(dimensions.height())

    frames = []
    durations = []

    for index in range(frame_count):
        bitmap = skia.Bitmap()
        bitmap.allocN32Pixels(width, height, False)
        bitmap.eraseARGB(0, 0, 0, 0)

        options = skia.Codec.Options()
        options.fFrameIndex = index

        # Tell Skia there is no useful previous frame already in our fresh
        # destination. It will decode any dependency frames required to
        # produce the complete visual frame.
        options.fPriorFrame = skia.Codec.kNoFrame
        options.fZeroInitialized = skia.Codec.kYes_ZeroInitialized

        result = codec.getPixels(bitmap.pixmap(), options)
        if result != skia.Codec.kSuccess:
            return None

        bitmap.setImmutable()
        image = skia.Image.MakeFromBitmap(bitmap)
        if image is None:
            return None

        frames.append(image)

        milliseconds = infos[index].fDuration if index < len(infos) else 100
        durations.append(max(_MIN_FRAME_SECONDS, float(milliseconds) / 1000.0))

    return AnimatedGIF(
        frames=tuple(frames),
        durations=tuple(durations),
        repetition_count=int(codec.getRepetitionCount()),
    )
