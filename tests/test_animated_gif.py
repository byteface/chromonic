import base64

from chromonic.animated_gif import decode_animated_gif


# 2x2 GIF: red frame for 50ms, green frame for 80ms, loops forever.
_TWO_FRAME_GIF = base64.b64decode(
    "R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQIBQAAACwAAAAAAgACAAAIBgABCAQQEAAh+QQICAAAACwAAAAAAgACAIEA/wAAAAAAAAAAAAAIBgABCAQQEAA7"
)


def test_decode_animated_gif():
    gif = decode_animated_gif(_TWO_FRAME_GIF)

    assert gif is not None
    assert len(gif.frames) == 2
    assert gif.frames[0].width() == 2
    assert gif.frames[0].height() == 2

    start = gif.started
    assert gif.index_at(start + 0.01) == 0
    assert gif.index_at(start + 0.06) == 1
    assert gif.index_at(start + 0.14) == 0

    # visit: https://tenor.com/view/test-gif-27402391 to see a GIF in action