"""Direct Skia/OpenGL browser window. No PNG, image transport, or webview.

GLFW owns the window/context; Skia draws into its framebuffer. Taffy still
owns document geometry. Window imports are lazy so the controller is testable
without a display. All DOM/layout/GPU work remains on the main thread.
"""
from __future__ import annotations

import time
import urllib.parse

import skia

from . import browser, browser_images, domonic_canvas_patch, fonts, hittest, paint, tree
from .tree import warm_text_layout

TOOLBAR = 44

# Minimum real time between two image-triggered relayouts (see
# `View.poll_images`) -- a real page can have dozens of `<img>`s, and
# `tree.layout()` always rebuilds the *entire* Taffy tree (no dirty-bit
# system -- a deliberate POC-scope choice). Relayouting on every single
# arrival -- easily dozens a second, with several images completing close
# together off a small thread pool -- turns "a page with a lot of images"
# into a storm of full-tree relayouts fighting the main/GL thread for CPU:
# measured, a 40-image page produced 34 separate relayouts for one burst of
# arrivals. Throttling to at most one relayout per this interval still
# picks up every arrival (whatever landed gets caught by the next relayout
# `_image_generation` compares against), just far less often -- closer to
# a real browser's own capped repaint rate than to "relayout per resource".
_IMAGE_RELAYOUT_INTERVAL = 0.2


def sync_window_size(view, window, glfw_module):
    """Reflow to GLFW's settled logical size after processing window events."""
    logical_size = glfw_module.get_window_size(window)
    if logical_size == (view.width, view.height):
        return False
    view.resize(*logical_size)
    return True


