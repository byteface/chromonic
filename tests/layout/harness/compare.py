from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import skia

from .schema import RECT_FIELDS, STYLE_PROPERTIES, write_json


def compare_results(chrome: dict, ours: dict, *, tolerance: float = 0.5) -> dict:
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    if chrome.get("viewport") != ours.get("viewport"):
        raise ValueError("cannot compare captures with different viewports")
    mismatches = []
    chrome_elements = chrome.get("elements", {})
    our_elements = ours.get("elements", {})
    for element_id in sorted(set(chrome_elements) | set(our_elements)):
        if element_id not in chrome_elements or element_id not in our_elements:
            mismatches.append({
                "element": element_id, "field": "element",
                "chrome": element_id in chrome_elements,
                "ours": element_id in our_elements,
                "delta": None, "kind": "geometry",
            })
            continue
        reference = chrome_elements[element_id]
        actual = our_elements[element_id]
        for field in RECT_FIELDS:
            expected = float(reference["rect"][field])
            got = float(actual["rect"][field])
            delta = got - expected
            if not math.isfinite(expected) or not math.isfinite(got) or abs(delta) > tolerance:
                mismatches.append({
                    "element": element_id, "field": field,
                    "chrome": expected, "ours": got, "delta": delta,
                    "kind": "geometry",
                })
        for field in STYLE_PROPERTIES:
            expected = reference["style"].get(field, "")
            got = actual["style"].get(field, "")
            if expected != got:
                mismatches.append({
                    "element": element_id, "field": field,
                    "chrome": expected, "ours": got, "delta": None,
                    "kind": "style",
                })
        for fragment_type in ("element", "text"):
            expected_fragments = reference.get("fragments", {}).get(fragment_type, [])
            actual_fragments = actual.get("fragments", {}).get(fragment_type, [])
            if len(expected_fragments) != len(actual_fragments):
                mismatches.append({
                    "element": element_id, "field": f"{fragment_type}.count",
                    "chrome": len(expected_fragments), "ours": len(actual_fragments),
                    "delta": len(actual_fragments) - len(expected_fragments), "kind": "fragment",
                })
            for index, (expected_fragment, actual_fragment) in enumerate(zip(expected_fragments, actual_fragments)):
                for field in RECT_FIELDS:
                    expected = float(expected_fragment[field])
                    got = float(actual_fragment[field])
                    delta = got - expected
                    if not math.isfinite(expected) or not math.isfinite(got) or abs(delta) > tolerance:
                        mismatches.append({
                            "element": element_id, "field": f"{fragment_type}[{index}].{field}",
                            "chrome": expected, "ours": got, "delta": delta, "kind": "fragment",
                        })
    geometry = [item for item in mismatches if item["kind"] == "geometry"]
    styles = [item for item in mismatches if item["kind"] == "style"]
    fragments = [item for item in mismatches if item["kind"] == "fragment"]
    geometry.sort(key=lambda item: abs(item["delta"] or 0), reverse=True)
    fragments.sort(key=lambda item: abs(item["delta"] or 0), reverse=True)
    return {
        "fixture": chrome.get("fixture"), "tolerance": tolerance,
        "passed": not geometry and not fragments, "geometry_mismatches": geometry,
        "style_mismatches": styles,
        "fragment_mismatches": fragments,
    }


def print_report(comparison: dict) -> None:
    state = "PASS" if comparison["passed"] else "FAIL"
    print(f'{state} {comparison["fixture"]}: '
          f'{len(comparison["geometry_mismatches"])} geometry, '
          f'{len(comparison.get("fragment_mismatches", []))} fragments, '
          f'{len(comparison["style_mismatches"])} style mismatches')
    for item in (comparison["geometry_mismatches"] + comparison.get("fragment_mismatches", []))[:20]:
        print(f'  #{item["element"]}.{item["field"]}')
        print(f'    Chrome: {item["chrome"]}')
        print(f'    Ours:   {item["ours"]}')
        if item["delta"] is not None:
            print(f'    Delta:  {item["delta"]:+.4f}')
    visual = comparison.get("visual")
    if visual:
        marker = "PASS" if comparison.get("visual_passed", True) else "FAIL"
        print(f'  visual {marker}: {visual["changed_fraction"]:.1%} changed pixels, '
              f'mean delta {visual["mean_delta"]:.2f}')


