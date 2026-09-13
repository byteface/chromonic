"""Compare identical seeded domonic/Taffy scenes: PNG production vs direct GPU.

Needs a display; uses a hidden window. Excludes real webview IPC/decode and
vsync. GPU timings synchronize with glFinish; these are work costs, not FPS.
"""
import argparse
import base64
import json
from pathlib import Path
import random
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from particles import ParticleInteraction
from particles2 import ParticleView
from chromonic.native_browser import GLRenderer
import glfw
from OpenGL import GL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[50, 200, 500, 1000])
    parser.add_argument('--repeat', type=int, default=15)
    parser.add_argument('--output', type=Path, default=Path('/tmp/particles-comparison.json'))
    args = parser.parse_args()
    if args.repeat < 1 or any(n < 0 or n > 10000 for n in args.counts):
        parser.error('positive repeat and counts in 0..10000 required')
    assert glfw.init(), 'A display is required'
    win = renderer = None
    results = []
    try:
        glfw.window_hint(glfw.VISIBLE, False)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        win = glfw.create_window(800, 644, 'particles benchmark', None, None)
        assert win
        glfw.make_context_current(win)
        glfw.swap_interval(0)
        renderer = GLRenderer()
        # Equal physical pixel size: no Retina advantage/disadvantage in this
        # transport comparison. GPU includes its small toolbar as extra work.
        for count in args.counts:
            random.seed(42)
            old = ParticleInteraction(width=800, height=600, count=count)
            random.seed(42)
            new = ParticleView(count)
            timings = {'png': [], 'gpu': []}
            for i in range(args.repeat + 3):
                for mode in (('png', 'gpu') if i % 2 else ('gpu', 'png')):
                    start = time.perf_counter()
                    if mode == 'png':
                        old.tick()
                        base64.b64encode(old.render(relayout=False)).decode('ascii')
                    else:
                        new.tick()
                        renderer.draw(new, (800, 644))
                        GL.glFinish()
                    if i >= 3:
                        timings[mode].append((time.perf_counter() - start) * 1000)
            assert [(p.x,p.y) for p in old.particles] == [(p.x,p.y) for p in new.interaction.particles]
            row = {'count': count, 'median_ms': {k: statistics.median(v) for k,v in timings.items()}, 'samples_ms': timings}
            results.append(row)
            print(json.dumps({'count':count, 'median_ms':row['median_ms']}), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'results':results, 'limits':__doc__},indent=2)+'\n')
    finally:
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()


if __name__ == '__main__':
    main()
