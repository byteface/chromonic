"""Direct GPU Skia browser: no webview, PNG encoding, or image frame transport.

Install: make develop
Run: .venv/bin/python chromonic/examples/browse2.py [https://example.com/]
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', default='https://example.com/')
    parser.add_argument('--frames', type=int, help='close after this many frames (display smoke test)')
    args = parser.parse_args()
    if args.frames is not None and args.frames < 1:
        parser.error('--frames must be positive')
    try:
        from chromonic.native_browser import run
    except ModuleNotFoundError as error:
        if error.name == 'chromonic._native':
            parser.exit(1, "Missing compiled chromonic extension. Rebuild from the repository root:\n"
                        "  .venv/bin/maturin develop --release --offline\n")
        raise
    view = run(args.url, frames=args.frames)
    if args.frames is not None and view.page is None:
        parser.exit(1, f'Page did not load: {view.status}\n')


if __name__ == '__main__':
    main()