class View:
    def __init__(self, width=1000, height=800, loader=None):
        domonic_canvas_patch.install()
        self.width, self.height = width, height
        self.loader = loader or browser.load
        self.page = None
        self.page_title = ""
        self.url = self.address = ''
        self.history = []
        self.scroll_y = 0.0
        self.content_height = 0.0
        self.editing = False
        self.select_all = False
        self.caret = 0
        self.focused_element = None
        self.input_caret = 0
        self.navigation_handler = None
        self.status = ''
        self.loading = False
        self.dirty = True
        self._image_generation = 0
        self._last_image_relayout = 0.0
        self.display_list = []
        self.layout_projection = tree.LayoutProjection()
        self.last_painted_elements = 0
        self.console_open = False
        self.console_input = ''
        self.console_caret = 0
        # Scrollback: a list of (kind, text) pairs, kind in
        # ('in', 'out', 'err') -- kept separate from `self.console_input`
        # (the not-yet-submitted line) the same way `self.address` is kept
        # separate from navigation history.
        self.console_lines = []
        # Three real, one-time setup costs (typeface resolution, Parley's
        # font enumeration, myjs's JS-interpreter-globals) each cost tens to
        # ~100ms the first time only -- pay them now, during startup, not
        # during the first page's load/paint.
        fonts.warm_cache()
        warm_text_layout()
        browser.warm_interpreter()

    @property
    def viewport_height(self):
        return max(1, self.height - TOOLBAR)

    def navigate(self, address, *, back=False, method='GET', data=None):
        url = urllib.parse.urljoin(self.url, browser._normalize_address(address))
        try:
            browser._validate_navigable(url)
            if self.navigation_handler is not None:
                return self.navigation_handler(url, back=back, method=method, data=data)
            page = (self.loader(url) if method == 'GET' and data is None
                    else browser.load(url, method=method, data=data))
        except Exception as error:
            self.status = str(error)
            self.dirty = True
            return False
        return self.commit_page(page, url, back=back)

    def commit_page(self, page, url, *, back=False):
        # `page.url` is the real, final URL `browser.load` fetched (after
        # any HTTP redirect -- a POST form submission commonly gets one on
        # success) -- `url` is only what was originally *requested*, which
        # a redirect can leave stale. Preferring `page.url` keeps the
        # address bar and later relative-link/form resolution (`self.url`)
        # pointed at where the page actually ended up.
        url = getattr(page, 'url', None) or url
        self.page, self.url, self.address = page, url, url
        titles = page.document.getElementsByTagName("title")
        self.page_title = (titles[0].textContent or "").strip() if titles else ""
        self.caret = len(url)
        if back:
            self.history.pop()
        else:
            self.history.append(url)
        self.scroll_y = 0
        self.editing = self.select_all = False
        self.status = ''
        self.loading = False
        self.relayout()
        return True

    def back(self):
        if len(self.history) > 1:
            return self.navigate(self.history[-2], back=True)
        return False

    def relayout(self, *, reuse_styles: bool = False):
        """`reuse_styles=True` skips re-resolving CSS for elements whose
        styling inputs provably haven't changed (see `tree.layout`'s
        docstring) -- only `poll_images()` may pass this, since an
        image-arrival relayout never touches class/inline-style/stylesheets.
        A resize can flip which `@media` rules apply, and a click can run a
        real DOM event handler that mutates style -- both must keep the
        default so they always get a fully fresh resolution."""
        if self.page is not None:
            # The CSS viewport remains window-sized even though the document
            # is allowed to grow vertically and is clipped/scrolled at paint.
            browser.set_viewport(self.page, self.width, self.viewport_height)
            doc = self.page.document
            # domonic indexes media rules on the document; resize changes which
            # rules can apply and must not reuse the old viewport's index.
            if hasattr(doc, '_cssom_rule_index'):
                doc._cssom_rule_index = None
            nodes = self.layout_projection.layout(
                doc.body, width=self.width, height=None, reuse_styles=reuse_styles,
                viewport_height=self.viewport_height,
            )
            self.content_height = max(
                (max(box.y + box.height,
                     float(el.__dict__.get("_chromonic_scroll_extent", 0.0)))
                 for el in nodes.values()
                 if (box := el.__dict__.get("_layout_box")) is not None),
                default=0,
            )
            self.display_list = paint.build_display_list(doc.body)
            self.scroll_y = min(self.scroll_y, max(0, self.content_height - self.viewport_height))
        self.dirty = True

    def poll_images(self):
        """Whether any background `<img>` fetch (`browser_images.py`)
        finished since the last check -- if so, relayout+repaint so it
        actually appears, instead of the page staying however it looked
        before the image arrived. Called once per `run()` loop iteration,
        the same "poll on the main thread, never touch the DOM off it"
        shape `Navigation.poll()` already uses for page loads.

        Throttled to at most one relayout per `_IMAGE_RELAYOUT_INTERVAL` --
        see its own comment for why: a page with many images can have
        several arrive within milliseconds of each other, and relayouting
        for each one individually (rather than once for however many
        arrived since the last relayout) turned "loading images" into a
        storm of full-tree relayouts competing with the GL thread for CPU.

        That throttle alone wasn't enough on a real page (bbc.co.uk, ~4600
        elements): a *single* relayout there costs several seconds, because
        domonic's CSS cascade resolution re-parses every candidate selector
        from scratch for every element with no caching at all (see
        `docs/domonic-wrinkles.md` #16) -- so even one relayout every 200ms
        was enough to beachball the page. `reuse_styles=True` is what
        actually fixes that: an image finishing a background fetch never
        changes any element's class/inline-style/stylesheets, so this
        relayout can reuse every element's already-resolved CSS and only
        needs to redo layout math + the arrived image's own intrinsic
        size (`_apply_image_intrinsic_size` already handles that
        independently of the CSS cache)."""
        from . import webfonts
        if self.page is not None:
            reg = webfonts.registry(self.page.document.body)
            if reg is not None and reg.poll():
                self.relayout()
        if browser_images.advance_animations():
            self.dirty = True
        current = browser_images.generation()
        if current == self._image_generation:
            return
        now = time.monotonic()
        if now - self._last_image_relayout < _IMAGE_RELAYOUT_INTERVAL:
            return  # too soon -- the next poll will catch this and whatever else lands before it
        self._image_generation = current
        self._last_image_relayout = now
        self.relayout(reuse_styles=True)

    def resize(self, width, height):
        if width > 0 and height > 0 and (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.relayout()

    def scroll(self, delta):
        self.scroll_y = max(0, min(self.scroll_y + delta,
                                  max(0, self.content_height - self.viewport_height)))
        self.dirty = True  # scrolling only repaints; it never relayouts

    @staticmethod
    def _editable_ancestor(element):
        """The nearest `<input>`/`<textarea>` at or above `element`, or
        `None` -- the same ancestor-walk shape `click()` already uses to
        find an enclosing `<a>`, so a click landing on text/an icon nested
        inside a text field still focuses the field itself."""
        while element is not None:
            if str(getattr(element, 'tagName', '')).lower() in ('input', 'textarea'):
                return element
            element = getattr(element, 'parentElement', None)
        return None

    @staticmethod
    def _form_ancestor(element):
        while element is not None:
            if str(getattr(element, 'tagName', '')).lower() == 'form':
                return element
            element = getattr(element, 'parentElement', None)
        return None

    @staticmethod
    def _is_submit_control(element):
        tag = str(getattr(element, 'tagName', '')).lower()
        if tag == 'button':
            return (element.getAttribute('type') or 'submit').lower() == 'submit'
        if tag == 'input':
            return (element.getAttribute('type') or 'text').lower() in ('submit', 'image')
        return False

    @staticmethod
    def _collect_form_data(form):
        """Serialize `form`'s controls into `application/x-www-form-
        urlencoded` name/value pairs, the same subset real browsers send:
        named, enabled controls only; unchecked checkboxes/radios and
        submit/reset/button/file/image inputs are excluded (no submit
        button is included at all -- this project has no notion of "the
        button that was actually clicked" for an Enter-key submit, and
        omitting an unnamed one, by far the common case, matches most
        real forms exactly). `<select>` reads its selected `<option>` (or
        the first one, same fallback `tree._select_display_text` uses)."""
        pairs = []
        for el in form.querySelectorAll('input, select, textarea'):
            name = el.getAttribute('name')
            if not name or el.getAttribute('disabled') is not None:
                continue
            tag = str(getattr(el, 'tagName', '')).lower()
            if tag == 'input':
                input_type = (el.getAttribute('type') or 'text').lower()
                if input_type in ('submit', 'reset', 'button', 'file', 'image'):
                    continue
                if input_type in ('checkbox', 'radio'):
                    if el.getAttribute('checked') is None:
                        continue
                    value = el.getAttribute('value') or 'on'
                else:
                    value = getattr(el, 'value', None)
                    if value is None:
                        value = el.getAttribute('value') or ''
            elif tag == 'textarea':
                value = getattr(el, 'value', None)
                if value is None:
                    value = el.textContent or ''
            else:  # select
                option = el.querySelector('option[selected]') or el.querySelector('option')
                if option is None:
                    value = ''
                else:
                    value = option.getAttribute('value')
                    if value is None:
                        value = option.textContent or ''
            pairs.append((name, str(value)))
        return pairs

    def submit_form(self, form):
        """Submit `form` for real -- GET encodes its fields into the query
        string of an ordinary navigation; POST sends them as the request
        body through `browser.load`'s new `method`/`data`, which shares
        this browser's persistent cookie session (`browser._shared_http_
        session`), so a login that sets a session cookie actually stays
        logged in for whatever page the response lands on."""
        method = (form.getAttribute('method') or 'get').strip().lower()
        action = form.getAttribute('action') or ''
        url = urllib.parse.urljoin(self.url, action)
        pairs = self._collect_form_data(form)
        if method == 'post':
            self.navigate(url, method='POST', data=pairs)
        else:
            query = urllib.parse.urlencode(pairs)
            joiner = '&' if urllib.parse.urlsplit(url).query else '?'
            self.navigate(url + (joiner + query if query else ''))

    def click(self, x, y):
        if y < TOOLBAR:
            self.focused_element = None
            if x < 60:
                self.back()
            elif x > self.width - 60:
                self.navigate(self.address)
            else:
                self.focus_address()
            self.dirty = True
            return
        self.editing = self.select_all = False
        if self.page is None:
            return
        el = hittest.hit_test(self.page.document.body, x, y - TOOLBAR + self.scroll_y)
        anchor = el
        while anchor is not None:
            if str(getattr(anchor, 'tagName', '')).lower() == 'a':
                href = anchor.getAttribute('href')
                if href:
                    self.focused_element = None
                    resolved = urllib.parse.urljoin(self.url, href)
                    if urllib.parse.urldefrag(resolved)[0] == urllib.parse.urldefrag(self.url)[0] and '#' in resolved:
                        target = self.page.document.getElementById(urllib.parse.unquote(resolved.split('#', 1)[1]))
                        if target is not None and target.get_layout_box() is not None:
                            self.scroll(target.get_layout_box().y - self.scroll_y)
                        return
                    self.navigate(resolved)
                    return
            anchor = getattr(anchor, 'parentElement', None)
        submit = el
        while submit is not None:
            if self._is_submit_control(submit):
                form = self._form_ancestor(submit)
                if form is not None:
                    self.focused_element = None
                    self.submit_form(form)
                    return
                break
            submit = getattr(submit, 'parentElement', None)
        field = self._editable_ancestor(el)
        self.focused_element = field
        if field is not None:
            self.input_caret = len(str(getattr(field, 'value', '') or ''))
        if el is not None:
            from domonic.events import MouseEvent
            el.dispatchEvent(MouseEvent('click', {'bubbles': True, 'clientX': x, 'clientY': y - TOOLBAR}))
            self.relayout()

    def cursor_kind(self, x, y):
        """Cursor shape for (x, y) in window coordinates -- 'pointer',
        'text', or 'arrow'. Driven entirely off the existing DOM hit-test
        (`hittest.hit_test` + `cursor_for_element`), the same lookup
        `click()` uses, so it always follows the real element under the
        mouse instead of separate geometry rules. Read-only: never touches
        layout or paint."""
        if y < TOOLBAR:
            return 'text' if 60 <= x <= self.width - 60 else 'arrow'
        if self.page is None:
            return 'arrow'
        el = hittest.hit_test(self.page.document.body, x, y - TOOLBAR + self.scroll_y)
        if el is None:
            return 'arrow'
        return hittest.cursor_for_element(el)

    def focus_address(self):
        self.focused_element = None
        self.editing = self.select_all = True
        self.caret = len(self.address)
        self.dirty = True

    def _dispatch_field_event(self, element, name):
        from domonic.events import Event
        try:
            element.dispatchEvent(Event(name, {'bubbles': True}))
        except Exception:
            pass  # best-effort -- a page without a listener has nothing to lose

    def input_type_text(self, text):
        element = self.focused_element
        if element is None:
            return
        text = ''.join(c for c in text if c.isprintable())
        if not text:
            return
        value = str(getattr(element, 'value', '') or '')
        element.value = value[:self.input_caret] + text + value[self.input_caret:]
        self.input_caret += len(text)
        self._dispatch_field_event(element, 'input')
        self.relayout()

    def input_edit_key(self, key):
        element = self.focused_element
        if element is None:
            return
        value = str(getattr(element, 'value', '') or '')
        changed = False
        if key == 'backspace' and self.input_caret:
            element.value = value[:self.input_caret - 1] + value[self.input_caret:]
            self.input_caret -= 1
            changed = True
        elif key == 'delete':
            element.value = value[:self.input_caret] + value[self.input_caret + 1:]
            changed = True
        elif key == 'left':
            self.input_caret = max(0, self.input_caret - 1)
        elif key == 'right':
            self.input_caret = min(len(value), self.input_caret + 1)
        elif key == 'home':
            self.input_caret = 0
        elif key == 'end':
            self.input_caret = len(value)
        elif key == 'enter' and str(getattr(element, 'tagName', '')).lower() == 'textarea':
            element.value = value[:self.input_caret] + '\n' + value[self.input_caret:]
            self.input_caret += 1
            changed = True
        elif key == 'enter':
            # A real browser submits an `<input>`'s enclosing form on
            # Enter (textarea, handled above, inserts a newline instead --
            # multi-line fields never submit on bare Enter). No enclosing
            # form is a no-op, same as a real browser.
            form = self._form_ancestor(element)
            if form is not None:
                self.focused_element = None
                self.submit_form(form)
                return
        if changed:
            self._dispatch_field_event(element, 'input')
            self.relayout()
        else:
            self.dirty = True

    def type_text(self, text):
        if not self.editing:
            return
        text = ''.join(c for c in text if c.isprintable())
        if self.select_all:
            self.address = ''
            self.caret = 0
        self.address = self.address[:self.caret] + text + self.address[self.caret:]
        self.caret += len(text)
        self.select_all = False
        self.dirty = True

    def edit_key(self, key):
        if not self.editing:
            return
        if key in ('backspace', 'delete'):
            if self.select_all:
                self.address, self.caret = '', 0
            elif key == 'backspace' and self.caret:
                self.address = self.address[:self.caret - 1] + self.address[self.caret:]
                self.caret -= 1
            elif key == 'delete':
                self.address = self.address[:self.caret] + self.address[self.caret + 1:]
        elif key == 'left':
            self.caret = 0 if self.select_all else max(0, self.caret - 1)
        elif key == 'right':
            self.caret = len(self.address) if self.select_all else min(len(self.address), self.caret + 1)
        elif key == 'home':
            self.caret = 0
        elif key == 'end':
            self.caret = len(self.address)
        elif key == 'escape':
            self.address, self.caret = self.url, len(self.url)
            self.editing = False
        self.select_all = False
        self.dirty = True

    CONSOLE_HEIGHT = 260

    def toggle_console(self):
        self.console_open = not self.console_open
        self.dirty = True

    def console_type_text(self, text):
        text = ''.join(c for c in text if c.isprintable())
        self.console_input = self.console_input[:self.console_caret] + text + self.console_input[self.console_caret:]
        self.console_caret += len(text)
        self.dirty = True

    def console_edit_key(self, key):
        if key == 'backspace' and self.console_caret:
            self.console_input = self.console_input[:self.console_caret - 1] + self.console_input[self.console_caret:]
            self.console_caret -= 1
        elif key == 'delete':
            self.console_input = self.console_input[:self.console_caret] + self.console_input[self.console_caret + 1:]
        elif key == 'left':
            self.console_caret = max(0, self.console_caret - 1)
        elif key == 'right':
            self.console_caret = min(len(self.console_input), self.console_caret + 1)
        elif key == 'home':
            self.console_caret = 0
        elif key == 'end':
            self.console_caret = len(self.console_input)
        elif key == 'enter':
            self.console_submit()
        self.dirty = True

    def console_submit(self):
        """Evaluate `self.console_input` as a Python expression against the
        loaded page's `document`/`window` -- domonic's DOM already mirrors
        the real JS API 1:1 (`getElementById`, `querySelector`, camelCase
        properties, ...), so a typical devtools-style one-liner like
        `document.getElementById('x').style.color` is valid Python as-is;
        this deliberately doesn't pretend to be a real JS engine (myjs's is
        only ever attached to a locally-loaded `Page`, not a remote
        `domonic.scrape()`'d one, which is what `native_browser` normally
        navigates)."""
        expr = self.console_input.strip()
        self.console_input = ''
        self.console_caret = 0
        if not expr:
            return
        self.console_lines.append(('in', expr))
        doc = self.page.document if self.page is not None else None
        session = getattr(self.page, 'session', None) if self.page is not None else None
        window_obj = getattr(session, 'window', None) or getattr(doc, 'defaultView', None)
        try:
            result = eval(expr, {'__builtins__': __builtins__},
                          {'document': doc, 'window': window_obj, 'view': self})
        except Exception as error:
            self.console_lines.append(('err', f'{type(error).__name__}: {error}'))
        else:
            self.console_lines.append(('out', 'undefined' if result is None else repr(result)))
        del self.console_lines[:-500]
        self.dirty = True

    def draw_input_caret(self, canvas):
        """Caret for `self.focused_element`, positioned the same way
        `paint.py` positions that field's own displayed text (box origin +
        border + padding, first line's baseline) so it tracks real text
        rendering rather than an independently-guessed spot. Drawn from
        inside `draw()`'s already-scrolled/clipped page canvas transform,
        so it scrolls with the page like any other content. Solid, not
        blinking -- redraws here only happen on a real input event (this
        view has no animation-frame timer), so a time-based blink would
        just freeze at whatever phase the last keystroke happened to catch
        it at, rather than actually animating."""
        element = self.focused_element
        if element is None:
            return
        box = element.get_layout_box()
        if box is None:
            return
        padding = element.__dict__.get('_chromonic_padding', (0.0, 0.0, 0.0, 0.0))
        pad_top, _pad_right, _pad_bottom, pad_left = padding
        style = element.__dict__.get('_chromonic_paint_style', {})
        font_size = paint._px(style.get('font_size'), 16.0)
        bold = paint._fontmetrics.is_bold(style.get('font_weight'))
        italic = fonts.is_italic(style.get('font_style'))
        font = paint._font(font_size, bold=bold, italic=italic, family=style.get('font_family'))
        value = str(getattr(element, 'value', '') or '')
        prefix = value[:self.input_caret]
        if (element.getAttribute('type') or '').lower() == 'password':
            prefix = '•' * len(prefix)
        text_x = box.x + box.border_left + pad_left + font.measureText(prefix)
        baseline_y = box.y + box.border_top + pad_top + font_size
        ink = skia.Paint(Color=skia.ColorBLACK, AntiAlias=True)
        canvas.drawLine(text_x, baseline_y - font_size, text_x, baseline_y + 2, ink)

    def draw_console(self, canvas):
        height = min(self.CONSOLE_HEIGHT, self.height - TOOLBAR)
        if height <= 0:
            return
        canvas.drawRect(skia.Rect.MakeXYWH(0, TOOLBAR, self.width, height), skia.Paint(Color=0xff000000))
        font = paint._font(13, family='monospace')
        green = skia.Paint(Color=0xff33ff33, AntiAlias=True)
        line_height = 16
        input_y = TOOLBAR + height - 10
        prompt = '> ' + self.console_input
        canvas.drawString(prompt, 6, input_y, font, green)
        caret_x = 6 + font.measureText('> ' + self.console_input[:self.console_caret])
        canvas.drawLine(caret_x, input_y - 12, caret_x, input_y + 3, green)
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(0, TOOLBAR, self.width, height - line_height - 6))
        prefix = {'in': '> ', 'out': '< ', 'err': '! '}
        y = input_y - line_height
        for kind, text in reversed(self.console_lines):
            for row in reversed((prefix[kind] + text).splitlines() or ['']):
                canvas.drawString(row, 6, y, font, green)
                y -= line_height
            if y < TOOLBAR:
                break
        canvas.restore()

    def draw_loading_spinner(self, canvas, cx, cy, radius=8.0):
        """A small rotating arc, animating continuously off `time.monotonic()`
        while `self.loading` -- the one visible sign a navigation is actually
        under way rather than the window just sitting there looking frozen
        for however long the fetch (and, currently unavoidably, the one big
        synchronous relayout once it lands -- see `commit_page`) takes. The
        run loop keeps forcing a redraw for exactly this long (`navigation.
        pending is not None`), so this really does animate rather than
        freezing at whatever phase happened to be on screen when the last
        real event landed, the same reasoning that ruled out a blinking (as
        opposed to solid) text caret elsewhere in this file."""
        paint_ = skia.Paint(
            Color=skia.ColorBLACK, AntiAlias=True, Style=skia.Paint.kStroke_Style,
            StrokeWidth=2.5, StrokeCap=skia.Paint.kRound_Cap,
        )
        start_angle = (time.monotonic() * 320.0) % 360.0
        canvas.drawArc(
            skia.Rect.MakeXYWH(cx - radius, cy - radius, radius * 2, radius * 2),
            start_angle, 270.0, False, paint_,
        )

    def draw(self, canvas):
        canvas.clear(skia.ColorWHITE)
        if self.page is not None:
            canvas.save()
            canvas.clipRect(skia.Rect.MakeXYWH(0, TOOLBAR, self.width, self.viewport_height))
            canvas.translate(0, TOOLBAR - self.scroll_y)
            self.last_painted_elements = paint.paint_display_list(
                canvas, self.display_list,
                top=self.scroll_y, bottom=self.scroll_y + self.viewport_height,
            )
            self.draw_input_caret(canvas)
            canvas.restore()
        canvas.drawRect(skia.Rect.MakeWH(self.width, TOOLBAR), skia.Paint(Color=0xffe2e8f0))
        canvas.drawRect(skia.Rect.MakeXYWH(60, 6, max(1, self.width - 120), 32),
                        skia.Paint(Color=0xffbfdbfe if self.select_all else skia.ColorWHITE))
        font = paint._font(15)
        ink = skia.Paint(Color=skia.ColorBLACK, AntiAlias=True)
        canvas.drawString('Back', 8, 27, font, ink)
        canvas.drawString('Go', self.width - 44, 27, font, ink)
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(65, 6, max(1, self.width - 130), 32))
        caret_width = font.measureText(self.address[:self.caret])
        offset = max(0, caret_width - max(1, self.width - 142)) if self.editing else 0
        canvas.drawString(self.address, 68 - offset, 27, font, ink)
        if self.editing and not self.select_all:
            x = 68 + caret_width - offset
            canvas.drawLine(x, 12, x, 31, ink)
        canvas.restore()
        if self.status:
            bar_color = 0xffe0edff if self.loading else 0xffffdddd
            canvas.drawRect(skia.Rect.MakeXYWH(0, TOOLBAR, self.width, 30), skia.Paint(Color=bar_color))
            canvas.drawString(self.status, 8, TOOLBAR + 21, font, ink)
            if self.loading:
                self.draw_loading_spinner(canvas, self.width - 18, TOOLBAR + 15)
        if self.console_open:
            self.draw_console(canvas)
        self.dirty = False


