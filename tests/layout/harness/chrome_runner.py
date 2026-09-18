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
from urllib.parse import quote

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
  // WPT's own convention -- a meta tag named "flags" whose content
  // includes the word "ahem" (possibly combined with other space-
  // separated flags) -- marks a fixture as relying on the Ahem test font
  // for exact, predictable glyph metrics. If it somehow did not load (a
  // network hiccup, the WPT static server not running, a relative font
  // source that did not resolve), silently capturing this baseline
  // against whatever fallback system font Chrome used instead would look
  // like a real geometry mismatch against chromonic later, for a reason
  // that has nothing to do with either engine's layout. Fail loudly here
  // instead: the result output element never gets appended below, so
  // Python's own regex match fails and run() raises the same way any
  // other Chrome capture failure already does.
  //
  // No angle brackets or ampersands appear anywhere in this comment block
  // on purpose: an xht fixture is parsed as strict XML, where either one,
  // unescaped, inside script text is invalid markup or a malformed entity
  // reference and silently mangles or aborts the parse of everything that
  // follows in the document, this whole geometry capture included.
  const flagsMeta = document.querySelector('meta[name="flags"]');
  const flags = (flagsMeta?.getAttribute('content') || '').trim().split(/\\s+/);
  if (flags.includes('ahem')) {{
    if (!document.fonts.check('40px Ahem')) {{
      throw new Error('Ahem font failed to load for ' + {json.dumps(fixture_name)} +
        ' -- refusing to record fallback-font geometry as a baseline');
    }}
  }}
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
    # `source_override`, when given, is already-processed markup (e.g. with
    # extra instrumentation of its own) to probe instead of `fixture`'s own
    # content verbatim.
    source = source_override if source_override is not None else fixture.read_text()
    # A `file://` document can never load a `http(s)://` subresource at all
    # -- not merely a `<base>`-resolution nuance, a hard same-scheme security
    # restriction Chrome enforces regardless of what URL a resource resolves
    # to. Confirmed directly: an absolute-root `/fonts/ahem.css` (real WPT
    # markup) 404s nothing and just silently never loads at all when probed
    # from a `file://` page, `<base>` pointed at the real HTTP origin or not
    # -- `document.fonts` reports the `@font-face` as `status: "error""`,
    # and a bare `fetch()` to the same URL rejects with "Failed to fetch".
    # `base_url` (a real `http(s)://` URL the caller has already made this
    # exact content reachable at, via the WPT static server -- see `run_wpt.
    # run_folder`) is therefore not just a `<base>` value: this whole probe
    # must *navigate* there directly, an ordinary same-origin-as-its-own-
    # resources page, exactly the way a real browser loads the real site.
    # `file://` (`base_url is None`, chromonic's own local fixtures under
    # `tests/layout/fixtures/`, none of which reach outside their own
    # directory) is unaffected -- unchanged below.
    if base_url is not None:
        chrome_temp_path = fixture.with_name(f"_chromonic_diff_chrome_{os.getpid()}_{fixture.name}")
        chrome_url = base_url.rsplit("/", 1)[0] + "/" + quote(chrome_temp_path.name, safe="")
        instrumented_source = _instrument(source, fixture.name)
        chrome_temp_path.write_text(instrumented_source, encoding="utf-8")
        try:
            window_height = viewport[1] + _WINDOW_HEIGHT_ADJUSTMENT
            for _attempt in range(2):
                dumped = subprocess.run(
                    [*command(window_height), "--dump-dom", chrome_url],
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
            # Chrome's screenshot CLI treats window size as the content
            # viewport in headless mode when device scale is forced to one.
            # Reuses the same served (script-instrumented, but that script
            # only appends an invisible `<pre>`, so it never affects paint)
            # URL rather than `fixture.resolve().as_uri()`, for the same
            # same-origin-subresource reason as the dump-dom probe above.
            subprocess.run(
                [*command(window_height), f"--screenshot={screenshot.resolve()}", chrome_url],
                check=True, capture_output=True, text=True, timeout=30,
            )
        finally:
            try:
                chrome_temp_path.unlink()
            except FileNotFoundError:
                pass
    else:
        with tempfile.TemporaryDirectory(prefix="chromonic-layout-") as temporary:
            instrumented = Path(temporary) / fixture.name
            # The probe lives in a temporary directory; retain resource
            # resolution against the fixture, including an authored
            # relative <base href>.
            base = f'<base href="{html.escape(fixture.resolve().as_uri(), quote=True)}" />'
            existing_base = re.search(r'<base\b[^>]*href=["\'](.*?)["\'][^>]*>', source, re.I)
            if existing_base:
                import urllib.parse
                base = f'<base href="{html.escape(urllib.parse.urljoin(fixture.resolve().as_uri(), existing_base.group(1)), quote=True)}" />'
            insertion = (re.search(r"<head\b[^>]*>", source, re.I)
                         or re.search(r"<html\b[^>]*>", source, re.I)
                         or re.search(r"<!doctype\b[^>]*>", source, re.I))
            index = insertion.end() if insertion else 0
            based_source = source[:index] + base + source[index:]
            instrumented.write_text(_instrument(based_source, fixture.name))
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
