from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil

from .chrome_runner import run as run_chrome
from .compare import compare_files, print_report
from .native_runner import run as run_native
from .schema import write_json

HERE = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = HERE / "fixtures"
DEFAULT_PAGES = HERE / "pages"
DEFAULT_OUTPUT = HERE / "artifacts"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare chromonic layout with headless Chrome")
    parser.add_argument("fixtures", type=Path, nargs="*", help="specific fixture files")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tolerance", type=float, default=0.5)
    parser.add_argument("--visual-tolerance", type=float, default=0.08,
                        help="maximum changed-pixel fraction for whole-page fixtures")
    parser.add_argument("--chrome")
    parser.add_argument("--keep", action="store_true", help="do not clear old artifacts")
    args = parser.parse_args(argv)
    fixtures = args.fixtures or (sorted(DEFAULT_FIXTURES.glob("*.html")) + sorted(DEFAULT_PAGES.glob("*.html")))
    if not fixtures:
        parser.error("no fixtures found")
    if not math.isfinite(args.tolerance) or args.tolerance < 0:
        parser.error("tolerance must be finite and non-negative")
    if not math.isfinite(args.visual_tolerance) or not 0 <= args.visual_tolerance <= 1:
        parser.error("visual tolerance must be between zero and one")
    if any(not fixture.is_file() for fixture in fixtures):
        parser.error("every fixture must be an existing file")
    if len({fixture.stem for fixture in fixtures}) != len(fixtures):
        parser.error("fixture names must be unique to avoid overwriting artifacts")
    if any(args.output.resolve() in fixture.resolve().parents for fixture in fixtures):
        parser.error("output must not contain fixture sources")
    if args.output.exists() and not args.keep:
        shutil.rmtree(args.output)
    failed = 0
    comparisons = []
    for fixture in fixtures:
        destination = args.output / fixture.stem
        run_chrome(fixture, destination, destination / "chrome.png", chrome=args.chrome)
        run_native(fixture, destination, destination / "ours.png")
        is_page = fixture.parent.resolve() == DEFAULT_PAGES.resolve()
        comparison = compare_files(destination, tolerance=args.tolerance,
                                   visual_tolerance=args.visual_tolerance if is_page else None)
        print_report(comparison)
        comparisons.append(comparison)
        failed += not comparison["passed"]
    # Include earlier artifacts when --keep is used to resume a long run.
    all_comparisons = [
        json.loads(path.read_text())
        for path in sorted(args.output.glob("*/comparison.json"))
    ]
    ranked_geometry = []
    for comparison in all_comparisons:
        for mismatch in comparison["geometry_mismatches"] + comparison.get("fragment_mismatches", []):
            ranked_geometry.append({"fixture": comparison["fixture"], **mismatch})
    ranked_geometry.sort(key=lambda item: abs(item.get("delta") or 0), reverse=True)
    summary = {
        "total": len(all_comparisons),
        "passed": sum(item["passed"] for item in all_comparisons),
        "failed": sum(not item["passed"] for item in all_comparisons),
        "largest_geometry_mismatches": ranked_geometry[:50],
        "visual": sorted(({
            "fixture": item["fixture"], **item["visual"],
            "passed": item["visual_passed"],
        } for item in all_comparisons), key=lambda item: item["changed_fraction"], reverse=True),
    }
    write_json(args.output / "summary.json", summary)
    print(f"\n{len(fixtures) - failed} passed, {failed} failed; artifacts: {args.output}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
