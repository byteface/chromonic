"""Reversible state snapshots for Domonic's recorded Canvas commands."""
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


def _snapshot_state(context):
    return {
        "fillStyle": _snapshot_value(context.fillStyle),
        "strokeStyle": _snapshot_value(context.strokeStyle),
        "globalAlpha": float(context.globalAlpha),
        "lineWidth": float(context.lineWidth),
        "lineCap": str(context.lineCap),
        "lineJoin": str(context.lineJoin),
        "font": str(context.font),
        "textAlign": str(context.textAlign),
        "textBaseline": str(context.textBaseline),
        "lineDash": deepcopy(list(context._line_dash)),
        "transform": tuple(context._transform),
    }


def _chromonic_record(self, name, *args):
    self.commands.append({"name": name,
                          "args": [_snapshot_value(arg) for arg in args],
                          "state": _snapshot_state(self)})


def install() -> bool:
    """Install once and return whether this call changed Domonic."""
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
