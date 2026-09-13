import skia
import pytest

from domonic.html import canvas, div
from domonic.webapi.canvas import CanvasRenderingContext2D

from chromonic import canvas2d, domonic_canvas_patch, tree


@pytest.fixture(autouse=True)
def _isolated_patch():
    domonic_canvas_patch.uninstall()
    yield
    domonic_canvas_patch.uninstall()


def _context():
    return canvas(_width="80", _height="50").getContext("2d")


def test_patch_is_explicit_idempotent_and_reversible():
    assert CanvasRenderingContext2D._record is domonic_canvas_patch._ORIGINAL_RECORD
    assert domonic_canvas_patch.install()
    assert not domonic_canvas_patch.install()
    assert CanvasRenderingContext2D._record is domonic_canvas_patch._chromonic_record


def test_recorded_fill_styles_preserve_historical_state():
    domonic_canvas_patch.install()
    ctx = _context()
    ctx.fillStyle = "red"
    ctx.fillRect(0, 0, 10, 10)
    ctx.fillStyle = "blue"
    ctx.fillRect(20, 0, 10, 10)
    assert [command["state"]["fillStyle"] for command in ctx.commands] == ["red", "blue"]


def test_recorded_path_is_not_mutated_by_later_path_commands():
    domonic_canvas_patch.install()
    ctx = _context()
    ctx.beginPath()
    ctx.moveTo(0, 0)
    ctx.lineTo(10, 10)
    ctx.strokeStyle = "green"
    ctx.stroke()
    stroke = ctx.commands[-1]

    ctx.beginPath()
    ctx.moveTo(20, 20)
    ctx.lineTo(30, 30)

    assert stroke["args"][0] == {"type": "Path2D", "commands": [
        {"name": "moveTo", "args": [0, 0]},
        {"name": "lineTo", "args": [10, 10]},
    ]}
    assert stroke["state"]["strokeStyle"] == "green"


def test_skia_replay_uses_each_commands_recorded_state():
    domonic_canvas_patch.install()
    ctx = _context()
    ctx.fillStyle = "red"; ctx.fillRect(0, 0, 10, 10)
    ctx.fillStyle = "blue"; ctx.fillRect(20, 0, 10, 10)
    pixels = canvas2d.replay(ctx.commands, 80, 50).toarray()
    assert tuple(pixels[5, 5, :3]) != tuple(pixels[5, 25, :3])


def test_canvas_is_intrinsically_sized_and_painted_as_a_dom_element():
    domonic_canvas_patch.install()
    element = canvas(_width="80", _height="50")
    ctx = element.getContext("2d")
    ctx.fillStyle = "#ff0000"
    ctx.fillRect(0, 0, 80, 50)
    root = div(element, _style="display:block;width:100px")
    tree.layout(root, width=100)
    assert (element.get_layout_box().width, element.get_layout_box().height) == (80, 50)
    surface = skia.Surface(100, 60)
    surface.getCanvas().clear(skia.ColorWHITE)
    from chromonic import paint
    paint.paint_tree(surface.getCanvas(), root)
    pixel = surface.makeImageSnapshot().toarray()[25, 40]
    assert tuple(pixel[:3]) != (255, 255, 255)
