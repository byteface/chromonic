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

# Image pixels can paint as soon as they arrive, but intrinsic dimensions may
# still require geometry to settle. Coalesce those expensive Taffy passes for
# this long after the latest resource arrival. Deferred work also has a hard
# maximum latency so a page streaming resources continuously still gets
# periodic geometry updates rather than postponing layout forever.
_IMAGE_RELAYOUT_INTERVAL = 0.2
_MAX_DEFERRED_LAYOUT_LATENCY = 0.5


def _clipboard_text(glfw_module, window):
    """Return GLFW clipboard contents as text across glfw Python versions."""
    value = glfw_module.get_clipboard_string(window) or ''
    return value.decode('utf-8') if isinstance(value, bytes) else value


#: Treated as boundaries alongside whitespace for `_word_boundary` -- an
#: address bar's own content is almost always a URL, not prose, so
#: stopping only at spaces would make Option+Left jump the entire
#: `https://example.com/some/path` in one leap (no spaces in it at all).
#: Splitting on URL structure too (matching what a real mac text field's
#: Option+Left/Right visibly does inside an address bar) gives one stop
#: per path segment/host label/query pair instead.
_WORD_BOUNDARY_CHARS = frozenset("/.:?&=-_")


def _word_boundary(text: str, pos: int, direction: int) -> int:
    """The next word boundary in `text` from `pos`, matching macOS's own
    Option+Left/Right ("move by word") behaviour: skip any run of
    boundary characters adjoining `pos` first, then the run of non-
    boundary characters beyond it -- so from the middle of a word,
    `direction=-1` lands on that word's own start (not the previous
    word's), and from a run of trailing boundary characters, `direction=1`
    skips them *and* the next word."""
    def is_boundary(ch: str) -> bool:
        return ch.isspace() or ch in _WORD_BOUNDARY_CHARS

    n = len(text)
    if direction < 0:
        i = pos
        while i > 0 and is_boundary(text[i - 1]):
            i -= 1
        while i > 0 and not is_boundary(text[i - 1]):
            i -= 1
        return i
    i = pos
    while i < n and is_boundary(text[i]):
        i += 1
    while i < n and not is_boundary(text[i]):
        i += 1
    return i


def _ancestor(element, predicate):
    """Return the first element in ``element``'s ancestor chain matching predicate."""
    while element is not None:
        if predicate(element):
            return element
        element = getattr(element, 'parentElement', None)
    return None


