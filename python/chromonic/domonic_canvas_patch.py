"""Preserve replayable Path2D arguments in Domonic canvas commands.

Domonic 1.8.4 snapshots drawing state, but its generic JSON conversion
stringifies ``Path2D`` arguments. Chromonic needs their recorded commands for
Skia replay, so this adapter retains Domonic's command/state shape while
serializing replayable canvas values.
"""
from __future__ import annotations

from copy import deepcopy
from domonic.webapi import canvas as _canvas

CanvasRenderingContext2D = _canvas.CanvasRenderingContext2D
CanvasGradient = _canvas.CanvasGradient
CanvasPattern = _canvas.CanvasPattern
Path2D = _canvas.Path2D

_ORIGINAL_RECORD = CanvasRenderingContext2D._record
_INSTALLED = False


def _snapshot_value(value):
    if isinstance(value, Path2D):
        return {"type": "Path2D", "commands": deepcopy([{
            "name": command["name"],
            "args": [_snapshot_value(arg) for arg in command.get("args", [])],
        } for command in value.commands])}
    if isinstance(value, CanvasGradient):
        return {"type": "CanvasGradient", "kind": value.kind,
                "args": [_canvas._json_safe(arg) for arg in value.args],
                "colorStops": [[float(offset), str(color)] for offset, color in value.colorStops]}
    if isinstance(value, CanvasPattern):
        return {"type": "CanvasPattern", "repetition": value.repetition,
                "image": _canvas._json_safe(value.image)}
    return deepcopy(_canvas._json_safe(value))


def _chromonic_record(self, name, *args):
    self.commands.append({
        "name": name,
        "args": [_snapshot_value(arg) for arg in args],
        "state": {key: _snapshot_value(value)
                  for key, value in self._capture_style_state().items()},
    })


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return False
    CanvasRenderingContext2D._record = _chromonic_record
    _INSTALLED = True
    return True


def uninstall() -> bool:
    global _INSTALLED
    if not _INSTALLED:
        return False
    CanvasRenderingContext2D._record = _ORIGINAL_RECORD
    _INSTALLED = False
    return True


def is_installed() -> bool:
    return _INSTALLED