class GLRenderer:
    """Own the Skia surface while the associated GL context is current."""
    def __init__(self):
        from OpenGL import GL
        self.gl = GL
        self.context = skia.GrDirectContext.MakeGL()
        if self.context is None:
            raise RuntimeError('Skia could not create an OpenGL GPU context')
        self.surface = self.target = None
        self.size = None

    def draw(self, view, framebuffer_size):
        width, height = framebuffer_size
        if width <= 0 or height <= 0:
            return
        if self.size != (width, height):
            self.context.flushAndSubmit()
            self.surface = self.target = None
            info = skia.GrGLFramebufferInfo(int(self.gl.glGetIntegerv(self.gl.GL_FRAMEBUFFER_BINDING)), self.gl.GL_RGBA8)
            self.target = skia.GrBackendRenderTarget(width, height, 0, 8, info)
            self.surface = skia.Surface.MakeFromBackendRenderTarget(
                self.context, self.target, skia.kBottomLeft_GrSurfaceOrigin,
                skia.kRGBA_8888_ColorType, skia.ColorSpace.MakeSRGB())
            if self.surface is None:
                raise RuntimeError('Skia could not wrap the window framebuffer')
            self.size = (width, height)
        canvas = self.surface.getCanvas()
        canvas.save()
        # GLFW input/layout are logical units; the framebuffer uses physical
        # pixels. Use actual dimensions, not an assumed Retina scale of two.
        canvas.scale(width / view.width, height / view.height)
        view.draw(canvas)
        canvas.restore()
        self.context.flushAndSubmit()

    def close(self):
        self.surface = self.target = None
        self.context.releaseResourcesAndAbandonContext()


