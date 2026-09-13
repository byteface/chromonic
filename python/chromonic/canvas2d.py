"""Replay immutable Domonic Canvas 2D command dictionaries onto Skia."""
from __future__ import annotations

import math
import re
import skia

from . import fonts

_FONT_RE = re.compile(r"(?:(italic)\s+)?(?:(bold|[1-9]00)\s+)?([\d.]+)px\s+(.+)", re.I)


def _color(value, alpha=1.0):
    if not isinstance(value, str):
        kind = value.get("type") if isinstance(value, dict) else type(value).__name__
        raise NotImplementedError(f"Canvas {kind} paint styles are not supported yet")
    named = {
        "black": "#000000", "white": "#ffffff", "red": "#ff0000",
        "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00",
        "gray": "#808080", "grey": "#808080", "transparent": "transparent",
    }
    value = named.get(value.strip().lower(), value)
    from .paint import _color as parse_color
    parsed = parse_color(value)
    if parsed is None:
        return skia.Color4f(0, 0, 0, 0)
    return skia.Color4f(parsed.fR, parsed.fG, parsed.fB, parsed.fA * alpha)


def _paint(state, stroke=False):
    paint = skia.Paint(Color4f=_color(
        state["strokeStyle" if stroke else "fillStyle"], state.get("globalAlpha", 1.0)),
        AntiAlias=True, Style=skia.Paint.kStroke_Style if stroke else skia.Paint.kFill_Style)
    if stroke:
        paint.setStrokeWidth(float(state.get("lineWidth", 1.0)))
        paint.setStrokeCap({"round": skia.Paint.kRound_Cap, "square": skia.Paint.kSquare_Cap}.get(
            state.get("lineCap"), skia.Paint.kButt_Cap))
        paint.setStrokeJoin({"round": skia.Paint.kRound_Join, "bevel": skia.Paint.kBevel_Join}.get(
            state.get("lineJoin"), skia.Paint.kMiter_Join))
        if state.get("lineDash"):
            paint.setPathEffect(skia.DashPathEffect.Make(state["lineDash"], 0))
    return paint


def _path(snapshot):
    path = skia.Path()
    if not isinstance(snapshot, dict) or snapshot.get("type") != "Path2D":
        return path
    for command in snapshot.get("commands", []):
        name, args = command.get("name"), command.get("args", [])
        if name == "moveTo": path.moveTo(*args)
        elif name == "lineTo": path.lineTo(*args)
        elif name == "rect": path.addRect(skia.Rect.MakeXYWH(*args))
        elif name == "arc":
            x, y, radius, start, end, counterclockwise = args
            sweep = math.degrees(end - start)
            if counterclockwise and sweep > 0: sweep -= 360
            if not counterclockwise and sweep < 0: sweep += 360
            path.addArc(skia.Rect.MakeLTRB(x-radius, y-radius, x+radius, y+radius), math.degrees(start), sweep)
        elif name == "closePath": path.close()
    return path


def _font(state):
    match = _FONT_RE.search(state.get("font", "10px sans-serif"))
    if not match:
        return skia.Font(fonts.resolve_typeface("sans-serif"), 10)
    italic, weight, size, family = match.groups()
    bold = weight == "bold" or bool(weight and weight.isdigit() and int(weight) >= 600)
    return skia.Font(fonts.resolve_typeface(family, bold=bold, italic=bool(italic)), float(size))


def replay(commands, width: int, height: int) -> skia.Image:
    surface = skia.Surface(max(1, width), max(1, height))
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    for command in commands:
        name, args, state = command.get("name"), command.get("args", []), command.get("state")
        if state is None:
            continue
        if name in ("clip", "drawImage", "putImageData"):
            raise NotImplementedError(f"Canvas command {name} is not supported yet")
        canvas.save()
        a, b, c, d, e, f = state.get("transform", (1, 0, 0, 1, 0, 0))
        canvas.concat(skia.Matrix.MakeAll(a, c, e, b, d, f, 0, 0, 1))
        if name == "clearRect":
            canvas.save(); canvas.clipRect(skia.Rect.MakeXYWH(*args)); canvas.clear(skia.ColorTRANSPARENT); canvas.restore()
        elif name == "fillRect": canvas.drawRect(skia.Rect.MakeXYWH(*args), _paint(state))
        elif name == "strokeRect": canvas.drawRect(skia.Rect.MakeXYWH(*args), _paint(state, True))
        elif name in ("fill", "stroke"): canvas.drawPath(_path(args[0]), _paint(state, name == "stroke"))
        elif name in ("fillText", "strokeText"):
            text, x, y = str(args[0]), float(args[1]), float(args[2])
            font = _font(state)
            text_width = font.measureText(text)
            if state.get("textAlign") == "center": x -= text_width / 2
            elif state.get("textAlign") in ("right", "end"): x -= text_width
            canvas.drawString(text, x, y, font, _paint(state, name == "strokeText"))
        canvas.restore()
    return surface.makeImageSnapshot()


def paint_element(canvas, element, box):
    context = getattr(element, "_context", None)
    if context is None:
        return
    image = replay(list(context.commands), int(context.width), int(context.height))
    canvas.drawImageRect(image, skia.Rect.MakeXYWH(box.x, box.y, box.width, box.height))
