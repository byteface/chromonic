"""Direct GPU particles, keeping particles.py's domonic/Taffy simulation.

Run: .venv/bin/python chromonic/examples/particles2.py [count]
Drag the slider, use +/- to change count, Space to pause. --frames bounds a run.
"""
from __future__ import annotations

import argparse
import time

from particles import ParticleInteraction, WIDTH, HEIGHT
from chromonic import paint, tree
from chromonic.native_browser import GLRenderer
import skia

BAR = 44
MAX_PARTICLES = 10_000


class ParticleView:
    def __init__(self, count=200, width=WIDTH, height=HEIGHT + BAR):
        self.width, self.height = width, height
        self.interaction = ParticleInteraction(
            width=width, height=height - BAR,
            count=max(0, min(MAX_PARTICLES, count)),
        )
        self.paused = False
        self.fps = 0.0
        self.pending_count = None
        self.dragging = False
        self.dirty = True
        self.layout_projection = tree.LayoutProjection()
        self.layout_projection.layout(
            self.interaction.root, width=width, height=height - BAR,
        )
        self.display_list = paint.build_display_list(self.interaction.root)

    def set_count(self, count):
        # Coalesce mouse events: rebuild once per rendered frame.
        self.pending_count = max(0, min(MAX_PARTICLES, int(count)))
        self.dirty = True

    def resize(self, width, height):
        if width > 0 and height > BAR and (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.interaction.width, self.interaction.height = width, height - BAR
            self.set_count(len(self.interaction.particles))

    def tick(self):
        if self.pending_count is not None:
            self.interaction.set_count(self.pending_count)
            self.pending_count = None
            self.layout_projection.layout(
                self.interaction.root, width=self.width, height=self.height - BAR,
            )
            self.display_list = paint.build_display_list(self.interaction.root)
        elif not self.paused:
            inset_updates = []
            for particle in self.interaction.particles:
                particle.step(self.interaction.width, self.interaction.height)
                # Particle.step has already updated Domonic's authoritative
                # inline style. Only left/top changed, so patch the equivalent
                # translated Taffy inset without resolving CSS or reconciling
                # the unchanged tree.
                inset_updates.append((
                    particle.element, round(particle.y, 1), 0.0, 0.0,
                    round(particle.x, 1),
                ))
            self.layout_projection.patch_insets(inset_updates)
            self.layout_projection.compute(
                self.interaction.root,
                width=self.width, height=self.height - BAR,
            )
        self.dirty = True

    def slider(self, x):
        self.set_count(round((x - 150) / max(1, self.width - 360) * (MAX_PARTICLES // 10)) * 10)

    def draw(self, canvas):
        canvas.clear(skia.ColorBLACK)
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(0, BAR, self.width, self.height - BAR))
        canvas.translate(0, BAR)
        paint.paint_display_list(
            canvas, self.display_list, top=0.0, bottom=self.height - BAR,
        )
        canvas.restore()
        canvas.drawRect(skia.Rect.MakeWH(self.width, BAR), skia.Paint(Color=0xff1a202c))
        ink = skia.Paint(Color=0xffe2e8f0, AntiAlias=True)
        font = paint._font(14)
        count = len(self.interaction.particles)
        canvas.drawString(f'Particles: {count}', 10, 28, font, ink)
        canvas.drawLine(150, 22, self.width - 210, 22, ink)
        x = 150 + count / MAX_PARTICLES * max(1, self.width - 360)
        canvas.drawCircle(x, 22, 7, skia.Paint(Color=0xff63b3ed, AntiAlias=True))
        label = 'Paused' if self.paused else f'{self.fps:.1f} fps'
        canvas.drawString(label, self.width - 195, 28, font, ink)
        canvas.drawString('Space: pause', self.width - 105, 28, font, ink)
        self.dirty = False


def run(count=200, *, frames=None):
    import glfw
    if not glfw.init():
        raise RuntimeError('GLFW needs a working display')
    win = renderer = None
    try:
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        win = glfw.create_window(WIDTH, HEIGHT + BAR, 'chromonic — GPU particles', None, None)
        if not win:
            raise RuntimeError('Could not create GPU window')
        glfw.set_window_size_limits(win, 440, 160, glfw.DONT_CARE, glfw.DONT_CARE)
        glfw.make_context_current(win)
        glfw.swap_interval(1)
        renderer = GLRenderer()
        view = ParticleView(count, *glfw.get_window_size(win))
        def mouse(w, button, action, mods):
            if button == glfw.MOUSE_BUTTON_LEFT:
                x, y = glfw.get_cursor_pos(win)
                view.dragging = action == glfw.PRESS and y < BAR
                if view.dragging:
                    view.slider(x)
        def key(w, key, code, action, mods):
            if action not in (glfw.PRESS, glfw.REPEAT):
                return
            if key == glfw.KEY_SPACE and action == glfw.PRESS:
                view.paused = not view.paused
                view.dirty = True
            elif key in (glfw.KEY_EQUAL, glfw.KEY_MINUS):
                view.set_count((view.pending_count if view.pending_count is not None else len(view.interaction.particles)) + (50 if key == glfw.KEY_EQUAL else -50))
        glfw.set_key_callback(win, key)
        glfw.set_mouse_button_callback(win, mouse)
        glfw.set_cursor_pos_callback(win, lambda w, x, y: view.slider(x) if view.dragging else None)
        glfw.set_window_size_callback(win, lambda w, x, y: view.resize(x, y))
        glfw.set_framebuffer_size_callback(win, lambda *a: setattr(view, 'dirty', True))
        glfw.set_window_refresh_callback(win, lambda w: setattr(view, 'dirty', True))
        last = time.perf_counter()
        elapsed = 0.0
        samples = drawn = 0
        while not glfw.window_should_close(win):
            glfw.poll_events()
            if not view.paused or view.dirty or frames is not None:
                view.tick()
                renderer.draw(view, glfw.get_framebuffer_size(win))
                glfw.swap_buffers(win)
                now = time.perf_counter()
                if not view.paused:
                    elapsed += now - last
                    samples += 1
                    if elapsed >= .5:
                        view.fps = samples / elapsed
                        elapsed = 0.0
                        samples = 0
                else:
                    elapsed = 0.0
                    samples = 0
                last = now
                drawn += 1
                if frames is not None and drawn >= frames:
                    break
            else:
                glfw.wait_events_timeout(.1)
                last = time.perf_counter()
    finally:
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('count', type=int, nargs='?', default=200)
    parser.add_argument('--frames', type=int)
    args = parser.parse_args()
    if args.frames is not None and args.frames < 1:
        parser.error('--frames must be positive')
    run(args.count, frames=args.frames)
