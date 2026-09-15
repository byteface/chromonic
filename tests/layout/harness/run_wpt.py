from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

try:
    from . import chrome_runner, native_runner, compare
    from .schema import write_json
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness import chrome_runner, native_runner, compare
    from harness.schema import write_json


DEFAULT_BASE_URL = "http://127.0.0.1:8943"
DEFAULT_OUTPUT = Path("tests/layout/artifacts/wpt")
SUFFIXES = {".html", ".htm", ".xht", ".xhtml"}

SKIP_TAGS = {
    "html", "head", "title", "base", "link",
    "meta", "style", "script", "noscript",
}


class LayoutTagger(HTMLParser):
    """Add id + data-layout to elements in a throwaway WPT copy."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.in_body = not bool(re.search(r"<body\b", source, re.I))
        self.counter = 0
        self.used_ids = set()
        self.marked = 0

    def new_id(self):
        while True:
            self.counter += 1
            value = f"__chromonic_wpt_{self.counter:05d}"
            if value not in self.used_ids:
                return value

    def inject(self, raw, attrs):
        if not attrs:
            return raw

        stripped = raw.rstrip()
        closing = "/>" if stripped.endswith("/>") else ">"
        pos = raw.rfind(closing)

        extra = "".join(
            f' {name}="{escape(value, quote=True)}"'
            for name, value in attrs
        )

        return raw[:pos] + extra + raw[pos:]

    def process_start(self, tag, attrs, raw):
        tag_lower = tag.lower()

        if tag_lower == "body":
            self.in_body = True

        if not self.in_body or tag_lower in SKIP_TAGS:
            return raw

        attr_map = {
            name.lower(): value
            for name, value in attrs
            if name
        }

        additions = []

        element_id = attr_map.get("id")

        if element_id:
            # Don't create ambiguous duplicate ids.
            if element_id in self.used_ids:
                return raw
        else:
            element_id = self.new_id()
            additions.append(("id", element_id))

        self.used_ids.add(element_id)

        if "data-layout" not in attr_map:
            additions.append(("data-layout", ""))

        self.marked += 1

        return self.inject(raw, additions)

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        self.parts.append(self.process_start(tag, attrs, raw))

    def handle_startendtag(self, tag, attrs):
        raw = self.get_starttag_text()
        self.parts.append(self.process_start(tag, attrs, raw))

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

        if tag.lower() == "body":
            self.in_body = False

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")


def auto_tag(source):
    # Domonic currently does not unwrap XHTML CDATA inside <style>.
    # Strip it only in this throwaway WPT copy so we're testing CSS/layout,
    # not a known upstream parser limitation.
    source = re.sub(
        r'(<style\b[^>]*>)\s*<!\[CDATA\[(.*?)\]\]>\s*(</style>)',
        lambda m: m.group(1) + m.group(2) + m.group(3),
        source,
        flags=re.I | re.S,
    )

    tagger = LayoutTagger(source)
    tagger.feed(source)
    tagger.close()

    return "".join(tagger.parts), tagger.marked


def find_wpt_root(path: Path):
    current = path.resolve()

    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if candidate.name == "wpt" and (candidate / "css").exists():
            return candidate

    raise RuntimeError(
        "Couldn't locate WPT root. Expected something like tests/wpt/css/..."
    )


def should_skip(path):
    stem = path.stem.lower()

    if "manual" in stem:
        return True

    if re.search(r"(?:^|[-_.])(ref|reference)(?:[-_.]|$)", stem):
        return True

    parts = {p.lower() for p in path.parts}

    if parts & {"reference", "references", "support", "resources"}:
        return True

    return False


def run_folder(folder, output, base_url, tolerance, chrome=None, limit=None):
    folder = folder.resolve()
    wpt_root = find_wpt_root(folder)

    files = sorted(
        path
        for path in folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUFFIXES
        and not should_skip(path)
    )

    if limit:
        files = files[:limit]

    print(f"WPT root : {wpt_root}")
    print(f"Folder   : {folder}")
    print(f"Tests    : {len(files)}")
    print(f"Output   : {output}")
    print()

    results = []

    for i, fixture in enumerate(files, 1):
        relative = fixture.relative_to(wpt_root)
        relative_folder = fixture.relative_to(folder)

        # Preserve directory structure, but give each file its own artifact dir.
        artifact_dir = output / relative_folder
        artifact_dir.mkdir(parents=True, exist_ok=True)

        source = fixture.read_text(
            encoding="utf-8",
            errors="replace",
        )

        tagged, marked = auto_tag(source)

        temp_name = f"_chromonic_diff_{os.getpid()}_{fixture.name}"
        temp_path = fixture.with_name(temp_name)

        temp_relative = temp_path.relative_to(wpt_root)

        url = (
            base_url.rstrip("/")
            + "/"
            + quote(temp_relative.as_posix(), safe="/")
        )

        print(
            f"[{i:03}/{len(files):03}] "
            f"{relative.as_posix()} "
            f"({marked} elements)"
        )

        try:
            if not marked:
                raise RuntimeError("No elements were auto-tagged")

            # Native browser needs to fetch the throwaway copy over HTTP.
            temp_path.write_text(tagged, encoding="utf-8")

            chrome_result = chrome_runner.run(
                fixture,
                artifact_dir,
                artifact_dir / "chrome.png",
                chrome=chrome,
                base_url=url,
                source_override=tagged,
            )

            native_result = native_runner.run(
                fixture,
                artifact_dir,
                artifact_dir / "ours.png",
                load_url=url,
            )

            comparison = compare.compare_results(
                chrome_result,
                native_result,
                tolerance=tolerance,
            )

            write_json(
                artifact_dir / "comparison.json",
                comparison,
            )

            geometry = comparison["geometry_mismatches"]
            fragments = comparison.get("fragment_mismatches", [])
            styles = comparison["style_mismatches"]

            deltas = [
                abs(float(item["delta"]))
                for item in geometry + fragments
                if item.get("delta") is not None
            ]

            result = {
                "file": relative_folder.as_posix(),
                "status": "PASS" if comparison["passed"] else "FAIL",
                "marked_elements": marked,
                "geometry_mismatches": len(geometry),
                "fragment_mismatches": len(fragments),
                "style_mismatches": len(styles),
                "max_delta": max(deltas) if deltas else None,
            }

            results.append(result)

            print(
                f"    {result['status']} — "
                f"{len(geometry)} geometry, "
                f"{len(fragments)} fragment, "
                f"{len(styles)} style"
            )

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            print(f"    ERROR: {type(exc).__name__}: {exc}")

            results.append({
                "file": relative_folder.as_posix(),
                "status": "ERROR",
                "marked_elements": marked,
                "error": f"{type(exc).__name__}: {exc}",
            })

        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

        # Write continuously so a long run isn't lost.
        write_summary(
            output,
            folder,
            results,
        )

    return write_summary(
        output,
        folder,
        results,
    )


def write_summary(output, folder, results):
    passed = sum(x["status"] == "PASS" for x in results)
    failed = sum(x["status"] == "FAIL" for x in results)
    errors = sum(x["status"] == "ERROR" for x in results)

    worst = sorted(
        (
            {
                "file": x["file"],
                "max_delta": x["max_delta"],
                "geometry_mismatches": x["geometry_mismatches"],
                "fragment_mismatches": x["fragment_mismatches"],
            }
            for x in results
            if x.get("max_delta") is not None
        ),
        key=lambda x: x["max_delta"],
        reverse=True,
    )

    summary = {
        "folder": str(folder),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "largest_mismatches": worst[:50],
        "results": results,
    }

    write_json(output / "summary.json", summary)

    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "folder",
        type=Path,
        help="WPT folder, e.g. tests/wpt/css/CSS2/box",
    )

    parser.add_argument(
        "--output",
        type=Path,
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
    )

    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
    )

    parser.add_argument(
        "--chrome",
    )

    parser.add_argument(
        "--limit",
        type=int,
        help="Only run the first N tests",
    )

    args = parser.parse_args(argv)

    folder = args.folder

    if not folder.is_dir():
        parser.error(f"Not a directory: {folder}")

    wpt_root = find_wpt_root(folder)
    relative = folder.resolve().relative_to(wpt_root)

    output = (
        args.output
        if args.output
        else DEFAULT_OUTPUT / relative
    )

    output.mkdir(parents=True, exist_ok=True)

    summary = run_folder(
        folder,
        output,
        args.base_url,
        args.tolerance,
        chrome=args.chrome,
        limit=args.limit,
    )

    print()
    print("=" * 60)
    print(
        f"{summary['passed']} passed, "
        f"{summary['failed']} failed, "
        f"{summary['errors']} errors"
    )
    print(f"Summary: {output / 'summary.json'}")

    return 1 if summary["failed"] or summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())