def sync_window_size(view, window, glfw_module):
    """Reflow to GLFW's settled logical size after processing window events."""
    logical_size = glfw_module.get_window_size(window)
    if logical_size == (view.width, view.height):
        return False
    # During a live window drag, don't run a full CSS+Taffy pass for every
    # intermediate size. Paint immediately and settle geometry shortly after.
    view.resize(*logical_size, defer=True)
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
        # `None` means no selection; an int is the *other* end of the
        # selected range from `self.caret` (mac-style: a plain arrow key
        # collapses to one edge, Shift+arrow moves `caret` and keeps
        # `select_anchor` fixed, extending or shrinking the range).
        self.select_anchor = None
        self.caret = 0
        self.focused_element = None
        self.input_caret = 0
        self.navigation_handler = None
        self.status = ''
        self.loading = False
        self.dirty = True
        self._image_generation = 0
        self._last_image_relayout = 0.0
        self._page_image_urls = set()
        self._deferred_layout_at = None
        self._deferred_layout_first_at = None
        self._deferred_layout_reuse_styles = True
        self.display_list = []
        self.layout_projection = tree.LayoutProjection()
        self.last_painted_elements = 0

        # Lightweight built-in profiler. F10 toggles the overlay; keeping the
        # counters here means the browser can tell us *why* it feels slow
        # without needing an external profiler for every iteration.
        self.perf_open = False
        self.layout_count = 0
        self.last_layout_ms = 0.0
        self.avg_layout_ms = 0.0
        self.last_display_list_ms = 0.0
        self.last_frame_ms = 0.0
        self.avg_frame_ms = 0.0
        self.last_navigation_ms = 0.0
        self.dom_element_count = 0
        self.image_paint_only_count = 0
        self.image_relayout_count = 0

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
        #
        # `myjs.Page(...)` itself defaults `.url` to the literal string
        # `"about:blank"` whenever nothing sets it explicitly -- true for
        # every `loader(url)` that builds a `Page` straight from an HTML
        # string (real pages included, not just tests: nothing about
        # `browser.load()`'s own result is required to set `.url` either).
        # Unconditionally preferring `page.url` treated that placeholder
        # as if it were a real resolved destination, permanently stranding
        # `self.url`/history at `"about:blank"` on every navigation.
        page_url = getattr(page, 'url', None)
        if page_url and page_url != 'about:blank':
            url = page_url
        self.page, self.url, self.address = page, url, url
        titles = page.document.getElementsByTagName("title")
        self.page_title = (titles[0].textContent or "").strip() if titles else ""
        self.caret = len(url)
        if back:
            self.history.pop()
        else:
            self.history.append(url)
        self.scroll_y = 0
        self.editing = False
        self.select_anchor = None
        self.status = ''
        self.loading = False
        # Events completed before this point are already visible to the initial
        # display-list/layout pass.  Start this view's event cursor here so old
        # pages/resources do not cause a redundant repaint immediately after
        # navigation commits.
        self._image_generation = browser_images.generation()
        self.relayout()
        return True

    def back(self):
        if len(self.history) > 1:
            return self.navigate(self.history[-2], back=True)
        return False

    @staticmethod
    def _ema(previous, value, alpha=0.2):
        return value if previous <= 0 else previous + alpha * (value - previous)

    def rebuild_display_list(self):
        """Rebuild paint commands without touching CSS or geometry."""
        if self.page is None:
            self.dirty = True
            return
        started = time.perf_counter()
        self.display_list = paint.build_display_list(self.page.document.body)
        self._refresh_page_image_urls()
        self.last_display_list_ms = (time.perf_counter() - started) * 1000.0
        self.dirty = True

    def request_relayout(self, *, delay=0.0, reuse_styles=True):
        """Coalesce expensive layouts and run one after a short idle window.

        Repeated calls push the deadline out, which is ideal for typing and
        resource bursts: paint can update immediately while geometry catches
        up once the burst settles.
        """
        now = time.monotonic()
        if self._deferred_layout_first_at is None:
            self._deferred_layout_first_at = now
        deadline = min(
            now + max(0.0, delay),
            self._deferred_layout_first_at + _MAX_DEFERRED_LAYOUT_LATENCY,
        )
        if self._deferred_layout_at is None or deadline > self._deferred_layout_at:
            self._deferred_layout_at = deadline
        # A single caller requiring fresh styles makes the eventual layout full.
        self._deferred_layout_reuse_styles = (
            self._deferred_layout_reuse_styles and reuse_styles
        )

    def poll_deferred_work(self):
        deadline = self._deferred_layout_at
        if deadline is None or time.monotonic() < deadline:
            return False
        reuse_styles = self._deferred_layout_reuse_styles
        self._deferred_layout_at = None
        self._deferred_layout_first_at = None
        self._deferred_layout_reuse_styles = True
        self.relayout(reuse_styles=reuse_styles)
        return True

    def _refresh_page_image_urls(self):
        if self.page is None:
            self._page_image_urls = set()
            return self._page_image_urls
        self._page_image_urls = {
            src
            for image in self.page.document.getElementsByTagName('img')
            if (src := image.getAttribute('src'))
        }
        return self._page_image_urls

    @staticmethod
    def _image_has_fixed_geometry(image):
        """Whether intrinsic pixels cannot change this image's layout box."""
        width = image.getAttribute('width')
        height = image.getAttribute('height')
        if width and height:
            return True

        # Prefer resolved/computed style when tree.py has already attached it;
        # this catches dimensions supplied by stylesheets, not only inline CSS.
        style = image.__dict__.get('_chromonic_paint_style', {}) or {}

        def fixed(value):
            if value is None:
                return False
            return str(value).strip().lower() not in ('', 'auto', 'initial', 'inherit', 'unset')

        if fixed(style.get('width')) and fixed(style.get('height')):
            return True

        inline = (image.getAttribute('style') or '').lower().replace(' ', '')
        declarations = {}
        for declaration in inline.split(';'):
            if ':' in declaration:
                key, value = declaration.split(':', 1)
                declarations[key] = value
        return fixed(declarations.get('width')) and fixed(declarations.get('height'))

    def _images_need_intrinsic_layout(self, changed_urls):
        """Check only images that actually completed, not every image on page."""
        if self.page is None:
            return False
        changed = set(changed_urls)
        if not changed:
            return False
        for image in self.page.document.getElementsByTagName('img'):
            if image.getAttribute('src') not in changed:
                continue
            if not self._image_has_fixed_geometry(image):
                return True
        return False

    def relayout(self, *, reuse_styles: bool = False):
        """Recompute document geometry and rebuild the display list.

        ``reuse_styles`` is reserved for changes that cannot affect selector
        matching or computed CSS (currently resource/font arrival). In that
        path we deliberately retain domonic's CSSOM rule index as well as the
        per-element resolved-style cache. Resize, DOM events, and navigation
        use the default full style pass because they may affect media queries,
        classes, inline styles, or stylesheets.
        """
        if self.page is None:
            self.dirty = True
            return

        started = time.perf_counter()
        browser.set_viewport(self.page, self.width, self.viewport_height)
        doc = self.page.document

        # Invalidating this index on the fast path defeats a meaningful part
        # of style reuse. Only a full style pass needs a fresh selector index.
        if not reuse_styles and hasattr(doc, '_cssom_rule_index'):
            doc._cssom_rule_index = None

        nodes = self.layout_projection.layout(
            doc.body,
            width=self.width,
            height=None,
            reuse_styles=reuse_styles,
            viewport_height=self.viewport_height,
        )
        self.content_height = max(
            (
                max(
                    box.y + box.height,
                    float(element.__dict__.get('_chromonic_scroll_extent', 0.0)),
                )
                for element in nodes.values()
                if (box := element.__dict__.get('_layout_box')) is not None
            ),
            default=0.0,
        )
        display_started = time.perf_counter()
        self.display_list = paint.build_display_list(doc.body)
        self._refresh_page_image_urls()
        self.last_display_list_ms = (time.perf_counter() - display_started) * 1000.0
        max_scroll = max(0.0, self.content_height - self.viewport_height)
        self.scroll_y = min(self.scroll_y, max_scroll)
        # `nodes` already represents the elements participating in layout;
        # don't walk the DOM again just to feed the profiler.
        self.dom_element_count = len(nodes)
        self.last_layout_ms = (time.perf_counter() - started) * 1000.0
        self.avg_layout_ms = self._ema(self.avg_layout_ms, self.last_layout_ms)
        self.layout_count += 1
        self.dirty = True

    def poll_images(self):
        """Integrate resource completions with precise per-page invalidation.

        ``browser_images`` now keeps generation-stamped completion events.  We
        filter them against this document, repaint only for successful images
        the page actually references, and ask for Taffy only when *those exact
        images* rely on intrinsic geometry.  Completions from a previous page
        become harmless cache warm-ups instead of invalidating this one.
        """
        from . import webfonts

        if self.page is not None:
            reg = webfonts.registry(self.page.document.body)
            if reg is not None and reg.poll():
                self.rebuild_display_list()
                self.request_relayout(delay=0.05, reuse_styles=True)

        page_urls = self._page_image_urls
        if browser_images.advance_animations(page_urls):
            self.dirty = True

        current, events = browser_images.events_since(self._image_generation)
        if current == self._image_generation:
            return
        self._image_generation = current

        if not events or not page_urls:
            return
        relevant = [event for event in events if event.url in page_urls and event.success]
        if not relevant:
            return

        # Pixels are ready: make them visible now.  Layout is a separate,
        # conditional decision below.
        self.rebuild_display_list()
        changed_urls = {event.url for event in relevant}

        if not self._images_need_intrinsic_layout(changed_urls):
            self.image_paint_only_count += len(relevant)
            return

        # Only intrinsic-size-dependent arrivals get Taffy, and an arrival burst
        # is still coalesced into one style-reusing pass.
        self.image_relayout_count += 1
        self._last_image_relayout = time.monotonic()
        self.request_relayout(delay=_IMAGE_RELAYOUT_INTERVAL, reuse_styles=True)

    def resize(self, width, height, *, defer=False):
        if width > 0 and height > 0 and (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            if defer:
                self.dirty = True
                self.request_relayout(delay=0.06, reuse_styles=False)
            else:
                self.relayout()

    def scroll(self, delta):
        self.scroll_y = max(0, min(self.scroll_y + delta,
                                  max(0, self.content_height - self.viewport_height)))
        self.dirty = True  # scrolling only repaints; it never relayouts

    @staticmethod
    def _editable_ancestor(element):
        return _ancestor(
            element,
            lambda node: str(getattr(node, 'tagName', '')).lower() in ('input', 'textarea'),
        )

    @staticmethod
    def _form_ancestor(element):
        return _ancestor(
            element,
            lambda node: str(getattr(node, 'tagName', '')).lower() == 'form',
        )

    @staticmethod
    def _anchor_ancestor(element):
        return _ancestor(
            element,
            lambda node: str(getattr(node, 'tagName', '')).lower() == 'a',
        )

    @classmethod
    def _submit_control_ancestor(cls, element):
        return _ancestor(element, cls._is_submit_control)

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

    def submit_form(self, form, *, submitter=None):
        """Submit a form through the browser session.

        The clicked submit control may override the form action/method and is
        included in successful controls when it has a name, matching normal
        browser submission more closely than treating every submit as an
        anonymous Enter-key submission.
        """
        method = (form.getAttribute('method') or 'get').strip().lower()
        action = form.getAttribute('action') or ''
        if submitter is not None:
            method = (submitter.getAttribute('formmethod') or method).strip().lower()
            action = submitter.getAttribute('formaction') or action

        url = urllib.parse.urljoin(self.url, action)
        pairs = self._collect_form_data(form)
        if submitter is not None:
            name = submitter.getAttribute('name')
            if name:
                pairs.append((name, submitter.getAttribute('value') or ''))

        if method == 'post':
            return self.navigate(url, method='POST', data=pairs)

        query = urllib.parse.urlencode(pairs)
        parts = urllib.parse.urlsplit(url)
        # GET form submission replaces the action's query and preserves its
        # fragment, rather than accidentally appending '?x=y' after '#frag'.
        target = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )
        return self.navigate(target)

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

        self.editing = False
        self.select_anchor = None
        if self.page is None:
            return

        document_y = y - TOOLBAR + self.scroll_y
        element = hittest.hit_test(self.page.document.body, x, document_y)
        field = self._editable_ancestor(element)
        self.focused_element = field
        if field is not None:
            self.input_caret = len(str(getattr(field, 'value', '') or ''))

        # Dispatch the event before its default browser action. This lets link
        # and button handlers actually run, and honours preventDefault when the
        # domonic event implementation exposes it.
        event = None
        if element is not None:
            from domonic.events import MouseEvent
            event = MouseEvent('click', {'bubbles': True, 'clientX': x, 'clientY': y - TOOLBAR})
            element.dispatchEvent(event)
        if getattr(event, 'defaultPrevented', False):
            self.relayout()
            return

        anchor = self._anchor_ancestor(element)
        if anchor is not None:
            href = anchor.getAttribute('href')
            if href:
                self.focused_element = None
                resolved = urllib.parse.urljoin(self.url, href)
                current_base = urllib.parse.urldefrag(self.url)[0]
                resolved_base, fragment = urllib.parse.urldefrag(resolved)
                if resolved_base == current_base and fragment:
                    target = self.page.document.getElementById(urllib.parse.unquote(fragment))
                    if target is not None and target.get_layout_box() is not None:
                        self.scroll(target.get_layout_box().y - self.scroll_y)
                    return
                self.navigate(resolved)
                return

        submitter = self._submit_control_ancestor(element)
        if submitter is not None:
            form = self._form_ancestor(submitter)
            if form is not None:
                self.focused_element = None
                self.submit_form(form, submitter=submitter)
                return

        if element is not None:
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
        self.editing = True
        self.select_anchor = 0
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
        # Keep typing responsive on large documents. The control's text can be
        # repainted immediately; any script/style-driven geometry changes are
        # coalesced into one full layout once typing pauses briefly.
        self.rebuild_display_list()
        self.request_relayout(delay=0.12, reuse_styles=False)

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
            self.rebuild_display_list()
            self.request_relayout(delay=0.12, reuse_styles=False)
        else:
            self.dirty = True

    def _selection_range(self):
        """`(start, end)` of the address bar's current selection, or
        `None` -- `select_anchor` is the fixed end, `caret` the other
        (mac-style: which one moves depends on which key last set it)."""
        if self.select_anchor is None or self.select_anchor == self.caret:
            return None
        return (min(self.select_anchor, self.caret), max(self.select_anchor, self.caret))

    def type_text(self, text):
        if not self.editing:
            return
        text = ''.join(c for c in text if c.isprintable())
        selection = self._selection_range()
        if selection is not None:
            start, end = selection
            self.address = self.address[:start] + self.address[end:]
            self.caret = start
        self.address = self.address[:self.caret] + text + self.address[self.caret:]
        self.caret += len(text)
        self.select_anchor = None
        self.dirty = True

    def edit_key(self, key, *, word=False, extend=False):
        """`word` is macOS's Option modifier (jump/delete by word instead
        of by character); `extend` is Shift (move `caret` while keeping
        `select_anchor` fixed, extending or shrinking the selection,
        instead of collapsing to one edge)."""
        if not self.editing:
            return
        selection = self._selection_range()
        if key in ('backspace', 'delete'):
            if selection is not None:
                start, end = selection
                self.address = self.address[:start] + self.address[end:]
                self.caret = start
            elif key == 'backspace' and self.caret:
                start = _word_boundary(self.address, self.caret, -1) if word else self.caret - 1
                self.address = self.address[:start] + self.address[self.caret:]
                self.caret = start
            elif key == 'delete':
                end = _word_boundary(self.address, self.caret, 1) if word else self.caret + 1
                self.address = self.address[:self.caret] + self.address[end:]
            self.select_anchor = None
        elif key in ('left', 'right', 'home', 'end'):
            if key == 'home':
                new_caret = 0
            elif key == 'end':
                new_caret = len(self.address)
            elif word:
                new_caret = _word_boundary(self.address, self.caret, -1 if key == 'left' else 1)
            elif not extend and selection is not None:
                # A plain (non-extending) arrow with an existing selection
                # collapses to whichever edge is in that direction, same
                # as every other mac-style text field -- not a single
                # character step from the caret's own current side.
                new_caret = selection[0] if key == 'left' else selection[1]
            else:
                new_caret = max(0, self.caret - 1) if key == 'left' else min(len(self.address), self.caret + 1)
            if extend:
                if self.select_anchor is None:
                    self.select_anchor = self.caret
            else:
                self.select_anchor = None
            self.caret = new_caret
        elif key == 'escape':
            self.address, self.caret = self.url, len(self.url)
            self.select_anchor = None
            self.editing = False
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

    def toggle_perf(self):
        self.perf_open = not self.perf_open
        self.dirty = True

    def record_frame_time(self, elapsed_ms):
        self.last_frame_ms = elapsed_ms
        self.avg_frame_ms = self._ema(self.avg_frame_ms, elapsed_ms, alpha=0.12)

    def draw_scroll_indicator(self, canvas):
        if self.content_height <= self.viewport_height or self.page is None:
            return
        track_height = max(1.0, self.viewport_height - 8.0)
        thumb_height = max(24.0, track_height * self.viewport_height / self.content_height)
        max_scroll = max(1.0, self.content_height - self.viewport_height)
        travel = max(0.0, track_height - thumb_height)
        thumb_y = TOOLBAR + 4.0 + travel * (self.scroll_y / max_scroll)
        canvas.drawRoundRect(
            skia.Rect.MakeXYWH(self.width - 7.0, thumb_y, 4.0, thumb_height),
            2.0, 2.0, skia.Paint(Color=0x77909090, AntiAlias=True),
        )

    def draw_perf_hud(self, canvas):
        if not self.perf_open:
            return
        font = paint._font(12, family='monospace')
        image_stats = browser_images.cache_info()
        cache_mb = image_stats['cache_bytes'] / (1024 * 1024)
        network_mb = image_stats['network_bytes'] / (1024 * 1024)
        lines = [
            f'frame {self.last_frame_ms:6.1f} ms  avg {self.avg_frame_ms:6.1f} ms',
            f'layout {self.last_layout_ms:5.1f} ms  avg {self.avg_layout_ms:5.1f}  x{self.layout_count}',
            f'display-list {self.last_display_list_ms:5.1f} ms',
            f'DOM {self.dom_element_count}  painted {self.last_painted_elements}',
            f'images pending={image_stats["pending"]} cache={image_stats["entries"]} ({cache_mb:.1f} MB) hits={image_stats["cache_hits"]}',
            f'image I/O {network_mb:.1f} MB  fetch={image_stats["avg_fetch_ms"]:.1f} ms  decode={image_stats["avg_decode_ms"]:.1f} ms',
            f'image invalidation paint-only={self.image_paint_only_count} relayout-batches={self.image_relayout_count}',
            f'workers fetch={image_stats["fetch_workers"]} decode={image_stats["decode_workers"]}  evictions={image_stats["evictions"]}',
            f'navigation {self.last_navigation_ms:5.1f} ms',
        ]
        width = min(self.width - 12, max(font.measureText(line) for line in lines) + 16)
        height = len(lines) * 17 + 12
        x = 6
        y = self.height - height - 6
        background = skia.Paint(Color=0xdd111111)
        foreground = skia.Paint(Color=0xff7CFC00, AntiAlias=True)
        canvas.drawRoundRect(skia.Rect.MakeXYWH(x, y, width, height), 5, 5, background)
        baseline = y + 18
        for line in lines:
            canvas.drawString(line, x + 8, baseline, font, foreground)
            baseline += 17

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
                        skia.Paint(Color=skia.ColorWHITE))
        font = paint._font(15)
        ink = skia.Paint(Color=skia.ColorBLACK, AntiAlias=True)
        canvas.drawString('Back', 8, 27, font, ink)
        canvas.drawString('Go', self.width - 44, 27, font, ink)
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(65, 6, max(1, self.width - 130), 32))
        caret_width = font.measureText(self.address[:self.caret])
        offset = max(0, caret_width - max(1, self.width - 142)) if self.editing else 0
        selection = self._selection_range() if self.editing else None
        if selection is not None:
            start, end = selection
            start_x = 68 - offset + font.measureText(self.address[:start])
            end_x = 68 - offset + font.measureText(self.address[:end])
            canvas.drawRect(
                skia.Rect.MakeLTRB(start_x, 8, end_x, 30),
                skia.Paint(Color=0xffbfdbfe),
            )
        canvas.drawString(self.address, 68 - offset, 27, font, ink)
        if self.editing and selection is None:
            x = 68 + caret_width - offset
            canvas.drawLine(x, 12, x, 31, ink)
        canvas.restore()
        if self.status:
            bar_color = 0xffe0edff if self.loading else 0xffffdddd
            canvas.drawRect(skia.Rect.MakeXYWH(0, TOOLBAR, self.width, 30), skia.Paint(Color=bar_color))
            canvas.drawString(self.status, 8, TOOLBAR + 21, font, ink)
            if self.loading:
                self.draw_loading_spinner(canvas, self.width - 18, TOOLBAR + 15)
        self.draw_scroll_indicator(canvas)
        if self.console_open:
            self.draw_console(canvas)
        self.draw_perf_hud(canvas)
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
        frame_started = time.perf_counter()
        canvas = self.surface.getCanvas()
        canvas.save()
        # GLFW input/layout are logical units; the framebuffer uses physical
        # pixels. Use actual dimensions, not an assumed Retina scale of two.
        canvas.scale(width / view.width, height / view.height)
        view.draw(canvas)
        canvas.restore()
        self.context.flushAndSubmit()
        view.record_frame_time((time.perf_counter() - frame_started) * 1000.0)

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
        self.pending = (self.executor.submit(loader), url, back, time.perf_counter())
        self.view.address = url
        self.view.caret = len(url)
        self.view.status = 'Loading ' + url
        self.view.loading = True
        self.view.dirty = True
        return True

    def poll(self):
        if self.pending is None or not self.pending[0].done():
            return
        future, url, back, started = self.pending
        self.pending = None
        self.view.last_navigation_ms = (time.perf_counter() - started) * 1000.0
        try:
            page = future.result()
        except Exception as error:
            self.view.status = str(error)
            self.view.loading = False
            self.view.dirty = True
        else:
            # Do not discard text typed while a request was in flight.
            edit = (self.view.address, self.view.caret, self.view.select_anchor) if self.view.editing else None
            self.view.commit_page(page, url, back=back)
            if edit is not None:
                self.view.address, self.view.caret, self.view.select_anchor = edit
                self.view.editing = True

    def close(self):
        self.view.navigation_handler = None
        self.executor.shutdown(wait=False, cancel_futures=True)


