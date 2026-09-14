from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import skia

from .schema import RECT_FIELDS, STYLE_PROPERTIES, VIEWPORT, result, write_json

_RESULT_RE = re.compile(r'<pre id="__chromonic_layout_result"[^>]*>(.*?)</pre>', re.S)
_WINDOW_HEIGHT_ADJUSTMENT = 0


def find_chrome(explicit: str | None = None) -> str:
    candidates = [
        explicit,
        os.environ.get("CHROME"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise FileNotFoundError("Chrome/Chromium not found; set CHROME=/path/to/browser")


def _instrument(source: str, fixture_name: str) -> str:
    properties = json.dumps(STYLE_PROPERTIES)
    rect_fields = json.dumps(RECT_FIELDS)
    script = f"""<script>
(async () => {{
  if (document.readyState !== 'complete') {{
    await new Promise(resolve => window.addEventListener('load', resolve, {{once: true}}));
  }}
  await document.fonts.ready;
  const properties = {properties};
  const rectFields = {rect_fields};
  const elements = {{}};
  const marked = [...document.querySelectorAll('[data-layout], [data-layout-root] [id]')];
  for (const element of marked) {{
    if (!(element instanceof Element)) continue;
    if (!element.id) throw new Error('data-layout elements require an id');
    const rect = element.getBoundingClientRect();
    const computed = getComputedStyle(element);
    const elementFragments = [...element.getClientRects()].map(r =>
      Object.fromEntries(rectFields.map(field => [field, r[field]])));
    const textFragments = [];
    for (const node of element.childNodes) {{
      if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {{
        textFragments.push({{...Object.fromEntries(rectFields.map(field => [field, r[field]])),
          text: node.textContent}});
      }}
    }}
    elements[element.id] = {{rect: {{}}, style: {{}},
      fragments: {{element: elementFragments, text: textFragments}}}};
    for (const field of rectFields) elements[element.id].rect[field] = rect[field];
    for (const property of properties) elements[element.id].style[property] = computed.getPropertyValue(property);
  }}
  const payload = {{schema: 2, fixture: {json.dumps(fixture_name)}, engine: 'chrome',
    viewport: {{width: innerWidth, height: innerHeight}}, elements}};
  const output = document.createElement('pre');
  output.id = '__chromonic_layout_result';
  output.style.display = 'none';
  output.textContent = JSON.stringify(payload);
  document.documentElement.appendChild(output);
}})();
</script>"""
    index = source.lower().rfind("</body>")
    return source[:index] + script + source[index:] if index >= 0 else source + script


def run(fixture: Path, output: Path, screenshot: Path, *, chrome=None, viewport=VIEWPORT,
        base_url: "str | None" = None, source_override: "str | None" = None) -> dict:
    global _WINDOW_HEIGHT_ADJUSTMENT
    executable = find_chrome(chrome)
    output.mkdir(parents=True, exist_ok=True)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    def command(window_height):
        return [
        executable, "--headless=new", "--hide-scrollbars", "--disable-extensions",
        "--disable-background-networking", "--force-device-scale-factor=1",
        f"--window-size={viewport[0]},{window_height}", "--virtual-time-budget=1000",
        ]
    with tempfile.TemporaryDirectory(prefix="chromonic-layout-") as temporary:
        instrumented = Path(temporary) / fixture.name
        # `source_override`, when given, is already-processed markup (e.g.
        # with extra instrumentation of its own) to probe instead of
        # `fixture`'s own content verbatim.
        source = source_override if source_override is not None else fixture.read_text()
        # The probe lives in a temporary directory; retain resource resolution
        # against the fixture, including an authored relative <base href>.
        # `base_url`, when given, overrides this with an explicit URL instead
        # (an absolute-root resource reference like `/fonts/ahem.css`, common
        # in the real web-platform-tests suite, only resolves correctly
        # against a real HTTP(S) origin -- `file://` has no such root).
        base = f'<base href="{html.escape(base_url if base_url is not None else fixture.resolve().as_uri(), quote=True)}" />'
        existing_base = re.search(r'<base\b[^>]*href=["\'](.*?)["\'][^>]*>', source, re.I)
        if existing_base and base_url is None:
            import urllib.parse
            base = f'<base href="{html.escape(urllib.parse.urljoin(fixture.resolve().as_uri(), existing_base.group(1)), quote=True)}" />'
        insertion = (re.search(r"<head\b[^>]*>", source, re.I)
                     or re.search(r"<html\b[^>]*>", source, re.I)
                     or re.search(r"<!doctype\b[^>]*>", source, re.I))
        index = insertion.end() if insertion else 0
        source = source[:index] + base + source[index:]
        instrumented.write_text(_instrument(source, fixture.name))
        window_height = viewport[1] + _WINDOW_HEIGHT_ADJUSTMENT
        for _attempt in range(2):
            dumped = subprocess.run(
                [*command(window_height), "--dump-dom", instrumented.resolve().as_uri()],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout
            match = _RESULT_RE.search(dumped)
            if not match:
                raise RuntimeError(f"Chrome did not emit layout JSON for {fixture.name}")
            captured = json.loads(html.unescape(match.group(1)))
            delta = viewport[1] - captured["viewport"]["height"]
            if delta == 0 and captured["viewport"]["width"] == viewport[0]:
                _WINDOW_HEIGHT_ADJUSTMENT = window_height - viewport[1]
                break
            window_height += delta
        else:
            raise RuntimeError(f"Chrome viewport calibration failed for {fixture.name}: {captured['viewport']}")
    # Chrome's screenshot CLI treats window size as the content viewport in
    # headless mode when device scale is forced to one.
    subprocess.run(
        [*command(window_height), f"--screenshot={screenshot.resolve()}", fixture.resolve().as_uri()],
        check=True, capture_output=True, text=True, timeout=30,
    )
    image = skia.Image.MakeFromEncoded(screenshot.read_bytes())
    if image is None or image.width() < viewport[0] or image.height() < viewport[1]:
        raise RuntimeError(f"Chrome emitted an invalid screenshot for {fixture.name}")
    if (image.width(), image.height()) != viewport:
        cropped = image.makeSubset(skia.IRect.MakeWH(*viewport))
        screenshot.write_bytes(bytes(cropped.encodeToData()))
    write_json(output / "chrome.json", captured)
    return captured


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chrome")
    args = parser.parse_args(argv)
    run(args.fixture, args.output, args.output / "chrome.png", chrome=args.chrome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