class Navigation:
    """Fetch/parse off-thread; commit DOM and perform layout on the UI thread.

    Only the most recent request may commit. No native Tree or GL objects
    are created by a worker, and queued superseded requests are cancelled.
    """
    def __init__(self, view, executor=None):
        from concurrent.futures import ThreadPoolExecutor
        self.view = view
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix='chromonic-load')
        self.pending = None
        view.navigation_handler = self.request

    def request(self, url, *, back=False, method='GET', data=None):
        if self.pending is not None:
            self.pending[0].cancel()
        loader = ((lambda: self.view.loader(url)) if method == 'GET' and data is None
                  else (lambda: browser.load(url, method=method, data=data)))
        self.pending = (self.executor.submit(loader), url, back)
        self.view.address = url
        self.view.caret = len(url)
        self.view.status = 'Loading ' + url
        self.view.loading = True
        self.view.dirty = True
        return True

    def poll(self):
        if self.pending is None or not self.pending[0].done():
            return
        future, url, back = self.pending
        self.pending = None
        try:
            page = future.result()
        except Exception as error:
            self.view.status = str(error)
            self.view.loading = False
            self.view.dirty = True
        else:
            # Do not discard text typed while a request was in flight.
            edit = (self.view.address, self.view.caret, self.view.select_all) if self.view.editing else None
            self.view.commit_page(page, url, back=back)
            if edit is not None:
                self.view.address, self.view.caret, self.view.select_all = edit
                self.view.editing = True

    def close(self):
        self.view.navigation_handler = None
        self.executor.shutdown(wait=False, cancel_futures=True)


