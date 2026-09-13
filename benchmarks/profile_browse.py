"""Reproducible headless browse profile; GUI delivery is deliberately excluded.

Run: .venv/bin/python chromonic/benchmarks/profile_browse.py --nodes 200 --rules 100
Use --url https://example.com/ for a live page (fetch measured separately).
"""
from __future__ import annotations

import argparse
import base64
import cProfile
import io
import json
import importlib
import importlib.metadata
import platform
from pathlib import Path
import pstats
import statistics
import skia
import domonic
import time
from unittest.mock import patch

from myjs import Page
from chromonic import tree, window, paint


def fixture(nodes, rules):
    css = '\n'.join(f'.item-{i} {{color: rgb({i % 255},30,40); padding: 2px;}}' for i in range(rules))
    children = ''.join(f'<p class="item-{i % max(rules, 1)}">Item {i}: browser rendering benchmark</p>' for i in range(nodes))
    return f'<html><head><style>body {{display:block}} p {{display:block; height:20px}} {css}</style></head><body>{children}</body></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url')
    parser.add_argument('--html', type=Path)
    parser.add_argument('--nodes', type=int, default=200)
    parser.add_argument('--rules', type=int, default=100)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path, default=Path('/tmp/chromonic-profile'))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    from contextlib import ExitStack
    page_module = importlib.import_module('myjs.html')
    load_stages = {}
    def track(label, function):
        def timed(*a, **kw):
            started = time.perf_counter()
            try:
                return function(*a, **kw)
            finally:
                load_stages.setdefault(label, []).append((time.perf_counter() - started) * 1000)
        return timed
    with ExitStack() as stack:
        for name in ('_parse', 'Session', '_batch_fetch_text', '_fetch_text'):
            stack.enter_context(patch.object(page_module, name, track(name, getattr(page_module, name))))
        started = time.perf_counter()
        page = Page.load(args.url or args.html, run=False) if args.url or args.html else Page(fixture(args.nodes, args.rules), run=False)
        load_ms = (time.perf_counter() - started) * 1000
    interaction = window.Interaction(page.document.body, width=1000, height=800)
    interaction.render()  # warm fonts and imports outside timing
    timings = {}
    def measure(name, fn):
        values = []
        for _ in range(args.repeat):
            start = time.perf_counter()
            result = fn()
            values.append((time.perf_counter() - start) * 1000)
        timings[name] = {'median_ms': statistics.median(values), 'samples_ms': values}
        return result
    measure('layout', lambda: tree.layout(interaction.root, width=1000, height=800))
    def raster():
        surface = skia.Surface(1000, 800)
        surface.getCanvas().clear(skia.ColorWHITE)
        paint.paint_tree(surface.getCanvas(), interaction.root)
        return surface.makeImageSnapshot()
    image = measure('raster_and_snapshot', raster)
    measure('png_encode', lambda: bytes(image.encodeToData()))
    png = measure('paint_and_png', lambda: paint.render_png(interaction.root, width=1000, height=800))
    measure('base64', lambda: base64.b64encode(png).decode('ascii'))
    measure('render', interaction.render)
    measure('tick_then_render', lambda: (interaction.tick(), interaction.render()))
    class Sink:
        def evaluate_js(self, script):
            self.script_bytes = len(script.encode('utf-8'))
    sink = Sink()
    api = window._Api(interaction)
    api.attach(sink)
    measure('bridge_tick', api.tick)
    measure('duplicate_layout_bridge_tick', lambda: (interaction.tick(), api.push_frame()))
    profiler = cProfile.Profile()
    with patch.object(tree, 'layout', wraps=tree.layout) as layouts:
        profiler.runcall(api.tick)
        calls = layouts.call_count
    args.output.parent.mkdir(parents=True, exist_ok=True)
    profiler.dump_stats(str(args.output) + '.prof')
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(35)
    Path(str(args.output) + '.txt').write_text(stream.getvalue())
    report = {'source': args.url or (str(args.html) if args.html else None) or {'nodes': args.nodes, 'rules': args.rules}, 'load_ms': load_ms,
              'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                              'domonic_distribution': importlib.metadata.version('domonic'),
                              'domonic_runtime_version': domonic.__version__,
                              'skia-python': importlib.metadata.version('skia-python')},
              'load_stages_ms': load_stages, 'resource_errors': [str(e) for e in page.errors],
              'png_bytes': len(png), 'layouts_per_bridge_tick': calls, 'bridge_script_bytes': sink.script_bytes, 'timings': timings,
              'limits': 'Load stages can overlap (stylesheet fetches); do not sum them. Headless; excludes pywebview IPC, PNG decode, display composition, and input latency.'}
    Path(str(args.output) + '.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(stream.getvalue())


if __name__ == '__main__':
    main()
