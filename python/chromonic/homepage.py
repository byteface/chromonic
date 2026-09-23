"""Chromonic's built-in start page.

Serves two pages, both generated on the fly with domonic's HTML builder --
no static assets on disk: `chromonic://home` (search + a link grid to the
project's own sites) and `chromonic://settings` (lets the user point their
homepage at any URL instead, persisted to a small JSON prefs file). Wired
into navigation by `browser.py`'s `_load_internal`.
"""

from __future__ import annotations

import json
from pathlib import Path

from domonic.html import *  # noqa: F401,F403 -- tag builders (div, a, form, ...)

# `domonic.html` exports its own `html`/`title` tag builders, which would
# shadow the stdlib `html` module's `escape()` this file also needs for
# safely embedding a user-supplied string (the custom homepage URL) as
# *text* -- unlike an attribute value (`_value=...` below), domonic's
# serializer does not escape text children, so an unescaped `<`/`&` there
# would parse back out as real markup. Imported after the star-import so
# this name wins.
import html as _html

PREFS_PATH = Path.home() / ".chromonic" / "prefs.json"

HOME_URL = "chromonic://home"
SETTINGS_URL = "chromonic://settings"
SAVE_SETTINGS_URL = "chromonic://save-settings"

#: (title, description, href) -- mirrors the mockup's card grid. The last
#: card is always the internal settings page, appended in `_cards()`.
_LINKS = [
    ("Domonic Docs", "Documentation & examples", "https://domonic.readthedocs.io/"),
    ("Ecosia", "Search the web, plant trees", "https://www.ecosia.org/"),
    ("Chromonic on PyPI", "Install & releases", "https://pypi.org/project/chromonic/"),
    ("GitHub", "Source code & issues", "https://github.com/byteface/chromonic"),
    ("Projects", "Demos, experiments & tools",
     "https://github.com/byteface/chromonic/tree/master/examples"),
]

_STYLE = """
    * { box-sizing: border-box; }
    body {
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, #eef1f8 0%, #dfe6f3 55%, #cfd9ec 100%);
        font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
        color: #2b2f3a;
    }
    .page { max-width: 760px; margin: 0 auto; padding: 64px 24px 48px; }
    .brand { display: flex; align-items: center; justify-content: center; gap: 14px; margin-bottom: 28px; }
    .brand .mark {
        width: 56px; height: 56px; border-radius: 16px;
        background: linear-gradient(135deg, #3a3f4b 0%, #202124 100%);
        color: white; font-family: Georgia, "Times New Roman", serif;
        font-size: 30px; font-weight: 700; font-style: italic;
        display: flex; align-items: center; justify-content: center;
    }
    .brand .word { font-size: 34px; font-weight: 700; letter-spacing: -0.5px; }
    .tagline { text-align: center; color: #5b6270; margin: 0 0 32px; font-size: 14px; }
    form.search { display: flex; margin: 0 0 36px; }
    form.search input[type=search] {
        flex: 1; padding: 14px 18px; font-size: 15px; border-radius: 24px 0 0 24px;
        border: 1px solid #ccd3e0; border-right: none; outline: none; background: white;
    }
    form.search button {
        padding: 0 22px; font-size: 14px; font-weight: 600; border-radius: 0 24px 24px 0;
        border: 1px solid #ccd3e0; background: #202124; color: white; cursor: pointer;
    }
    .grid { display: flex; flex-wrap: wrap; gap: 16px; }
    .card {
        flex: 1 1 220px; min-width: 220px; display: block; padding: 16px 18px;
        background: white; border: 1px solid #dfe3ec; border-radius: 12px;
        text-decoration: none; color: inherit;
    }
    .card .title { font-weight: 600; font-size: 15px; margin-bottom: 4px; }
    .card .desc { font-size: 13px; color: #6b7280; }
    .settings-box {
        background: white; border: 1px solid #dfe3ec; border-radius: 12px;
        padding: 22px 24px; margin-top: 8px;
    }
    .settings-box label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 8px; }
    .settings-box input[type=text] {
        width: 100%; padding: 11px 14px; font-size: 14px; border-radius: 8px;
        border: 1px solid #ccd3e0; outline: none; margin-bottom: 6px;
    }
    .hint { font-size: 12px; color: #7a8190; margin: 0 0 16px; }
    .row { display: flex; gap: 10px; margin-top: 14px; }
    .btn {
        padding: 10px 18px; font-size: 13px; font-weight: 600; border-radius: 8px;
        border: 1px solid #202124; cursor: pointer; text-decoration: none;
    }
    .btn.primary { background: #202124; color: white; }
    .btn.secondary { background: white; color: #202124; }
    .saved-banner {
        background: #e6f4ea; border: 1px solid #b7dfc2; color: #1e6b34;
        border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px;
    }
    .back-link { display: inline-block; margin-top: 24px; font-size: 13px; color: #5b6270; }
"""


