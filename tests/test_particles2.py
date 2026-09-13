from pathlib import Path
import sys

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
sys.path.insert(0, str(EXAMPLES))
from particles2 import MAX_PARTICLES, ParticleView


def test_native_particles_move_using_domonic_layout():
    view = ParticleView(4)
    old = [(p.x, p.y) for p in view.interaction.particles]
    view.tick()
    assert [(p.x, p.y) for p in view.interaction.particles] != old
    for p in view.interaction.particles:
        assert abs(p.element.get_layout_box().x - p.x) <= .55  # Taffy rounds geometry to pixels


def test_slider_coalesces_and_pause_preserves_positions():
    view = ParticleView(2)
    view.paused = True
    old = [(p.x, p.y) for p in view.interaction.particles]
    view.tick()
    assert [(p.x, p.y) for p in view.interaction.particles] == old
    view.set_count(10)
    view.set_count(20)
    assert len(view.interaction.particles) == 2
    view.tick()
    assert len(view.interaction.particles) == 20
    view.slider(-50)
    view.tick()
    assert len(view.interaction.particles) == 0


def test_slider_and_initial_count_allow_ten_thousand_particles():
    view = ParticleView(0)
    view.slider(view.width - 210)
    assert view.pending_count == MAX_PARTICLES
    view.set_count(MAX_PARTICLES + 1)
    assert view.pending_count == MAX_PARTICLES


def test_resize_and_direct_paint(monkeypatch):
    import skia
    from chromonic import paint
    view = ParticleView(3)
    view.resize(500, 400)
    view.tick()
    assert view.interaction.root.get_layout_box().width == 500
    assert view.interaction.root.get_layout_box().height == 356
    def forbidden(*a, **kw):
        raise AssertionError('PNG is not part of direct rendering')
    monkeypatch.setattr(paint, 'render_png', forbidden)
    view.draw(skia.Surface(500, 400).getCanvas())