def write_image_diff(chrome_path: Path, ours_path: Path, output_path: Path, overlay_path: Path) -> dict:
    def decode(path):
        # Goes through `skia.Image.MakeFromEncoded` + `.toarray()`, not the
        # lower-level `skia.Codec.MakeFromData()` + `codec.getPixels(...)`
        # this used to use: that pairing was genuinely flaky in skia-python
        # (reproduced directly -- `getPixels` returned `kErrorInInput` on the
        # *same* input bytes 4 times out of 5, `kSuccess` on the 5th, no
        # retry or backoff involved), which is what made a full suite run
        # crash non-deterministically with "could not read screenshot
        # pixels" on whichever fixture's decode happened to lose the coin
        # flip. `Image.MakeFromEncoded`/`toarray()` decoded the same file
        # correctly across 10/10 attempts and is already the path this
        # codebase trusts elsewhere (`browser_images.py`, `chrome_runner.py`
        # above). `MakeWithCopy`, not `MakeWithoutCopy`, for the `Data`: the
        # bytes from `read_bytes()` are a throwaway temporary with nothing
        # keeping them alive once this call returns, so `MakeWithoutCopy`
        # would risk Skia holding a pointer into memory Python has already
        # freed.
        image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(path.read_bytes()))
        if image is None:
            raise ValueError(f"could not decode screenshot {path}")
        return image.toarray(colorType=skia.ColorType.kRGBA_8888_ColorType)

    left = decode(chrome_path)
    right = decode(ours_path)
    if left.shape != right.shape:
        raise ValueError(f"screenshot dimensions differ: {left.shape} != {right.shape}")
    height, width = min(left.shape[0], right.shape[0]), min(left.shape[1], right.shape[1])
    difference = np.abs(left[:height, :width].astype(np.int16) - right[:height, :width].astype(np.int16))
    difference = np.clip(difference * 4, 0, 255).astype(np.uint8)
    # Equal opaque source alphas subtract to zero; the diagnostic itself must
    # remain opaque or all RGB differences disappear in an image viewer.
    difference[:, :, 3] = 255
    image = skia.Image.fromarray(difference)
    output_path.write_bytes(bytes(image.encodeToData()))
    overlay = ((left[:height, :width].astype(np.float32) + right[:height, :width].astype(np.float32)) / 2).astype(np.uint8)
    overlay_path.write_bytes(bytes(skia.Image.fromarray(overlay).encodeToData()))
    # Ignore tiny antialiasing changes. This deliberately detects broad visual
    # disagreement rather than pretending screenshots from two font stacks are pixel-identical.
    rgb_delta = np.max(np.abs(left[:height, :width, :3].astype(np.int16) - right[:height, :width, :3].astype(np.int16)), axis=2)
    return {"changed_fraction": float(np.mean(rgb_delta > 48)), "mean_delta": float(np.mean(rgb_delta))}


def compare_files(output: Path, *, tolerance=0.5, visual_tolerance=None) -> dict:
    chrome = json.loads((output / "chrome.json").read_text())
    ours = json.loads((output / "ours.json").read_text())
    comparison = compare_results(chrome, ours, tolerance=tolerance)
    visual = write_image_diff(output / "chrome.png", output / "ours.png", output / "diff.png", output / "overlay.png")
    comparison["visual"] = visual
    comparison["visual_tolerance"] = visual_tolerance
    comparison["visual_passed"] = visual_tolerance is None or visual["changed_fraction"] <= visual_tolerance
    comparison["passed"] = comparison["passed"] and comparison["visual_passed"]
    write_json(output / "comparison.json", comparison)
    return comparison