def load_prefs() -> dict:
    """The persisted prefs dict, or `{}` if none has been saved yet."""
    try:
        return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_prefs(prefs: dict) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def custom_homepage(prefs: "dict | None" = None) -> "str | None":
    """The user's own configured start page, or `None` for the built-in one."""
    value = (prefs if prefs is not None else load_prefs()).get("homepage", "")
    value = value.strip() if isinstance(value, str) else ""
    return value or None


def start_url() -> str:
    """Where chromonic should open on launch."""
    return custom_homepage() or HOME_URL


def _document(*, page_title: str, children) -> str:
    doc = html(
        head(
            meta(_charset="utf-8"),
            title(page_title),
            style(_STYLE),
        ),
        body(div(*children, _class="page")),
    )
    return "<!DOCTYPE html>" + str(doc)


def _brand():
    # A plain CSS badge, not an emoji/image glyph -- Skia here has no
    # color-emoji font fallback wired up, so "\U0001F40D" rendered as
    # nothing rather than a snake (confirmed: chromonic's font/paint code
    # has no emoji-glyph handling at all). A single styled letter always
    # has glyph coverage.
    return div(
        div("C", _class="mark"),
        div("chromonic", _class="word"),
        _class="brand",
    )


def _cards(prefs: dict):
    links = list(_LINKS)
    tiles = [
        a(
            div(_html.escape(label), _class="title"),
            div(_html.escape(desc), _class="desc"),
            _href=href,
            _class="card",
        )
        for label, desc, href in links
    ]
    tiles.append(
        a(
            div("Settings", _class="title"),
            div("Choose your homepage", _class="desc"),
            _href=SETTINGS_URL,
            _class="card",
        )
    )
    return div(*tiles, _class="grid")


def build_homepage_html(prefs: "dict | None" = None) -> str:
    prefs = prefs if prefs is not None else load_prefs()
    children = [
        _brand(),
        p("A browser powered by Python.", _class="tagline"),
        form(
            input(_type="search", _name="q", _placeholder="Search Ecosia or enter an address..."),
            button("Search", _type="submit"),
            _class="search",
            _method="get",
            _action="https://www.ecosia.org/search",
        ),
        _cards(prefs),
    ]
    return _document(page_title="chromonic", children=children)


def build_settings_html(prefs: "dict | None" = None, *, saved: bool = False) -> str:
    prefs = prefs if prefs is not None else load_prefs()
    current = custom_homepage(prefs)

    banner = [div("Saved.", _class="saved-banner")] if saved else []

    box = div(
        *banner,
        label("Homepage", _for="homepage"),
        input(
            _type="text",
            _id="homepage",
            _name="homepage",
            _placeholder="chromonic://home",
            _value=current or "",
        ),
        p(
            "Leave blank to use chromonic's built-in start page. "
            f"Currently: {_html.escape(current or HOME_URL)}",
            _class="hint",
        ),
        div(
            button("Save", _type="submit", _class="btn primary"),
            a("Reset to default", _href=SAVE_SETTINGS_URL, _class="btn secondary"),
            _class="row",
        ),
        _class="settings-box",
    )

    children = [
        _brand(),
        p("Settings", _class="tagline"),
        form(box, _method="get", _action=SAVE_SETTINGS_URL),
        a("← Back to homepage", _href=HOME_URL, _class="back-link"),
    ]
    return _document(page_title="chromonic — Settings", children=children)