def run(url='https://google.com/', *, width=1000, height=800, title='chromonic — direct Skia', frames=None):
    """Run on the main thread. ``frames`` bounds a display smoke test."""
    import glfw
    if not glfw.init():
        raise RuntimeError('GLFW could not initialize a display')
    win = renderer = navigation = None
    try:
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        win = glfw.create_window(width, height, title, None, None)
        if not win:
            raise RuntimeError('GLFW could not create an OpenGL window')
        glfw.set_window_size_limits(win, 280, 120, glfw.DONT_CARE, glfw.DONT_CARE)
        glfw.make_context_current(win)
        glfw.swap_interval(1)
        renderer = GLRenderer()
        view = View(*glfw.get_window_size(win))
        navigation = Navigation(view)
        view.navigate(url)
        def key(_win, key, scancode, action, mods):
            if action not in (glfw.PRESS, glfw.REPEAT):
                return
            command = mods & (glfw.MOD_CONTROL | glfw.MOD_SUPER)

            reload_key = (
                (command and key == glfw.KEY_R)
                or key == glfw.KEY_F5
            )

            back_key = (
                ((mods & glfw.MOD_ALT) and key == glfw.KEY_LEFT)
                or (
                    (mods & glfw.MOD_SUPER)
                    and key == glfw.KEY_LEFT_BRACKET
                )
            )

            if key == glfw.KEY_F12:
                view.toggle_console()

            elif view.console_open:
                if key == glfw.KEY_ENTER:
                    view.console_submit()
                elif key in (glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_HOME, glfw.KEY_END):
                    view.console_edit_key({glfw.KEY_BACKSPACE: 'backspace', glfw.KEY_DELETE: 'delete',
                                            glfw.KEY_LEFT: 'left', glfw.KEY_RIGHT: 'right',
                                            glfw.KEY_HOME: 'home', glfw.KEY_END: 'end'}[key])
                elif command and key == glfw.KEY_V:
                    text = glfw.get_clipboard_string(win) or b''
                    view.console_type_text(text.decode('utf-8') if isinstance(text, bytes) else text)
                elif key == glfw.KEY_ESCAPE:
                    view.toggle_console()

            elif command and key == glfw.KEY_L:
                view.focus_address()

            elif reload_key:
                if view.url:
                    view.navigate(view.url)

            elif back_key:
                view.back()

            elif view.editing:
                if key == glfw.KEY_ENTER:
                    view.navigate(view.address)
                elif key in (glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_HOME, glfw.KEY_END):
                    view.edit_key({glfw.KEY_BACKSPACE: 'backspace', glfw.KEY_DELETE: 'delete',
                                   glfw.KEY_LEFT: 'left', glfw.KEY_RIGHT: 'right',
                                   glfw.KEY_HOME: 'home', glfw.KEY_END: 'end'}[key])
                elif command and key == glfw.KEY_A:
                    view.select_all = True
                elif command and key == glfw.KEY_V:
                    text = glfw.get_clipboard_string(win) or b''
                    view.type_text(text.decode('utf-8') if isinstance(text, bytes) else text)
                elif key == glfw.KEY_ESCAPE:
                    view.edit_key('escape')
            elif view.focused_element is not None:
                if key in (glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_LEFT, glfw.KEY_RIGHT,
                           glfw.KEY_HOME, glfw.KEY_END, glfw.KEY_ENTER):
                    view.input_edit_key({glfw.KEY_BACKSPACE: 'backspace', glfw.KEY_DELETE: 'delete',
                                          glfw.KEY_LEFT: 'left', glfw.KEY_RIGHT: 'right',
                                          glfw.KEY_HOME: 'home', glfw.KEY_END: 'end',
                                          glfw.KEY_ENTER: 'enter'}[key])
                elif command and key == glfw.KEY_V:
                    text = glfw.get_clipboard_string(win) or b''
                    view.input_type_text(text.decode('utf-8') if isinstance(text, bytes) else text)
                elif key == glfw.KEY_ESCAPE:
                    view.focused_element = None
            elif key in (glfw.KEY_DOWN, glfw.KEY_UP, glfw.KEY_PAGE_DOWN, glfw.KEY_PAGE_UP):
                amount = view.viewport_height * .9 if key in (glfw.KEY_PAGE_DOWN, glfw.KEY_PAGE_UP) else 40
                view.scroll(amount if key in (glfw.KEY_DOWN, glfw.KEY_PAGE_DOWN) else -amount)
            view.dirty = True
        glfw.set_key_callback(win, key)

        def char_callback(_win, code):
            char = chr(code)
            if view.console_open:
                view.console_type_text(char)
            elif view.focused_element is not None:
                view.input_type_text(char)
            else:
                view.type_text(char)
        glfw.set_char_callback(win, char_callback)
        glfw.set_mouse_button_callback(win, lambda w, button, action, mods:
                                       view.click(*glfw.get_cursor_pos(win)) if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS else None)
        glfw.set_scroll_callback(win, lambda w, dx, dy: view.scroll(-dy * 40))
        # GLFW standard cursors, created lazily per shape and cached for the
        # life of this window -- `set_cursor` is cheap, but there is no
        # reason to re-create the same cursor object on every mouse-move
        # event. Kept local to `run()` (not module-level) since GLFW tears
        # every cursor down with the window/`glfw.terminate()`.
        cursors = {}
        cursor_shapes = {
            'pointer': glfw.HAND_CURSOR,
            'text': glfw.IBEAM_CURSOR,
            'arrow': glfw.ARROW_CURSOR,
        }
        last_cursor_kind = [None]

        def cursor_pos(_win, x, y):
            kind = view.cursor_kind(x, y)
            if kind == last_cursor_kind[0]:
                return
            last_cursor_kind[0] = kind
            cursor = cursors.get(kind)
            if cursor is None:
                cursor = cursors[kind] = glfw.create_standard_cursor(cursor_shapes[kind])
            glfw.set_cursor(win, cursor)
        glfw.set_cursor_pos_callback(win, cursor_pos)
        # Do not perform a potentially expensive CSS/layout pass inside the
        # platform callback. The loop below reads GLFW's authoritative final
        # logical size after each event batch and reflows once for that size.
        glfw.set_window_size_callback(win, lambda *args: setattr(view, 'dirty', True))
        glfw.set_framebuffer_size_callback(win, lambda *args: setattr(view, 'dirty', True))
        glfw.set_window_refresh_callback(win, lambda w: setattr(view, 'dirty', True))
        drawn = 0
        current_title = title
        while not glfw.window_should_close(win):
            sync_window_size(view, win, glfw)
            navigation.poll()
            wanted_title = view.page_title or title
            if wanted_title != current_title:
                glfw.set_window_title(win, wanted_title)
                current_title = wanted_title
            view.poll_images()
            if view.loading:
                # Keep the loading spinner animating for the whole time a
                # navigation is in flight -- without this, `view.dirty` only
                # ever flips true once (when the request starts) and the
                # window shows one static "Loading ..." frame and otherwise
                # doesn't repaint at all until the page lands, indistinguishable
                # from having actually frozen for however long that takes.
                view.dirty = True
            if view.dirty or frames is not None:
                renderer.draw(view, glfw.get_framebuffer_size(win))
                glfw.swap_buffers(win)
                drawn += 1
                from . import webfonts
                reg = webfonts.registry(view.page.document.body) if view.page is not None else None
                if frames is not None and drawn >= frames and navigation.pending is None and not (reg and reg.pending()):
                    break
            eager = (
                navigation.pending is not None
                or frames is not None
                or browser_images.has_pending()
                or browser_images.has_active_animations()
            )
            glfw.wait_events_timeout(.02 if eager else .25)
        return view
    finally:
        if navigation is not None:
            navigation.close()
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()