class WindowInput:
    """Translate GLFW callbacks into high-level ``View`` operations.

    Keeping this out of ``run()`` makes the event policy independently
    readable/testable and leaves the application loop responsible only for
    lifecycle, polling, and rendering.
    """

    def __init__(self, glfw_module, window, view):
        self.glfw = glfw_module
        self.window = window
        self.view = view
        self._cursors = {}
        self._last_cursor_kind = None
        self._cursor_shapes = {
            'pointer': self.glfw.HAND_CURSOR,
            'text': self.glfw.IBEAM_CURSOR,
            'arrow': self.glfw.ARROW_CURSOR,
        }

    def install(self):
        g = self.glfw
        w = self.window
        g.set_key_callback(w, self.on_key)
        g.set_char_callback(w, self.on_char)
        g.set_mouse_button_callback(w, self.on_mouse_button)
        g.set_scroll_callback(w, self.on_scroll)
        g.set_cursor_pos_callback(w, self.on_cursor_pos)
        # Resizing is coalesced in the main loop; callbacks only request paint.
        g.set_window_size_callback(w, self._mark_dirty)
        g.set_framebuffer_size_callback(w, self._mark_dirty)
        g.set_window_refresh_callback(w, self._mark_dirty)

    def close(self):
        destroy = getattr(self.glfw, 'destroy_cursor', None)
        if destroy is not None:
            for cursor in self._cursors.values():
                destroy(cursor)
        self._cursors.clear()

    def _mark_dirty(self, *_args):
        self.view.dirty = True

    def _command_pressed(self, mods):
        return bool(mods & (self.glfw.MOD_CONTROL | self.glfw.MOD_SUPER))

    def _edit_key_name(self, key, *, include_enter=False):
        g = self.glfw
        mapping = {
            g.KEY_BACKSPACE: 'backspace',
            g.KEY_DELETE: 'delete',
            g.KEY_LEFT: 'left',
            g.KEY_RIGHT: 'right',
            g.KEY_HOME: 'home',
            g.KEY_END: 'end',
        }
        if include_enter:
            mapping[g.KEY_ENTER] = 'enter'
        return mapping.get(key)

    def on_key(self, _window, key, _scancode, action, mods):
        g = self.glfw
        if action not in (g.PRESS, g.REPEAT):
            return

        command = self._command_pressed(mods)
        reload_key = (command and key == g.KEY_R) or key == g.KEY_F5
        # Option+Left is also macOS's "move/select by word" modifier in a
        # text field -- while the address bar is being edited, that meaning
        # wins; Option+Left only means "back" the rest of the time.
        back_key = not self.view.editing and (
            ((mods & g.MOD_ALT) and key == g.KEY_LEFT)
            or ((mods & g.MOD_SUPER) and key == g.KEY_LEFT_BRACKET)
        )

        if key == g.KEY_F10:
            self.view.toggle_perf()
        elif key == g.KEY_F12:
            self.view.toggle_console()
        elif self.view.console_open:
            self._console_key(key, command)
        elif command and key == g.KEY_L:
            self.view.focus_address()
        elif reload_key:
            if self.view.url:
                self.view.navigate(self.view.url)
        elif back_key:
            self.view.back()
        elif self.view.editing:
            self._address_key(key, command, mods)
        elif self.view.focused_element is not None:
            self._field_key(key, command)
        elif key in (g.KEY_DOWN, g.KEY_UP, g.KEY_PAGE_DOWN, g.KEY_PAGE_UP):
            page = key in (g.KEY_PAGE_DOWN, g.KEY_PAGE_UP)
            amount = self.view.viewport_height * 0.9 if page else 40
            direction = 1 if key in (g.KEY_DOWN, g.KEY_PAGE_DOWN) else -1
            self.view.scroll(direction * amount)
        elif key == g.KEY_SPACE:
            direction = -1 if (mods & g.MOD_SHIFT) else 1
            self.view.scroll(direction * self.view.viewport_height * 0.9)
        elif key == g.KEY_HOME:
            self.view.scroll(-self.view.scroll_y)
        elif key == g.KEY_END:
            self.view.scroll(self.view.content_height)

        self.view.dirty = True

    def _console_key(self, key, command):
        g = self.glfw
        edit_key = self._edit_key_name(key)
        if key == g.KEY_ENTER:
            self.view.console_submit()
        elif edit_key:
            self.view.console_edit_key(edit_key)
        elif command and key == g.KEY_V:
            self.view.console_type_text(_clipboard_text(g, self.window))
        elif key == g.KEY_ESCAPE:
            self.view.toggle_console()

    def _address_key(self, key, command, mods):
        g = self.glfw
        edit_key = self._edit_key_name(key)
        if key == g.KEY_ENTER:
            self.view.navigate(self.view.address)
        elif edit_key:
            # macOS: Option ("Alt") moves/deletes by word instead of by
            # character; Shift extends the selection instead of just
            # moving the caret. Both combine (Option+Shift+Left extends
            # by a whole word), matching every native mac text field.
            self.view.edit_key(
                edit_key,
                word=bool(mods & g.MOD_ALT),
                extend=bool(mods & g.MOD_SHIFT),
            )
        elif command and key == g.KEY_A:
            self.view.select_anchor = 0
            self.view.caret = len(self.view.address)
        elif command and key == g.KEY_V:
            self.view.type_text(_clipboard_text(g, self.window))
        elif key == g.KEY_ESCAPE:
            self.view.edit_key('escape')

    def _field_key(self, key, command):
        g = self.glfw
        edit_key = self._edit_key_name(key, include_enter=True)
        if edit_key:
            self.view.input_edit_key(edit_key)
        elif command and key == g.KEY_V:
            self.view.input_type_text(_clipboard_text(g, self.window))
        elif key == g.KEY_ESCAPE:
            self.view.focused_element = None

    def on_char(self, _window, codepoint):
        char = chr(codepoint)
        if self.view.console_open:
            self.view.console_type_text(char)
        elif self.view.focused_element is not None:
            self.view.input_type_text(char)
        else:
            self.view.type_text(char)

    def on_mouse_button(self, _window, button, action, _mods):
        g = self.glfw
        if button == g.MOUSE_BUTTON_LEFT and action == g.PRESS:
            self.view.click(*g.get_cursor_pos(self.window))

    def on_scroll(self, _window, _dx, dy):
        self.view.scroll(-dy * 40)

    def on_cursor_pos(self, _window, x, y):
        kind = self.view.cursor_kind(x, y)
        if kind == self._last_cursor_kind:
            return
        self._last_cursor_kind = kind
        cursor = self._cursors.get(kind)
        if cursor is None:
            cursor = self.glfw.create_standard_cursor(self._cursor_shapes[kind])
            self._cursors[kind] = cursor
        self.glfw.set_cursor(self.window, cursor)


