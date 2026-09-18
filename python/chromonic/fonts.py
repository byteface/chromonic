"""
tree.py uses Parley/fontique for text shaping and layout.
This module independently resolves Skia typefaces for painting.
Downloaded @font-face resources are registered from identical bytes with
Parley/fontique and Skia. Document-private aliases resolve before installed
fonts, then generic/platform fallbacks. No fonts are installed into the OS.
"""

from __future__ import annotations

import skia
import math
import sys
from functools import lru_cache

# CSS generic family keywords -> one concrete name Skia's platform font
# manager can actually resolve. Skia's own fuzzy matching (see
# `resolve_typeface`) has no idea what "serif" or "monospace" mean as CSS
# keywords -- it treats an unrecognised name as a request to fall back to
# the platform default, same as passing `None`. `None` here means exactly
# that fallback is already the right answer (asking for "sans-serif" and
# getting Skia's own default -- already a sans-serif-ish system font on
# every platform this repo targets -- needs no translation).
_GENERIC_FAMILIES = {
    "serif": "Times New Roman",
    "sans-serif": None,
    "monospace": "Menlo" if sys.platform == "darwin" else "Courier New",
    "cursive": "Comic Sans MS",
    "fantasy": "Papyrus",
    "system-ui": None,
    "ui-serif": "Times New Roman",
    "ui-sans-serif": None,
    "ui-monospace": "Menlo" if sys.platform == "darwin" else "Courier New",
    # Browser-internal "use the OS UI font" keywords, not real family names
    # -- no font manager lists a family literally called "-apple-system", so
    # without this they'd always fail `_is_installed` and fall through
    # (usually harmlessly, to the *next* name in the stack, but wastefully:
    # a guaranteed-failing lookup for a name that could never succeed).
    # Mapped straight to `None` (the platform default) instead, same as the
    # generic `sans-serif` these keywords are always paired with in a real
    # font stack (this repo's own `ua_style.py` included).
    "-apple-system": None,
    "-webkit-system-font": None,
    "blinkmacsystemfont": None,
}

# (family_name_or_None, bold, italic) -> skia.Typeface. Resolving a typeface
# is real work (platform font-manager lookup); the same handful of fonts
# repaint every piece of text on a page, every frame, so this is worth
# caching exactly like `paint.py`'s own `_FONT_CACHE` already caches
# `skia.Font` objects built from these typefaces.
_typeface_cache: "dict[tuple, skia.Typeface]" = {}
_web_typefaces: "dict[str, skia.Typeface]" = {}

# Shared `FontMgr` for `_is_installed()` -- constructing one isn't free, and
# it's stateless (queries the platform's installed fonts), so one instance
# for the whole process is enough.
_font_mgr = skia.FontMgr()


def _is_installed(name: "str | None") -> bool:
    """Whether `name` actually matches an installed font family -- *not*
    what `skia.Typeface(name, style)` alone can tell you (see
    `resolve_typeface`'s docstring for why that constructor is the wrong
    tool for this check)."""
    if not name:
        return False
    return _font_mgr.matchFamily(name).count() > 0


def parse_family_list(value: "str | None") -> list:
    """A CSS `font-family` computed value (e.g. `Georgia, "Helvetica Neue",
    sans-serif`) -> an ordered list of plain family names, surrounding
    quotes stripped, generic keywords left as-is (mapped later, in
    `resolve_typeface`). Empty if the property was never set -- domonic's
    own "nothing declared, nothing inherited" value for `font-family` is
    the literal string `"none"`, not an empty string, so that's checked for
    explicitly rather than just falsiness."""
    if not value or value == "none":
        return []
    names = []
    for raw in value.split(","):
        name = raw.strip().strip("'\"")
        if name:
            names.append(name)
    return names


def is_italic(font_style: "str | None") -> bool:
    return isinstance(font_style, str) and font_style.strip().lower() in ("italic", "oblique")


@lru_cache(maxsize=512)
def x_height(family, size, bold=False, italic=False) -> float:
    """The CSS `ex` unit's own basis (CSS 2.1 4.3.2): the resolved font's
    real x-height, in CSS pixels, at `size` -- read straight from Skia's
    own `FontMetrics.fXHeight`, the same font resolution `text_metrics()`
    already uses for ascent/descent. CSS falls back to `0.5em` when a font
    reports no x-height at all (`fXHeight <= 0`, e.g. some bitmap/synthetic
    fonts) -- most real fonts do report one; Ahem, deliberately, reports
    exactly `0.8em` for predictable testing (confirmed directly: Skia's
    own `fXHeight` for the real downloaded Ahem face already comes back as
    `0.8 * size`, so no special-casing is needed here at all -- only a
    fallback for the rare font that reports none)."""
    face = resolve_typeface(family, bold=bold, italic=italic)
    metrics = skia.Font(face, size).getMetrics()
    if metrics.fXHeight > 0:
        return float(metrics.fXHeight)
    return size * 0.5


