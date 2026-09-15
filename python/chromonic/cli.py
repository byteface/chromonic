"""Command-line entry point for Chromonic."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import functools
import http.server
import subprocess
import sys
import threading
import urllib.parse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


_DEFAULT_URL = "https://google.com/"
_LOCAL_SUFFIXES = {".html", ".htm", ".xhtml", ".xht"}


def _version() -> str:
    try:
        return version("chromonic")
    except PackageNotFoundError:
        return "dev"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().replace("×", "x").split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT, e.g. 1280x720") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("width and height must be greater than zero")
    return width, height


def _normalize_cli_target(value: str) -> str:
    """Convenient browser-ish shorthand before native_browser sees the target."""
    value = (value or "").strip()

    if value.startswith(":") and value[1:].isdigit():
        return "http://127.0.0.1" + value

    lowered = value.lower()
    if lowered.startswith(("localhost:", "127.0.0.1:", "0.0.0.0:", "[::1]:")):
        return "http://" + value

    return value


def _local_path(value: str) -> Path | None:
    """Return a local path when *value* clearly names one."""
    parsed = urllib.parse.urlparse(value)

    # Explicit web URLs are never local paths.
    if parsed.scheme.lower() in ("http", "https"):
        return None

    if parsed.scheme.lower() == "file":
        return Path(urllib.parse.unquote(parsed.path)).expanduser()

    path = Path(value).expanduser()
    if path.exists():
        return path

    # Avoid silently turning a mistyped local page into https://foo.html.
    if (
        path.suffix.lower() in _LOCAL_SUFFIXES
        or value.startswith((".", "/", "~"))
        or "/" in value
        or "\\" in value
    ):
        return path

    return None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args) -> None:
        pass


class _LocalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def _target_url(value: str):
    """Yield a browser URL, serving local files from a temporary HTTP server."""
    value = _normalize_cli_target(value)
    path = _local_path(value)

    if path is None:
        yield value
        return

    path = path.resolve()
    if path.is_dir():
        path = path / "index.html"

    if not path.is_file():
        raise FileNotFoundError(f"local page not found: {path}")

    handler = functools.partial(_QuietHandler, directory=str(path.parent))
    server = _LocalServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="chromonic-local-server",
        daemon=True,
    )
    thread.start()

    try:
        port = server.server_address[1]
        name = urllib.parse.quote(path.name)
        yield f"http://127.0.0.1:{port}/{name}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _repo_root() -> Path | None:
    """Find a Chromonic source checkout from cwd or this editable install."""
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    seen: set[Path] = set()

    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "python" / "chromonic").is_dir()
            ):
                return candidate
    return None


def _need_repo(command: str) -> Path:
    root = _repo_root()
    if root is None:
        raise RuntimeError(
            f"`chromonic {command}` is a development command and needs "
            "to be run from a Chromonic source checkout."
        )
    return root


def _example_names(root: Path) -> list[str]:
    examples = root / "examples"
    if not examples.is_dir():
        return []
    return sorted(
        path.stem
        for path in examples.glob("*.py")
        if not path.name.startswith("_")
    )


def _list_examples() -> int:
    root = _need_repo("examples")
    names = _example_names(root)
    if not names:
        print("No Python examples found.")
        return 0
    for name in names:
        print(name)
    return 0


def _run_example(argv: list[str]) -> int:
    root = _need_repo("example")
    names = _example_names(root)

    if not argv:
        print("Usage: chromonic example NAME [args...]\n")
        for name in names:
            print(name)
        return 2

    requested = argv[0][:-3] if argv[0].endswith(".py") else argv[0]
    script = root / "examples" / f"{requested}.py"

    if not script.is_file():
        close = difflib.get_close_matches(requested, names, n=3, cutoff=0.45)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise RuntimeError(f"unknown example {requested!r}.{hint}")

    return subprocess.call(
        [sys.executable, str(script), *argv[1:]],
        cwd=root,
    )


def _resolve_wpt_target(root: Path, value: str) -> Path:
    direct = Path(value).expanduser()
    candidates = [
        direct if direct.is_absolute() else root / direct,
        root / "tests" / "wpt" / value,
        root / "tests" / "wpt" / "css" / "CSS2" / value,
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    # Handy for `chromonic wpt ltr-basic.xht`.
    matches = list((root / "tests" / "wpt").rglob(value))
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        choices = ", ".join(str(p.relative_to(root)) for p in matches[:5])
        raise RuntimeError(f"ambiguous WPT target {value!r}: {choices}")

    raise RuntimeError(f"WPT target not found: {value}")


def _run_wpt(argv: list[str]) -> int:
    root = _need_repo("wpt")
    if not argv:
        print("Usage: chromonic wpt PATH\nExample: chromonic wpt box")
        return 2

    target = _resolve_wpt_target(root, argv[0])
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "tests.layout.harness.run_wpt",
            str(target),
            *argv[1:],
        ],
        cwd=root,
    )


def _run_tests(argv: list[str]) -> int:
    root = _need_repo("test")
    return subprocess.call(
        [sys.executable, "-m", "pytest", *argv],
        cwd=root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chromonic",
        description="Open a URL or local HTML file in Chromonic.",
        epilog=(
            "Developer conveniences:\n"
            "  chromonic examples\n"
            "  chromonic example NAME\n"
            "  chromonic wpt box\n"
            "  chromonic test [-k EXPR]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=_DEFAULT_URL,
        help="URL, hostname, HTML file, or directory containing index.html",
    )
    parser.add_argument(
        "--width",
        type=_positive_int,
        default=1000,
        help="initial window width (default: 1000)",
    )
    parser.add_argument(
        "--height",
        type=_positive_int,
        default=800,
        help="initial window height (default: 800)",
    )
    parser.add_argument(
        "--size",
        type=_size,
        metavar="WIDTHxHEIGHT",
        help="set both dimensions, e.g. --size 390x844",
    )
    parser.add_argument(
        "--title",
        default="chromonic — direct Skia",
        help="window title",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="render two frames and exit",
    )
    parser.add_argument(
        "--frames",
        type=_positive_int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version()}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        if argv:
            command, *rest = argv
            if command == "examples":
                return _list_examples()
            if command == "example":
                return _run_example(rest)
            if command == "wpt":
                return _run_wpt(rest)
            if command in ("test", "tests"):
                return _run_tests(rest)

        parser = build_parser()
        args = parser.parse_args(argv)

        width, height = args.size or (args.width, args.height)
        frames = args.frames if args.frames is not None else (2 if args.smoke else None)

        with _target_url(args.target) as url:
            # Keep heavyweight GL/Skia imports out of help/version/dev dispatch.
            from .native_browser import run

            run(
                url,
                width=width,
                height=height,
                title=args.title,
                frames=frames,
            )
    except FileNotFoundError as exc:
        build_parser().error(str(exc))
    except RuntimeError as exc:
        print(f"chromonic: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    return 0