def run(url='https://google.com/', *, width=1000, height=800, title='chromonic — direct Skia v3', frames=None):
    """Run the native browser window on the calling thread.

    ``frames`` bounds deterministic display/smoke tests. Network fetches stay
    off the UI thread, while DOM commit, layout, event dispatch, and GPU work
    remain on it.
    """
    import glfw

    if not glfw.init():
        raise RuntimeError('GLFW could not initialize a display')

    win = renderer = navigation = input_controller = None
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
        input_controller = WindowInput(glfw, win, view)
        input_controller.install()
        view.navigate(url)

        drawn = 0
        current_title = title

        while not glfw.window_should_close(win):
            sync_window_size(view, win, glfw)
            navigation.poll()
            view.poll_images()
            view.poll_deferred_work()

            wanted_title = view.page_title or title
            if wanted_title != current_title:
                glfw.set_window_title(win, wanted_title)
                current_title = wanted_title

            # Loading is the only continuous toolbar animation. Everything
            # else is demand-driven and can sleep for longer between events.
            if view.loading:
                view.dirty = True

            if view.dirty or frames is not None:
                renderer.draw(view, glfw.get_framebuffer_size(win))
                glfw.swap_buffers(win)
                drawn += 1

                from . import webfonts
                registry = webfonts.registry(view.page.document.body) if view.page is not None else None
                if (
                    frames is not None
                    and drawn >= frames
                    and navigation.pending is None
                    and not (registry and registry.pending())
                ):
                    break

            eager = (
                navigation.pending is not None
                or frames is not None
                or browser_images.has_pending(view._page_image_urls)
                or browser_images.has_active_animations(view._page_image_urls)
                or view._deferred_layout_at is not None
            )
            glfw.wait_events_timeout(0.02 if eager else 0.25)

        return view
    finally:
        if input_controller is not None:
            input_controller.close()
        if navigation is not None:
            navigation.close()
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()