@lru_cache(maxsize=512)
def zero_advance_width(family, size, bold=False, italic=False) -> float:
    """The CSS `ch` unit's own basis (CSS Values 4 6.2): the resolved
    font's real advance width of the `"0"` (U+0030 DIGIT ZERO) glyph, in
    CSS pixels, at `size` -- `skia.Font.measureText` on that one
    character, the same real, resolved typeface `text_metrics()`/
    `x_height()` already use. CSS falls back to `0.5em` when the font has
    no `"0"` glyph at all (rare; `measureText` returns `0` for a glyph
    that doesn't exist, indistinguishable from a genuinely zero-width
    one -- the fallback covers both, matching spec: "in the cases where
    it is impossible or impractical to determine the measure of the
    '0' glyph, it must be assumed to be 0.5em")."""
    face = resolve_typeface(family, bold=bold, italic=italic)
    width = skia.Font(face, size).measureText("0")
    if width > 0:
        return float(width)
    return size * 0.5


@lru_cache(maxsize=512)
def text_metrics(family, size, bold=False, italic=False):
    """CSS ascent, descent and normal line height, in CSS pixels.

    Round font metrics separately, as Blink does, rather than rounding their
    sum. The macOS legacy-font adjustment matches Blink's FontMetrics::
    AscentDescentWithHacks (also used by Safari); it is not fixture-specific.
    """
    face = resolve_typeface(family, bold=bold, italic=italic)
    metrics = skia.Font(face, size).getMetrics()
    ascent = math.floor(-metrics.fAscent + 0.5)
    descent = math.floor(metrics.fDescent + 0.5)
    if sys.platform == 'darwin' and face.getFamilyName() in ('Times', 'Helvetica', 'Courier'):
        ascent += math.floor((ascent + descent) * 0.15 + 0.5)
    return float(ascent), float(descent), float(ascent + descent + math.floor(metrics.fLeading + 0.5))


def warm_cache() -> None:
    """Resolve the four typeface combinations almost every page uses
    (default family, normal/bold x upright/italic) once, up front. Measured
    (see PLAN.md's "Phase 9" perf notes, profiling `native_browser.py`):
    resolving a `skia.Typeface` for the very first time is a real ~25ms
    platform font-manager lookup, one-time but real -- every combination
    after the first hits `_typeface_cache` and costs close to nothing. A
    caller that's about to show a window (`native_browser.py`'s `View`)
    calls this once at startup so that cost lands before the first frame
    is due, not silently inside it."""
    for bold in (False, True):
        for italic in (False, True):
            resolve_typeface(None, bold=bold, italic=italic)


def resolve_typeface(family_value: "str | None", *, bold: bool = False, italic: bool = False) -> "skia.Typeface":
    """The `skia.Typeface` to paint text in, given a CSS `font-family`
    computed value and resolved `bold`/`italic` flags.

    **Every name in the list is tried, in order, against what's actually
    installed** -- an earlier version of this function tried only the
    *first* name and stopped, reasoning that `skia.Typeface(name, style)`
    is documented to never return null so there was no signal to act on.
    That reasoning was wrong: `Typeface()`'s own fallback is exactly the
    problem, not a reason to skip checking -- a real CSS font stack like
    `-apple-system, "Segoe UI", sans-serif` (this repo's own UA stylesheet)
    has its *first* name be a non-standard, browser-internal keyword no
    font manager has ever heard of, and `Typeface("-apple-system", ...)`
    silently, successfully resolves to *something* (Helvetica, on this
    repo's own dev machine) without ever giving the second or third names a
    chance -- exactly backwards from what a font stack is for. The actual
    reliable signal is `FontMgr().matchFamily(name)`, which comes back
    *empty* for a name nothing provides (`_is_installed`, above); this
    walks the list until one name is actually installed, only then handing
    it to `Typeface()`. A generic CSS keyword (`serif`, `monospace`, ...) is
    translated to one concrete candidate name first (`_GENERIC_FAMILIES`),
    since font managers have no notion of CSS's generic keywords on their
    own -- that translated name is still checked for installation like any
    other candidate, not assumed to exist."""
    name = None  # nothing resolves -> the platform default, same as an empty/unset font-family
    for candidate in parse_family_list(family_value):
        web = _web_typefaces.get(candidate.lower())
        if web is not None:
            return web
        mapped = _GENERIC_FAMILIES.get(candidate.lower(), candidate)
        if mapped is None or _is_installed(mapped):
            name = mapped
            break
        # not installed -- fall through to the next name in the stack,
        # exactly as CSS's own font-family fallback is supposed to work.

    key = (name, bold, italic)
    typeface = _typeface_cache.get(key)
    if typeface is None:
        if bold and italic:
            style = skia.FontStyle.BoldItalic()
        elif bold:
            style = skia.FontStyle.Bold()
        elif italic:
            style = skia.FontStyle.Italic()
        else:
            style = skia.FontStyle.Normal()
        typeface = skia.Typeface(name, style)
        _typeface_cache[key] = typeface
    return typeface
