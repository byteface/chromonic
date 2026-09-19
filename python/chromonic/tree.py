"""Walk a live domonic DOM, build a mirroring Taffy tree, run layout, and
write the resulting geometry straight back onto the domonic elements via
`element.set_layout_box(...)` -- the domonic 1.8.0 API that
`getBoundingClientRect`/`clientWidth`/`offsetWidth`/etc. already consult.

Node identity: a plain `dict[int (Taffy node id), Element]` built while
walking (see PLAN.md). Invalidation: POC-scope means "rebuild the whole
thing" -- `layout()` below is meant to be called again, in full, after any
mutation. That is the entire "invalidation strategy": there isn't a dirty-bit
system here, on purpose (see PLAN.md).

Performance note (found stress-testing `examples/particles.py`): computing
an element's cascaded style is the dominant cost of a full relayout, by a
wide margin over Taffy itself -- profiling a few hundred moving elements
showed most wall-clock time inside domonic's `ComputedStyleDeclaration`
construction and CSS-text (re)parsing, not in the Rust layout call. This
file used to ask for that twice per element (once to check `display` before
deciding whether to descend, once more to build the Taffy style dict), and
`paint.py` asked a third time to read paint-only properties (background/
border/text colour). All three now share exactly **one**
`ComputedStyleDeclaration` per element per pass (see `_describe` below) --
built by reaching into `domonic.layout.LayoutStyle._from_computed` directly
(the one actual implementation behind the public `layout_style()`, which
otherwise always builds its own fresh `ComputedStyleDeclaration` internally
and gives a caller no way to reuse one it already has). That reach into a
non-public method is exactly the kind of gap this repo logs as a domonic
wrinkle -- see `docs/domonic-wrinkles.md` #13: a `layout_style(element,
computed=...)` overload, or a `LayoutStyle.from_computed` public alias,
would let a caller like this drop the workaround entirely.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import math

import skia

from domonic import _fontmetrics
from domonic import bs4 as domonic_bs4
from domonic.dom import Element
from domonic.layout import LayoutBox, LayoutStyle
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from . import fonts, style_bridge, ua_style
from ._native import Tree, layout_text

# `Utils.case_kebab` (camelCase/snake_case -> kebab-case, via two regex
# substitutions) backs every `ComputedStyleDeclaration` property read
# (`getPropertyValue` -> `_css_property_name` -> `case_kebab`), and is a
# pure function of its input string -- the *same* handful of property names
# ("backgroundColor", "fontSize", ...) get re-run through those regexes on
# every single access, for every element, every relayout. Profiling
# `examples/particles.py` at 1,000 particles found this the single largest
# cost after `ComputedStyleDeclaration` construction itself: 390k calls,
# ~2s cumulative out of a ~6.7s/10-tick run. Memoizing it here (once, at
# import time, process-wide) cut a 1,000-particle `tick()` from ~194ms to
# ~126ms -- measured, not assumed. Not a `tree.py`-only fix: this speeds up
# every `ComputedStyleDeclaration` property access anywhere in the process.
# Safe because `case_kebab` has no side effects and is a pure string
# transform -- same input always gives the same output. Worked around here
# rather than patched in domonic's own source, per this repo's convention
# of stress-testing domonic rather than editing its tree directly.
Utils.case_kebab = staticmethod(functools.lru_cache(maxsize=2048)(Utils.case_kebab))

# A stylesheet's selectors are tested against many elements, but domonic's
# matcher parses their text again for every test.  These parsers are pure: the
# returned structures describe selector text and the matchers only read them.
# Cache them at the renderer boundary so a page pays the parse cost once per
# distinct selector rather than once per candidate element.
Element._parse_simple_selector = staticmethod(
    functools.lru_cache(maxsize=8192)(Element._parse_simple_selector)
)
domonic_bs4._split_simple_selector_chain = functools.lru_cache(maxsize=8192)(
    domonic_bs4._split_simple_selector_chain
)
domonic_bs4._strip_simple_pseudo = functools.lru_cache(maxsize=8192)(
    domonic_bs4._strip_simple_pseudo
)
domonic_bs4._parse_stripped_selector = functools.lru_cache(maxsize=8192)(
    domonic_bs4._parse_stripped_selector
)

ELEMENT_NODE = 1
TEXT_NODE = 3


class _AnonymousTextFragment:
    """Retained layout/paint projection for a direct DOM text node."""

    def __init__(self, source, parent):
        self.source = source
        self.parent = parent
        self.childNodes = []
        self.nodeType = TEXT_NODE
        self._chromonic_tag_name = "#text"
        self._chromonic_has_layout_children = False


class _AnonymousInlineRun(_AnonymousTextFragment):
    """Retained Taffy-only row for consecutive inline element children."""


class _PseudoElement:
    """A `::before`/`::after` generated box -- not a real DOM node (domonic
    has no CSSOM object for one, only a way to resolve its *computed
    style*: `ComputedStyleDeclaration(owner, "::before")`), just enough
    surface for `build()`'s ordinary recursive machinery and `paint.py`'s
    generic per-box painting to treat it like a real, childless element
    sitting at the very start (`before`) or end (`after`) of `owner`'s own
    content -- reused across relayouts (`_get_pseudo_object`) so Taffy's
    retained projection and measure-caching see a stable identity, the
    same way a real element does.

    Deliberately never a child in any real `Element.childNodes` -- nothing
    that walks the real DOM (`hittest`, `getElementsByTagName`, `paint_tree`/
    `build_display_list`'s own recursion) should ever discover one. It
    reaches Taffy only via `_inline_mixed_content`'s synthesized "element"
    items, and reaches paint only via `owner._chromonic_inline_fragments`
    (the same side-channel list already used for retained text fragments)."""

    def __init__(self, owner, which):
        self.owner = owner
        self.which = which
        self.parentElement = owner
        self.childNodes = ()
        self.text = ""

    @property
    def ownerDocument(self):
        return getattr(self.owner, "ownerDocument", None)

    @property
    def tagName(self):
        return "::" + self.which

    @property
    def textContent(self):
        return self.text

    def getAttribute(self, _name):
        return None


def _get_pseudo_object(element, which: str) -> "_PseudoElement":
    cache = element.__dict__.setdefault("_chromonic_pseudo_objs", {})
    obj = cache.get(which)
    if obj is None:
        obj = cache[which] = _PseudoElement(element, which)
    return obj


class _InlineFormattingPlan:
    """Measured shared line boxes for one block's mixed inline contents."""

    def __init__(self, element, runs, parent_style, owner_display):
        self.element = element
        self.runs = runs
        self.parent_style = parent_style
        self.owner_display = owner_display
        self.fragments = []
        self.owner_boxes = {}
        self.height = 0.0
        # CSS 2.1 9.10: a `direction: rtl` block's own line boxes start from
        # its *right* edge -- glyph order within a same-direction run (plain
        # Latin text here; full bidi reordering across mixed-direction runs
        # is out of scope) stays untouched, only each line's *position*
        # mirrors. `element` is the block establishing this inline
        # formatting context (never a nested wrapper's own `direction` --
        # that would need a real embedding, `unicode-bidi: embed/isolate`,
        # not implemented), so its own computed `direction` governs every
        # plan built for it, split or not.
        computed = getattr(element, "_chromonic_computed_style", None)
        self.rtl = (getattr(computed, "direction", "ltr") or "ltr").strip().lower() == "rtl"

    def measure(self, available_width, _available_height):
        width = float(available_width or 0.0)
        if width <= 0 or width > 1_000_000:
            width = sum(run.get("intrinsic_width", 0.0) for run in self.runs)
        self._measured_width = width  # `publish()` needs this to mirror a `<br>`'s own box for RTL
        base_height = _resolved_line_height(self.parent_style["line_height"])
        base_font = _fontmetrics.parse_length(self.parent_style["font_size"], default=16.0)
        base_ascent, base_descent, normal = fonts.text_metrics(
            self.parent_style["font_family"], base_font,
            _parse_font_weight(self.parent_style["font_weight"]) >= 600,
            fonts.is_italic(self.parent_style["font_style"]))
        base_height = base_height or normal
        base_above = base_ascent + math.floor((base_height - base_ascent - base_descent) / 2)
        base_below = base_height - base_above
        x = y = 0.0
        line_margin_start = 0.0  # how much of the current line's `x` is `margin_start`, not content
        line_leading_total = 0.0  # and how much is a leading border/padding edge
        above, below = base_above, base_below
        self._line_baselines = {}
        # run index -> (x, y, line height, line's own margin_start, line's own leading edge)
        self._break_positions = {}
        # id(escapee element) -> (x, y) cursor position where it sat in the
        # flow -- CSS 2.1 10.3.7/10.6.4's real "static position" for a
        # `top`/`left:auto` absolutely-positioned element mixed into inline
        # content (`_build_text_runs_from_nodes`'s "escapee" runs): it's out
        # of flow and contributes no width/height of its own, but its
        # static position is still wherever it falls in the surrounding
        # text's own layout, not merely "before all the text" or "after
        # all of it". `publish()` turns this into a real page position.
        self._escapee_positions = {}
        placed = []
        for run_index, run in enumerate(self.runs):
            if run.get("escapee"):
                # Doesn't occupy space -- record where the cursor already
                # was and move on, unlike a forced line-break above.
                self._escapee_positions[id(run["element"])] = (x, y)
                continue
            if run.get("break"):
                # Forced line-break: flush the current line and move to the next.
                self._line_baselines[y] = above
                self._break_positions[run_index] = (x, y, above + below, line_margin_start, line_leading_total)
                y += above + below
                x = 0.0
                line_margin_start = 0.0
                line_leading_total = 0.0
                above, below = base_above, base_below
                continue
            tokens = run["tokens"]
            for index, (text, token_width) in enumerate(tokens):
                leading = run["leading"] if index == 0 else 0.0
                trailing = run["trailing"] if index == len(tokens) - 1 else 0.0
                line_leading_total += leading
                # margin-start shifts the whole run right on the first token
                # only — not carried onto subsequent lines after a <br>.
                if index == 0:
                    x += run.get("margin_start", 0.0)
                    line_margin_start += run.get("margin_start", 0.0)
                advance_width = (max(token_width, run["atomic_width"])
                                 if len(tokens) == 1 else token_width)
                total = leading + advance_width + trailing
                fit_total = total - (run["space_width"] if text[-1:].isspace() else 0.0)
                following_space = 0.0
                if index == len(tokens) - 1 and run["owner"] is not self.element:
                    for later in self.runs[run_index + 1:]:
                        if later.get("break"):
                            break
                        if later.get("escapee"):
                            # Out of flow -- contributes no text/tokens of
                            # its own, so it can't be "the next token" for
                            # trailing-space purposes; skip past it to
                            # whatever real run actually follows.
                            continue
                        if later["tokens"]:
                            if not later["tokens"][0][0].strip():
                                following_space = later["tokens"][0][1]
                            break
                if x and x + fit_total + following_space > width and text.strip():
                    self._line_baselines[y] = above
                    y += above + below
                    x = 0.0
                    line_margin_start = 0.0
                    line_leading_total = 0.0
                    above, below = base_above, base_below
                token_height = run["box_height"]
                above = max(above, run["above"])
                below = max(below, run["below"])
                placed.append((run, text, x + leading, y, token_width, token_height,
                               leading, trailing, advance_width))
                x += total
                if index == len(tokens) - 1:
                    # CSS 2.1 10.3.1/10.3.3: horizontal margins on a non-
                    # replaced inline element are real spacing (unlike its
                    # vertical margins, which don't affect line height at
                    # all) and never collapse with an adjoining element's
                    # own margin the way two vertical block margins do --
                    # each side is added independently. `margin_start`
                    # (margin-left) already shifts the cursor before this
                    # run's own first token; this is its missing right-
                    # side counterpart, added only once, after this run's
                    # own last token, so it becomes real space before
                    # whatever comes next without being part of *this*
                    # run's own reported width (matching how `margin_
                    # start` already never became part of it either).
                    # Found on `wpt/css/CSS2/margin-padding-clear/margin-
                    # collapse-001.xht`: two adjacent `<span>`s, each
                    # `margin:5em`, landed `100px` (one whole margin) too
                    # close together -- only the second span's own
                    # margin-left was ever applied at all.
                    x += run.get("margin_end", 0.0)
        self._line_baselines[y] = above
        # CSS 2.1 9.4.2: a line box "collapses" to zero height when it has
        # no text, no preserved white space, and no in-flow content with a
        # non-zero margin/border/padding -- an empty, zero-edge inline
        # (`_empty_inline_strut_run`, `<span></span>` with no border/
        # padding/margin of its own) alone on a line must not, by itself,
        # create a normal font-metrics-tall line the way real content
        # would. Only when *every* run on the line is such a strut does
        # this apply -- the moment there's any real text (or a bordered/
        # padded/margined empty inline) alongside it, the line is not
        # empty and every run, struts included, contributes normally
        # (already correct, see `_empty_inline_strut_run`'s own docstring).
        # Found on `wpt/css/CSS2/linebox/empty-inline-001.html`: a bare
        # `<span></span>`, alone in a `<div>`, inflated the div to a normal
        # `18px` instead of collapsing to `0`.
        is_all_zero_edge_empty = placed and all(
            run.get("empty_strut") and run["leading"] == 0.0 and run["trailing"] == 0.0
            and run["box_height"] <= run["glyph_height"] + 1e-6 and run.get("margin_start", 0.0) == 0.0
            for run in self.runs if not run.get("break") and not run.get("escapee")
        )
        self.height = 0.0 if is_all_zero_edge_empty else (y + above + below if placed else 0.0)
        if is_all_zero_edge_empty:
            # Real Chrome also reports each such strut's own element
            # fragment at zero height, not its font-metrics `box_height`
            # (18px) -- consistent with the line it sits on not existing at
            # all. `empty-inline-003.xht` (a strut alongside real text) is
            # unaffected: `is_all_zero_edge_empty` is only ever true when
            # *every* run is a zero-edge empty strut, so a strut sharing a
            # line with real content never reaches this branch.
            placed = [
                (run, text, x, y, token_width, 0.0, leading, trailing, advance_width)
                for run, text, x, y, token_width, _token_height, leading, trailing, advance_width in placed
            ]
        content_width = min(width, max(
            (px + advance for _r, _t, px, _y, _pw, _h, _l, _tr, advance in placed), default=0.0))
        if self.rtl and placed:
            # Mirror every placed token's position against the same `width`
            # each was placed within -- reflecting a whole line as a rigid
            # group (not each token independently) preserves their relative
            # spacing/order while moving the group as a whole to the
            # container's right edge, exactly `direction: rtl`'s effect on
            # a line shorter than its container (the common case: each
            # split segment here is its own single-fragment line). Computed
            # from the *original* (pre-mirror) `placed` above, so the
            # intrinsic-width return value isn't affected by this.
            #
            # Margin is never part of a fragment's own box -- only its
            # position -- so it must not be fed into the mirror as if it
            # were: `margin_start` (physical margin-left, baked into the
            # very first token's `x` before mirroring, index 0 only) is
            # subtracted back out of `outer_left` here so mirroring doesn't
            # drag it along, landing the box `margin_start` px further
            # right than a naive mirror would (the margin stays on its own
            # physical left, outside the box, wherever the box ends up).
            # Symmetrically, the *last* token's owner's physical margin-
            # right (never added to `x` at all above, since ordinary LTR
            # placement never needed to) is subtracted from its mirrored
            # position afterward, pushing that fragment away from the
            # line's right edge by that amount -- the RTL mirror of margin-
            # left doing the same for the first fragment.
            # A plan built for a *leading* or *interior* split segment
            # (`_split_inline_flow_around_blocks`, a block interruption
            # follows it) is explicitly marked `False` -- margin-right
            # belongs only to the wrapper's true trailing segment, never
            # one of these, even though each is alone in its own `placed`
            # list and so would otherwise look "last" too. Absent for an
            # ordinary (non-split) plan, where the true last entry always
            # legitimately owns the trailing margin -- default `True`.
            is_final_segment = getattr(self, "_chromonic_final_split_fragment", True)
            last_index = len(placed) - 1
            mirrored = []
            for index, (run, text, px, y, token_width, token_height, leading, trailing, advance) in enumerate(placed):
                margin_start = run.get("margin_start", 0.0)
                outer_left = px - leading - margin_start
                outer_width = leading + advance + trailing
                new_px = (width - outer_left - outer_width) + leading
                if index == last_index and is_final_segment:
                    owner_style = getattr(run.get("owner"), "_chromonic_native_style", None) or {}
                    owner_margin = owner_style.get("margin") or (0.0, 0.0, 0.0, 0.0)
                    new_px -= _numeric_edge(owner_margin[1])
                mirrored.append((run, text, new_px, y, token_width, token_height, leading, trailing, advance))
            placed = mirrored
        self._placed = placed
        return (content_width, self.height)

    def publish(self, box, padding, owner_accum, element_fragments_accum):
        origin_x = box.x + box.border_left + padding[3]
        origin_y = box.y + box.border_top + padding[0]
        escapee_positions = getattr(self, "_escapee_positions", None)
        if escapee_positions:
            # Real page-coordinate static position for each `top`/`left:
            # auto` escapee this plan carries -- `_fix_inline_escapee_
            # static_position` (a later, dedicated pass; this element's own
            # Taffy box doesn't exist yet here) applies it once every
            # element's own box has been written.
            for run in self.runs:
                if run.get("escapee"):
                    position = escapee_positions.get(id(run["element"]))
                    if position is not None:
                        run["element"]._chromonic_static_position = (
                            origin_x + position[0], origin_y + position[1])
        self.fragments = []
        owner_rects = {}
        grouped = {}
        placed = getattr(self, "_placed", ())
        for placed_index, (run, text, x, y, width, token_height, leading, trailing, advance) in enumerate(placed):
            ends_line = placed_index + 1 == len(placed) or placed[placed_index + 1][3] != y
            collapsed_space = (run["space_width"]
                               if text[-1:].isspace() and ends_line else 0.0)
            visual_width = max(0.0, width - collapsed_space)
            visual_advance = max(0.0, advance - collapsed_space)
            font_size = run["font_size"]
            glyph_height = min(token_height, run["glyph_height"])
            glyph_y = y + self._line_baselines[y] - run["ascent"]
            key = (id(run["source"]), y)
            entry = grouped.get(key)
            # A genuinely empty inline's own strut run (`_empty_inline_
            # strut_run`, one `("", 0.0)` token) places a real element
            # rect (via `owner_rects` below) but is never a text-range
            # fragment -- there's no source text node at all, and real
            # Chrome's own `getClientRects()` for such an element reports
            # zero *text* fragments (only the element/box ones). Grouping
            # it here anyway would synthesize a spurious empty-string
            # entry in `_chromonic_owned_fragments`.
            if entry is None and text == "":
                pass
            elif entry is None:
                fragment = _AnonymousTextFragment(run["source"], self.element)
                fragment.owner = run["owner"]
                fragment._chromonic_paint_style = run["paint_style"]
                fragment._chromonic_text_lines = [text]
                fragment._chromonic_text_line_widths = [visual_width]
                fragment._chromonic_line_height = glyph_height
                fragment._layout_box = LayoutBox(
                    x=origin_x + x, y=origin_y + glyph_y,
                    width=visual_width, height=glyph_height,
                    client_width=visual_width, client_height=glyph_height,
                )
                grouped[key] = fragment
                self.fragments.append(fragment)
            else:
                entry._chromonic_text_lines[0] += text
                old = entry._layout_box
                combined_width = origin_x + x + visual_width - old.x
                entry._chromonic_text_line_widths[0] = combined_width
                entry._layout_box = LayoutBox(
                    x=old.x, y=old.y, width=combined_width, height=old.height,
                    client_width=combined_width, client_height=old.client_height,
                )
            owner = run["owner"]
            owner_y = origin_y + (y if run["atomic_width"] else glyph_y - run["top_edge"])
            rect = (origin_x + x - leading, owner_y,
                    visual_advance + leading + trailing, token_height)
            # `run.get("split_group")` (set by `_split_wrapping_inline_
            # element`) is which segment, in split/document order, this rect
            # belongs to -- `None` for an ordinary (non-split) owner, where
            # there's only ever one segment. Carried through so
            # `_finalize_inline_owner_boxes` can place interruption-marker
            # rects at their logical position instead of a geometric sort.
            owner_rects.setdefault(owner, []).append((rect, run.get("split_group")))
        # When the plan owner is exactly `display:inline` it participates in
        # fragmentation just like any child inline owner: its rects come from
        # `owner_rects[self.element]` (already built above with correct
        # padding/border edges) and must be merged per-line then unioned for
        # getBoundingClientRect().  `inline-block` and block owners are atomic
        # and must keep the Taffy container box unchanged.
        #
        # Accumulate into `owner_accum` rather than finalizing here directly:
        # an inline element split around an in-flow block child (CSS 2.1
        # 9.2.1.1, `_split_wrapping_inline_element`) contributes fragments
        # from *multiple*, independently-published `_InlineFormattingPlan`s
        # (one per split segment, each its own Taffy leaf) that all still
        # belong to the same original owner -- writing the owner's box here
        # directly, once per plan, would have whichever plan happens to
        # publish last silently overwrite every earlier plan's fragments
        # instead of the two together forming the owner's real (multi-
        # fragment) bounding box. `_finalize_inline_owner_boxes` does that
        # union once, after every plan sharing `owner_accum` has run.
        owner_is_inline = self.owner_display == "inline"
        for owner, rect_group_pairs in owner_rects.items():
            if owner is self.element and not owner_is_inline:
                continue
            entry = owner_accum.get(id(owner))
            if entry is None:
                entry = owner_accum[id(owner)] = (owner, {}, [])
            groups = entry[1]
            for rect, group in rect_group_pairs:
                groups.setdefault(group, []).append(rect)
            entry[2].extend(fragment for fragment in self.fragments if fragment.owner is owner)
        # Publish layout boxes for <br> elements sized to the line-box height
        # so the harness reports the correct height (Chrome: 18px, not 0).
        for run_index, run in enumerate(self.runs):
            if run.get("break"):
                br_x, br_y, line_h, line_margin_start, line_leading_total = self._break_positions.get(
                    run_index, (0.0, 0.0, 0.0, 0.0, 0.0))
                if self.rtl:
                    # `_break_positions` is captured mid-`measure()`, before
                    # the RTL mirror pass below runs on `placed` -- mirror
                    # it here too against that same measured width, or a
                    # `<br>`'s own marker box stays at its un-mirrored (LTR)
                    # cursor position while the real text around it moves.
                    # `<br>` marks the *cursor* position right after the
                    # line's real content -- excluding `line_margin_start`
                    # (this line's own `margin_start`, already baked into
                    # `br_x`, and which sits *outside* the fragment on
                    # mirror, same as for a real text token) but *including*
                    # `line_leading_total` back in (a leading border/padding
                    # edge, unlike margin, is part of the fragment itself,
                    # so the cursor continues from just past it, matching
                    # where a hypothetical next token would be placed).
                    br_x = (getattr(self, "_measured_width", 0.0)
                            - (br_x - line_margin_start) + line_leading_total)
                run["element"].__dict__["_layout_box"] = LayoutBox(
                    x=origin_x + br_x, y=origin_y + br_y,
                    width=0.0, height=line_h,
                    client_width=0.0, client_height=line_h,
                )
                run["element"]._chromonic_has_layout_children = False
        # Same accumulate-not-overwrite reasoning as `owner_accum` above,
        # for the plan's own `element` (paint.py's `_chromonic_inline_
        # fragments` is what actually gets drawn for it) -- a split
        # element's *other* segment(s) are published by different `plan`
        # instances that all still share this same `self.element`.
        elem_entry = element_fragments_accum.get(id(self.element))
        if elem_entry is None:
            elem_entry = element_fragments_accum[id(self.element)] = (self.element, [])
        elem_entry[1].extend(self.fragments)

# Tags that never paint a box of their own, real-page metadata/logic rather
# than content. domonic's cascade has no UA stylesheet giving these
# `display: none` by default (confirmed: `ComputedStyleDeclaration` reports
# plain `inline` for `head`/`title`/`style`/`script`, same as any unknown
# tag) -- a real browser hardcodes exactly this exclusion in its own UA
# stylesheet, so chromonic does the same rather than laying out and painting a
# `<script>` element's source text as if it were a paragraph. Logged
# alongside wrinkle #10 in docs/domonic-wrinkles.md.
_NON_RENDERING_TAGS = frozenset({"script", "style", "head", "title", "meta", "link", "noscript", "template"})


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _extract_paint_style(computed) -> dict:
    """The handful of paint-only properties `paint.py` needs (background/
    border/text colour, font size/weight/style/family), read out of
    `computed` exactly once here and cached as plain strings.

    Profiling `paint_tree()` in isolation (see PLAN.md's "Phase 9" perf
    notes) found this a real, avoidable cost: a `ComputedStyleDeclaration`
    attribute access re-resolves its value from the underlying style text
    on *every single access* -- domonic caches nothing on the object itself
    -- so without this, every repaint (not just every relayout) re-parses
    every element's colours and font properties from scratch. That matters
    whenever painting runs more often than layout does, which is
    `native_browser.py`'s entire scroll/expose story ("scroll/expose only
    paint", never relayout) -- a page that never changes still re-resolved
    every element's style on every single scroll event before this."""
    raw = computed._resolved.get

    def raw_or_computed(name: str, attribute: str) -> str:
        value = raw(name)
        # Custom properties still need element-specific expansion.
        return getattr(computed, attribute) if value and "var(" in value else value

    return {
        "background_color": computed.backgroundColor,
        "background_image": computed.backgroundImage,
        "background_size": computed.backgroundSize,
        "background_position": computed.backgroundPosition,
        "background_repeat": computed.backgroundRepeat,
        "overflow_x": computed.overflowX,
        "overflow_y": computed.overflowY,
        "border_top_color": computed.borderTopColor,
        "color": computed.color,
        "font_size": computed.fontSize,
        # These three have no used-value conversion in getPropertyValue;
        # _ResolvedView already supplies inheritance and initial values.
        "font_weight": raw_or_computed("font-weight", "fontWeight"),
        "font_style": raw_or_computed("font-style", "fontStyle"),
        "font_family": raw_or_computed("font-family", "fontFamily"),
        # not read by paint.py itself -- included so _make_measure can work
        # entirely from this one already-extracted dict (see its docstring)
        # rather than touching `computed` again on a `reuse_styles=True` pass.
        "letter_spacing": computed.letterSpacing,
        "word_spacing": computed.wordSpacing,
        "line_height": computed.lineHeight,
        "white_space": computed.whiteSpace,
        "text_align": computed.textAlign,
        "text_transform": computed.textTransform,
    }


def _css_generated_content_text(value: "str | None") -> str:
    """Decode the simple string form of CSS generated `content`.

    This deliberately handles only the common, layout-relevant case:
    quoted strings, including CSS escapes such as Font Awesome's "\\f03e".
    Keywords like `none`, `normal`, counters, images, and attributes remain
    out of scope for now.
    """
    if not value:
        return ""
    text = str(value).strip()
    if text in ("none", "normal", "initial", "inherit"):
        return ""
    if len(text) < 2 or text[0] not in ("'", '"') or text[-1] != text[0]:
        return ""
    inner = text[1:-1]
    inner = re.sub(r"\\\\(?=[0-9a-fA-F]{1,6}(?:\s|$))", r"\\", inner)

    def replace_escape(match):
        escaped = match.group(1)
        if not escaped:
            return ""
        if re.fullmatch(r"[0-9a-fA-F]{1,6}\s?", escaped):
            return chr(int(escaped.strip(), 16))
        return escaped[-1]

    return re.sub(r"\\([0-9a-fA-F]{1,6}\s?|.)", replace_escape, inner)


def _pseudo_generates_box(raw_content: "str | None") -> bool:
    """Whether a `::before`/`::after` rule's raw (un-unquoted) `content`
    value actually generates a box at all, as opposed to merely resolving
    to empty *text*. These are different questions: `content: ""` (an
    empty quoted string) generates a real, paintable box with no text in
    it -- used for icon-only pseudo-elements sized by `width`/`height`
    and painted via `background-image` alone (`csszengarden.com`'s
    `h1::before`, the site's logo) -- while no matching rule at all, or an
    explicit `content: none`/the initial `normal`, generates no box.
    domonic's own unset/initial value for `content` is the literal string
    `"none"`, not spec's `"normal"` -- both are treated as "no box" here."""
    text = (raw_content or "").strip().lower()
    return text not in ("", "none", "normal", "initial", "inherit")


def _extract_generated_content(element, computed_cache):
    """`(before_text, after_text, before_info, after_info)` -- the first
    two are the plain generated-content strings (used by `_own_text` for
    an element with no real pseudo *box*, e.g. `content: counter(...)` or
    a shorthand this project doesn't turn into a real box), the last two
    are `None`, or `(pseudo_computed, text)` when the pseudo-element
    should become a real, separately-styled/positioned box (see
    `_pseudo_generates_box`) -- consulted by `_inline_mixed_content` to
    synthesize an `("element", _PseudoElement, ...)` item for it."""
    document = getattr(element, "ownerDocument", None)

    # With no document/stylesheets there cannot be authored pseudo-element
    # generated content. Avoid two unnecessary cascade resolutions per
    # element -- particularly important for programmatically-created DOMs.
    if document is None or not getattr(document, "styleSheets", None):
        return "", "", None, None

    chain_cache = computed_cache.setdefault("_chromonic_chain_cache", {})

    before = ComputedStyleDeclaration(
        element,
        "::before",
        _chain_cache=chain_cache,
    )
    after = ComputedStyleDeclaration(
        element,
        "::after",
        _chain_cache=chain_cache,
    )

    before_text = _css_generated_content_text(before.content)
    after_text = _css_generated_content_text(after.content)
    before_info = (before, before_text) if _pseudo_generates_box(before.content) else None
    after_info = (after, after_text) if _pseudo_generates_box(after.content) else None
    return before_text, after_text, before_info, after_info


def _describe(element, computed_cache=None, *, reuse_styles=False):
    """`(ComputedStyleDeclaration, LayoutStyle)` for `element`. Stashes the
    `ComputedStyleDeclaration` on the element itself (`_chromonic_computed_style`)
    so `paint.py`'s later walk of the same tree -- reading paint-only
    properties `LayoutStyle` deliberately excludes -- doesn't build a second
    one from scratch, *and* the specific properties paint actually reads off
    it (`_chromonic_paint_style`, see `_extract_paint_style`) so a repaint with
    no relayout in between doesn't re-resolve them either. Both overwritten
    on every *fresh* resolution, so they reflect the most recent one, the
    same lifetime `_chromonic_padding` below already has.

    `reuse_styles=True` skips resolving CSS for this element at all if a
    prior resolution (`element._chromonic_resolved_style`) already exists,
    reusing it as-is. **Real, measured need, not a hypothetical one**:
    profiling a relayout of a real page (`bbc.co.uk`, ~4,600 elements) found
    a *single* `tree.layout()` call taking **6.7 seconds** -- almost all of
    it inside domonic's `ComputedStyleDeclaration`/`_collect_author_declarations`,
    which re-parses every candidate CSS selector's text from scratch against
    every element it's tested against, with no per-selector or per-property
    caching at all (a real, serious domonic performance gap -- logged as
    `docs/domonic-wrinkles.md` #16). A background `<img>` arriving (or a
    scroll, or a resize with the same viewport bucket) never changes *any*
    element's class/inline-style/stylesheets -- there is nothing for a fresh
    CSS resolution to find that a cached one wouldn't already have -- so
    `native_browser.py`'s image-triggered relayouts pass `reuse_styles=True`
    and skip that 6.7s entirely; a real DOM mutation (a click handler
    changing a class) still needs a fresh resolution and does not.

    `computed_cache` holds our own `(computed, style_obj)` tuples keyed by
    `id(element)`, and is handed to domonic as `_chain_cache` too -- but
    domonic's own chain cache expects that key to map to a bare
    `ComputedStyleDeclaration` (`_parent_computed()` does `cache.get(id(parent))`
    and calls `._font_size_px()` straight on whatever it finds), not our
    tuple. The two must not share one dict: a nested dict under a string key
    (never collides with the `int` ids used everywhere else) is what
    actually gets passed as `_chain_cache`, so domonic's own ancestor-style
    sharing keeps working without seeing our tuples at all."""
    cache = {} if computed_cache is None else computed_cache
    cached = cache.get(id(element))
    if cached is not None:
        return cached

    if reuse_styles:
        prior = getattr(element, "_chromonic_resolved_style", None)
        if prior is not None:
            cache[id(element)] = prior
            return prior

    # Share ancestor resolution across this layout pass, not across frames.
    # domonic's default cache only spans one element's ancestor walk; fresh
    # walks for siblings otherwise resolve the same parents repeatedly. Kept
    # in its own nested dict, not `cache` itself -- see the docstring above.
    chain_cache = cache.setdefault("_chromonic_chain_cache", {})
    computed = ComputedStyleDeclaration(element, _chain_cache=chain_cache)
    # domonic's own `_parent_computed()` only *reads* `chain_cache` looking
    # for an already-built parent -- it never registers a
    # `ComputedStyleDeclaration` under its own id when one is constructed
    # directly (as here), only when it builds one itself as somebody else's
    # parent. Without this, a child resolved right after `element` wouldn't
    # find `element` in `chain_cache` and would silently build (and resolve)
    # a second, separate `ComputedStyleDeclaration` for it.
    chain_cache[id(element)] = computed
    style_obj = LayoutStyle._from_computed(computed)
    element._chromonic_computed_style = computed
    element._chromonic_paint_style = _extract_paint_style(computed)
    (element._chromonic_before_text, element._chromonic_after_text,
     element._chromonic_before_pseudo, element._chromonic_after_pseudo) = _extract_generated_content(element, cache)
    from . import webfonts
    webfonts.resolve_style(element, element._chromonic_paint_style)
    # Give Parley and Skia the same platform choice for CSS monospace.
    family = element._chromonic_paint_style["font_family"]
    if family and family.strip().lower() in ("monospace", "ui-monospace"):
        element._chromonic_paint_style["font_family"] = fonts._GENERIC_FAMILIES[family.strip().lower()]
    result = (computed, style_obj)
    element._chromonic_resolved_style = result
    cache[id(element)] = result
    return result


def _renders(style_obj) -> bool:
    """Whether an element already known to be an ordinary rendering tag (see
    `_NON_RENDERING_TAGS`, checked by the caller before this) should still be
    walked into the Taffy tree -- false for anything the cascade resolved to
    `display: none` (a real browser's "don't lay this out, don't paint it,
    don't hit-test it" is exactly `display: none`)."""
    display = style_obj.display
    return getattr(display, "value", display) != "none"


def _child_elements(element, computed_cache=None, *, reuse_styles=False) -> list:
    """`[(child, computed, style_obj), ...]` for children that should
    render -- each child's style computed exactly once here, then handed
    straight to the recursive `build()` call below instead of being
    recomputed there."""
    result = []
    for child in element.childNodes or []:
        if not _is_element(child):
            continue
        if (getattr(child, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
            continue
        computed, style_obj = _describe(child, computed_cache, reuse_styles=reuse_styles)
        if _renders(style_obj):
            result.append((child, computed, style_obj))
        else:
            _clear_stale_layout_geometry(child)
    return result


def _clear_stale_layout_geometry(element) -> None:
    """`element` just resolved to `display:none` (or still is) -- real CSS
    gives it, and everything inside it, no box at all. Without this, any
    `_layout_box`/`_chromonic_inline_fragments` published for it on some
    *earlier* pass, back when it still rendered, was simply left in place:
    excluded from `node_map` (so nothing here ever touches it again), but
    not cleared either, so it kept reading as "has a box" to every piece of
    code that isn't `tree.py`'s own build/adjust pipeline -- `paint_tree`
    walks the real DOM, not `node_map`, so it never itself learns this
    subtree was excluded, and kept drawing it at that frozen position
    forever; `getBoundingClientRect()`/hit-testing read the same stale box
    for the same reason. Found via a live `chromonic.App` run (`examples/
    kanban.py`): clicking a `.filter` button set a card's own `display` to
    `none` (or reset it to `""`), and the layout heights genuinely changed
    accordingly, but the card being hidden never itself became visible on
    screen -- it just kept painting at its last real position."""
    element.__dict__.pop("_layout_box", None)
    element.__dict__.pop("_chromonic_inline_fragments", None)
    for child in element.childNodes or []:
        if _is_element(child):
            _clear_stale_layout_geometry(child)


# CSS 2.1 16.6.1's white-space collapsing only ever touches ASCII space,
# tab, newline, CR, and form feed -- *not* U+00A0 (non-breaking space,
# `&nbsp;`), a distinct character that always renders as a real glyph and
# never collapses. Python's own `str.strip()`/`str.split()`/`\s` regex class
# all treat U+00A0 as whitespace too (it carries the Unicode "White_Space"
# property), so using them here silently collapsed an nbsp-only text node
# down to nothing -- found on `wpt/css/CSS2/positioning/absolute-non-
# replaced-max-height-007.xht`: a `<div>&nbsp;</div>` (its only content)
# measured as having none at all, collapsing its `height:auto` to `0`
# instead of a real line height (then correctly clamped by `max-height`).
_CSS_COLLAPSIBLE_WHITESPACE_RE = re.compile(r"[ \t\n\r\f]+")
_CSS_WHITESPACE_STRIP_CHARS = " \t\n\r\f"


def _apply_text_transform(text: str, transform: str | None) -> str:
    transform = (transform or "none").strip().lower()
    if transform == "uppercase":
        return text.upper()
    if transform == "lowercase":
        return text.lower()
    if transform == "capitalize":
        return re.sub(r"\b(\w)", lambda match: match.group(1).upper(), text)
    return text


def _own_text(element) -> str:
    # Mixed content (text alongside child *elements*) is out of this POC's
    # scope -- an element with any child element is a Taffy branch, full
    # stop; only a childless element's own text is ever measured.
    #
    # `_rendering_text_content`, not raw `.textContent` -- a childless
    # element (no real child *elements*, see `_child_elements`'s own
    # `_NON_RENDERING_TAGS` filtering) can still have a `<style>`/`<script>`
    # descendant (a Wikipedia TemplateStyles injection is exactly this:
    # `<td><span><link .../><style>...</style></span></td>`, no other
    # content at all) -- plain DOM `.textContent` includes that raw source
    # text verbatim regardless, so it was rendering as if it were the
    # cell's own visible prose. Found on `en.wikipedia.org`'s infobox
    # (stray `.mw-parser-output .plainlist ol,...` CSS-selector text
    # appearing as a literal line of content at the top of the box).
    text = (
        getattr(element, "_chromonic_before_text", "")
        + _rendering_text_content(element)
        + getattr(element, "_chromonic_after_text", "")
    )
    style = element.__dict__.get("_chromonic_paint_style", {})
    text = _apply_text_transform(text, style.get("text_transform"))
    if style.get("white_space") in ("pre", "pre-wrap", "break-spaces"):
        return text
    return _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", text).strip(_CSS_WHITESPACE_STRIP_CHARS)


def _collapsed_text_node(node) -> str:
    raw = getattr(node, "textContent", None)
    if raw is None:
        raw = getattr(node, "data", "")
    if not raw:
        return ""
    return _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw).strip(_CSS_WHITESPACE_STRIP_CHARS)


def _inline_mixed_content(element, children, element_is_inline=False):
    """Return DOM-order inline items when a block contains direct text or
    when all children are inline-level elements (spans, links, etc.) that
    themselves contain text.

    Block children deliberately opt out, *except* when `element` is itself
    a genuine `display:inline` element (`element_is_inline`, from `build()`'s
    own `is_genuinely_inline`) -- CSS 2.1 9.2.1.1's own trigger: an in-flow
    block child of an inline element forces that inline to split around it,
    which only ever has a chance to happen (`_split_inline_flow_around_
    blocks`/`_contains_in_flow_block`, called on whatever `inline_items`
    this returns) if a direct block child is allowed to reach it as an
    ordinary "element" item here, instead of being rejected before that by
    this function's own general block-children-opt-out rule. An ordinary
    block `element` (not inline) still opts a real block child out entirely
    -- it establishes its own line break there and needs a fuller anonymous-
    block implementation this project doesn't have. Otherwise, this path
    handles the common prose case of text interleaved with spans, links,
    strong/em and code, including spans that contain forced line-breaks via
    <br>.
    """
    has_direct_text = any(
        getattr(node, "nodeType", None) == TEXT_NODE and _collapsed_text_node(node).strip()
        for node in (element.childNodes or [])
    )
    # Also qualify when all children are inline-level elements (or <br>) --
    # e.g. <div><span>…</span></div>. Not gated on any child actually having
    # text: CSS 2.1 9.2.1.1/10.8 says a genuinely *empty* non-replaced
    # inline (`<div><span></span></div>`, no text anywhere) still
    # participates in the line box -- contributes its own font/line-height
    # to the line's height/baseline exactly like a real text run would,
    # width 0 -- so it must not be excluded from this path and fall through
    # to plain block treatment (dropping its line-box contribution
    # entirely). `_is_inline_level` already tag-gates against domonic's
    # un-cascaded "every tag defaults to inline" ambiguity (see its own
    # docstring), so nothing else here needs a text-based safety net on top
    # of that. Found on `wpt/css/CSS2/linebox/empty-inline-002.xht`/
    # `-003.xht`: an empty `<span>` as a `<div>`'s only content, or mixed
    # with real text, was dropped from layout entirely instead of sizing
    # the line the way its own `line-height` requires.
    def _child_qualifies(child, style_obj) -> bool:
        return (
            _is_inline_level(child, style_obj)
            or _is_absolutely_positioned(style_obj)
            or (getattr(child, "tagName", "") or "").lower() == "br"
            # CSS 2.1 9.2.1.1: a genuine in-flow block child of an inline
            # `element` is exactly the split trigger, not a reason to
            # reject this element's own content wholesale -- let it
            # through as an ordinary "element" item so `_split_inline_
            # flow_around_blocks`/`_contains_in_flow_block` (called on
            # whatever this function returns) get the chance to see it
            # and split `element` around it. Found on `wpt/css/CSS2/
            # linebox/inline-box-001.xht`/`-002.xht`: `<div id=div1
            # style="display:inline">First line<div>Filler Text</div>Last
            # line</div>` -- the nested block is a *direct* child of the
            # inline `div1` itself, not nested inside a further wrapping
            # inline, so this project's existing "block children opt out"
            # rule rejected it before the split machinery ever ran.
            or element_is_inline
        )

    has_inline_only_children = (
        not has_direct_text
        and bool(children)
        and all(_child_qualifies(child, style_obj) for child, _computed, style_obj in children)
    )
    before_info = getattr(element, "_chromonic_before_pseudo", None)
    after_info = getattr(element, "_chromonic_after_pseudo", None)
    if not has_direct_text and not has_inline_only_children and before_info is None and after_info is None:
        return None
    by_id = {id(child): (child, computed, style_obj) for child, computed, style_obj in children}
    # Out-of-flow positioned children do not break an inline formatting run.
    # Keep them in the retained projection so Taffy can anchor them, while the
    # surrounding direct text still gets its own measurable fragment.
    # <br> elements are handled as forced line-breaks and are always permitted.
    if any(not _child_qualifies(child, style_obj) for child, _computed, style_obj in children):
        return None
    items = []
    pending_space = False
    previous_was_element = False
    for node in element.childNodes or []:
        if getattr(node, "nodeType", None) == TEXT_NODE:
            text = _collapsed_text_node(node)
            if text:
                fragment = getattr(node, "_chromonic_fragment", None)
                if fragment is None:
                    fragment = _AnonymousTextFragment(node, element)
                    node._chromonic_fragment = fragment
                fragment._chromonic_leading_collapsed_space = (
                    pending_space or (previous_was_element and text[:1].isalnum())
                )
                items.append(("text", fragment, text, None, None))
                pending_space = False
                previous_was_element = False
            elif (getattr(node, "textContent", "") or ""):
                pending_space = True
        elif id(node) in by_id:
            child, computed, style_obj = by_id[id(node)]
            if (getattr(child, "tagName", "") or "").lower() == "br":
                items.append(("break", child, None, computed, style_obj))
                pending_space = False
                previous_was_element = False
            else:
                items.append(("element", child, None, computed, style_obj))
                previous_was_element = True

    def pseudo_item(which, info):
        pseudo_computed, text = info
        pseudo = _get_pseudo_object(element, which)
        pseudo.text = text
        pseudo_style_obj = LayoutStyle._from_computed(pseudo_computed)
        # `build()` only ever sets `_chromonic_paint_style`/`_chromonic_
        # computed_style` from inside `_describe()`, which real elements
        # always go through (via `_child_elements`) before reaching here --
        # a synthetic pseudo never does (there's no real DOM node
        # `ComputedStyleDeclaration` could resolve one for), so it has to
        # be set explicitly here or `build()`'s own text/paint code would
        # read an empty `{}` paint style (wrong font, wrong color) for it.
        pseudo._chromonic_paint_style = _extract_paint_style(pseudo_computed)
        pseudo._chromonic_computed_style = pseudo_computed
        # `_describe()` (never reached for a pseudo, see above) is also
        # where a downloaded `@font-face` gets substituted in for its raw
        # CSS family name (`font-family: 'verdemoderna'` -> the actual
        # registered alias `paint.py`'s font resolution can find) --
        # skipping it left an icon-font pseudo-element (`csszengarden.com`'s
        # footer nav icons) painting its glyph character in a fallback
        # system font instead, at the icon font's real (much larger, e.g.
        # 36px) size -- a giant literal letter instead of a small icon.
        from . import webfonts
        webfonts.resolve_style(element, pseudo._chromonic_paint_style)
        return ("element", pseudo, None, pseudo_computed, pseudo_style_obj)

    if before_info is not None:
        items.insert(0, pseudo_item("before", before_info))
    if after_info is not None:
        items.append(pseudo_item("after", after_info))
    return items


def _inline_text_style(parent_style):
    style = dict(parent_style)
    style.update({
        "display": "block", "position": "relative", "width": "auto", "height": "auto",
        "inset": ["auto", "auto", "auto", "auto"],
        "min_width": "auto", "min_height": "auto", "max_width": "auto", "max_height": "auto",
        "margin": [0.0, 0.0, 0.0, 0.0], "padding": [0.0, 0.0, 0.0, 0.0],
        "border": [0.0, 0.0, 0.0, 0.0], "flex_grow": 0.0, "flex_shrink": 1.0,
    })
    return style


def _numeric_edge(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _collapse_margin_set(margins: list) -> float:
    """CSS 2.1 8.3.1: when several margins are adjoining (no border,
    padding, clearance, or non-empty in-flow content between them --
    an empty block's own top *and* bottom margin both join the same
    adjoining set as whatever is on either side of it, since the empty
    block itself doesn't separate them), they collapse into a single
    margin: the largest positive value combined with the largest-
    magnitude negative value (their sum, since one is positive and one
    is negative) -- reducing to a plain max() when every margin shares
    one sign, since the missing side then contributes zero. An empty
    `margins` list (nothing adjoining yet) collapses to no margin at all."""
    positive = max((m for m in margins if m > 0), default=0.0)
    negative = min((m for m in margins if m < 0), default=0.0)
    return positive + negative


def _block_margins_collapse_through(child, child_box) -> bool:
    """CSS 2.1 8.3.1: an empty in-flow block -- no border, no padding, no
    height of its own (`auto`, not an explicit `0`), and no content that
    gave it real extent -- does not stop its own top and bottom margins
    from collapsing with each other and with whatever is adjoining on
    either side. `child_box.height == 0` alone is not enough to conclude
    "empty": a zero explicit `height` still stops collapse-through
    (CSS 2.1's own wording: only `height: auto` (and `min-height` -- not
    modelled here) with nothing forcing a non-zero height qualifies)."""
    if child_box.height != 0.0:
        return False
    native = getattr(child, "_chromonic_native_style", None) or {}
    if native.get("height") != "auto":
        return False
    if any(_numeric_edge(v) != 0.0 for name in ("border", "padding") for v in native.get(name, ())):
        return False
    return True


def _empty_inline_strut_run(owner, leading_edge, trailing_edge, top_edge_val, extra_height, margin_start):
    """CSS 2.1 9.2.1.1/10.8: a genuinely empty, non-replaced inline element
    (`<span></span>`, no text/`<br>`/element descendants at all) still
    generates one zero-width inline box that participates in the
    surrounding line -- contributing its *own* font/`line-height` to the
    line's height/baseline exactly like a real text run would (`above`/
    `below` below, purely font+line-height derived), while its own
    vertical padding/border/margin (`extra_height`/`leading_edge`/
    `trailing_edge`) only grow *its own* reported box (`box_height`,
    read back by `publish()`'s ordinary run handling below), never the
    *line*'s height -- matches every other run's existing above/below
    vs. box_height split, just with one empty `("", 0.0)` token instead of
    real text so `measure()`'s placement loop places it (a zero-width
    fragment) without ever treating it as wrappable content.

    Found on `wpt/css/CSS2/linebox/empty-inline-002.xht` (an empty `<span>`
    with `line-height`/padding/border/margin, alone in its containing
    `<div>`) and `-003.xht` (an empty `<span>` with just `line-height`,
    next to real text) -- both previously produced zero runs for the
    empty owner at all, dropping it and its line-box contribution
    entirely."""
    paint_style = owner._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    # An explicit `line-height: 0` is a real, valid (if unusual) authored
    # value, not "unset" -- `_resolved_line_height` already returns `None`
    # for the genuinely-unset/`normal` case, so a `... or normal_height`
    # here would wrongly treat the *value* `0.0` the same way, falling
    # back to the font's own metrics-based line-height instead of the
    # explicit zero the author asked for.
    resolved_line_height = _resolved_line_height(paint_style["line_height"])
    used_line_height = resolved_line_height if resolved_line_height is not None else normal_height
    above = ascent + math.floor((used_line_height - glyph_height) / 2)
    below = used_line_height - above
    return {
        "source": owner, "owner": owner, "paint_style": paint_style,
        "font_size": font_size, "tokens": [("", 0.0)],
        "leading": leading_edge, "trailing": trailing_edge,
        "box_height": glyph_height + extra_height,
        "glyph_height": glyph_height, "ascent": ascent,
        "above": above, "below": below, "top_edge": top_edge_val,
        "space_width": 0.0, "atomic_width": 0.0, "margin_start": margin_start,
        "intrinsic_width": 0.0, "empty_strut": True,
    }


def _empty_decoration_only_run(owner, leading_edge, trailing_edge, top_edge_val, extra_height, margin_start):
    """A split-inline leading/trailing segment (CSS 2.1 9.2.1.1) with no
    real text on that side still needs its own fragment when it carries
    border/padding/margin decoration (see `_build_text_runs_from_nodes`'s
    own no-text-content branch) -- but unlike `_empty_inline_strut_run`'s
    case (a genuinely empty *inline* element, alone or beside real content
    on an ordinary shared line, which CSS 9.4.2 gives its own font/`line-
    height` contribution the same as a real text run), this fragment is
    never a real participant in inline flow at all: it exists purely to
    publish the wrapper's own border/padding for painting and `getClient
    Rects()`, on its own dedicated block-flow line (`_split_wrapping_
    inline_element`'s pieces each become their own ordinary block-flow
    child), where Chrome gives it exactly `0` height when otherwise empty
    -- no glyph/line-height contribution at all, only its own border/
    padding (`extra_height`).

    Found on `wpt/css/CSS2/normal-flow/block-in-inline-empty-001.xht`:
    reusing `_empty_inline_strut_run` unmodified gave the empty edge
    fragment a real `28px` (glyph height + leading/trailing) on top of its
    `10px` border/padding, inflating both the split wrapper's own height
    (`41px` -> `64px`) and body's (`36px` -> `64px`) -- Chrome reports the
    empty edge fragment's own height as exactly its border/padding, `0`
    otherwise."""
    return {
        "source": owner, "owner": owner, "paint_style": owner._chromonic_paint_style,
        "font_size": 0.0, "tokens": [("", 0.0)],
        "leading": leading_edge, "trailing": trailing_edge,
        "box_height": extra_height,
        "glyph_height": 0.0, "ascent": 0.0,
        "above": 0.0, "below": 0.0, "top_edge": top_edge_val,
        "space_width": 0.0, "atomic_width": 0.0, "margin_start": margin_start,
        "intrinsic_width": 0.0, "empty_strut": True,
    }


def _build_text_runs_from_nodes(child_nodes, paint_style, owner, *,
                                 leading_edge=0.0, trailing_edge=0.0,
                                 top_edge_val=0.0, extra_height=0.0,
                                 margin_start=0.0, margin_end=0.0, computed_cache=None):
    """Build inline-formatting-plan `runs` entries for the text/`<br>`
    content of `child_nodes` (in DOM order), attributing `leading_edge` (an
    owning element's own border-left+padding-left) to the very first text
    run and `trailing_edge` (border-right+padding-right) to the very last,
    plus `top_edge_val`/`extra_height` (top/bottom border+padding, added to
    every run's box height) and `margin_start`/`margin_end` (margin-left/
    -right, applied once each, before the first fragment and after the
    last, shifting placement without being part of any run's own rect).
    `[]` if `child_nodes` has no non-empty text.

    Extracted from `_make_inline_formatting_plan`'s own "kind == 'element'"
    branch so `_split_wrapping_inline_element` (CSS 2.1 9.2.1.1: an inline
    element split around an in-flow block child) can reuse the exact same
    per-run construction for each of its own fragments, passing 0 for
    whichever edge that particular fragment doesn't own (an interior
    fragment, between two block interruptions, owns neither).

    `computed_cache`, when given, additionally recognizes two more node
    kinds a split segment can carry that plain text/`<br>` alone doesn't
    cover (`_make_inline_formatting_plan`'s own call site never needs
    either -- it already bails, before ever reaching here, the moment
    `child_nodes` contains *any* element other than `<br>`):

    - A *simple* nested inline element (only text/`<br>` of its own, no
      further element nesting -- the same one-level scope limit used
      throughout this split machinery) is flattened in place via a
      recursive call, using *its own* paint style/edges, so e.g. a
      `<strong>` inside a split wrapper's segment keeps its own bold
      weight and box model instead of silently vanishing.
    - An absolutely-positioned element becomes an "escapee" run (`{
      "escapee": True, "element": ...}`) -- it contributes no width/height
      of its own (correct: it's out of flow), but its position in `runs`
      still marks *where* `_InlineFormattingPlan.measure()`'s cursor was
      when it appeared, which `publish()` turns into its real CSS 2.1
      10.3.7/10.6.4 static position (used when its own `top`/`left` are
      `auto`) -- see `_fix_inline_escapee_static_position`. Building its
      real subtree is `build()`'s own job (via `split_pieces`), not this
      function's; a run only ever *marks* it.

    Deeper nesting, or anything else neither text/`<br>` nor one of these
    two, is silently skipped, same as before this was added -- consistent
    with this whole split path's existing scope limits."""

    def is_text_bearing(node) -> bool:
        node_type = getattr(node, "nodeType", None)
        if node_type == TEXT_NODE:
            return bool(_collapsed_text_node(node).strip())
        if not _is_element(node):
            return False
        if (getattr(node, "tagName", "") or "").lower() == "br":
            return False
        if computed_cache is not None:
            _node_computed, node_style_obj = _describe(node, computed_cache)
            if _is_absolutely_positioned(node_style_obj):
                return False  # an escapee -- out of flow, no text-bearing slot
        return bool((getattr(node, "textContent", "") or "").strip())

    text_node_indices = [i for i, n in enumerate(child_nodes) if is_text_bearing(n)]
    if not text_node_indices:
        return []
    runs = []
    for node_index, child_node in enumerate(child_nodes):
        node_tag = (getattr(child_node, "tagName", "") or "").lower()
        if getattr(child_node, "nodeType", None) == TEXT_NODE:
            raw_text = _collapsed_text_node(child_node)
            if not raw_text:
                continue
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            run_leading = leading_edge if is_first_text else 0.0
            run_trailing = trailing_edge if is_last_text else 0.0
            t = _apply_text_transform(
                _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw_text),
                paint_style.get("text_transform"),
            )
            if not t.strip(_CSS_WHITESPACE_STRIP_CHARS):
                continue
            t = t.strip(_CSS_WHITESPACE_STRIP_CHARS)
            font_size_i = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
            family_i = ("" if paint_style["font_family"] == "none"
                        else paint_style["font_family"])
            weight_i = _parse_font_weight(paint_style["font_weight"])
            italic_i = fonts.is_italic(paint_style["font_style"])
            ascent_i, descent_i, normal_i = fonts.text_metrics(
                family_i, font_size_i, weight_i >= 600, italic_i)
            glyph_h_i = ascent_i + descent_i
            # See the identical comment at this same pattern's first
            # occurrence, above: an explicit `line-height: 0` must not be
            # treated the same as unset.
            resolved_lh_i = _resolved_line_height(paint_style["line_height"])
            used_lh_i = resolved_lh_i if resolved_lh_i is not None else normal_i
            above_i = ascent_i + math.floor((used_lh_i - glyph_h_i) / 2)
            below_i = used_lh_i - above_i
            box_h_i = glyph_h_i + extra_height
            one_w = layout_text("a", family_i, font_size_i,
                                font_weight=weight_i, italic=italic_i)[0]
            spaced_w = layout_text("a a", family_i, font_size_i,
                                   font_weight=weight_i, italic=italic_i)[0]
            space_w_i = max(0.0, spaced_w - 2.0 * one_w)
            tokens_i = []
            for tok in re.findall(r"\S+\s*|\s+", t):
                m, _h, _ls = layout_text(
                    tok, family_i, font_size_i,
                    font_weight=weight_i, italic=italic_i,
                    letter_spacing=_fontmetrics.parse_length(
                        paint_style["letter_spacing"], default=0.0),
                    word_spacing=_fontmetrics.parse_length(
                        paint_style["word_spacing"], default=0.0),
                )
                tokens_i.append((tok, sum(l[1] for l in _ls)))
            runs.append({
                "source": child_node, "owner": owner,
                "paint_style": paint_style,
                "font_size": font_size_i, "tokens": tokens_i,
                "leading": run_leading, "trailing": run_trailing,
                "box_height": box_h_i,
                "glyph_height": glyph_h_i, "ascent": ascent_i,
                "above": above_i, "below": below_i,
                "top_edge": top_edge_val,
                "space_width": space_w_i,
                "atomic_width": 0.0,
                "margin_start": margin_start if is_first_text else 0.0,
                "margin_end": margin_end if is_last_text else 0.0,
                "intrinsic_width": (
                    (margin_start if is_first_text else 0.0)
                    + run_leading + sum(w for _t, w in tokens_i)
                    + run_trailing
                    + (margin_end if is_last_text else 0.0)
                ),
            })
        elif node_tag == "br":
            runs.append({"break": True, "element": child_node})
        elif _is_element(child_node) and computed_cache is not None:
            child_computed, child_style = _describe(child_node, computed_cache)
            if _is_absolutely_positioned(child_style):
                runs.append({"escapee": True, "element": child_node,
                             "computed": child_computed, "style": child_style})
                continue
            non_br_element_children = [
                node for node in (child_node.childNodes or ())
                if _is_element(node) and (getattr(node, "tagName", "") or "").lower() != "br"
            ]
            if non_br_element_children:
                continue  # further nesting -- out of this split path's scope, dropped as before
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            nested_native = style_bridge.to_dict(child_style)
            nested_left = _numeric_edge(nested_native["padding"][3]) + _numeric_edge(nested_native["border"][3])
            nested_right = _numeric_edge(nested_native["padding"][1]) + _numeric_edge(nested_native["border"][1])
            nested_top = _numeric_edge(nested_native["padding"][0]) + _numeric_edge(nested_native["border"][0])
            nested_extra = (nested_top + _numeric_edge(nested_native["padding"][2])
                             + _numeric_edge(nested_native["border"][2]))
            nested_margin_start = _numeric_edge(nested_native["margin"][3])
            runs.extend(_build_text_runs_from_nodes(
                list(child_node.childNodes or ()), child_node._chromonic_paint_style, child_node,
                leading_edge=(leading_edge if is_first_text else 0.0) + nested_left,
                trailing_edge=(trailing_edge if is_last_text else 0.0) + nested_right,
                top_edge_val=top_edge_val + nested_top,
                extra_height=extra_height + nested_extra,
                margin_start=(margin_start if is_first_text else 0.0) + nested_margin_start,
                computed_cache=computed_cache,
            ))
    return runs


def _is_genuine_inline_wrapper(node, style_obj) -> bool:
    """Whether `node` is a genuinely `display:inline` wrapper, as opposed
    to an atomic inline-level box (`inline-block`, or a replaced element)
    that merely happens to also be inline-level. Only a genuine inline
    wrapper's own content is reachable "through" it for CSS 2.1 9.2.1.1
    purposes (`_contains_in_flow_block`'s own recursive walk) -- an
    `inline-block` establishes its own block formatting context and owns
    its descendants entirely, so a `display:block` child *inside* one must
    never be treated as if it were a direct block child of whatever
    ancestor merely contains that inline-block.

    Found on `wpt/css/CSS2/normal-flow/block-formatting-contexts-010.xht`:
    a `display:inline-block` `<span>` (200x200, its own BFC) containing
    two `display:block` children was itself being treated as "an inline
    wrapper split around a block child" by its own parent's own split
    check, hoisting its two children out to become direct block-flow
    siblings of the *outer* `<div>` (`784px` wide -- the outer div's own
    content width, with `height:25%` resolving against nothing sensible)
    instead of staying inside the inline-block's own `200px` box, which
    vanished from layout entirely (`0x0`, no fragment) as a result."""
    tag_name = (getattr(node, "tagName", "") or "").lower()
    return (
        tag_name not in _REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and _trusts_computed_inline(node, tag_name)
    )


def _contains_in_flow_block(element, computed_cache) -> bool:
    """Whether `element`'s subtree contains a genuine in-flow (not
    absolutely positioned), block-level descendant reachable by walking
    only through inline-level elements -- the CSS 2.1 9.2.1.1 "anonymous
    block box" trigger: an inline element that contains an in-flow block
    child splits around it instead of the block being (wrongly) folded
    into the surrounding inline formatting context.

    `select`/`svg` are never walked into -- `_child_elements` already
    treats their real DOM children (`<option>`, SVG shapes) as not real
    layout content at all (`build()`'s `tag_name in ("select", "svg")`
    special-case), and this function must agree: found as a regression
    (`<option>` ending up its own laid-out Taffy node, `<select>` mistaken
    for an inline element split around one of its own `<option>`s) --
    `element.childNodes` has no such filtering built in, unlike
    `_child_elements`."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg"):
        return False
    for node in element.childNodes or ():
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style):
            continue
        if not _is_inline_level(node, child_style):
            return True
        if _is_genuine_inline_wrapper(node, child_style) and _contains_in_flow_block(node, computed_cache):
            return True
    return False


def _has_direct_in_flow_block_child(element, computed_cache) -> bool:
    """Like `_contains_in_flow_block`, but shallow -- true only when one of
    `element`'s *own* immediate children is a genuine in-flow block, not
    when a further-nested inline descendant merely contains one deeper
    down. Distinguishes CSS 2.1 9.2.1.1's two shapes, which need different
    handling in `_split_inline_flow_around_blocks`: `element` itself
    directly parenting a block (`<div id=x style="display:inline">text
    <div>block</div>text</div>` -- `element` itself must split, via
    `_split_wrapping_inline_element` called on `element`) versus a nested
    wrapper doing so (`<div><span>text<div>block</div>text</span></div>`
    -- only the nested `<span>` splits, `element` itself stays an ordinary
    block container of plain text/element items)."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg"):
        return False
    for node in element.childNodes or ():
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style):
            continue
        if not _is_inline_level(node, child_style):
            return True
    return False


def _split_wrapping_inline_element(wrapper, computed_cache, container):
    """CSS 2.1 9.2.1.1: `wrapper`, an inline-level element whose own
    children include a genuine in-flow block (`_contains_in_flow_block`),
    splits into a sequence of fragments around each such block -- yields
    `("run", runs)` (a `_build_text_runs_from_nodes` result, possibly
    empty) or `("block", child, child_computed, child_style)`, in DOM
    order. Only the first run fragment gets `wrapper`'s own left
    border/padding/margin; only the last gets its right border/padding --
    an interior fragment (between two block interruptions) gets neither,
    matching how a real inline box's edges only ever show up on its
    outermost fragments. `wrapper` itself is never built as a Taffy node --
    the split exists only in the layout projection; its DOM (and the real
    `wrapper` element/its children) is untouched, so a later relayout with
    different content still walks the same real nodes.

    `container` (`_split_inline_flow_around_blocks`'s own `element` -- the
    real ancestor whose ordinary block-flow children the split pieces
    become) is stashed on `wrapper` for two post-layout corrections that
    need its finished geometry, not available yet at this (pre-Taffy-
    compute) point: `_finalize_inline_owner_boxes`'s block-interruption
    marker rect (CSS 2.1 9.2.1.1's anonymous block box is `width:auto` --
    100% of *this* containing block, not the real block child's own,
    possibly narrower, width) and `_fix_split_inline_relative_offset`'s
    percentage `top`/`left` basis."""
    wrapper._chromonic_split_container = container
    wrapper_computed, wrapper_style_obj = _describe(wrapper, computed_cache)
    native = style_bridge.to_dict(wrapper_style_obj)
    wrapper._chromonic_native_style = native
    left_edge = _numeric_edge(native["padding"][3]) + _numeric_edge(native["border"][3])
    right_edge = _numeric_edge(native["padding"][1]) + _numeric_edge(native["border"][1])
    # CSS 2.1 9.2.1.1: the split's own leading/trailing fragments carry the
    # wrapper's *logical* start/end edge, not always its physical left/
    # right -- in `direction:rtl`, the fragment generated first (DOM
    # order, `is_first` below) is the one nearer the line's visual right,
    # so it owns the wrapper's own right border/padding, and the last
    # (`is_last`) fragment owns the left -- physical left/right are always
    # swapped from the `ltr` assignment, not just visually repositioned.
    # Found on `wpt/css/CSS2/normal-flow/block-in-inline-empty-002.xht`/
    # `-004.xht`: `direction:rtl` with `padding-right`/`padding-left`
    # respectively -- Chrome puts the decorated (non-zero-width) fragment
    # on the *leading*/*trailing* side opposite of what the plain
    # physical-edge assignment below would give.
    is_rtl = (getattr(wrapper_computed, "direction", "ltr") or "ltr").strip().lower() == "rtl"
    if is_rtl:
        left_edge, right_edge = right_edge, left_edge
    top_edge_val = _numeric_edge(native["padding"][0]) + _numeric_edge(native["border"][0])
    extra_height = (top_edge_val + _numeric_edge(native["padding"][2])
                    + _numeric_edge(native["border"][2]))
    margin_start = _numeric_edge(native["margin"][3])
    if wrapper is container:
        # The direct-child ("element itself splits") shape: `wrapper` is a
        # real Taffy node (`build()`'s `split_pieces` handling), so Taffy
        # has *already* physically shifted every one of its text-leaf
        # children by this same border/padding/margin -- unlike a nested
        # wrapper (never built as a real node at all, see this function's
        # own docstring), where nothing else ever applies them and the
        # run-level math below is the only place they take effect.
        # `left_edge`/`right_edge`/`margin_start` are zeroed here to avoid
        # double-counting horizontal *position* (which the run math
        # below, and `publish()` after it, computes as a straight
        # addition-then-subtraction that exactly cancels back to the
        # leaf's own, already-shifted position -- see `_finalize_inline_
        # owner_boxes`'s own comment on this); `top_edge_val` stays, since
        # `box_height` never subtracts it back out (a real, one-way
        # addition, so it can't double-count) -- but its own vertical
        # *position* cancels exactly the same way the horizontal one does,
        # so `_finalize_inline_owner_boxes` corrects that too, from
        # `wrapper._chromonic_split_self_edges` below. Found on `wpt/css/
        # CSS2/linebox/inline-box-001.xht`: every fragment landed `2px`
        # right of, and `2px` below, Chrome's, in both cases because the
        # run math added that border's width/position on top of a leaf
        # position Taffy had already shifted by that same border.
        wrapper._chromonic_split_self_edges = (left_edge, right_edge, top_edge_val)
        left_edge = right_edge = margin_start = 0.0
    else:
        wrapper.__dict__.pop("_chromonic_split_self_edges", None)
    paint_style = wrapper._chromonic_paint_style

    segments: list = [[]]
    blocks: list = []
    for node in wrapper.childNodes or ():
        if _is_element(node):
            tag = (getattr(node, "tagName", "") or "").lower()
            if tag in _NON_RENDERING_TAGS:
                continue
            if tag != "br":
                child_computed, child_style = _describe(node, computed_cache)
                if not _renders(child_style):
                    continue
                if not _is_absolutely_positioned(child_style) and not _is_inline_level(node, child_style):
                    blocks.append((node, child_computed, child_style))
                    segments.append([])
                    continue
        segments[-1].append(node)

    # `getClientRects()`/`getBoundingClientRect()`: Chrome exposes one extra,
    # zero-height rect per interruption -- positioned exactly where the
    # interrupting block sits -- alongside the real leading/trailing
    # fragment rects (confirmed: a 2-fragment split's `element.getClientRects
    # ()` returns *3* rects in real Chrome, not 2). `_finalize_inline_owner_
    # boxes` adds these once boxes are final; record which blocks to use here
    # (overwritten fresh on every relayout that reaches this branch) rather
    # than recomputing the split there, where only `owner_accum`'s already-
    # merged rects are visible.
    wrapper._chromonic_interruption_blocks = [block for block, _computed, _style in blocks]
    wrapper._chromonic_atomic_segment_elements = {}
    wrapper._chromonic_split_edge_flow_height = {}
    # `wrapper` itself is never built as a real Taffy node at all when it's
    # a *nested* wrapper (see this function's own docstring) -- it never
    # ends up in `node_map`, so nothing that only ever walks `node_map.
    # values()` (e.g. `_fix_nested_split_flow_extent`) can discover it
    # directly. Each real interruption block *is* a genuine node, though,
    # so a back-reference on it is a reliable way back to its wrapper.
    for block, _computed, _style in blocks:
        block.__dict__["_chromonic_split_wrapper_ref"] = wrapper

    for index, seg_nodes in enumerate(segments):
        is_first, is_last = index == 0, index == len(segments) - 1
        seg_leading = left_edge if is_first else 0.0
        seg_trailing = right_edge if is_last else 0.0
        seg_margin_start = margin_start if is_first else 0.0
        runs = _build_text_runs_from_nodes(
            seg_nodes, paint_style, wrapper,
            leading_edge=seg_leading,
            trailing_edge=seg_trailing,
            top_edge_val=top_edge_val, extra_height=extra_height,
            margin_start=seg_margin_start,
            computed_cache=computed_cache,
        )
        if not runs and (seg_leading or seg_trailing):
            # A leading/trailing segment with no real text of its own
            # still needs a fragment when it has real *inline extent* --
            # `padding-left`/`padding-right` (whichever this segment owns,
            # `seg_leading`/`seg_trailing` above) making its own box
            # genuinely non-zero-width -- CSS 2.1 9.2.1.1's split still
            # generates an (anonymous) inline box on this side even when
            # no text sits between the wrapper's own edge and the block
            # interruption. Such a fragment *is* a real line (CSS 9.4.2,
            # the same font/line-height contribution a genuinely empty
            # ordinary inline gets -- `_empty_inline_strut_run`), so its
            # own height is the font's line-height plus the wrapper's
            # vertical border/padding (`extra_height`) on top of it, not
            # `extra_height` alone.
            #
            # A segment with *no* inline extent (no horizontal padding
            # assigned to it) is different: genuinely `0x0`, and must not
            # be treated as a line at all -- no font contribution, and
            # (`top_edge_val`/`extra_height` deliberately excluded from
            # this `if`'s own condition) no vertical border either, since
            # there is nothing here for a border to wrap around. Found on
            # `wpt/css/CSS2/normal-flow/block-in-inline-empty-001.xht`
            # (`border-top/bottom` + `padding-right` only, no `padding-
            # left`): the leading segment (no `padding-left`) was
            # incorrectly still getting the wrapper's vertical border as
            # its own height, adding a spurious extra line and shifting
            # everything after it down by that height.
            runs = [_empty_inline_strut_run(
                wrapper, seg_leading, seg_trailing, top_edge_val, extra_height, seg_margin_start,
            )]
            # `_adjust_body_collapsed_margins`-style ancestor auto-height
            # must not read this fragment's own *visual* box height (which
            # includes the wrapper's vertical border/padding on top of the
            # line) -- only the real *line box* itself (`above`+`below`,
            # the same font-metrics-derived line-height every other line
            # in this document advances block flow by) actually consumes
            # ordinary block-flow space; the border/padding is decoration
            # that can visually extend past the line without pushing
            # anything below it further down. Stashed per edge
            # (`_fix_nested_split_flow_extent` reads it post-layout to
            # correct the wrapper's own flow contribution).
            wrapper.__dict__.setdefault("_chromonic_split_edge_flow_height", {})[
                "leading" if is_first else "trailing"
            ] = runs[0]["above"] + runs[0]["below"]
        if not runs:
            # A segment can also come up empty not because there's nothing
            # in it, but because it holds one or more atomic inline-level
            # elements (`inline-block`/replaced) with no text alongside
            # them -- `_build_text_runs_from_nodes` has no way to build a
            # real subtree for those at all (it only ever flattens text/
            # `<br>`/a simple text-only nested wrapper). Represented the
            # same way a genuine block interruption already is -- a real,
            # recursively-built subtree, its own ordinary block-flow piece
            # -- which is exactly right here: each is alone in its own
            # segment already (nothing else to stay inline beside).
            #
            # Found on `wpt/css/CSS2/normal-flow/block-in-inline-client-
            # rects-001.html`: a `<span>` whose content is an `inline-
            # block` bar, a plain `<div>` (the real block interruption),
            # and a second `inline-block` bar published *no* fragment at
            # all for either bar -- `getBoundingClientRect()`'s union
            # never included their width, reporting `0` instead of the
            # `200`/`500`/`500` the fixture expects.
            atomic_candidates = [
                (node,) + _describe(node, computed_cache)
                for node in seg_nodes if _is_element(node)
            ]
            if atomic_candidates and all(
                _is_inline_level(node, node_style) and not _is_genuine_inline_wrapper(node, node_style)
                for node, _node_computed, node_style in atomic_candidates
            ):
                wrapper._chromonic_atomic_segment_elements[index] = [node for node, _c, _s in atomic_candidates]
                for node, node_computed, node_style in atomic_candidates:
                    yield ("block", node, node_computed, node_style)
                # A zero-edge marker run, not a real fragment of its own --
                # `_finalize_inline_owner_boxes` only interleaves a genuine
                # block interruption's own anonymous-block marker rect
                # between *groups* of already-tagged runs; without at
                # least one (even contentless) run tagged to this segment,
                # that segment contributes no group at all, and a wrapper
                # whose every segment is atomic-only would end up with no
                # groups whatsoever -- silently skipping the real block
                # interruption's own marker rect too, not just this
                # segment's.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
            else:
                # Genuinely nothing on this side (no text, no inline
                # extent, no atomic content) -- still published as an
                # explicit `0x0` fragment (Chrome reports one), but must
                # not contribute any height/font participation of its own
                # or advance the block-flow stack at all.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
        if runs:
            # Tags each run with which segment (0-based, in split/document
            # order) it belongs to, so `_finalize_inline_owner_boxes` can
            # place the interruption-marker rects at their *logical* split
            # position -- Chrome preserves document order in
            # `getClientRects()`, not a geometric top-to-bottom/left-to-
            # right sort (which happened to put a later segment's rect
            # before an earlier interruption's marker purely because of a
            # couple of stray pixels in this project's own line-height
            # rounding).
            for run in runs:
                if not run.get("break"):
                    run["split_group"] = index
            yield ("run", runs)
        if index < len(blocks):
            yield ("block",) + blocks[index]


def _split_inline_flow_around_blocks(element, inline_items, style, css_display, computed_cache):
    """`inline_items` (`_inline_mixed_content`'s own shape) contains, at
    some depth reachable only through inline-level elements, a genuine
    in-flow block -- CSS 2.1 9.2.1.1's "anonymous block box" case
    (`<div><span>One<div/>Two</span></div>`: the block's presence forces
    `span` to split into a "One" fragment, the real block, and a "Two"
    fragment, each a sibling in normal block flow -- not one flex row, and
    not folded into a single inline run the way `_make_inline_formatting_
    plan` alone would (return `None`, unable to represent a block inside
    one measured text leaf) or discarded into `build()`'s flex-row/wrap
    fallback (loses the CSS-required block-level split entirely).

    Returns an ordered list of pieces -- `("plan", _InlineFormattingPlan)`
    or `("block", child, child_computed, child_style)` -- ready to become
    `element`'s Taffy children as an *ordinary* block stack (the caller
    must not use flex for this), each plan becoming one measured text leaf
    and each block its own recursively-built subtree via `build()`. `None`
    if nothing in `inline_items` actually needs splitting -- the caller
    should fall back to its existing handling unchanged.

    Two distinct shapes both reach this function, needing different
    handling (`_has_direct_in_flow_block_child` distinguishes them):
    `element` itself may be the genuinely-inline element directly
    parenting the block (`<div id=x style="display:inline">text<div>
    block</div>text</div>` -- `element`=`x`, splits itself, dispatched
    immediately below, reusing `_split_wrapping_inline_element` on
    `element`), or a plain block `element` may merely contain a *nested*
    inline item that itself wraps a block one level deeper (`<div><span>
    text<div>block</div>text</span></div>` -- only the nested `<span>`
    splits; `element` stays an ordinary container of plain items, handled
    by the per-item loop below, unchanged from before this distinction
    existed)."""
    if _has_direct_in_flow_block_child(element, computed_cache):
        pieces: list = []
        runs_acc: list = []
        for sub in _split_wrapping_inline_element(element, computed_cache, element):
            if sub[0] == "run":
                runs_acc.extend(sub[1])
            else:
                if runs_acc:
                    plan = _InlineFormattingPlan(
                        element, runs_acc, element._chromonic_paint_style, css_display)
                    plan._chromonic_final_split_fragment = False
                    pieces.append(("plan", plan))
                    runs_acc = []
                pieces.append(sub)
        if runs_acc:
            plan = _InlineFormattingPlan(
                element, runs_acc, element._chromonic_paint_style, css_display)
            plan._chromonic_final_split_fragment = True
            pieces.append(("plan", plan))
        return pieces
    element.__dict__.pop("_chromonic_interruption_blocks", None)
    element.__dict__.pop("_chromonic_split_container", None)
    pieces: list = []
    pending: list = []
    found_split = False

    def flush_pending():
        if pending:
            plan = _make_inline_formatting_plan(element, list(pending), style, css_display)
            if plan is not None:
                pieces.append(("plan", plan))
            pending.clear()

    for kind, item, text, child_computed, child_style in inline_items:
        if (kind == "element" and not _is_absolutely_positioned(child_style)
                and _is_genuine_inline_wrapper(item, child_style)
                and _contains_in_flow_block(item, computed_cache)):
            found_split = True
            flush_pending()
            runs_acc: list = []
            for sub in _split_wrapping_inline_element(item, computed_cache, element):
                if sub[0] == "run":
                    runs_acc.extend(sub[1])
                else:
                    if runs_acc:
                        plan = _InlineFormattingPlan(
                            element, runs_acc, element._chromonic_paint_style, css_display)
                        # A block interruption follows -- this plan is a
                        # *leading* or *interior* segment, never the
                        # wrapper's true trailing one, regardless of
                        # whether it happens to be the only (and so,
                        # locally, "last") entry in its own `placed` list.
                        # `measure()`'s RTL mirror must not apply the
                        # wrapper's margin-right to it on that false
                        # signal -- margin-right belongs only to the one
                        # segment that comes after every interruption.
                        plan._chromonic_final_split_fragment = False
                        pieces.append(("plan", plan))
                        runs_acc = []
                    pieces.append(sub)
            if runs_acc:
                # Nothing follows this plan for `item` -- the real trailing
                # segment, where `measure()` should apply the wrapper's
                # margin-right normally (the default when this attribute is
                # absent, as for every ordinary non-split plan).
                plan = _InlineFormattingPlan(
                    element, runs_acc, element._chromonic_paint_style, css_display)
                plan._chromonic_final_split_fragment = True
                pieces.append(("plan", plan))
        else:
            pending.append((kind, item, text, child_computed, child_style))
    flush_pending()
    return pieces if found_split else None


def _make_inline_formatting_plan(element, inline_items, style, css_display):
    """Build styled text runs for a shared inline formatting context."""
    if any(kind == "element" and (
            _is_absolutely_positioned(child_style)
            or isinstance(item, _PseudoElement)
            # A *real* nested element with only text children (no further
            # element nesting) would otherwise be absorbed straight into
            # this plan as flattened text runs (see the "element" branch
            # below, `_build_text_runs_from_nodes`) -- which never calls
            # `build()` on it at all, so its *own* `::before`/`::after`
            # (one level deeper than this function ever looks) would
            # silently never be considered. Bail so the flex-row fallback's
            # real recursive `build()` call on it runs instead, exactly
            # like an absolutely-positioned or pseudo item already does.
            or getattr(item, "_chromonic_before_pseudo", None) is not None
            or getattr(item, "_chromonic_after_pseudo", None) is not None)
           for kind, item, _text, _computed, child_style in inline_items):
        # A generated-content pseudo-element needs its own real box (own
        # font, own position, possibly absolute) -- this shared-plan path
        # only ever measures *text runs* sharing one Taffy leaf, with no
        # way to represent a distinct nested box at all, let alone one an
        # empty-`content` pseudo (an icon-only box with no text of its own,
        # e.g. `csszengarden.com`'s `h1::before`) needs just to exist. The
        # flex-row fallback below already builds every "element" item as
        # its own real recursive `build()` subtree -- exactly what a
        # pseudo-element needs -- so route it there unconditionally,
        # same as an absolutely-positioned item already does.
        return None
    runs = []
    for kind, item, collapsed, child_computed, child_style in inline_items:
        if kind == "break":
            # Forced line-break: store a sentinel run so measure() can end the line.
            runs.append({"break": True, "element": item})
            continue
        if kind == "text":
            source = item.source
            owner = element
            paint_style = element._chromonic_paint_style
            native = None
        else:
            # Nested markup is flattened into this plan only when it contains
            # text and its element children are all <br> forced line-breaks.
            # Other element descendants (nested spans, etc.) stay on the
            # established retained projection.
            non_br_element_children = [
                node for node in (item.childNodes or [])
                if _is_element(node)
                and (getattr(node, "tagName", "") or "").lower() != "br"
            ]
            if non_br_element_children:
                return None
            # `item` is taking the ordinary (non-split) path this layout --
            # clear any stale `_chromonic_interruption_blocks` a *previous*
            # layout's `_split_wrapping_inline_element` may have left on it
            # (its block child since removed/changed), so
            # `_finalize_inline_owner_boxes` doesn't insert a phantom
            # interruption-marker rect using now-unrelated geometry.
            item.__dict__.pop("_chromonic_interruption_blocks", None)
            item.__dict__.pop("_chromonic_split_container", None)
            # Walk childNodes to collect text segments and <br> breaks,
            # producing runs for each and decorating them with the child
            # element's border+padding edges (CSS 2.1: first fragment gets
            # left edge, last fragment gets right edge). See
            # `_build_text_runs_from_nodes` -- also reused, with a real
            # split, by `_split_wrapping_inline_element`.
            native = style_bridge.to_dict(child_style)
            item._chromonic_native_style = native
            left_edge = (_numeric_edge(native["padding"][3])
                         + _numeric_edge(native["border"][3]))
            right_edge = (_numeric_edge(native["padding"][1])
                          + _numeric_edge(native["border"][1]))
            top_edge_val = (_numeric_edge(native["padding"][0])
                            + _numeric_edge(native["border"][0]))
            extra_height = (top_edge_val
                            + _numeric_edge(native["padding"][2])
                            + _numeric_edge(native["border"][2]))
            # margin-left/-right apply before the first/after the last LTR
            # fragment only; CSS 2.1 10.3.1/10.3.3: real spacing, but never
            # part of either fragment's own rect, and -- unlike a block's
            # vertical margins -- never collapses with an adjoining
            # element's own margin (`_InlineFormattingPlan.measure()`'s own
            # `margin_end` handling adds both sides independently).
            margin_start = _numeric_edge(native["margin"][3])
            margin_end = _numeric_edge(native["margin"][1])
            child_runs = _build_text_runs_from_nodes(
                list(item.childNodes or []), item._chromonic_paint_style, item,
                leading_edge=left_edge, trailing_edge=right_edge,
                top_edge_val=top_edge_val, extra_height=extra_height,
                margin_start=margin_start, margin_end=margin_end,
            )
            item_display = (getattr(child_computed, "display", "") or "").strip().lower()
            item_tag = (getattr(item, "tagName", "") or "").lower()
            if (not child_runs and not (item.childNodes or [])
                    and item_display == "inline" and item_tag not in _REPLACED_OR_CONTROL_TAGS):
                # CSS 2.1 9.2.1.1/10.8's empty-inline strut applies only to
                # a plain, non-replaced `display:inline` element -- an
                # `inline-block` (or replaced element) is an *atomic*
                # inline-level box that keeps its own explicit used width/
                # height even with no content at all (CSS 2.1 10.3.10:
                # `width`/`height` DO apply to it, unlike a non-replaced
                # inline). Found regressing `wpt/css/CSS2/linebox/
                # fractional-line-height.html`: an empty `display:inline-
                # block` `<span style="width:10px;height:100.25px">`
                # dropped its own explicit `10x100.25` box entirely and
                # got a zero-width, font-metrics-height strut instead,
                # right after the strut fix above was added for the
                # genuinely-non-replaced-inline case.
                child_runs = [_empty_inline_strut_run(
                    item, left_edge, right_edge, top_edge_val, extra_height, margin_start,
                )]
            runs.extend(child_runs)
            continue
        raw = getattr(source, "textContent", "") or collapsed or ""
        text = _apply_text_transform(_CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw), paint_style.get("text_transform"))
        if not text.strip(_CSS_WHITESPACE_STRIP_CHARS):
            continue
        # Whitespace collapses across run boundaries. Keep a single leading
        # or trailing space only when the source actually contains one.
        inferred_leading = (kind == "text" and
                            getattr(item, "_chromonic_leading_collapsed_space", False))
        text = ((" " if (raw[:1] and raw[:1] in _CSS_WHITESPACE_STRIP_CHARS) or inferred_leading else "")
                + text.strip(_CSS_WHITESPACE_STRIP_CHARS)
                + (" " if raw[-1:] and raw[-1:] in _CSS_WHITESPACE_STRIP_CHARS else ""))
        font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
        family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
        weight = _parse_font_weight(paint_style["font_weight"])
        italic = fonts.is_italic(paint_style["font_style"])
        ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
        glyph_height = ascent + descent
        # See the identical comment at this same pattern's first
        # occurrence, above: an explicit `line-height: 0` must not be
        # treated the same as unset.
        resolved_line_height = _resolved_line_height(paint_style["line_height"])
        used_line_height = resolved_line_height if resolved_line_height is not None else normal_height
        above = ascent + math.floor((used_line_height - glyph_height) / 2)
        below = used_line_height - above
        nowrap = bool(kind == "element" and child_computed.whiteSpace == "nowrap")
        token_texts = [text] if nowrap else re.findall(r"\S+\s*|\s+", text)
        one_width = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
        spaced_width = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
        space_width = max(0.0, spaced_width - 2.0 * one_width)
        tokens = []
        for token in token_texts:
            measured, _height, _lines = layout_text(
                token, family, font_size, font_weight=weight, italic=italic,
                letter_spacing=_fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0),
                word_spacing=_fontmetrics.parse_length(paint_style["word_spacing"], default=0.0),
            )
            measured = sum(line[1] for line in _lines)
            tokens.append((token, measured))
        leading = trailing = 0.0
        box_height = glyph_height
        top_edge = 0.0
        if native is not None:
            top_edge = _numeric_edge(native["padding"][0]) + _numeric_edge(native["border"][0])
            leading = _numeric_edge(native["padding"][3]) + _numeric_edge(native["border"][3])
            trailing = _numeric_edge(native["padding"][1]) + _numeric_edge(native["border"][1])
            box_height += (_numeric_edge(native["padding"][0]) + _numeric_edge(native["padding"][2])
                           + _numeric_edge(native["border"][0]) + _numeric_edge(native["border"][2]))
            if isinstance(native["height"], (int, float)):
                box_height = max(box_height, float(native["height"]))
        atomic_width = (float(native["width"])
                        if native is not None and isinstance(native["width"], (int, float)) else 0.0)
        runs.append({
            "source": source, "owner": owner, "paint_style": paint_style,
            "font_size": font_size, "tokens": tokens, "leading": leading,
            "trailing": trailing, "box_height": box_height,
            "glyph_height": glyph_height, "ascent": ascent,
            "above": above, "below": below, "top_edge": top_edge,
            "space_width": space_width,
            "atomic_width": atomic_width,
            "intrinsic_width": leading + max(
                atomic_width, sum(width for _text, width in tokens)
            ) + trailing,
        })
    # DOM boundaries with identical shaping properties are not kerning
    # boundaries. Preserve the pair adjustment across adjacent text owners.
    shaping_keys = ("font_family", "font_size", "font_weight", "font_style",
                    "letter_spacing", "word_spacing")
    for left, right in zip(runs, runs[1:]):
        if left.get("break") or right.get("break"):
            continue
        if left["trailing"] or right["leading"] or left["atomic_width"] or right["atomic_width"]:
            continue
        if any(left["paint_style"][key] != right["paint_style"][key] for key in shaping_keys):
            continue
        ls = left["paint_style"]
        a = ''.join(token for token, _width in left["tokens"])
        b = ''.join(token for token, _width in right["tokens"])
        def advance(value):
            return sum(line[1] for line in layout_text(
                value, ls["font_family"], left["font_size"],
                font_weight=_parse_font_weight(ls["font_weight"]), italic=fonts.is_italic(ls["font_style"]),
                letter_spacing=_fontmetrics.parse_length(ls["letter_spacing"], default=0.0),
                word_spacing=_fontmetrics.parse_length(ls["word_spacing"], default=0.0))[2])
        adjustment = advance(a + b) - advance(a) - advance(b)
        token, old_width = left["tokens"][-1]
        left["tokens"][-1] = (token, old_width + adjustment)
        left["intrinsic_width"] += adjustment
    return _InlineFormattingPlan(element, runs, element._chromonic_paint_style, css_display) if runs else None


def _group_inline_element_runs(tree, parent, entries, parent_style, node_map, projection):
    """Wrap consecutive inline siblings in retained anonymous flex rows."""
    if parent_style["display"] != "block":
        return [node_id for _child, _style, node_id in entries]
    output, run_index, index = [], 0, 0
    cache = parent.__dict__.setdefault("_chromonic_inline_runs", {})
    while index < len(entries):
        child, child_style, node_id = entries[index]
        if not _is_inline_level(child, child_style):
            output.append(node_id)
            index += 1
            continue
        run = []
        while index < len(entries) and _is_inline_level(entries[index][0], entries[index][1]):
            run.append(entries[index][2])
            index += 1
        wrapper = cache.get(run_index)
        if wrapper is None:
            wrapper = cache[run_index] = _AnonymousInlineRun(None, parent)
        run_index += 1
        wrapper_style = _inline_text_style(parent_style)
        # A real inline formatting context's default cross-axis alignment is
        # the text baseline, not CSS's flex initial value ("normal"/stretch)
        # -- without this, a shorter inline-block sibling top-aligns with a
        # taller one instead of sitting on the shared baseline the way
        # `elif inline_items:` below already gives siblings mixed with text.
        wrapper_style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap",
                              "align_items": "baseline"})
        wrapper._chromonic_native_style = wrapper_style
        wrapper_id = (projection.upsert(wrapper, wrapper_style, run, None, None)
                      if projection else tree.new_with_children(wrapper_style, run))
        node_map[wrapper_id] = wrapper
        output.append(wrapper_id)
    for stale in [key for key in cache if key >= run_index]:
        cache.pop(stale)
    return output


def _parse_font_weight(value) -> float:
    """CSS `font-weight`'s computed value -- domonic normalises it to a
    plain numeric string ("400"/"700") for anything but a genuinely unknown
    input, but named fallbacks are handled here too rather than assumed."""
    if not value:
        return 400.0
    text = str(value).strip().lower()
    named = {"normal": 400.0, "bold": 700.0, "bolder": 700.0, "lighter": 300.0}
    if text in named:
        return named[text]
    try:
        return float(text)
    except ValueError:
        return 400.0


def _resolved_line_height(value) -> "float | None":
    """`None` means "normal" -- let Parley use the font's own metrics-based
    line height (its own default), rather than guessing one ourselves.
    domonic already resolves an explicit `line-height` (unitless or not) to
    a plain `"Npx"` string, so this is just `_fontmetrics.parse_length`
    guarded against the unset/"normal" case."""
    if not value or value == "normal":
        return None
    return _fontmetrics.parse_length(value, default=None)


def _make_measure(paint_style: dict, text: str, element):
    """Real text layout via Parley (`chromonic._native.layout_text`) -- font
    matching (`fontique`), shaping, and genuine Unicode line-breaking,
    replacing what used to be `domonic._fontmetrics`'s one hardcoded
    Helvetica-shaped advance-width table plus a hand-rolled greedy
    word-wrap. The raw CSS `font-family` value ("Georgia, 'Times New
    Roman', serif", or the literal string `"none"` when unset) is taken
    from `paint_style` (`tree.py`'s `_extract_paint_style`) -- the same
    already-extracted dict `paint.py` reads, deliberately, *not* a fresh
    `computed.fontFamily` access: see `_describe`'s docstring for why
    (a `ComputedStyleDeclaration` attribute access is expensive -- it
    re-resolves from scratch every time -- and this text leaf shouldn't
    pay that cost a second time when the paint-style extraction already
    paid it once). Parley/`parlance` parse a real CSS font-family list
    directly, generic keywords included, so it's passed straight through
    with no translation needed on this side (unlike `chromonic.fonts`, which
    still has to resolve a *painting* typeface itself -- skia-python does
    its own font matching and shares no glyph-ID space with Parley's, so
    Parley's job here is strictly the layout decisions: where lines break
    and how much space the result needs, not what actually gets drawn).

    `Element.getBBox()` is SVG-only (domonic zeroes it for any other
    element -- see `dom.py`'s `getBBox`) and was never a measurement option
    here to begin with; this reach into `chromonic._native` for a real text
    layout engine is what stands in for it."""
    font_family = paint_style["font_family"]
    if font_family == "none":
        font_family = ""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    font_weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    letter_spacing = _fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0)
    word_spacing = _fontmetrics.parse_length(paint_style["word_spacing"], default=0.0)
    line_height = _resolved_line_height(paint_style["line_height"])
    ascent, descent, normal_height = fonts.text_metrics(font_family, font_size, font_weight >= 600, italic)

    def measure(available_width, available_height):
        width, height, lines = layout_text(
            text, font_family, font_size,
            font_weight=font_weight, italic=italic,
            max_width=None if paint_style.get("white_space") in ("pre", "nowrap") else available_width,
            letter_spacing=letter_spacing, word_spacing=word_spacing, line_height=line_height,
        )
        if line_height is None and lines:
            # Chrome exposes integral CSS line-box heights for the platform
            # fonts in our fixtures (15px Arial -> 17px, 32px -> 34px), while
            # Parley's raw font metrics retain fractional ascender/descent
            # values. Keep glyph widths subpixel-precise, but normalize the
            # implicit `normal` line box before it accumulates down a page.
            lines = [(line_text, line_width, normal_height)
                     for line_text, line_width, _height in lines]
            height = normal_height * len(lines)
        # stashed for paint.py -- the *only* record of how this text wrapped
        # (and, now, its real per-line height); paint draws exactly these
        # lines rather than re-wrapping or re-measuring itself.
        element._chromonic_text_lines = [line_text for line_text, _line_width, _line_height in lines]
        element._chromonic_text_line_widths = [line_width for _line_text, line_width, _line_height in lines]
        element._chromonic_line_height = lines[0][2] if lines else font_size * 1.2
        return (width, height)

    return measure


def _form_control_display_text(element) -> str:
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name == "textarea":
        return getattr(element, "value", "") or element.textContent or ""
    input_type = (element.getAttribute("type") or "text").lower()
    if input_type in {"checkbox", "radio", "button", "submit", "reset", "file", "hidden"}:
        return ""
    value = getattr(element, "value", "") or ""
    if value and input_type == "password":
        text = "•" * len(str(value))
    else:
        text = str(value) if value else element.getAttribute("placeholder") or ""
    style = element.__dict__.get("_chromonic_paint_style", {})
    return _apply_text_transform(text, style.get("text_transform"))


def _select_display_text(element) -> str:
    """`<select>` is a native, *closed* dropdown: a real browser shows only
    its currently-selected option's text, never all of them stacked as
    visible content -- the full list only ever appears in an OS-native
    popup while it's open, which isn't something chromonic draws at all.
    Found rendering `https://www.wikipedia.org/`: its language-picker
    `<select>` has 250+ `<option>`s, and with no special handling at all a
    `<select>` is just an ordinary element with element children --
    `build()` recursed into every `<option>` as if it were regular block
    content, stacking all 250+ as real, visible, full-height boxes right
    on the page (the "wall of grey language names" overlapping everything
    below the search box). Picks the first `<option selected>`, falling
    back to the first `<option>` at all (a real `<select>` with nothing
    marked `selected` shows its first option), and `""` if it has none."""
    options = element.getElementsByTagName("option")
    if not options:
        return ""
    for opt in options:
        if opt.hasAttribute("selected"):
            return " ".join((opt.textContent or "").split())
    return " ".join((options[0].textContent or "").split())


def _apply_image_intrinsic_size(style: dict, element) -> None:
    """`<img>` is a "replaced element": an `auto` width/height uses its
    *intrinsic* (natural pixel) size, full stop -- unlike a text leaf, which
    reflows its height to whatever width the surrounding block layout gives
    it. A text leaf's `measure(available_width, ...)` callback works for
    text because Taffy always resolves an ordinary block box to its
    container's full width before consulting `measure` for a height at that
    width -- so the same mechanism can't give an image its own, *narrower*
    intrinsic width: a block child's width isn't `measure`'s to give. The
    only way to get a genuinely intrinsic-sized box is to put the number
    directly in the style *before* Taffy ever sees it -- baking the image's
    real pixel dimensions in as if they'd been written as `width:NNpx` --
    which is exactly what this does, only for whichever of `width`/`height`
    the cascade actually left `auto` (an explicit CSS size on the `<img>`
    is left alone, same as any other element's explicit size always wins).
    See `browser_images.py` for the fetch/decode/cache side of this."""
    from . import browser_images

    image = browser_images.load_image(element.getAttribute("src") or "")
    if image is None:
        return  # no image to size from -- left as whatever the cascade said (probably auto -> an empty box)
    if style["width"] == "auto":
        style["width"] = float(image.width())
    if style["height"] == "auto":
        style["height"] = float(image.height())


def _apply_canvas_intrinsic_size(style: dict, element) -> None:
    """Canvas is a replaced element with a 300 x 150 default bitmap."""
    if style["width"] == "auto":
        style["width"] = float(element.getAttribute("width") or 300)
    if style["height"] == "auto":
        style["height"] = float(element.getAttribute("height") or 150)


def _apply_svg_intrinsic_size(style: dict, element) -> None:
    """Treat an outer SVG viewport as one replaced element for HTML layout."""
    width_attr = (element.getAttribute("width") or "").strip()
    height_attr = (element.getAttribute("height") or "").strip()
    width = (_fontmetrics.parse_length(width_attr, default=None)
             if width_attr and not width_attr.endswith("%") else None)
    height = (_fontmetrics.parse_length(height_attr, default=None)
              if height_attr and not height_attr.endswith("%") else None)
    view_box = (element.getAttribute("viewBox") or "").replace(",", " ").split()
    ratio = None
    if len(view_box) == 4:
        try:
            view_width, view_height = float(view_box[2]), float(view_box[3])
            ratio = view_width / view_height if view_height else None
        except ValueError:
            pass
    if style["width"] == "auto":
        if width is not None:
            style["width"] = float(width)
        elif height is not None and ratio is not None:
            style["width"] = float(height * ratio)
    if style["height"] == "auto":
        if height is not None:
            style["height"] = float(height)
        elif width is not None and ratio is not None:
            style["height"] = float(width / ratio)


def _measure_intrinsic_width(element, computed_cache) -> "float | None":
    """The natural (max-content, unconstrained-width) width `element` would
    take with no line wrapping -- built and computed in a disposable,
    throwaway Taffy tree so real, already-correct layout (nested tags, mixed
    fonts/weights, inline-block children, ...) does the measuring instead of
    a hand-rolled approximation limited to plain text (contrast
    `_apply_button_intrinsic_width`, which only needs a single font run).
    `tree.Tree.compute()`'s `available_width=None` is Taffy's own
    `AvailableSpace::MaxContent`, exactly this. `None` on any failure (an
    empty/degenerate subtree) -- the caller falls back to today's behaviour
    rather than guessing."""
    scratch = Tree()
    try:
        root_id = build(scratch, element, {}, computed_cache=computed_cache, reuse_styles=False)
        boxes = scratch.compute(root_id, None, None)
        box = boxes.get(root_id)
        return float(box[2]) if box is not None else None
    except Exception:
        return None


def _rendering_text_content(element) -> str:
    """`element.textContent`, but skipping any descendant subtree rooted at
    a `_NON_RENDERING_TAGS` tag (`<style>`, `<script>`, ...) -- plain DOM
    `.textContent` includes their raw text verbatim (a `<style>`'s CSS
    source is real text content, just never *rendered*), which found a
    real bug in `_measure_min_content_width`: a Wikipedia infobox cell
    carrying a `<style>` (a TemplateStyles injection) had its "longest
    word" come from a CSS selector/declaration inside it instead of any
    actually-visible text, once inflating a colspan'd cell's minimum
    content width to 750px+.

    No `childNodes` at all (`tree._PseudoElement`'s generated-content
    text, e.g. an icon font's `content: "\\f0c2"` glyph, is its own plain
    `.text` attribute with nothing backing it in the DOM tree) falls back
    to `element.textContent` directly -- there is nothing to walk, and a
    synthetic pseudo-element can't have a `<style>`/`<script>` of its
    own anyway."""
    child_nodes = getattr(element, "childNodes", None)
    if not child_nodes:
        return getattr(element, "textContent", None) or ""
    parts = []

    def walk(node):
        # domonic represents a plain-string child exactly as the raw
        # `str` it was constructed with (`domonic.html.p("hi")` -- the
        # programmatic-construction helpers, as opposed to parsing real
        # markup, never wrap it in a `Text` node at all) -- `getattr(node,
        # "nodeType", None)` is `None` for a bare string, same as for
        # anything else with no such attribute, so it has to be checked
        # for explicitly or a raw-string child is silently dropped instead
        # of counted as its own text.
        if isinstance(node, str):
            if node:
                parts.append(node)
            return
        node_type = getattr(node, "nodeType", None)
        if node_type == TEXT_NODE:
            text = getattr(node, "textContent", None) or getattr(node, "data", "")
            if text:
                parts.append(text)
            return
        if node_type == ELEMENT_NODE:
            if (getattr(node, "tagName", "") or "").lower() in _NON_RENDERING_TAGS:
                return
            for child in node.childNodes or ():
                walk(child)

    for child in child_nodes:
        walk(child)
    return "".join(parts)


def _measure_min_content_width(element, computed_cache) -> "float | None":
    """The width of `element`'s own longest unbreakable token (its longest
    whitespace-separated word, measured in its own font) -- CSS 2.1
    17.5.2.2's real "minimum content width" for auto table-layout column
    sizing: the smallest a column can be made without literally breaking a
    word mid-token. Deliberately *not* `_measure_intrinsic_width`'s
    max-content (the width if the content never wrapped at all) -- that's
    the right "requirement" for a short, rarely-wrapping label cell, but
    wildly too wide a floor for a colspan'd cell holding a whole wrapping
    sentence or list. Found on `en.wikipedia.org`'s Python-article infobox:
    a colspan'd "Influenced by" cell listing dozens of comma-separated
    language names measured over 1200px unwrapped -- using that as the
    column's required width forced it absurdly wide instead of letting it
    wrap across several lines the way Chrome renders it.

    A plain per-token font-metrics measurement (not a real Taffy layout
    pass, unlike `_measure_intrinsic_width`) -- deliberately minimal, and
    good enough for ordinary prose/lists: it doesn't account for a nested
    element's own different font, only `element`'s own (that nested
    element's *content* still counts, via `_rendering_text_content`, just
    measured in the outer font)."""
    text = _rendering_text_content(element).strip()
    if not text:
        return None
    _describe(element, computed_cache)
    paint_style = element._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    widest = 0.0
    for token in text.split():
        width, _height, _lines = layout_text(token, family, font_size, font_weight=weight, italic=italic)
        widest = max(widest, width)
    return widest


def _compute_table_column_widths(table_element, computed_cache) -> dict:
    """`{id(cell_element): resolved_width}` for *every* cell in
    `table_element`, colspan'd or not -- a deliberately minimal CSS 2.1
    17.5.2.2 "auto" table-layout pass: enough for ordinary HTML tables
    (Wikipedia infoboxes/wikitables included), not full spec compliance.

    Three steps, each a single pass over the table's own rows (nested
    tables' own rows are skipped -- `querySelectorAll` matches at any
    depth):

    1. Establish the column model and each colspan-1 cell's own intrinsic
       (max-content) width; a column's width is the *widest* same-column
       cell across every row.
    2. A colspan'd cell's width is the *sum* of the columns it covers. If
       its own *minimum* content width (its longest unbreakable word --
       `_measure_min_content_width`, not the max-content/never-wraps width
       step 1 uses) needs more than that sum, spread the shortfall evenly
       across just the columns it spans (not the whole table), growing
       them to fit -- the only place this pass adjusts a plain column's
       width on a colspan'd cell's account. Deliberately the *minimum*,
       not max-content: a colspan'd cell very commonly holds wrapping
       prose or a long comma-separated list (a Wikipedia infobox's
       "Influenced by" row, say) that's fine wrapping across several
       lines -- growing its columns to fit the whole thing unwrapped would
       make ordinary wrapping content force the table absurdly wide.
    3. Resolve every cell (colspan'd or not) to one definite pixel width
       from the now-final column widths.

    This is the actual fix for a colspan'd cell's content collapsing to a
    tiny, wrapped width instead of filling its row (found on `en.wikipedia.
    org`'s Python-article infobox, a colspan'd `<th>` section header with a
    nested wikilink measuring ~47px instead of ~350px): the previous
    version skipped colspan'd cells entirely, leaving them on `tree.build
    ()`'s flex-grow/flex-shrink equal-share fallback, which requires Taffy
    to call the cell's own text-measurement callback to discover a size --
    and for a cell with *mixed* inline content (its own text plus a nested
    element), that callback was being invoked with a small, effectively
    arbitrary `available_width` rather than the row's real remaining
    space, well before `flex-grow` ever got a chance to redistribute
    anything. Every cell returned here instead gets a single, definite,
    already-correct pixel width *before* `tree.build()` ever measures its
    inline content -- Taffy's own flex-measurement guessing (the actual
    bug) never enters into it at all, colspan'd or not."""
    per_column: dict[int, float] = {}
    single_cells: dict[int, int] = {}  # id(cell) -> col_index, colspan == 1
    span_cells: list = []  # (cell, start_col, colspan)
    try:
        rows = table_element.querySelectorAll("tr")
    except Exception:
        return {}
    for row in rows:
        # Skip a row that actually belongs to a *nested* table (its nearest
        # table ancestor isn't this one) -- `querySelectorAll` matches at any
        # depth, including inside a `<td>`'s own inner table.
        ancestor = row.parentElement
        while ancestor is not None and (getattr(ancestor, "tagName", "") or "").lower() != "table":
            ancestor = ancestor.parentElement
        if ancestor is not table_element:
            continue
        col_index = 0
        for cell in row.childNodes or ():
            if not _is_element(cell):
                continue
            tag = (getattr(cell, "tagName", "") or "").lower()
            if tag not in ("td", "th"):
                continue
            colspan_raw = cell.getAttribute("colspan") if hasattr(cell, "getAttribute") else None
            try:
                colspan = max(1, int(colspan_raw)) if colspan_raw else 1
            except ValueError:
                colspan = 1
            if colspan == 1:
                width = _measure_intrinsic_width(cell, computed_cache)
                single_cells[id(cell)] = col_index
                if width is not None:
                    per_column[col_index] = max(per_column.get(col_index, 0.0), width)
            else:
                span_cells.append((cell, col_index, colspan))
            col_index += colspan

    # Step 2: grow only the columns a colspan'd cell actually covers -- each
    # colspan's own shortfall is measured against the *base* (single-cell-
    # derived) column widths, not against widths already grown by an
    # earlier colspan, and only the largest shortfall any one column is
    # asked for wins (`max`, not an accumulating sum). Table sections are
    # commonly a whole run of same-span header/divider rows (e.g. a
    # Wikipedia infobox's `colspan="2"` section headers, every one of them
    # spanning the exact same two columns) -- summing each row's own
    # shortfall on top of the last would compound across every one of them
    # into a wildly inflated column, even though only the single *widest*
    # one actually needs to fit.
    extra_per_column: dict[int, float] = {}
    for cell, start_col, colspan in span_cells:
        covered = range(start_col, start_col + colspan)
        base_sum = sum(per_column.get(c, 0.0) for c in covered)
        needed = _measure_min_content_width(cell, computed_cache)
        if needed is not None and needed > base_sum:
            extra = (needed - base_sum) / colspan
            for c in covered:
                extra_per_column[c] = max(extra_per_column.get(c, 0.0), extra)
    for c, extra in extra_per_column.items():
        per_column[c] = per_column.get(c, 0.0) + extra

    resolved: dict[int, float] = {
        cell_id: per_column[col] for cell_id, col in single_cells.items() if col in per_column
    }
    for cell, start_col, colspan in span_cells:
        total = sum(per_column.get(c, 0.0) for c in range(start_col, start_col + colspan))
        if total > 0.0:
            resolved[id(cell)] = total
    return resolved


def _apply_button_intrinsic_width(style: dict, element) -> None:
    if style["width"] != "auto":
        return
    text = _own_text(element)
    paint_style = element._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=13.3333)
    width, _height, _lines = layout_text(
        text, paint_style["font_family"], font_size,
        font_weight=_parse_font_weight(paint_style["font_weight"]),
        italic=fonts.is_italic(paint_style["font_style"]),
    )
    horizontal = sum(float(value) for value in (style["padding"][1], style["padding"][3],
                                                 style["border"][1], style["border"][3])
                     if not isinstance(value, tuple) and value != "auto")
    style["width"] = width + horizontal


# Tags a real browser's UA stylesheet defaults to `display: inline`. Used
# only as a *fallback tag guess* now -- `ua_style.apply()` (run by every real
# `browser.load()`) already gives `div`/`p`/the other ordinary block tags a
# UA-default `display: block`, so `_is_inline_level` below can trust a
# genuinely computed "inline"/"inline-block" directly for a loaded page.
# This set only still matters for a raw DOM tree built without
# `browser.load()` (some unit tests do this to skip the UA stylesheet
# entirely), where domonic's un-cascaded initial value of "inline" applies
# to every tag alike and tag-name is the only signal left.
_USUALLY_INLINE_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "small", "code", "label", "abbr",
    "cite", "mark", "sub", "sup", "time", "kbd", "samp", "var", "q", "u", "s",
    "button", "input", "select", "textarea",
})

# Replaced elements and form controls size themselves from authored
# `width`/`height` even at `display:inline` -- unlike an ordinary inline
# element, whose box is purely a function of its content.
_REPLACED_OR_CONTROL_TAGS = frozenset({
    "img", "canvas", "svg", "input", "textarea", "select", "button", "iframe",
})

# CSS 2.1 17.4/CSS Tables 3: computed `display` keywords for the internal
# table boxes margin never applies to, regardless of what tag carries the
# value -- `display:table`/`inline-table` (the outer table box itself,
# where margin still applies normally) are deliberately not in this set.
_TABLE_INTERNAL_DISPLAYS = frozenset({
    "table-row-group", "table-header-group", "table-footer-group",
    "table-row", "table-cell", "table-column-group", "table-column",
})

# Tags with their own dedicated `build()` branch that must always run --
# see the `has_pseudo` check that uses this, right before `inline_items` is
# computed.
_NO_GENERATED_CONTENT_TAGS = frozenset({
    "img", "canvas", "svg", "input", "textarea", "select",
})


def _ua_stylesheet_applied(element) -> bool:
    """Whether `ua_style.apply()` ran on `element`'s document -- cached per
    document (this is checked once per element, on the hot `build()` path)."""
    document = getattr(element, "ownerDocument", None)
    if document is None:
        return False
    cached = getattr(document, "_chromonic_ua_applied_cache", None)
    if cached is None:
        cached = document.querySelector("style[data-chromonic-ua]") is not None
        document._chromonic_ua_applied_cache = cached
    return cached


def _trusts_computed_inline(element, tag_name: str) -> bool:
    """Whether a genuinely computed `display: inline`/`inline-block` on
    `element` can be trusted as real author intent rather than domonic's
    un-cascaded initial value (every tag's raw default, absent a UA
    stylesheet). True either for a tag this module already assumes is
    usually inline (`_USUALLY_INLINE_TAGS`), or -- more generally -- for
    any tag `ua_style.py`'s stylesheet gives an explicit `display: block`
    default (`ua_style.BLOCK_DEFAULT_TAGS`), *when that stylesheet actually
    ran* (`browser.load()` always runs it; a raw DOM tree built without it,
    as some unit tests do, did not). A tag in neither set -- `<tr>`/`<td>`/
    `<th>`/etc., which this project doesn't give a UA default at all -- has
    no way to disambiguate and is never trusted here, same as before."""
    if tag_name in _USUALLY_INLINE_TAGS:
        return True
    return tag_name in ua_style.BLOCK_DEFAULT_TAGS and _ua_stylesheet_applied(element)


def _is_inline_level(element, style_obj) -> bool:
    display = style_obj.display
    value = getattr(display, "value", display)
    if isinstance(value, str):
        match = style_bridge._SIMPLE_VAR_FALLBACK.match(value.strip())
        if match:
            value = match.group(1).strip()
    if value not in ("inline", "inline-block"):
        return False
    tag_name = (getattr(element, "tagName", "") or "").lower()
    return _trusts_computed_inline(element, tag_name)


def _is_floated(child_computed) -> bool:
    """Whether an author explicitly gave this element `float: left`/`right`
    -- unlike `display`, CSS's initial value for `float` is always `none`
    regardless of tag, so there's no domonic-default-ambiguity to guard
    against here (see `_is_inline_level`'s docstring for why *that* check
    needs a tag gate first): any non-`none` `float` is unambiguous author
    intent to leave normal flow and pack to one side of its line, the same
    "wants to sit in a row, not stack" signal an inline tag gives, just via
    a different CSS mechanism. Found needing this on `https://www.wikipedia.org/`:
    its "10 largest Wikipedias" grid is nothing but ten `float: left` boxes,
    no flexbox at all -- chromonic has no float implementation whatsoever (a
    separate, much larger project -- see PLAN.md), so without this they
    rendered stacked one per line, overlapping whatever content came after
    the (also-unimplemented) cleared/collapsed space they should have
    occupied."""
    float_value = getattr(child_computed, "float", None)
    return isinstance(float_value, str) and float_value.strip().lower() in ("left", "right")


def _wants_horizontal_flow(element, computed, style_obj) -> bool:
    return _is_inline_level(element, style_obj) or _is_floated(computed)


def _approximate_inline_flow(
    element, style: dict, child_elements: list, child_computeds: list, child_styles: list, computed,
) -> None:
    """A real browser lays a run of `display:inline`/`inline-block` children
    out left-to-right, wrapping onto new lines as needed -- ordinary CSS
    inline flow, needing nothing special in a page's own CSS (adjacent
    `<a>` tags in an unstyled nav list already do this by default, since
    `inline` is every element's own CSS initial value). Taffy has no inline
    flow at all; `style_bridge._display()` already collapses every
    non-flex/grid/none display to `"block"`, so without this, a horizontal
    nav bar like suckless.org's `<div id="menu"><a>home</a><a>dwm</a>...`
    (relying on nothing but that CSS default) renders as one link per line
    instead of a row -- found by comparing chromonic's output against a real
    browser's on a real page, not synthetically.

    This is a heuristic **approximation**, not real inline layout or real
    float layout, and deliberately conservative about *when* it fires: a
    container qualifies only if it has **two or more** children and
    **most** (80%+) of them "want" a horizontal flow (`_wants_horizontal_flow`
    -- either a tag a real UA stylesheet would default to inline, still
    computed as `inline`/`inline-block` (`_is_inline_level`), or an
    explicit `float: left`/`right` (`_is_floated`) -- the *other* real
    layout mode with no implementation here at all, needing this same
    treatment for the same reason: found next, comparing against Chrome on
    `https://www.wikipedia.org/`, whose "10 largest Wikipedias" grid is
    nothing but ten `float: left` boxes and no flexbox). The tag check (for
    the inline half) is what makes *that* half safe -- domonic gives
    *every* tag the raw CSS initial value `inline` with no UA stylesheet of
    its own (wrinkle #11), so trusting a computed "inline" by itself would
    also catch an entirely ordinary, unstyled `<div>` or `<p>` (an early
    version of this function did exactly that and broke plain
    single-paragraph layouts across this repo's own tests, none of which
    apply `ua_style.py`). `float` needs no such gate -- its initial value is
    always `none` regardless of tag, so any non-`none` value is unambiguous
    author intent, not a domonic-default false positive.

    A *majority*, not unanimity, is required: suckless.org's own nav is
    eight plain `<a>`s plus one `<span>` the site's own CSS gives `display:
    block` (almost certainly for unrelated dropdown/JS behaviour) -- one
    exception in a group of nine, which an "every child must qualify" rule
    let silently veto the whole nav back to one-link-per-line. Requiring 2+
    children means one incidentally-qualifying lone child (a single link in
    an otherwise block-shaped wrapper) doesn't trigger this either -- real
    patterns needing the approximation (nav bars, tag lists, badge rows,
    floated card grids) always have several.

    Qualifying containers are treated as `display:flex; flex-wrap:wrap`
    instead of plain block. Does **not** attempt real mixed inline content
    (text interleaved with inline elements, e.g. `<p>click <a>here</a>
    please</p>` -- already out of scope, see `_own_text`'s own docstring),
    does not merge adjacent text runs, does not clear floats or let
    non-floated content flow around them the way real float layout would,
    and does not implement inline-level *text* wrapping around floated/
    inline boxes -- only whole elements wrapping onto new rows, via
    ordinary flex-wrap."""
    element.__dict__.pop("_chromonic_float_flow_children", None)
    element.__dict__.pop("_chromonic_float_flow_qualifies", None)
    for child in child_elements:
        child.__dict__.pop("_chromonic_force_full_row_width", None)
    if style["display"] != "block":
        return  # already flex/grid/none -- a real, explicit layout mode wins, no guessing over it
    if len(child_elements) < 2:
        return
    qualifies = [
        _wants_horizontal_flow(child, child_computed, child_style)
        for child, child_computed, child_style in zip(child_elements, child_computeds, child_styles)
    ]
    # A strict "every child must qualify" reads safer but breaks on real
    # pages (see the docstring above for suckless.org's own exception) -- a
    # majority is safe *because* each individual check is already narrow
    # (a tag gate for inline, an always-explicit property for float), so
    # nothing reaching this point is a stray, misidentified ordinary block
    # element.
    #
    # A single floated child is enough on its own, regardless of the 80%
    # majority the inline-run heuristic needs -- unlike trusting a
    # computed "inline" (domonic's own tag-less default, needing the
    # majority vote as a guard against false positives), `float` is always
    # an explicit, unambiguous author declaration (initial value `none`
    # regardless of tag -- see this function's own docstring), and CSS 2.1
    # 9.5 has any float narrow its container's other in-flow content no
    # matter how small a fraction of the container's children it is. Found
    # on `wpt/css/CSS2/normal-flow/auto-margins-used-values-with-floats.
    # tentative.html`: one `float:right` child among three ordinary
    # `margin:auto` blocks (25%, well under 80%) left the container as
    # plain block, so the float was never excluded from the other blocks'
    # available width at all.
    if not any(_is_floated(cc) for cc in child_computeds) and sum(qualifies) < len(child_elements) * 0.8:
        return
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    # `_fix_float_flow_after_block_sibling` needs to know, after Taffy has
    # laid this container out as an ordinary flex-wrap row, which children
    # were real (non-floated, non-inline) ordinary blocks -- flex-wrap only
    # wraps on width overflow, so it has no idea a plain block sibling must
    # force every floated child *after* it onto a fresh block-flow line,
    # never sharing that block's own row just because there was still
    # horizontal room left on it.
    element._chromonic_float_flow_children = list(child_elements)
    element._chromonic_float_flow_qualifies = list(qualifies)
    # CSS 2.1 9.2.1: an ordinary, non-floated, non-inline block child --
    # here, one of the qualifying minority -- always fills its containing
    # block's full width, `width:auto` or not; unlike a float or inline
    # item, it never merely shrinks to its own content. Plain `flex-wrap`
    # (standing in for real float layout, see this function's own
    # docstring) has no notion of that at all -- it sizes every item by
    # ordinary flex shrink-to-fit, so a block breaker's own text content
    # width (here, a short paragraph) silently became its whole box width
    # instead of the row's. `build()`'s own per-child style resolution
    # checks this flag and forces `flex_basis:100%` for a `width:auto`
    # child so it wins the whole row -- explicit-width blocks are left
    # alone (they still force their own row via `_fix_float_flow_after_
    # block_sibling`'s position fix, just narrower, exactly as authored).
    # Found on `wpt/css/CSS2/linebox/fractional-line-height.html`: the
    # fixture's leading `<p>` measured `202px` (its own shrink-to-fit text
    # width) instead of the full `784px` row, which also wrapped its text
    # onto extra lines it would never have needed at the real width.
    for child, ok in zip(child_elements, qualifies):
        if not ok:
            child._chromonic_force_full_row_width = True
    inline_tag_qualifies = any(
        _is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
    )
    if inline_tag_qualifies and style["gap"] == (0.0, 0.0):
        # Real inline flow gets its spacing from the whitespace *text
        # nodes* between elements in the source (`<a>home</a>\n<a>dwm</a>`
        # collapses to one space, same as any other run of whitespace) --
        # exactly the "mixed content" this POC doesn't measure (see
        # `_own_text`'s docstring), so without this, flex-wrap packs
        # elements edge to edge with no gap at all ("homedwmstcore...").
        # Approximated as one space character's width in the container's
        # own font -- not exact (a flex gap is uniform; real inline
        # whitespace can vary/collapse differently), but far closer than
        # no gap, and only applied when nothing already set an explicit one.
        # Skipped for a purely `float`-qualified group: floated grids get
        # their spacing from each item's own margin, same as any other
        # block box, and already flow through Taffy's ordinary flex-item
        # margin handling with no compensation needed.
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        bold = _fontmetrics.is_bold(computed.fontWeight)
        space_width = _fontmetrics.advance_width(" ", font_size, bold)
        style["gap"] = (0.0, space_width)


def _is_absolutely_positioned(style_obj) -> bool:
    position = style_obj.position
    return getattr(position, "value", position) in ("absolute", "fixed")


def _establishes_bfc(computed) -> bool:
    """CSS 2.1 9.4.1: whether *this* box establishes its own new block
    formatting context -- `float`/absolute or fixed positioning/`flow-
    root`/`inline-block`/table-cell/table-caption, or any `overflow` other
    than the initial `visible` (`clip`/`hidden`/`scroll`/`auto` all
    qualify; Taffy's own Rust side already treats a non-`visible`
    `overflow` this way natively for margin-collapse-through purposes, see
    `style["overflow"]`'s own comment above). `overflow: visible` itself
    must NOT establish one -- found on `wpt/css/CSS2/normal-flow/block-
    formatting-contexts-016.xht`: an ordinary `overflow:visible` block
    sharing a container with a `float:left` sibling was being shifted
    aside to avoid the float (`x=108` instead of Chrome's `x=8`, its
    border box correctly extending behind/underneath the float) -- only a
    box that actually establishes a BFC is supposed to avoid a float that
    way; an ordinary block's own border box may extend behind one (only
    its *inline* content wraps around it, out of scope here -- see
    `_approximate_inline_flow`'s own docstring)."""
    if computed is None:
        return False
    display = (getattr(computed, "display", "") or "").strip().lower()
    if display in (
        "flow-root", "inline-block", "table-cell", "table-caption",
        "flex", "inline-flex", "grid", "inline-grid", "table", "inline-table",
    ):
        return True
    float_value = (getattr(computed, "float", None) or "none").strip().lower()
    if float_value != "none":
        return True
    position = (getattr(computed, "position", None) or "static").strip().lower()
    if position in ("absolute", "fixed"):
        return True
    overflow_x = (getattr(computed, "overflowX", "visible") or "visible").strip().lower()
    overflow_y = (getattr(computed, "overflowY", "visible") or "visible").strip().lower()
    return overflow_x != "visible" or overflow_y != "visible"


def _establishes_containing_block(style_obj) -> bool:
    """Whether an element's own CSS `position` makes it a valid *containing
    block* for `position:absolute`/`fixed` descendants -- anything but
    `static` (`relative`/`absolute`/`fixed`/`sticky`), per the CSS spec.
    `sticky` is treated the same as `relative` here (chromonic doesn't
    implement sticky's scroll-triggered repositioning at all --
    `style_bridge._position()` already maps both to Taffy's plain
    "relative", so this matches)."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"


def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created (including `element` itself). `computed`/`style_obj`, if given,
    are `element`'s already-computed style (see `_describe`) -- the caller
    (a parent's own `build()` call, via `_child_elements`) already had to
    compute them to decide whether `element` should be here at all, so the
    root of the walk is the only place they're ever computed twice... which
    is to say, never: `layout()` below computes the root's once, too.

    `is_containing_block`/`escapees` implement CSS's real containing-block
    rule for `position:absolute`/`fixed` -- **not** "positioned relative to
    the literal DOM parent", which is what a naive DOM-mirroring tree gives
    for free and which is wrong the moment that parent isn't itself
    positioned. A real browser resolves an absolutely-positioned element
    against its *nearest ancestor with `position != static`*, or the
    viewport if there is none -- found rendering `https://www.wikipedia.org/`
    through `browse2.py` and comparing against Chrome: a `position:absolute`
    search box with no positioned ancestor anywhere landed near its literal
    (`position:static`) DOM parent's origin instead of the page's, visibly
    overlapping unrelated content. Reproduced minimally (see
    `tests/test_chromonic.py`) and fixed here: every call defaults to
    `is_containing_block=True` (the root -- always a valid containing
    block, matching the CSS "initial containing block" is the viewport
    rule, and there's no ancestor above it to have gotten this from
    anyway); when true, this call owns a *fresh* `escapees` list -- any
    descendant, at any depth, that's absolutely positioned but whose own
    literal parent is *not* a containing block gets built normally but
    added to *this* list instead of its literal parent's Taffy children,
    so it ends up exactly one edge away from its real containing block in
    the Taffy tree, matching what CSS actually specifies. An intermediate
    `position:static` element passes its inherited `escapees` straight
    through unchanged, for exactly as many levels as it takes to reach one."""
    if computed_cache is None:
        computed_cache = {}
    if computed is None or style_obj is None:
        computed, style_obj = _describe(element, computed_cache, reuse_styles=reuse_styles)
    style = getattr(element, "_chromonic_native_style", None) if reuse_styles else None
    if style is None:
        style = style_bridge.to_dict(style_obj)
        # Not modelled in `LayoutStyle`/`style_bridge.to_dict()` at all, so
        # read straight off `computed` here. CSS 2.1 8.3.1: a non-`visible`
        # `overflow` makes an element establish a new block formatting
        # context, which -- among other things -- stops an in-flow child's
        # margin from collapsing through it. Taffy (`src/lib.rs`) already
        # implements this correctly *given* `Style.overflow`; leaving every
        # node at Taffy's `Overflow::Visible` default (this field was never
        # threaded through before) silently disabled that CSS rule
        # entirely. Any value Rust's `parse_overflow_axis` doesn't
        # recognise falls back to "visible" rather than erroring.
        _valid_overflow = ("visible", "clip", "hidden", "scroll", "auto")
        overflow_x = getattr(computed, "overflowX", "visible") or "visible"
        overflow_y = getattr(computed, "overflowY", "visible") or "visible"
        style["overflow"] = (
            overflow_x if overflow_x in _valid_overflow else "visible",
            overflow_y if overflow_y in _valid_overflow else "visible",
        )
        element._chromonic_native_style = style
    if is_grid_item and style["min_width"] == "auto":
        # Prevent an auto-width block descendant from feeding its containing
        # grid's full available width back as the track's intrinsic minimum.
        style["min_width"] = 0.0
    own_escapees = [] if is_containing_block else escapees
    tag_name = (getattr(element, "tagName", "") or "").lower()
    element._chromonic_tag_name = tag_name
    is_genuinely_inline = (
        tag_name not in _REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and _trusts_computed_inline(element, tag_name)
    )
    if is_genuinely_inline:
        # CSS 2.1 10.3.1: `width`/`height` never apply to a non-replaced
        # inline box -- only its content (and any inline-block/replaced
        # descendant) determines its size. `style_bridge._display()` already
        # collapsed "inline" onto Taffy's plain "block" display (Taffy has
        # no inline mode), which would otherwise make an authored
        # `width`/`height` on e.g. a `<div style="display:inline">` a hard
        # box size instead of being ignored like a real browser does. Same
        # `_trusts_computed_inline` gate as `_is_inline_level` -- a tag with
        # no UA default at all (`<tr>`/`<td>`/...), or a raw DOM tree built
        # without `browser.load()` (skipping `ua_style.py`, as some unit
        # tests do), still sees domonic's un-cascaded "inline" default and
        # must not have its authored size discarded on that basis alone.
        style["width"] = "auto"
        style["height"] = "auto"
        # CSS 2.1 10.3.1/10.6.1: `margin-top`/`margin-bottom` are likewise
        # accepted but have no effect on a non-replaced inline box's height
        # (only `margin-left`/`margin-right` add real horizontal spacing).
        # Taffy has no inline mode to know that on its own -- left as
        # ordinary flex-item margins, they inflated the anonymous inline
        # run's cross-axis size. Found via `wpt/css/CSS2/margin-padding-
        # clear/margin-applies-to-008.xht` (`div div { display: inline;
        # margin: 50px }`): the line grew 100px taller instead of staying
        # at the text's own line height.
        margin = list(style["margin"])
        margin[0] = margin[2] = 0.0
        style["margin"] = margin
    if getattr(style_obj.display, "value", "") in _TABLE_INTERNAL_DISPLAYS:
        # CSS 2.1 17.4/CSS Tables 3 (`table-row-group`/`table-header-
        # group`/`table-footer-group`/`table-row`/`table-cell`/`table-
        # column-group`/`table-column`): margin does not apply to any of
        # these internal table boxes at all, on any side -- unlike the
        # `display:inline` case just above (only the vertical sides don't
        # apply there), a real browser drops all four here. `style_bridge.
        # _display()` collapses every one of these keywords onto plain
        # native `"block"` (Taffy has no table layout mode at all), which
        # would otherwise let an authored `margin` on one push its
        # neighbours around inside the table the same way it would on any
        # ordinary block -- margin stays live only on the outer `display:
        # table` box itself, not caught by this check. Found on `wpt/css/
        # CSS2/mpc/margin-applies-to-001.xht`: a `display:table-row-group`
        # `<div>` with `margin:50px` pushed its own content `50px` away
        # from the table's own border on every side instead of flush
        # against it.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
    if (getattr(style_obj.display, "value", "") == "inline-block"
            and _trusts_computed_inline(element, tag_name)):
        # CSS 2.1 9.2.1/CSS Display 3: `inline-block` is an atomic
        # inline-level box that establishes its own block formatting
        # context, so (like `overflow:hidden`, fixed above) an in-flow
        # child's margin must not collapse through it -- confirmed on
        # `wpt/css/CSS2/margin-padding-clear/margin-collapse-015a.xht`
        # ("An element with its display set to 'inline-block' does not
        # collapse its margins with its children"). Taffy has no
        # `inline-block` display mode to key off (it's mapped onto plain
        # `Display::Block`, same as an ordinary block, by
        # `style_bridge._display()`), so this is signalled the same way
        # `overflow` was: a field Rust's `parse_style` turns into
        # `Contain::PAINT`, which establishes a new formatting context in
        # Taffy without the side effect `Contain::LAYOUT` would have had on
        # the separate inline-block baseline-alignment fix (see
        # `get_contain` in `src/lib.rs`).
        style["establishes_bfc"] = True
    if tag_name == "table":
        element._chromonic_border_collapse = computed.borderCollapse == "collapse"
        if element._chromonic_border_collapse:
            # Collapsed borders straddle the table grid edge. Reserving half
            # an outer border on each side gives rows the same 359px inner
            # grid inside a 360px border box that Chrome reports.
            style.update({"box_sizing": "border-box", "padding": [0.5] * 4})
        # Real "auto" table layout (CSS 2.1 17.5.2.2, the initial/default
        # `table-layout` -- *not* `table-layout: fixed`, which by definition
        # ignores cell content and *should* keep the old equal-share
        # behaviour) sizes each column to its widest cell's own content, not
        # an equal share of the row -- measured once per table (not per
        # cell) so every same-column cell agrees on one width. Found on a
        # real page (news.ycombinator.com): a fixed-width "rank number"/
        # "vote arrow" column and a long, wrapping title column, split into
        # three dead-equal thirds, pushed every title ~180px right of where
        # Chrome puts it. See `_compute_table_column_widths`.
        element._chromonic_table_column_widths = (
            _compute_table_column_widths(element, computed_cache)
            if computed.tableLayout != "fixed" else {}
        )
    if tag_name == "tr":
        # Taffy has no table formatting mode. A row is nevertheless a
        # horizontal formatting context, and this retained projection gives
        # ordinary fixed/equal-column tables the right fundamental geometry.
        style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap"})
    elif tag_name in ("td", "th") and style["width"] == "auto":
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and getattr(ancestor, "_chromonic_tag_name", None) != "table":
            ancestor = getattr(ancestor, "parentElement", None)
        column_width = None
        if ancestor is not None:
            column_width = getattr(ancestor, "_chromonic_table_column_widths", {}).get(id(element))
        if column_width is not None:
            # `flex_grow` proportional to the column's own intrinsic width
            # (not a uniform `1.0`) so any space *beyond* every column's
            # natural content width -- a table given more room than its
            # content strictly needs, the common case -- still goes mostly
            # to the column that actually wants it (a wrapping title column)
            # rather than inflating a small, fixed-content column (a rank
            # number, an icon) by the same absolute amount.
            style.update({"flex_grow": column_width, "flex_shrink": 1.0,
                          "flex_basis": column_width, "min_width": 0.0})
        else:
            # Colspan'd, or intrinsic measurement failed -- fall back to the
            # original equal-share behaviour rather than guessing.
            style.update({"flex_grow": 1.0, "flex_shrink": 1.0,
                          "flex_basis": 0.0, "min_width": 0.0})
        if ancestor is not None and getattr(ancestor, "_chromonic_border_collapse", False):
            style["border"] = [value / 2.0 if isinstance(value, (int, float)) else value
                               for value in style["border"]]
    if getattr(element, "_chromonic_force_full_row_width", False) and style["width"] == "auto":
        # Set by `_approximate_inline_flow` on a non-floated, non-inline
        # block sibling in a container it turned into a `flex-wrap` row to
        # stand in for real float layout -- see that function's own
        # docstring. An explicit author width is left alone (still forces
        # its own row, just narrower, via `_fix_float_flow_after_block_
        # sibling`'s position fix); only `width:auto` needs correcting
        # here, since that's the case ordinary flex shrink-to-fit gets
        # wrong (real CSS block flow always fills the containing block).
        style["flex_basis"] = ("pct", 1.0)
    # `<select>`'s `<option>` children are never real layout content -- see
    # `_select_display_text` -- so it's treated as childless here
    # regardless of what's actually in the DOM, the same way `<img>` below
    # is a leaf regardless of it usually having no children at all.
    # `<iframe>` likewise never renders its own light-DOM children as page
    # content (nothing standard gives it `<object>`-style fallback content)
    # -- forcing it childless here, rather than relying on real fixtures
    # simply having none, keeps it a plain replaced leaf (UA-default
    # `300x150` content box plus its `2px` border, `ua_style.py`) even if
    # markup puts something inside the tag.
    children = [] if tag_name in ("select", "svg", "iframe") else _child_elements(
        element, computed_cache, reuse_styles=reuse_styles
    )
    element._chromonic_has_layout_children = bool(children)
    if tag_name == "button":
        _apply_button_intrinsic_width(style, element)
    # Replaced/control elements each have their own dedicated branch below
    # (`img`/`canvas`/`svg`/`select`/`input`/`textarea`) that must always
    # run for them -- CSS generated content isn't rendered on these anyway
    # (spec: `::before`/`::after` don't apply to replaced elements or most
    # form controls), so a `::before`/`::after` rule that happens to target
    # one (unusual, but not impossible) must not divert it into the
    # inline-formatting path instead and skip that branch entirely.
    has_pseudo = tag_name not in _NO_GENERATED_CONTENT_TAGS and (
        getattr(element, "_chromonic_before_pseudo", None) is not None
        or getattr(element, "_chromonic_after_pseudo", None) is not None
    )
    inline_items = (_inline_mixed_content(element, children, element_is_inline=is_genuinely_inline)
                    if (children or has_pseudo) else None)
    # `<td>`/`<th>` have no real UA default in domonic at all, so their own
    # computed `display` is uninformatively "inline" for practically every
    # real cell -- chromonic already treats them as block-level table cells
    # regardless (the whole tag-gated table/tr/td machinery above), and
    # `_InlineFormattingPlan`'s `owner_display` needs to agree: it decides
    # whether `_finalize_inline_owner_boxes` treats `element`'s *own*
    # published box as "this element is itself inline, so its box is the
    # union of its own content" (correct for a real `<span>`/`<a>`) or
    # leaves Taffy's own box alone (correct for a block-level container).
    # Left as "inline" for a `<td>`/`<th>` with mixed content (its own text
    # plus a nested element, e.g. a link), the cell's *own* published box
    # narrowed to just its own text fragment's bounding rect, discarding
    # the nested element's contribution and Taffy's own (already-correct,
    # full column-width) box entirely. Found on `en.wikipedia.org`'s
    # Python-article infobox: a colspan'd `<th>` ("Major " + a nested
    # `<a>implementations</a>") published a ~47px box (just "Major "'s own
    # width) instead of Taffy's correct ~352px column-width box.
    css_display_value = ("block" if tag_name in ("td", "th")
                          else getattr(style_obj.display, "value", "").strip() or "block")
    split_pieces = (_split_inline_flow_around_blocks(
                         element, inline_items, style, css_display_value, computed_cache)
                     if inline_items else None)
    inline_plan = (_make_inline_formatting_plan(element, inline_items, style, css_display_value)
                   if inline_items and split_pieces is None else None)

    if split_pieces is not None:
        # CSS 2.1 9.2.1.1: an inline element split around an in-flow block
        # child -- see `_split_inline_flow_around_blocks`. Each piece
        # becomes its own ordinary block-flow child of `element` (never a
        # flex row): a "plan" piece is one measured text leaf (same
        # machinery as the single-inline-plan case below, just built once
        # per piece instead of once for the whole element), a "block" piece
        # is that child's own real, recursively-built subtree.
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if style["width"] == "auto" and element._chromonic_tag_name != "body":
            # Once split, `element` stands in for the sequence of CSS 2.1
            # 9.2.1.1 anonymous block boxes wrapping its own pieces --
            # ordinary block boxes, which always fill their containing
            # block at `width:auto` (Taffy's own "auto" here means shrink-
            # to-fit, not fill, the same reason the plain single-inline-
            # plan branch below needs this identical `("pct", 1.0)`
            # correction) regardless of `element`'s own nominal `display`.
            # Found on `wpt/css/CSS2/linebox/inline-box-001.xht`: a
            # `display:inline` `div1` split around a block child measured
            # `196px` (its own content's shrink-to-fit width) instead of
            # the real `784px` containing block.
            #
            # `<body>` itself is excluded: it already has its own, more
            # accurate root-width machinery (`_constrain_root_to_document_
            # element`/`_apply_root_margin_offset`, accounting for its own
            # UA margin against the true viewport) -- resolving a plain
            # `pct(1.0)` here instead would resolve against the *viewport*
            # directly (body has no further containing block of its own to
            # subtract its margin from), overriding that correct mechanism
            # with a wrong one. Found on `wpt/css/CSS2/normal-flow/block-
            # in-inline-empty-001.xht`: body's own child (a `<span>` with
            # no explicit display, so genuinely inline) split around its
            # block child *inside body's own `_split_inline_flow_around_
            # blocks` call* -- `element` here was body itself -- measuring
            # `800px` (the full viewport) instead of Chrome's `784px`
            # (`800px` minus body's own `8px` left/right UA margins).
            style["width"] = ("pct", 1.0)
        owner_cache = element.__dict__.setdefault("_chromonic_split_plan_owners", {})
        piece_ids = []
        plan_index = 0
        for kind, *payload in split_pieces:
            if kind == "plan":
                (plan,) = payload
                owner = owner_cache.get(plan_index)
                if owner is None:
                    owner = owner_cache[plan_index] = _AnonymousInlineRun(None, element)
                plan_index += 1
                plan_style = _inline_text_style(style)
                plan_style["width"] = ("pct", 1.0) if style["display"] == "block" else "auto"
                measure_key = ("inline-context", tuple(element._chromonic_paint_style.items()), tuple(
                    ("break", id(run["element"])) if run.get("break") else
                    ("escapee", id(run["element"])) if run.get("escapee") else
                    (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
                     tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
                     run["leading"], run["trailing"], run["top_edge"], run["atomic_width"],
                     run.get("margin_start", 0.0))
                    for run in plan.runs
                ))
                if projection is not None and not projection.measure_changed(owner, measure_key):
                    plan = owner._chromonic_inline_plan
                owner._chromonic_inline_plan = plan
                owner._chromonic_native_style = plan_style
                measure = (plan.measure
                           if projection is None or projection.measure_changed(owner, measure_key) else None)
                piece_id = (projection.upsert(owner, plan_style, [], measure, measure_key)
                            if projection else tree.new_text_leaf(plan_style, measure))
                node_map[piece_id] = owner
                # A run tagged "escapee" (an absolutely-positioned element
                # mixed into this segment, CSS 2.1 9.2.1 -- out of flow, so
                # it never breaks the inline run around it) marks *where*
                # it sits for static-position purposes (`measure`/`publish`
                # above), but building its own real subtree is this
                # function's job, same as any other absolutely-positioned
                # child -- added to `escapees` (never `own_escapees`: an
                # inline-context escapee's containing block is almost never
                # this split wrapper itself) so it ends up exactly one edge
                # away from its real containing block, same as any other
                # out-of-flow descendant `build()` hoists.
                for run in plan.runs:
                    if run.get("escapee"):
                        escapee_child = run["element"]
                        escapee_is_cb = _establishes_containing_block(run["style"])
                        escapee_id = build(
                            tree, escapee_child, node_map, computed=run["computed"], style_obj=run["style"],
                            computed_cache=computed_cache, is_containing_block=escapee_is_cb,
                            escapees=escapees if not is_containing_block else own_escapees,
                            reuse_styles=reuse_styles, projection=projection,
                        )
                        (own_escapees if is_containing_block else escapees).append(escapee_id)
            else:
                child, child_computed, child_style = payload
                child_is_cb = _establishes_containing_block(child_style)
                piece_id = build(
                    tree, child, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                    reuse_styles=reuse_styles, projection=projection,
                )
            piece_ids.append(piece_id)
        for stale in [key for key in owner_cache if key >= plan_index]:
            owner_cache.pop(stale)
        all_child_ids = piece_ids + (own_escapees if is_containing_block else [])
        node_id = (projection.upsert(element, style, all_child_ids, None, None)
                   if projection else tree.new_with_children(style, all_child_ids))
    elif inline_plan is not None:
        element._chromonic_has_layout_children = True
        # `style["display"]` is already Taffy-mapped ("block" for *every*
        # non-flex/grid box, `style_bridge._display()` has no real "inline"
        # mode at all) -- checking it here can't tell a genuine block-level
        # element (width:auto correctly stretches to fill its containing
        # block) apart from an inline/inline-block element that merely
        # ended up with its own `inline_plan` (its own text content) after
        # being recursively built as one atomic flex item inside an
        # ancestor's flex-row fallback (`elif inline_items:` below) --
        # those must stay sized to their own content, not stretch to 100%
        # of a row they only occupy part of. `css_display_value` (the real,
        # pre-Taffy-mapping computed CSS `display`, already resolved above)
        # distinguishes them correctly. Found on `news.ycombinator.com`:
        # nested `<span class="age">`/`<a>` elements inside `span.subline`
        # (itself already flex-row-fallback content) were forced to
        # `width:100%` of that row, stacking every one of them onto its own
        # full-width line instead of flowing inline -- inflating the whole
        # page's height by roughly 2x (~3365px measured vs Chrome's real
        # ~1451px for this page).
        if css_display_value == "block" and style["width"] == "auto":
            style["width"] = ("pct", 1.0)
        measure_key = ("inline-context", tuple(element._chromonic_paint_style.items()), tuple(
            ("break", id(run["element"])) if run.get("break") else
            (id(run["source"]), id(run["owner"]), tuple(run["paint_style"].items()),
             tuple(run["tokens"]), run["above"], run["below"], run["box_height"],
             run["leading"], run["trailing"], run["top_edge"], run["atomic_width"],
             run.get("margin_start", 0.0))
            for run in inline_plan.runs
        ))
        if projection is not None and not projection.measure_changed(element, measure_key):
            # Taffy retains the callback bound to the existing plan. Publish
            # that plan's placements too, including when cached layout is used.
            inline_plan = element._chromonic_inline_plan
        element._chromonic_inline_plan = inline_plan
        measure = (inline_plan.measure
                   if projection is None or projection.measure_changed(element, measure_key) else None)
        node_id = (projection.upsert(element, style, [], measure, measure_key)
                   if projection else tree.new_text_leaf(style, measure))
    elif inline_items:
        element.__dict__.pop("_chromonic_inline_plan", None)
        # `paint.py` only draws an element's own `textContent` directly
        # (falling back to it when `_chromonic_text_lines` was never set)
        # when it believes the element has *no* layout children at all --
        # true before this branch could ever be reached with `children`
        # empty (see `has_pseudo` above): every previous caller of this
        # branch already had real child elements, so `bool(children)`
        # (set above) was already `True`. A `::before`/`::after`-only
        # element (no real child elements, e.g. `csszengarden.com`'s
        # `<h1>`) reaches here for the first time with that flag still
        # `False`, and paint would draw the element's raw `textContent`
        # a second time *on top of* the correctly-styled fragment this
        # branch already builds for it below.
        element._chromonic_has_layout_children = True
        style["display"] = "flex"
        style["flex_direction"] = "row"
        style["flex_wrap"] = "wrap"
        style["align_items"] = "baseline"
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        paint_style = element._chromonic_paint_style
        family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
        weight = _parse_font_weight(paint_style["font_weight"])
        italic = fonts.is_italic(paint_style["font_style"])
        one = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
        spaced = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
        space_width = max(0.0, spaced - 2 * one)
        normal_child_ids = []
        fragments = []
        for item_index, (kind, item, text, child_computed, child_style) in enumerate(inline_items):
            if kind == "element":
                # An absolutely-positioned item counts as "inline" for
                # `_inline_mixed_content`'s own purposes regardless of its
                # real (block-level) display -- CSS 2.1 9.2.1 lets an
                # out-of-flow descendant sit anywhere in an inline run
                # without breaking it -- so it can land here, in the flex-
                # row fallback, instead of the `elif children:` branch
                # below, which *does* already escape such a child to its
                # real containing-block ancestor when `element` (this call,
                # its literal DOM parent) isn't a valid one. This branch
                # was missing that same check entirely -- every "element"
                # item, out-of-flow or not, became an ordinary flex child,
                # so Taffy positioned its `inset` relative to `element`'s
                # own box (adding `element`'s border/flow offset on top of
                # the author's `top`/`left`) instead of hoisting it past a
                # non-containing-block `element` the way CSS requires.
                # Found on `wpt/css/CSS2/positioning/position-003.xht`: a
                # plain (non-positioned) `#wrapper` around a single
                # `position:absolute` child measured `wrapper`'s own
                # `3px` border added onto the child's authored `left`.
                child_is_cb = _establishes_containing_block(child_style)
                if _is_absolutely_positioned(child_style) and not is_containing_block:
                    child_id = build(
                        tree, item, node_map, computed=child_computed, style_obj=child_style,
                        computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=escapees,
                        reuse_styles=reuse_styles, projection=projection,
                    )
                    escapees.append(child_id)
                else:
                    normal_child_ids.append(build(
                        tree, item, node_map, computed=child_computed, style_obj=child_style,
                        computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                        reuse_styles=reuse_styles, projection=projection,
                    ))
                if isinstance(item, _PseudoElement):
                    # Not a real DOM child, so `paint_tree`/`build_display_
                    # list`'s own `element.childNodes` recursion will never
                    # discover it -- reaches paint only via this same side-
                    # channel list retained text fragments already use.
                    fragments.append(item)
                continue
            fragment_style = _inline_text_style(style)
            raw = getattr(getattr(item, "source", None), "textContent", "") or ""
            leading = space_width if (raw[:1].isspace() or
                                      getattr(item, "_chromonic_leading_collapsed_space", False)) else 0.0
            has_later_in_flow_item = any(
                later_kind == "text" or not _is_absolutely_positioned(later_style)
                for later_kind, _later_item, _later_text, _later_computed, later_style
                in inline_items[item_index + 1:]
            )
            trailing = space_width if raw[-1:].isspace() and has_later_in_flow_item else 0.0
            fragment_style["margin"] = [0.0, trailing, 0.0, leading]
            item._chromonic_native_style = fragment_style
            item._chromonic_paint_style = element._chromonic_paint_style
            measure_key = _measure_key(item._chromonic_paint_style, text)
            measure = (_make_measure(item._chromonic_paint_style, text, item)
                       if projection is None or projection.measure_changed(item, measure_key) else None)
            child_id = (projection.upsert(item, fragment_style, [], measure, measure_key)
                        if projection else tree.new_text_leaf(fragment_style, measure))
            node_map[child_id] = item
            normal_child_ids.append(child_id)
            fragments.append(item)
        element._chromonic_inline_fragments = fragments
        all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
        node_id = (projection.upsert(element, style, all_child_ids, None, None)
                   if projection else tree.new_with_children(style, all_child_ids))
    elif children:
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        _approximate_inline_flow(
            element,
            style,
            [child for child, _computed, _child_style in children],
            [child_computed for _child, child_computed, _child_style in children],
            [child_style for _child, _computed, child_style in children],
            computed,
        )
        normal_child_ids = []
        normal_entries = []
        for child, child_computed, child_style in children:
            child_is_cb = _establishes_containing_block(child_style)
            if _is_absolutely_positioned(child_style) and not is_containing_block:
                # `element` (this call, the child's literal DOM parent) is
                # not a valid containing block -- this child's real one is
                # further up, whatever ancestor `escapees` actually belongs
                # to. Build it normally, but hand its node id to *that*
                # ancestor instead of adding it to `element`'s own children.
                child_id = build(
                    tree, child, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=escapees,
                    reuse_styles=reuse_styles, projection=projection,
                    is_grid_item=style["display"] == "grid",
                )
                escapees.append(child_id)
            else:
                child_id = build(
                    tree, child, node_map, computed=child_computed, style_obj=child_style,
                    computed_cache=computed_cache, is_containing_block=child_is_cb, escapees=own_escapees,
                    reuse_styles=reuse_styles, projection=projection,
                    is_grid_item=style["display"] == "grid",
                )
                normal_child_ids.append(child_id)
                normal_entries.append((child, child_style, child_id))
        if normal_entries:
            normal_child_ids = _group_inline_element_runs(
                tree, element, normal_entries, style, node_map, projection,
            )
        all_child_ids = normal_child_ids + (own_escapees if is_containing_block else [])
        node_id = (projection.upsert(element, style, all_child_ids, None, None)
                   if projection else tree.new_with_children(style, all_child_ids))
    elif tag_name == "br":
        # CSS 2.1 9.2.2: `<br>` is a forced inline line break -- it never
        # generates an ordinary block box at all, whether or not it sits
        # inside real mixed inline content. Inside a real paragraph's own
        # inline-formatting-plan (`_build_text_runs_from_nodes`'s "break"
        # runs, `_InlineFormattingPlan.measure()`'s own handling of them),
        # this element is never even reached recursively -- but a `<br>`
        # sitting directly among ordinary block siblings, with no
        # surrounding text or inline content to route it through that
        # machinery at all (`_inline_mixed_content`'s own gate correctly
        # declines a container whose other children are genuine blocks),
        # falls all the way through to this ordinary per-tag dispatch
        # instead. `<br>` isn't in `_USUALLY_INLINE_TAGS` (there's no safe
        # tag-based signal for "trust domonic's raw computed inline
        # default" the way there is for `<span>`/`<a>`/..., and none is
        # needed -- `<br>` never behaves like an ordinary inline anyway),
        # so without this it fell through as a plain, untrusted element:
        # an ordinary block, `width:auto` filling the full container and
        # `height:auto` collapsing to `0` with no content of its own.
        # Sized here to a single line's own strut instead -- zero width,
        # one line-height tall, from its own (inherited) font/line-height
        # -- the same font-metrics math `_empty_inline_strut_run` uses for
        # a real empty inline. Found on `wpt/css/CSS2/mpc/padding-top-
        # 036.xht`: a bare `<br />` between two block `<div>`s measured
        # `784x0` instead of Chrome's `0x18`, losing a whole line box's
        # height from the page and every following element's own `y`.
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        paint_style = element._chromonic_paint_style
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # See the identical comment at this same pattern's first
        # occurrence, in `_build_text_runs_from_nodes`: an explicit
        # `line-height: 0` must not be treated the same as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        style["width"] = 0.0
        style["height"] = line_height
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    elif tag_name in ("img", "canvas", "svg"):
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if tag_name == "img":
            _apply_image_intrinsic_size(style, element)
        elif tag_name == "canvas":
            _apply_canvas_intrinsic_size(style, element)
        else:
            _apply_svg_intrinsic_size(style, element)
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    elif tag_name == "select":
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        text = _select_display_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
                       if projection is None or projection.measure_changed(element, measure_key) else None)
            node_id = (projection.upsert(element, style, [], measure, measure_key)
                       if projection else tree.new_text_leaf(style, measure))
        else:
            element._chromonic_text_lines = []
            node_id = (projection.upsert(element, style, [], None, None)
                       if projection else tree.new_leaf(style))
    elif tag_name in ("input", "textarea"):
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        text = _form_control_display_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
                       if projection is None or projection.measure_changed(element, measure_key) else None)
            node_id = (projection.upsert(element, style, [], measure, measure_key)
                       if projection else tree.new_text_leaf(style, measure))
        else:
            element._chromonic_text_lines = []
            node_id = (projection.upsert(element, style, [], None, None)
                       if projection else tree.new_leaf(style))
    else:
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        text = _own_text(element)
        if text:
            measure_key = _measure_key(element._chromonic_paint_style, text)
            measure = (_make_measure(element._chromonic_paint_style, text, element)
                       if projection is None or projection.measure_changed(element, measure_key) else None)
            node_id = (projection.upsert(element, style, [], measure, measure_key)
                       if projection else tree.new_text_leaf(style, measure))
        else:
            element._chromonic_text_lines = []
            node_id = (projection.upsert(element, style, [], None, None)
                       if projection else tree.new_leaf(style))

    node_map[node_id] = element
    return node_id


def _measure_key(paint_style: dict, text: str) -> tuple:
    """Inputs that can change a Taffy leaf's intrinsic text measurement."""
    return (
        text, paint_style["font_family"], paint_style["font_size"],
        paint_style["font_weight"], paint_style["font_style"],
        paint_style["letter_spacing"], paint_style["word_spacing"],
        paint_style["line_height"], paint_style.get("white_space", "normal"),
    )


class LayoutProjection:
    """A retained Taffy projection of an authoritative live Domonic tree.

    Domonic remains the source of structure, styles and text. Each reconcile
    walks that tree, reuses native nodes by element identity, and only mutates
    native style, child or measure state whose snapshot changed.
    """

    def __init__(self):
        self.tree = Tree()
        self.nodes = {}
        self.state = {}
        self.node_map = {}
        self._seen = set()

    def begin(self):
        self._seen.clear()
        self.node_map = {}

    def measure_changed(self, element, measure_key):
        previous = self.state.get(id(element))
        return previous is None or previous[2] != measure_key

    def upsert(self, element, style, children, measure, measure_key):
        key = id(element)
        self._seen.add(key)
        children = tuple(children)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        if node is None:
            if children:
                node = self.tree.new_with_children(style, list(children))
            elif measure is not None:
                node = self.tree.new_text_leaf(style, measure)
            else:
                node = self.tree.new_leaf(style)
            self.nodes[key] = node
        else:
            old_style, old_children, old_measure_key = previous
            if old_style != style:
                self.tree.set_style(node, style)
            if old_children != children:
                self.tree.set_children(node, list(children))
            if old_measure_key != measure_key:
                self.tree.set_measure(node, measure)
        # Keep an independent value snapshot. Image intrinsic sizing
        # mutates its cached dictionary in place; retaining that object would
        # make the next dirty comparison miss the change.
        self.state[key] = (_snapshot_style(style), children, measure_key)
        self.node_map[node] = element
        return node

    def finish(self):
        stale = set(self.nodes) - self._seen
        for key in stale:
            self.tree.remove(self.nodes.pop(key))
            self.state.pop(key, None)

    def patch_style(self, element, **changes):
        """Apply known layout-field changes after their Domonic mutation.

        This is an explicit incremental bridge for callers that know exactly
        which translated Taffy fields their authoritative DOM write changed.
        Unknown CSS mutations must use ``layout()`` to reconcile normally.
        """
        key = id(element)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        style = getattr(element, "_chromonic_native_style", None)
        if node is None or previous is None or style is None:
            raise KeyError("element is not present in this layout projection")
        style = dict(style)
        style.update(changes)
        element._chromonic_native_style = style
        self.tree.set_style(node, style)
        _old_style, children, measure_key = previous
        self.state[key] = (_snapshot_style(style), children, measure_key)

    def patch_insets(self, updates):
        """Batch known ``(element, top, right, bottom, left)`` changes."""
        native_updates = []
        for element, top, right, bottom, left in updates:
            key = id(element)
            node = self.nodes.get(key)
            previous = self.state.get(key)
            style = getattr(element, "_chromonic_native_style", None)
            if node is None or previous is None or style is None:
                raise KeyError("element is not present in this layout projection")
            inset = [float(top), float(right), float(bottom), float(left)]
            native_updates.append((node, *inset))
            # Cached native style and its retained snapshot are independent;
            # update only the one changed field in each instead of copying a
            # roughly 50-property dictionary per animated element.
            style["inset"] = inset
            snapshot, _children, _measure_key = previous
            snapshot["inset"] = list(inset)
        self.tree.set_insets(native_updates)

    def compute(self, root_element, *, width, height=None, viewport_height=None):
        """Compute and publish geometry after explicit projection patches.
        `viewport_height`: see `layout()`'s own parameter of the same name."""
        root_id = self.nodes[id(root_element)]
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        _write_boxes(boxes, self.node_map)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )

    def layout(self, root_element, *, width, height=None, reuse_styles=False, viewport_height=None):
        """See the module-level `layout()` function for what every
        parameter here means -- this is the same operation, just against a
        retained projection that reuses native nodes by element identity
        instead of rebuilding the whole Taffy tree from scratch."""
        from . import webfonts
        if webfonts.prepare_layout(root_element):
            reuse_styles = False
        self.begin()
        with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
            root_id = build(
                self.tree, root_element, self.node_map,
                reuse_styles=reuse_styles, projection=self,
            )
        self.finish()
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        _write_boxes(boxes, self.node_map)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )


def _snapshot_style(style):
    # style_bridge emits primitives/tuples and top-level lists of those. Copy
    # list values so later intrinsic-image mutation cannot alias the snapshot;
    # dict equality then stays in optimized Python/C code during reconciliation.
    return {key: list(value) if isinstance(value, list) else value
            for key, value in style.items()}


def _root_compute_height(root_element, height, viewport_height):
    if height is not None or viewport_height is None:
        return height
    style = getattr(root_element, "_chromonic_native_style", {})
    root_height = style.get("height")
    if isinstance(root_height, tuple) and root_height == ("pct", 1.0):
        return viewport_height
    return height


def _document_element_box_edges(root_element):
    """`(left, right, top, bottom)` margin+border+padding from `<html>`'s
    own computed style, or `None` if `root_element` isn't `<body>` with a
    real `<html>` parent, or `<html>` has none of the three set at all.

    `<html>` -- the real CSS root -- is never built into the Taffy tree at
    all: chromonic hands Taffy `<body>` as *its* root instead (see
    `_apply_root_margin_offset`'s own docstring for the equivalent gap this
    already covers for `<body>`'s own margin), so `<html>`'s own margin/
    border/padding were never read anywhere, let alone applied. Found on
    `wpt/css/CSS2/positioning/abspos-016.xht`: `html { padding: 10px }`,
    `body` measured at the viewport's own origin/width instead of `html`'s
    content box (`x:10, width: viewport - 20`); `abspos-019.xht`/`-020.xht`:
    the same gap for `html { margin: 10px }` instead of `padding`. All
    three box-model layers push `<body>` inward from the viewport/shrink
    its available width exactly the same way from this function's point of
    view, so they're summed together rather than kept separate -- nothing
    downstream needs to tell them apart."""
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return None
    # `.parentElement` is broken specifically for `<body>` in domonic --
    # confirmed `None` even though `.parentNode` correctly gives the
    # `<html>` object (an ordinary element's own `.parentElement` works
    # fine; this is narrower than that) -- so `.parentNode` is used here
    # instead. That object's own `nodeType` is `9` (`DOCUMENT_NODE`), not
    # `1` (`ELEMENT_NODE`) -- domonic's `<html>` and `Document` appear to
    # be the same underlying object rather than distinct nodes (confirmed
    # both via a local file load and the real HTTP-serving path this
    # project's own harness uses) -- so `_is_element()` can't be used to
    # identify it either; `tagName` alone is the reliable signal.
    html_element = getattr(root_element, "parentNode", None)
    if (html_element is None
            or (getattr(html_element, "tagName", "") or "").lower() != "html"):
        return None
    from domonic.style import ComputedStyleDeclaration
    computed = ComputedStyleDeclaration(html_element)

    def edge_px(name: str) -> float:
        raw = str(getattr(computed, name, "") or "0px")
        try:
            return float(raw[:-2]) if raw.endswith("px") else 0.0
        except ValueError:
            return 0.0

    left = edge_px("marginLeft") + edge_px("paddingLeft") + edge_px("borderLeftWidth")
    right = edge_px("marginRight") + edge_px("paddingRight") + edge_px("borderRightWidth")
    top = edge_px("marginTop") + edge_px("paddingTop") + edge_px("borderTopWidth")
    bottom = edge_px("marginBottom") + edge_px("paddingBottom") + edge_px("borderBottomWidth")
    if left == 0.0 and right == 0.0 and top == 0.0 and bottom == 0.0:
        return None
    return (left, right, top, bottom)


def _constrain_root_to_document_element(tree_obj, root_element, root_id, width: float) -> float:
    """Corrects `root_element` (`<body>`)'s own Taffy style for `<html>`'s
    margin/border/padding (`_document_element_box_edges`) *before*
    `tree_obj.compute()` runs, and returns the available width `<body>`
    must actually be computed against (`width` minus `<html>`'s horizontal
    edges) -- callers pass this on to `compute()` in place of the raw
    viewport `width`.

    `<body>`'s own `width:auto` is *always* forced to a definite number --
    `<html>`'s own edges (if any) *and* `<body>`'s own margin subtracted
    from `width` -- rather than left for Taffy to resolve, regardless of
    whether `<html>` has any margin/border/padding of its own at all: a
    root Taffy node (no real parent to inherit ordinary block "stretch to
    fill available space" semantics from) whose only children are all
    out-of-flow (every one absolutely/fixed-positioned, contributing
    nothing to intrinsic content size) resolves `auto` via max-content
    (shrink-to-fit) sizing instead, collapsing to `0` -- found on both
    `abspos-015.xht` (`<html>` *does* have padding, already forced the fix
    to trigger) and `abspos-017.xht` (`<html>` has none at all, only
    `<body>`'s own margin -- previously left this case, a real, reachable
    page shape of its own (a page that's *only* positioned overlays), still
    broken since `_document_element_box_edges` returning `None` skipped the
    whole correction).

    The number forced into `style["width"]` is `<body>`'s own *content*
    width, not its border-box/viewport-constrained width -- CSS `width`
    (content-box, the default `box-sizing`) never includes padding/border,
    Taffy adds those back on top when it builds the actual border box. This
    was previously left out entirely (`style["width"]` was set to the full
    viewport-derived value with no padding/border subtracted at all),
    quietly treating the viewport width as `<body>`'s *content* width and
    then adding its padding/border back *outside* that -- found on
    `abspos-001.xht` (`body { padding: 16px }`, no `<html>` edges, no
    `<body>` margin): Chrome's `<body>` border box is `800x512` (content
    `768x480`, children starting at the `x:16` padding edge); chromonic's
    was `832` wide (`800` forced into `width` as if it were the content
    size, plus `16px` padding each side stacked back on top of that,
    instead of inside it), with every child starting at `x:0` instead of
    the real padding edge. `box-sizing: border-box` (rare on a real
    `<body>`, but not impossible) is left alone -- there, `width` already
    means the border-box total, and padding/border must *not* be
    subtracted a second time."""
    edges = _document_element_box_edges(root_element)
    html_left, html_right = edges[0:2] if edges is not None else (0.0, 0.0)
    style = root_element.__dict__.get("_chromonic_native_style")
    body_margin = style.get("margin") if style is not None else None
    body_margin_left = _resolve_inset((body_margin or (0.0,) * 4)[3], width) or 0.0
    body_margin_right = _resolve_inset((body_margin or (0.0,) * 4)[1], width) or 0.0
    available_width = max(0.0, width - html_left - html_right)
    if style is not None and style.get("width") == "auto":
        outer_width = max(0.0, available_width - body_margin_left - body_margin_right)
        if style.get("box_sizing") != "border-box":
            padding = style.get("padding") or (0.0,) * 4
            border = style.get("border") or (0.0,) * 4
            outer_width = max(0.0, outer_width
                               - _numeric_edge(padding[1]) - _numeric_edge(padding[3])
                               - _numeric_edge(border[1]) - _numeric_edge(border[3]))
        style["width"] = outer_width
        tree_obj.set_style(root_id, style)
    return available_width


def warm_text_layout() -> None:
    """Pay Parley's one-time `FontContext` setup cost now, not during the
    first real text on the first real page. Measured: the very first
    `layout_text()` call in a process costs ~100ms (font enumeration --
    building `fontique`'s font database); every call after that is close to
    free (backed by the `thread_local!` `FontContext`/`LayoutContext` in
    `src/lib.rs`, reused for the life of the process). The same "pay it at
    startup" idea `fonts.warm_cache()` and `browser.warm_interpreter()`
    already use -- called once from `native_browser.py`'s `View.__init__`."""
    layout_text("warm", "sans-serif", 16.0)


def _write_boxes(boxes, node_map):
    """Publish native geometry back onto the authoritative Domonic nodes."""
    for node_id, box in boxes.items():
        x, y, w, h, bt, br, bb, bl, pt, pr, pb, pl = box
        element = node_map[node_id]
        state = element.__dict__
        # domonic.layout.set_layout_box and Element.set_layout_box ultimately
        # perform this exact private-state assignment. We already have the
        # element here, so avoid two wrappers plus Element.__setattr__ for
        # every node while preserving all public geometry readers.
        state["_layout_box"] = LayoutBox(
            x=x, y=y, width=w, height=h,
            client_width=w - bl - br,
            client_height=h - bt - bb,
            border_top=bt, border_left=bl,
        )
        state["_chromonic_padding"] = (pt, pr, pb, pl)


def _fix_float_shrink_to_fit_width(tree_obj, node_map: dict) -> None:
    """CSS 2.1 10.3.5/10.3.6: a floated box with `width:auto` is sized by
    *shrink-to-fit* (its own max-content/intrinsic width, capped at the
    space available), not stretched to fill its containing block the way
    an ordinary in-flow block's `width:auto` is -- chromonic has no real
    float implementation at all (see `_is_floated`'s docstring), so a
    floated element reaches this point laid out as if it were an ordinary
    full-width block, needing correcting after the fact.

    Found on `wpt/css/CSS2/positioning/positioning-float-001.xht`/`-002.xht`:
    a `float:left`/`float:right` `<span>`/`<div>` with `width:auto`
    measured the full `784px` containing-block width instead of Chrome's
    shrink-to-fit `85.359px`/`102.641px`.

    Re-runs Taffy's own `compute()` for just this element's already-built
    node, at `available_width=None` (`AvailableSpace::MaxContent`, the same
    call `_measure_intrinsic_width` uses in a disposable tree) -- this
    re-lays-out the real subtree (nested tags, text wrapping included), not
    a hand-rolled text-only measurement, and returns every descendant's own
    box too, so they reflow into the narrower width instead of just being
    translated. Those boxes land in a coordinate space relative to the
    element's own origin (`collect_absolute` starts accumulating from
    `(0, 0)` at whatever node id it's given); `_write_boxes` publishes them
    as-is, then `_shift_subtree` carries them to the real page position in
    one step, the same two-part pattern `_fix_absolute_horizontal_auto_
    margins` and friends already use.

    Only ever *shrinks* -- if the intrinsic width isn't smaller than what
    this element already has (an ordinary block's full container width),
    there is nothing to correct; float positioning generally, and real
    content flowing around a float, are both still out of scope (see
    `_approximate_inline_flow`'s docstring)."""
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None or not _is_floated(resolved[0]):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("width") != "auto":
            continue
        node_id = by_id.get(id(element))
        if node_id is None:
            continue
        boxes = tree_obj.compute(node_id, None, None)
        own = boxes.get(node_id)
        if own is None:
            continue
        new_width = own[2]
        if new_width >= box.width:
            continue  # shrink-to-fit never grows a box past its available width
        float_value = getattr(resolved[0], "float", None)
        float_value = (float_value or "").strip().lower()
        target_x = (box.x + box.width - new_width) if float_value == "right" else box.x
        _write_boxes(boxes, node_map)
        dx = target_x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_subtree(element, dx, dy)


def _fix_float_flow_after_block_sibling(node_map: dict) -> None:
    """CSS 2.1 9.5: a floated box may never extend above its containing
    block's content edge or above an earlier in-flow block-level sibling's
    own box -- it starts at or below the current block-flow position, at
    the containing block's edge, not wherever a previous sibling's own box
    happened to end horizontally. `_approximate_inline_flow` stands in for
    real float layout with plain `flex-wrap` (see its own docstring for
    why), which has no notion of this rule at all: it only starts a new
    row once a row's *width* overflows, so an ordinary paragraph followed
    by floats packed the floats onto the paragraph's own row, right after
    its box, instead of dropping them below it.

    Runs after Taffy's flex-wrap layout, using the qualifying/non-
    qualifying split `_approximate_inline_flow` recorded on `element`
    (`_chromonic_float_flow_children`/`_chromonic_float_flow_qualifies`).
    Deliberately narrow: does nothing unless at least one child is an
    ordinary block (a "qualifies" entry of `False`) *and* every qualifying
    child is a real float (`_is_floated`), not merely inline-level --
    mixed block-plus-inline-tag groups (rarer, and already approximate,
    see `_approximate_inline_flow`'s own gap-spacing heuristic) are left
    to Taffy's own flex-wrap result rather than risk dropping that
    spacing. When it does apply, every child's position is recomputed by
    simple left-to-right block/float packing: an ordinary block resets the
    row to the containing block's content left edge, at or below
    everything laid out so far; a float packs onto the current row,
    wrapping to a new one only when it no longer fits.

    Found on `wpt/css/CSS2/linebox/fractional-line-height.html`: a `<p>`
    followed by four `float:left` containers landed all five on one flex-
    wrap row (`<p>`'s own width left enough room) instead of the floats
    starting on their own line below the `<p>`."""
    for element in list(node_map.values()):
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None or False not in qualifies:
            continue
        if any(is_flow and not _is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = box.x + box.border_left + pl
        content_right = content_left + (box.client_width - pl - pr)
        cursor_x = content_left
        right_cursor_x = content_right
        cursor_y = box.y + box.border_top + pt
        row_bottom = cursor_y
        # CSS 2.1 9.5: every still-uncleared float narrows the line box of
        # every row that overlaps its own vertical extent, on whichever
        # side it floats to -- not just the row it first packed onto.
        # Tracked here (side, the narrowing edge, and the y below which it
        # no longer applies) so a later ordinary block sharing that space
        # resolves its own `margin:auto` against the narrowed band, not the
        # container's full content width.
        active_floats: list = []
        # `_adjust_body_collapsed_margins` may have already anchored
        # `element`'s own top (`cursor_y` here, read from its finished
        # box) to this very first child's own margin-top -- CSS 2.1
        # 8.3.1's adjoining-margins collapse, folding the two into one
        # (the larger) that then escapes past `element` entirely, leaving
        # no *internal* gap between `element`'s content top and this
        # child at all. Treated as an already-resolved pending margin of
        # `0`, not `mt` -- every later ordinary block still collapses
        # normally with whatever came before it.
        first_margin_collapsed = getattr(element, "_chromonic_margin_collapsed", False)
        # CSS 2.1 8.3.1: adjoining margins collapse into one -- the block-
        # flow position (`block_bottom`) only ever advances by that one
        # collapsed value, never by each margin separately, and a float
        # in between two ordinary blocks (out of flow, so it never
        # separates them) does not break the adjoining chain. `pending_
        # margins` accumulates every not-yet-resolved margin in the
        # current chain (an empty block, CSS 2.1's own "collapses
        # through" case, joins *both* its own top and bottom margin to
        # it without resolving anything); a real block resolves the whole
        # set at once via `_collapse_margin_set` and starts a fresh chain
        # with just its own bottom margin.
        pending_margins: list = []
        block_bottom = cursor_y
        for index, (child, is_flow) in enumerate(zip(children, qualifies)):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            mt, mr, mb, ml = (_numeric_edge(v) for v in margin)
            if not is_flow:
                if index == 0 and first_margin_collapsed:
                    mt = 0.0
                pending_margins.append(mt)
                if _block_margins_collapse_through(child, child_box):
                    # Own top/bottom margin join the same adjoining set as
                    # whatever precedes and follows -- nothing resolves
                    # yet, so this empty block's own (zero-size) position
                    # is only ever a best-effort placement at the set's
                    # current resolution; a still-later margin joining the
                    # same set can't retroactively move it, but it has no
                    # visible extent for that to matter.
                    pending_margins.append(mb)
                    new_x = content_left + ml
                    new_y = block_bottom + _collapse_margin_set(pending_margins)
                    dx, dy = new_x - child_box.x, new_y - child_box.y
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        _shift_subtree(child, dx, dy)
                    cursor_x = content_left
                    continue
                # An ordinary in-flow block: own row, at the containing
                # block's own edge, below everything placed so far, at
                # the whole pending chain's one collapsed margin -- unless
                # a still-active float (CSS 2.1 9.5) narrows that row *and*
                # this child actually establishes its own BFC (9.4.1) --
                # only a BFC-establishing box avoids a float that way; an
                # ordinary block's border box may extend behind one (only
                # its inline content wraps around it, out of scope here).
                # When narrowed, any `margin:auto` on this side resolves
                # against the narrowed band, not the full content width.
                collapsed = _collapse_margin_set(pending_margins)
                new_y = block_bottom + collapsed
                narrowed_left = content_left
                narrowed_right = content_right
                child_computed = (getattr(child, "_chromonic_resolved_style", None) or (None,))[0]
                if _establishes_bfc(child_computed):
                    for active in active_floats:
                        if active["bottom"] <= new_y:
                            continue
                        if active["side"] == "left":
                            narrowed_left = max(narrowed_left, active["edge"])
                        else:
                            narrowed_right = min(narrowed_right, active["edge"])
                ml_auto = margin[3] == "auto"
                mr_auto = margin[1] == "auto"
                if ml_auto or mr_auto:
                    available = max(0.0, narrowed_right - narrowed_left)
                    remaining = available - child_box.width
                    if ml_auto and mr_auto:
                        ml = mr = remaining / 2.0
                    elif ml_auto:
                        ml = remaining - mr
                    else:
                        mr = remaining - ml
                new_x = narrowed_left + ml
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    _shift_subtree(child, dx, dy)
                block_bottom = new_y + child_box.height
                pending_margins = [mb]
                row_bottom = cursor_y = block_bottom
                cursor_x = content_left
                right_cursor_x = content_right
                continue
            if pending_margins:
                # A float never participates in margin collapsing itself
                # (CSS 2.1 8.3.1 only ever adjoins in-flow block boxes),
                # but it still starts *below* whatever vertical space a
                # still-pending collapsed margin resolves to -- resolved
                # here, once, the first time anything (this float) is
                # actually placed at that flow position; a later ordinary
                # block starts its own fresh chain from `block_bottom`
                # exactly as if this float were never there, matching the
                # float being out of flow for collapsing purposes.
                block_bottom = block_bottom + _collapse_margin_set(pending_margins)
                cursor_y = row_bottom = block_bottom
                pending_margins = []
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            float_side = "left"
            if child_resolved is not None:
                float_value = (getattr(child_resolved[0], "float", None) or "").strip().lower()
                if float_value == "right":
                    float_side = "right"
            if float_side == "right":
                # `float:right` packs flush to the containing block's own
                # right content edge (or the innermost edge still free on
                # the current row, for a second right float sharing it),
                # not the left-to-right packing below -- CSS 2.1 9.5.1.
                start_x = right_cursor_x - mr - child_box.width
                if start_x < cursor_x and right_cursor_x < content_right:
                    cursor_y = row_bottom
                    right_cursor_x = content_right
                    start_x = right_cursor_x - mr - child_box.width
                new_x, new_y = start_x, cursor_y + mt
                dx, dy = new_x - child_box.x, new_y - child_box.y
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    _shift_subtree(child, dx, dy)
                right_cursor_x = new_x - ml
                bottom = new_y + child_box.height + mb
                row_bottom = max(row_bottom, bottom)
                active_floats.append({"side": "right", "edge": new_x - ml, "bottom": bottom})
                continue
            start_x = cursor_x + ml
            if start_x + child_box.width + mr > right_cursor_x and cursor_x > content_left:
                cursor_x = content_left
                cursor_y = row_bottom
                start_x = cursor_x + ml
            new_x, new_y = start_x, cursor_y + mt
            dx, dy = new_x - child_box.x, new_y - child_box.y
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                _shift_subtree(child, dx, dy)
            cursor_x = new_x + child_box.width + mr
            bottom = new_y + child_box.height + mb
            row_bottom = max(row_bottom, bottom)
            active_floats.append({"side": "left", "edge": cursor_x, "bottom": bottom})


def _shift_later_siblings_for_height_delta(element, delta: float) -> None:
    """When `element`'s own height just changed by `delta` (a post-hoc
    correction, after Taffy already stacked its siblings using the old
    value), every later DOM sibling sharing its parent's ordinary block
    flow needs the same vertical shift -- Taffy positioned each one
    immediately after the previous sibling's own (now-stale) box.
    Absolutely/fixed-positioned siblings are excluded: their own position
    doesn't derive from preceding-sibling flow at all. A `display:none`
    sibling is excluded too, by `_shift_subtree` itself -- see its
    docstring."""
    parent = getattr(element, "parentNode", None)
    if parent is None or not _is_element(parent):
        return
    seen_self = False
    for sibling in (parent.childNodes or []):
        if sibling is element:
            seen_self = True
            continue
        if not seen_self or not _is_element(sibling):
            continue
        sibling_style = getattr(sibling, "_chromonic_native_style", None) or {}
        if sibling_style.get("position") in ("absolute", "fixed"):
            continue
        if sibling.__dict__.get("_layout_box") is None:
            continue
        _shift_subtree(sibling, 0.0, delta)


def _bfc_descendant_float_bottom(element, floor: float) -> float:
    """The deepest bottom-margin-edge of any float inside `element`'s own
    block formatting context (CSS 2.1 10.6.7) -- descends through every
    descendant that does *not* itself establish a BFC (an ordinary
    wrapper div is not that float's containing block; the nearest real
    BFC ancestor still owns it, however many plain-block layers of
    nesting sit in between), and stops at any descendant that *does*
    establish its own BFC (that one is responsible for its own floats,
    not this one)."""
    best = floor
    for child in element.childNodes or []:
        if not _is_element(child):
            continue
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is None:
            continue
        computed, style_obj = resolved
        if _is_absolutely_positioned(style_obj):
            continue
        child_box = child.__dict__.get("_layout_box")
        if child_box is None:
            continue
        if _is_floated(computed):
            native = getattr(child, "_chromonic_native_style", None) or {}
            margin = native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            best = max(best, child_box.y + child_box.height + _numeric_edge(margin[2]))
            continue
        if _establishes_bfc(computed):
            continue
        best = max(best, _bfc_descendant_float_bottom(child, floor))
    return best


def _fix_nested_bfc_float_auto_height(node_map: dict) -> None:
    """The same CSS 2.1 10.6.3/10.6.7 rule `_fix_float_flow_container_
    auto_height` applies -- a `height:auto` box's own auto-height never
    counts a float unless the box itself establishes a BFC -- but for the
    cases that heuristic doesn't reach at all: it only ever runs on a
    container `_approximate_inline_flow` converted to `flex-wrap` (2+
    children, most/any of them floated -- see that function's own
    docstring), so a lone float (the single child of an ordinary wrapper
    div) never gets marked and reaches Taffy as a plain in-flow block, its
    own full border-box height counted toward the wrapper's auto-height
    like any other child -- Taffy has no notion that it should be excluded
    at all, only that its *margin* might collapse through.

    Found on `wpt/css/CSS2/normal-flow/block-formatting-context-height-
    002.xht`: `#container` (`position:absolute`, so it establishes a BFC)
    contains one plain wrapper `<div>`, whose only child is a `float:left`
    `48px`-tall `#float` with a `48px` bottom margin. The wrapper -- an
    ordinary block, no BFC of its own -- measured `48px` (the float's own
    height, its escaped-through margin already handled correctly by
    Taffy's own native margin-collapse) instead of Chrome's `0px` (a box
    with no real in-flow children of its own is exactly `0px` tall,
    `10.6.3` -- a float is never in-flow); `#container` itself needs the
    opposite correction, recursing *past* that same non-BFC wrapper to
    find the float since a BFC's auto-height counts every descendant float
    within its own formatting context, not just direct children."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        if getattr(element, "_chromonic_float_flow_children", None) is not None:
            continue  # already handled by _fix_float_flow_container_auto_height
        if getattr(element, "_chromonic_tag_name", None) == "body":
            continue  # _adjust_body_collapsed_margins owns body
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        if native.get("display") in ("flex", "grid"):
            # CSS floats compute to `float: none` on a flex/grid item
            # regardless of their author value (CSS Flexbox 3 sect. 2, CSS
            # Grid sect. 3) -- a flex/grid container can *never* actually
            # have a floated child, so this function's whole premise (an
            # auto-height box whose real content-bottom needs recomputing
            # to account for an escaped/BFC-contained float) never applies
            # to one. `_establishes_bfc` returns `True` for every flex/grid
            # container regardless (a real, separate CSS fact -- they do
            # establish a BFC), which let every one of them reach the
            # child-scanning code below anyway and get "corrected" by it.
            # That recompute (`max` of each child's own bottom margin edge,
            # ordinary block-flow style) isn't equivalent to Taffy's own
            # flexbox/grid sizing at all -- a flex row's auto-height is its
            # own cross-axis extent (`align-items`, `gap`, baseline
            # alignment all included), not simply the lowest child's
            # bottom edge -- so it produced a different, wrong height
            # instead of leaving Taffy's already-correct one alone. Found
            # on `examples/kanban.py`: `.toolbar { display: flex; }`
            # measured `4px` shorter than Taffy's own flex-row height,
            # shifting every later sibling (and everything painted inside
            # them) up by that much.
            continue
        if not getattr(element, "_chromonic_has_layout_children", False):
            # A genuine leaf -- no element children at all, only its own
            # text (or nothing) -- was never sized by Taffy summing/
            # including any *element* child's contribution in the first
            # place (this function's whole premise); its `height:auto` is
            # already a real, correctly-measured text/line-box result
            # (`_own_text`'s `new_text_leaf`), not something to recompute
            # from `childNodes` here. Without this, any BFC-establishing
            # element (`_establishes_bfc`, e.g. `display:table-cell`)
            # holding nothing but text got its real measured height
            # silently zeroed -- this function's own child-scanning loop
            # below only ever looks at *element* nodes, finding none, and
            # so always computed `content_bottom == content_top`. Found on
            # `wpt/css/CSS2/normal-flow/block-formatting-contexts-011.xht`:
            # a `display:table-cell` `<span>` with only NBSP text content
            # measured `0px` tall instead of Chrome's real `18px` line box.
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = _establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        has_float_child = False
        for child in element.childNodes or []:
            if not _is_element(child):
                continue
            child_resolved = getattr(child, "_chromonic_resolved_style", None)
            if child_resolved is None:
                continue
            child_computed, child_style_obj = child_resolved
            if _is_absolutely_positioned(child_style_obj):
                continue
            if _is_floated(child_computed):
                has_float_child = True
                continue
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            normal_bottom = max(normal_bottom, child_box.y + child_box.height + _numeric_edge(margin[2]))
        if not has_float_child and not establishes_bfc:
            continue  # nothing this pass would change -- leave Taffy's own result alone
        content_bottom = normal_bottom
        if establishes_bfc:
            content_bottom = max(content_bottom, _bfc_descendant_float_bottom(element, content_top))
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            _shift_later_siblings_for_height_delta(element, delta)


def _fix_float_flow_container_auto_height(node_map: dict) -> None:
    """CSS 2.1 10.6.3/10.6.7: an element's own `height:auto` is the max
    extent of its in-flow content's bottom margin edge -- a float
    contributes too, but only when the element itself establishes a new
    block formatting context (9.4.1). Taffy's own flex-wrap row-summing
    (`_approximate_inline_flow`'s stand-in for real float layout) instead
    *adds* each wrapped row's own height together, double-counting a float
    row and a later normal-flow row that actually both start from the same
    content top instead of taking their max.

    Runs after `_fix_float_flow_after_block_sibling` has placed every
    child at its real, float-aware position -- recomputes the container's
    own height directly from those final positions rather than trusting
    Taffy's row-summed one.

    Found on `wpt/css/CSS2/normal-flow/auto-margins-used-values-with-
    floats.tentative.html`: a `display:flow-root` `.container` (5px
    padding, one `float:right` 40px-tall child, three ordinary 10px
    children stacked below the container's own content top) measured
    `60px` (`40px` float row + a wrapped second flex row's `10px`,
    summed) instead of Chrome's `50px` (`max(40, 30)` float/normal
    extent, plus `10px` padding)."""
    for element in node_map.values():
        children = getattr(element, "_chromonic_float_flow_children", None)
        qualifies = getattr(element, "_chromonic_float_flow_qualifies", None)
        if not children or qualifies is None:
            continue
        if any(is_flow and not _is_floated(
                (getattr(child, "_chromonic_resolved_style", None) or (None,))[0])
               for child, is_flow in zip(children, qualifies)):
            continue  # a qualifying-but-not-floated (inline-tag) child -- leave Taffy's own result alone
        if getattr(element, "_chromonic_tag_name", None) == "body":
            # `_adjust_body_collapsed_margins` already owns body's own
            # auto-height (including the float exclusion this function
            # also applies) with an extra precision this generic version
            # doesn't replicate -- a trailing child's bottom margin that
            # collapses through and escapes past a non-BFC body is
            # excluded there, but unconditionally included here. Recomputing
            # it a second time, differently, risks quietly regressing an
            # already-correct result rather than improving it.
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        establishes_bfc = _establishes_bfc(resolved[0] if resolved is not None else None)
        pt, pr, pb, pl = element.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_top = box.y + box.border_top + pt
        normal_bottom = content_top
        float_bottom = content_top
        for child, is_flow in zip(children, qualifies):
            child_box = child.__dict__.get("_layout_box")
            if child_box is None:
                continue
            margin = (getattr(child, "_chromonic_native_style", None) or {}).get("margin") \
                or (0.0, 0.0, 0.0, 0.0)
            bottom = child_box.y + child_box.height + _numeric_edge(margin[2])
            if is_flow:
                float_bottom = max(float_bottom, bottom)
            else:
                normal_bottom = max(normal_bottom, bottom)
        content_bottom = max(normal_bottom, float_bottom) if establishes_bfc else normal_bottom
        new_content_height = max(0.0, content_bottom - content_top)
        new_client_height = new_content_height + pt + pb
        border_bottom = box.height - box.client_height - box.border_top
        new_height = new_client_height + box.border_top + border_bottom
        if abs(new_height - box.height) > 1e-6:
            delta = new_height - box.height
            element.__dict__["_layout_box"] = dataclasses.replace(
                box, height=new_height, client_height=new_client_height,
            )
            # Taffy already stacked every *later* sibling in ordinary block
            # flow using this element's own pre-fix (Taffy-native, flex-
            # wrap-row-summed) height -- correcting this element's own
            # height alone leaves them positioned against a now-stale
            # value. Found on `wpt/css/CSS2/normal-flow/auto-margins-used-
            # values-with-floats.tentative.html`: the fixture's second
            # `.container` (an `ltr`/`rtl` pair, both siblings of the
            # first) measured `10px` too low -- exactly the first
            # container's own height correction, never propagated.
            _shift_later_siblings_for_height_delta(element, delta)


def _apply_linebox_strut_height(node_map: dict) -> None:
    """CSS 2.1 10.8: a line box's height always includes its own "strut" --
    an invisible, zero-width inline box using the line's own font/line-
    height -- even when the line's only real content is a single atomic
    inline-level box (an empty `inline-block`/replaced element) with no
    text of its own. Taffy has no concept of a line box at all; a block
    whose *only* in-flow content is one or more such atomic children never
    gets one, so its `height:auto` box comes out exactly as tall as its
    tallest child and nothing more.

    Found on `wpt/css/CSS2/linebox/fractional-line-height.html`: a
    `float:left; overflow:auto` container around one explicitly-sized
    `inline-block` `<span>` measured only the span's own height, missing
    Chrome's several extra pixels of strut descent below it. This is a
    general line-box gap, not a float one -- an ordinary, non-floated
    container in the same shape (auto height, one atomic inline-level
    child, no text) needs exactly the same correction, so this runs
    unconditionally rather than being gated on `float`.

    Scope, deliberately conservative: only a `height:auto` block whose
    in-flow children are *all* atomic inline-level boxes (`inline-block`
    or a replaced/control element) at the default `vertical-align:
    baseline`, with no text of its own, is corrected -- multi-line
    wrapping, mixed text/element content (already measured correctly by
    `_InlineFormattingPlan`), and any other `vertical-align` are out of
    scope."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        if getattr(element, "_chromonic_inline_plan", None) is not None:
            continue  # real text already measured a correct line box
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        child_nodes = element.childNodes or []
        if any(getattr(node, "nodeType", None) == TEXT_NODE
               and _collapsed_text_node(node).strip() for node in child_nodes):
            continue  # real text present -- not this function's scope
        children = [node for node in child_nodes if _is_element(node)]
        if not children:
            continue
        atomic_children = []
        for child in children:
            computed = getattr(child, "_chromonic_computed_style", None)
            child_box = child.__dict__.get("_layout_box")
            if computed is None or child_box is None:
                atomic_children = None
                break
            display = (getattr(computed, "display", "") or "").strip().lower()
            tag_name = (getattr(child, "tagName", "") or "").lower()
            if display != "inline-block" and tag_name not in _REPLACED_OR_CONTROL_TAGS:
                atomic_children = None
                break
            vertical_align = (getattr(computed, "verticalAlign", "") or "baseline").strip().lower()
            if vertical_align not in ("baseline", ""):
                atomic_children = None
                break
            atomic_children.append((child, child_box))
        if not atomic_children:
            continue
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # See the identical comment at this same pattern's first
        # occurrence, in `_build_text_runs_from_nodes`: an explicit
        # `line-height: 0` must not be treated the same as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        half_leading = (line_height - (ascent + descent)) / 2.0
        strut_above = ascent + half_leading
        strut_below = descent + half_leading
        max_above = strut_above
        for child, child_box in atomic_children:
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            # `vertical-align: baseline` on an atomic box with no baseline
            # of its own aligns its bottom *margin* edge to the line's
            # baseline (CSS 2.1 10.8.1) -- everything above that edge
            # (its own height plus its top margin) is what extends above
            # the baseline on this line.
            child_above = child_box.height + _numeric_edge(margin[0]) + _numeric_edge(margin[2])
            max_above = max(max_above, child_above)
        needed_height = max_above + strut_below
        if needed_height <= box.height + 0.01:
            continue
        delta = needed_height - box.height
        # `LayoutBox` is a frozen dataclass -- `dataclasses.replace` keeps
        # every other already-resolved field (border/margin/content size)
        # intact, only growing the two height fields.
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, height=box.height + delta, client_height=box.client_height + delta,
        )


def _apply_empty_inline_block_min_height(node_map: dict) -> None:
    """A genuinely empty `display:inline-block` box -- no children, no text,
    nothing -- still measures `height:auto` as one line's worth of its own
    font/line-height in every major browser, not zero. Unlike a plain
    non-replaced `display:inline` (whose *shared* ancestor line can
    legitimately collapse to zero height when empty, CSS 2.1 9.4.2 --
    see `_empty_inline_strut_run`/`_InlineFormattingPlan.measure`'s
    zero-edge check, which must not be reused here), an inline-block
    always establishes its *own*, self-contained inline formatting
    context, and that context's line box exists -- with the box's own
    strut -- even with nothing in it.

    Chromonic never gives this box any inline-formatting-context
    machinery at all when it has no children (`build()`'s childless
    fallback just makes it a plain Taffy leaf, sized purely from CSS,
    which resolves to `0` for `height:auto` with no content to measure).
    Found on `wpt/css/CSS2/linebox/crashtests/inline-block-baseline-
    crash.html`: `<div style="display:inline-block"></div>`, with no
    content whatsoever, measured `0px` tall instead of Chrome's `18px`."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        native = getattr(element, "_chromonic_native_style", None)
        if native is None or native.get("height") != "auto":
            continue
        computed = getattr(element, "_chromonic_computed_style", None)
        display = (getattr(computed, "display", "") or "").strip().lower() if computed is not None else ""
        if display != "inline-block":
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        # Deliberately not gated on `_chromonic_has_layout_children` --
        # that flag is set whenever `build()` took *any* branch with a
        # non-empty `inline_items`/`children`, including a degenerate one
        # (an empty `::after { content: "" }` alone routes through the
        # flex-row-wrap fallback, `elif inline_items:`, which sets it
        # `True` despite there being no real visible content at all).
        # What actually matters is only whether the box, whatever path it
        # took, ended up shorter than one line -- checked below by simply
        # comparing against `needed_height`, which a box with real
        # multi-line content already exceeds.
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        _ascent, _descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # See the identical comment at this same pattern's first
        # occurrence, in `_build_text_runs_from_nodes`: an explicit
        # `line-height: 0` must not be treated the same as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        if line_height <= 0.0:
            continue
        border = native.get("border") or (0.0,) * 4
        padding = native.get("padding") or (0.0,) * 4
        vertical_edges = (_numeric_edge(border[0]) + _numeric_edge(border[2])
                          + _numeric_edge(padding[0]) + _numeric_edge(padding[2]))
        needed_height = line_height + vertical_edges
        if needed_height <= box.height + 0.01:
            continue
        delta = needed_height - box.height
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, height=box.height + delta, client_height=box.client_height + delta,
        )


def _merge_adjacent_same_line_rects(rects) -> list:
    """Combine consecutive rects (already in the order they were placed --
    left-to-right within one line) that share a `y` into one wider rect,
    the way multiple runs on the same line (e.g. text either side of a
    nested `<b>`) collapse into a single `getClientRects()` entry."""
    merged = []
    for rect in rects:
        if merged and abs(merged[-1][1] - rect[1]) < 0.01:
            previous = merged[-1]
            merged[-1] = (previous[0], previous[1],
                          rect[0] + rect[2] - previous[0],
                          max(previous[3], rect[3]))
        else:
            merged.append(rect)
    return merged


def _finalize_inline_owner_boxes(owner_accum) -> None:
    """Merge each inline owner's accumulated fragment rects -- raw,
    pre-merge, gathered across every `_InlineFormattingPlan` that published
    fragments for it (CSS 2.1 9.2.1.1: an element split around an in-flow
    block child, `_split_wrapping_inline_element`, contributes fragments
    from multiple, independently-published plans, one per split segment,
    that all still belong to the same original owner) -- into that owner's
    final `_chromonic_inline_boxes`/`_layout_box`/`_chromonic_owned_
    fragments`, exactly once per owner per layout pass regardless of how
    many plans it was split across.

    Rects are grouped by `run["split_group"]` (`None` for an ordinary,
    non-split owner -- always exactly one group) rather than merged as one
    undifferentiated, re-sorted list: `getClientRects()` preserves *document*
    order, not a geometric top-to-bottom sort, and re-sorting previously
    placed a later real segment before an *earlier* interruption's marker
    purely from a couple of stray pixels in this project's own line-height
    rounding -- wrong even though the numeric `y` values technically sorted
    that way. Each group is merged internally in its own natural (already
    left-to-right/top-to-bottom) order; groups themselves are emitted in
    split order (0, 1, 2, ...), interleaved with that group's following
    interruption-marker rect, if any.

    CSS 2.1 9.2.1: an inline element's line-box fragments cover its nested
    inline descendants' content too, not just text/atomic runs it owns
    directly -- a wrapping `<span>` around another `<span>` reports one
    `getClientRects()`/`getBoundingClientRect()` extent spanning both, the
    same as real Chrome. Each owner's own merged rects are therefore also
    folded into every tracked inline ancestor's rects, deepest owner first
    so a grandparent picks up an already-unioned parent."""
    own_merged = {}
    for key, (owner, groups, _fragments) in owner_accum.items():
        if not groups:
            continue
        group_keys = sorted(groups, key=lambda key: (key is not None, key))
        merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
        own_merged[key] = [rect for group in merged_groups for rect in group]

    def _depth(owner) -> int:
        depth = 0
        node = getattr(owner, "parentElement", None)
        while node is not None:
            depth += 1
            node = getattr(node, "parentElement", None)
        return depth

    # `descendant_only[key]`: every rect contributed by `key`'s inline
    # descendants (transitively), *excluding* `key`'s own -- kept separate
    # from `own_merged` because only the *horizontal* extent of this
    # folds upward (see below); a descendant's own vertical extent must
    # never do so.
    descendant_only = {key: [] for key in own_merged}
    for key in sorted(own_merged, key=lambda key: _depth(owner_accum[key][0]), reverse=True):
        parent = getattr(owner_accum[key][0], "parentElement", None)
        while parent is not None:
            parent_key = id(parent)
            if parent_key in descendant_only:
                descendant_only[parent_key].extend(own_merged[key])
                descendant_only[parent_key].extend(descendant_only[key])
                break
            parent = getattr(parent, "parentElement", None)

    for key, (owner, groups, fragments) in owner_accum.items():
        if key not in own_merged:
            continue
        # CSS 2.1 9.2.1: a wrapping inline's own fragment(s) span the full
        # *horizontal* extent of everything nested inside them on that
        # line -- a big (e.g. `font-size:500%`) nested child widens the
        # wrapper's own box exactly like real Chrome. Vertically, though,
        # the wrapper's own fragment height/position come only from its
        # *own* font metrics: a taller nested child may visually extend
        # above/below the wrapper's own box without growing it, and Chrome
        # still reports exactly *one* fragment for the wrapper here, not
        # one merged box plus separate descendant-sized ones. Found on
        # `wpt/css/CSS2/linebox/anonymous-inline-inherit-001.html`: a
        # `font-size:500%` nested `<span>` inflated the *outer* span's own
        # reported height from `18px` to `92px` (and split it into two
        # fragments) when descendant rects were unioned wholesale instead
        # of only widening the outer span's own.
        desc = descendant_only[key]
        if desc:
            desc_left = min(r[0] for r in desc)
            desc_right = max(r[0] + r[2] for r in desc)
            all_merged = [
                (min(rx, desc_left), ry, max(rx + rw, desc_right) - min(rx, desc_left), rh)
                for rx, ry, rw, rh in own_merged[key]
            ]
        else:
            all_merged = own_merged[key]
        # `getClientRects()`: real Chrome exposes one extra rect per in-flow
        # block interruption (CSS 2.1 9.2.1.1) -- the *anonymous block box*
        # the real interrupting block sits inside, not the block's own
        # (possibly narrower) box: an anonymous block box is `width:auto`,
        # 100% of `owner`'s own containing block (`_chromonic_split_
        # container`, stashed by `_split_wrapping_inline_element`), and its
        # height wraps the real block's full margin box. Confirmed against
        # real Chrome on `wpt/css/CSS2/linebox/inline-box-001.xht`/`-002.xht`:
        # a `<div id=x style="width:2in">` nested inside a split inline
        # whose own containing block is `784px` wide reports this marker
        # rect `784px` wide, not `192px` (the nested div's own width) --
        # and `inline-box-002.xht`, where the split inline's containing
        # block itself is only `192px` wide, reports the marker at that
        # narrower `192px` instead, matching its own container, not a fixed
        # value. This is on top of, not merged with, the real leading/
        # trailing fragment rects either side of it, and at its *logical*
        # split position (between the segment before it and the segment
        # after), not wherever a geometric sort would place it.
        interruption_blocks = getattr(owner, "_chromonic_interruption_blocks", None) or ()
        if interruption_blocks:
            container = getattr(owner, "_chromonic_split_container", None)
            container_box = container.__dict__.get("_layout_box") if container is not None else None
            cpt, cpr, cpb, cpl = (container.__dict__.get("_chromonic_padding", (0.0,) * 4)
                                  if container is not None else (0.0,) * 4)
            group_keys = sorted(groups, key=lambda key: (key is not None, key))
            merged_groups = [_merge_adjacent_same_line_rects(groups[key]) for key in group_keys]
            self_edges = getattr(owner, "_chromonic_split_self_edges", None)
            self_left, self_right, self_top = self_edges or (0.0, 0.0, 0.0)
            if self_edges and merged_groups:
                # `owner` is a real Taffy node here (the direct-child split
                # shape), so *every* one of its text-leaf children -- not
                # just the leading segment's -- already sits physically
                # shifted right by `owner`'s own real border-left/padding-
                # left/top (Taffy applies that to every child alike).
                # Every rect in every group needs that same shift undone
                # first -- otherwise a trailing segment (which only ever
                # gains *width* below, never its own position correction)
                # stays off by that same amount, on both axes: `top_edge_
                # val` is never zeroed out of `box_height` (a one-way
                # addition, so it can't double-count there), but the
                # *position* it feeds into (`owner_y` in `publish()`)
                # cancels back to the leaf's own already-shifted position
                # exactly the way the horizontal one does. Only *after*
                # undoing the horizontal shift uniformly does the real
                # edge apply once more, correctly, to only the true
                # leading/trailing rects: left-widening the very first
                # rect of the first (leading) group, right-widening the
                # very last rect of the last (trailing) one -- exactly
                # which fragments a real inline box's own edges ever show
                # up on. The vertical shift has no such edge-widening
                # counterpart -- every segment's own `box_height` already
                # carries the *full* top+bottom edge unconditionally, so
                # only the position needs correcting, everywhere.
                if self_top:
                    merged_groups = [
                        [(rx, ry - self_top, rw, rh) for rx, ry, rw, rh in group]
                        for group in merged_groups
                    ]
                if self_left:
                    merged_groups = [
                        [(rx - self_left, ry, rw, rh) for rx, ry, rw, rh in group]
                        for group in merged_groups
                    ]
                    first = merged_groups[0][0]
                    merged_groups[0][0] = (first[0], first[1], first[2] + self_left, first[3])
                if self_right:
                    last = merged_groups[-1][-1]
                    merged_groups[-1][-1] = (last[0], last[1], last[2] + self_right, last[3])
            final_rects: list = []
            atomic_segment_elements = getattr(owner, "_chromonic_atomic_segment_elements", None) or {}
            for index, group in enumerate(merged_groups):
                atomic_nodes = atomic_segment_elements.get(group_keys[index])
                if atomic_nodes:
                    # This segment's own "group" is a zero-sized marker run
                    # (`_split_wrapping_inline_element`'s own dummy, kept
                    # only so every segment gets a group slot to interleave
                    # block-interruption markers around) -- its real
                    # content is one or more atomic inline-level elements
                    # (`inline-block`/replaced) built as real recursive
                    # subtrees instead, whose own already-final boxes are
                    # used here directly rather than the dummy rect.
                    for node in atomic_nodes:
                        node_box = node.__dict__.get("_layout_box")
                        if node_box is not None:
                            final_rects.append((node_box.x, node_box.y, node_box.width, node_box.height))
                else:
                    final_rects.extend(group)
                if index < len(interruption_blocks):
                    block = interruption_blocks[index]
                    block_box = block.__dict__.get("_layout_box")
                    if block_box is not None and container_box is not None:
                        margin = (getattr(block, "_chromonic_native_style", None) or {}).get("margin") \
                            or (0.0, 0.0, 0.0, 0.0)
                        mt, _mr, mb, _ml = (_numeric_edge(v) for v in margin)
                        block_y = block_box.y
                        if container is owner:
                            # The direct-child ("element itself splits")
                            # shape: `container` is `owner` itself, a real
                            # Taffy node forced to `width:100%` of *its
                            # own* containing block (`build()`'s own
                            # `split_pieces` handling) -- so `owner`'s
                            # border-box already equals that containing
                            # block's content width, and the marker must
                            # use the whole border box, not `owner`'s own
                            # (narrower) content box: the anonymous block
                            # box carries neither of `owner`'s own edges
                            # (border/padding only ever attach to the
                            # true leading/trailing fragments either side
                            # of it), so it is not inset by them either.
                            # `block_box.y`, unlike the text rects above,
                            # was never run through a position formula
                            # that cancelled `owner`'s own border-top back
                            # out -- it is `block`'s own real Taffy
                            # position, still including that physical
                            # shift, so it needs the same correction
                            # applied here explicitly.
                            marker_x = container_box.x
                            marker_width = container_box.width
                            block_y = block_y - self_top
                        else:
                            marker_x = container_box.x + container_box.border_left + cpl
                            marker_width = container_box.client_width - cpl - cpr
                        final_rects.append((
                            marker_x,
                            block_y - mt,
                            marker_width,
                            block_box.height + mt + mb,
                        ))
                    elif block_box is not None:
                        final_rects.append((block_box.x, block_box.y, block_box.width, 0.0))
            owner.__dict__["_chromonic_inline_boxes"] = final_rects
            all_merged = final_rects  # the block interruption also grows getBoundingClientRect()
        else:
            owner.__dict__["_chromonic_inline_boxes"] = all_merged
        # CSSOM-View's own `getBoundingClientRect()` algorithm unions every
        # `getClientRects()` rect *except* ones whose width or height is
        # zero -- `all_merged`/`final_rects` themselves stay unfiltered
        # (that's the real `getClientRects()` output, published above/
        # below as `_chromonic_inline_boxes`, and a zero-sized rect is a
        # real fragment there, e.g. an empty split-inline edge or a
        # collapsed block interruption's own marker) -- only the *union*
        # bounds computed here drop them. Found on `wpt/css/CSS2/normal-
        # flow/block-in-inline-client-rects-001.html`: `t1`'s block
        # interruption is a genuinely empty (zero-height) `<div>`, whose
        # `500px`-wide marker rect was still widening `t1`'s own bounding
        # rect to `500px` instead of Chrome's `200px` (the wider of its
        # two real, non-zero `inline-block` fragments).
        bounding_rects = [r for r in all_merged if r[2] != 0.0 and r[3] != 0.0] or all_merged
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right-left, height=bottom-top,
            client_width=right-left, client_height=bottom-top,
        )
        owner._chromonic_has_layout_children = True
        # Text-range fragments (`_chromonic_owned_fragments`) stay scoped to
        # this owner's own direct text -- unlike the element rects above,
        # `getClientRects()`-equivalent per-text-node Range fragments are
        # never unioned across a nested element boundary.
        owner._chromonic_owned_fragments = fragments


def _publish_inline_formatting(node_map) -> None:
    """Project shared line fragments after parent boxes reach final positions."""
    seen = set()
    owner_accum: dict = {}
    element_fragments_accum: dict = {}
    for element in node_map.values():
        if id(element) in seen:
            continue
        seen.add(id(element))
        plan = getattr(element, "_chromonic_inline_plan", None)
        box = element.__dict__.get("_layout_box")
        if plan is not None and box is not None:
            plan.publish(box, element.__dict__.get("_chromonic_padding", (0.0,) * 4),
                         owner_accum, element_fragments_accum)
    _finalize_inline_owner_boxes(owner_accum)
    _fix_split_inline_relative_offset(owner for owner, _groups, _fragments in owner_accum.values())
    for element, fragments in element_fragments_accum.values():
        element._chromonic_inline_fragments = fragments


def _fix_split_inline_relative_offset(owners) -> None:
    """CSS 2.1 9.4.3: `position:relative`'s `top`/`left` offset shifts
    *every* box an element generates. For a `display:inline` element split
    around an in-flow block child (CSS 2.1 9.2.1.1, `_split_wrapping_
    inline_element`), that includes the real block child's own box too --
    it visually moves along with the inline ancestor's split fragments
    that wrap it, even though the block itself is never `position:
    relative`. `wrapper` (the split inline) is deliberately never built as
    a real Taffy node at all (see `_split_wrapping_inline_element`'s
    docstring), so Taffy's own native `position:relative` handling never
    sees it and never applies this offset to anything -- this reapplies it
    by hand, after `_finalize_inline_owner_boxes` has published `wrapper`'s
    own fragment geometry (`_layout_box`/`_chromonic_inline_boxes`/
    `_chromonic_owned_fragments`).

    Found on `wpt/css/CSS2/linebox/inline-box-002.xht`: `#div2 { position:
    relative; top:2in }` left every one of `div2`'s own three generated
    fragments -- including its nested `#div3` block child's own box --
    at their static (unshifted) position.

    Takes the owners `_finalize_inline_owner_boxes` just published
    (`owner_accum`'s keys) rather than walking `node_map` -- a *nested*
    split wrapper (this function's whole reason to exist) is, by design,
    never itself built as a real Taffy node (see `_split_wrapping_inline_
    element`'s docstring) and so never appears there at all; only `_split_
    inline_flow_around_blocks`'s *other* shape (`element` itself directly
    parenting the block, `_has_direct_in_flow_block_child`) happens to
    still be a real node, which made this silently a no-op for the far
    more common nested case until traced against `inline-box-002.xht`
    directly."""
    for owner in owners:
        interruption_blocks = getattr(owner, "_chromonic_interruption_blocks", None)
        if not interruption_blocks:
            continue
        container = getattr(owner, "_chromonic_split_container", None)
        native = getattr(owner, "_chromonic_native_style", None)
        container_box = container.__dict__.get("_layout_box") if container is not None else None
        if container_box is None or native is None:
            continue
        cpt, cpr, cpb, cpl = container.__dict__.get("_chromonic_padding", (0.0,) * 4)
        basis_width = container_box.client_width - cpl - cpr
        basis_height = container_box.client_height - cpt - cpb
        top, right, bottom, left = native.get("inset") or ("auto",) * 4
        top_v = _resolve_inset(top, basis_height)
        bottom_v = _resolve_inset(bottom, basis_height)
        left_v = _resolve_inset(left, basis_width)
        right_v = _resolve_inset(right, basis_width)
        dy = top_v if top_v is not None else (-bottom_v if bottom_v is not None else 0.0)
        dx = left_v if left_v is not None else (-right_v if right_v is not None else 0.0)
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        box = owner.__dict__.get("_layout_box")
        if box is not None:
            owner.__dict__["_layout_box"] = dataclasses.replace(box, x=box.x + dx, y=box.y + dy)
        inline_boxes = owner.__dict__.get("_chromonic_inline_boxes")
        if inline_boxes:
            owner.__dict__["_chromonic_inline_boxes"] = [
                (rx + dx, ry + dy, rw, rh) for rx, ry, rw, rh in inline_boxes
            ]
        for fragment in getattr(owner, "_chromonic_owned_fragments", None) or ():
            fbox = fragment.__dict__.get("_layout_box")
            if fbox is not None:
                fragment._layout_box = dataclasses.replace(fbox, x=fbox.x + dx, y=fbox.y + dy)
        for block in interruption_blocks:
            _shift_subtree(block, dx, dy)


def _fix_nested_split_flow_extent(node_map: dict) -> None:
    """A CSS 2.1 9.2.1.1 split wrapper that was reached as a *nested*
    wrapper (`_split_wrapping_inline_element`'s "else" shape -- `wrapper
    is not container`, its own real DOM parent stays an ordinary block
    container of the generated pieces, and `wrapper` itself never becomes
    a real Taffy node at all) still gets a `_layout_box` published for it
    (`_finalize_inline_owner_boxes`, for `getBoundingClientRect()`/paint
    purposes) -- the *visual* union of every generated fragment, border/
    padding decoration included. That union can be taller than the real
    vertical space those fragments occupy in ordinary block flow (an edge
    fragment's own border can visually overlap an adjoining one), so an
    ancestor's own auto-height must not read it directly.

    Computes a second, decoration-free box here instead -- the real block-
    flow extent: the interruption block(s)' own already-final top/bottom
    edges, extended by whichever edge fragment(s) actually contributed
    real flow height (`_chromonic_split_edge_flow_height`, stashed at
    build time from the same font-metrics math the fragment's own strut
    used, before Taffy ever resolved a position) -- ordinary sequential
    stacking, not a visual union, so it can't overlap. `_adjust_body_
    collapsed_margins` (and anything else computing an ancestor's auto-
    height from a DOM child's box) prefers this when present.

    Found on `wpt/css/CSS2/normal-flow/block-in-inline-empty-001.xht`:
    body's own height tracked the wrapper's `41px` visual union (its
    trailing edge fragment's own border overlapping the block above it)
    instead of the real `36px` block-flow advancement."""
    seen_wrappers: set = set()
    for node in node_map.values():
        if not _is_element(node):
            continue
        element = node.__dict__.get("_chromonic_split_wrapper_ref")
        if element is None or id(element) in seen_wrappers:
            continue
        seen_wrappers.add(id(element))
        container = getattr(element, "_chromonic_split_container", None)
        if container is None or container is element:
            continue  # the "wrapper is container" case already has a real, accurate Taffy box
        blocks = getattr(element, "_chromonic_interruption_blocks", None)
        if not blocks:
            continue
        first_box = blocks[0].__dict__.get("_layout_box")
        last_box = blocks[-1].__dict__.get("_layout_box")
        if first_box is None or last_box is None:
            continue
        edge_heights = getattr(element, "_chromonic_split_edge_flow_height", None) or {}
        flow_top = first_box.y - edge_heights.get("leading", 0.0)
        flow_bottom = last_box.y + last_box.height + edge_heights.get("trailing", 0.0)
        element._chromonic_flow_extent_box = LayoutBox(
            x=first_box.x, y=flow_top, width=first_box.width,
            height=max(0.0, flow_bottom - flow_top),
            client_width=first_box.width, client_height=max(0.0, flow_bottom - flow_top),
        )


def _adjust_body_collapsed_margins(root_element):
    """Publish Chrome-compatible body geometry for collapsed child margins.

    Taffy correctly positions block children with collapsed sibling margins,
    but a root node has no containing block into which its first/last margin
    struts can escape. HTML's body is special: Chrome's body rect excludes
    those escaped margins. Restrict this correction to the simple eligible
    body case; complex block formatting stays with Taffy.

    `_chromonic_scroll_extent` (set below, only once this correction
    actually applies) doubles as the signal `_apply_root_margin_offset`
    uses to know this pass already accounted for the root's own top
    margin via collapsing, so it doesn't also add that margin a second
    time -- cleared unconditionally up front so a stale value can never
    survive from an earlier layout pass where a different code path
    applied (e.g. this page's `height` used to be `auto` and no longer is).
    """
    root_element.__dict__.pop("_chromonic_scroll_extent", None)
    root_element.__dict__.pop("_chromonic_margin_collapsed", None)
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return
    style = getattr(root_element, "_chromonic_native_style", {})
    # `_approximate_inline_flow` may have turned `body` into a `flex-wrap`
    # row to stand in for real float layout (see its own docstring) --
    # `_chromonic_float_flow_children`, set only by that heuristic, marks
    # this as chromonic's own approximation rather than a real author
    # `display:flex`, and margin collapsing still applies to its children
    # exactly as it would to an ordinary block body (flex doesn't change
    # how a child's own margin is applied, only how its box is placed
    # among siblings) -- so this correction must not skip it. A genuine
    # author flexbox body (no such marker) is correctly left alone: flex
    # containers don't collapse margins with their children at all.
    # Found on `wpt/css/CSS2/linebox/fractional-line-height.html`: skipping
    # this once body became a float-approximation flex row left the
    # leading `<p>`'s own `16px` top margin (1em, from `ua_style.py`) and
    # body's own `8px` margin both in effect, stacking to `24px` instead
    # of the `16px` the two are supposed to collapse to (the larger of the
    # two, not their sum).
    if (style.get("display") != "block"
            and getattr(root_element, "_chromonic_float_flow_children", None) is None):
        return
    if any(value not in (0.0, "auto") for name in ("padding", "border")
           for value in style.get(name, ())):
        return
    boxes = []
    visible_boxes = []
    for child in root_element.childNodes or []:
        if not _is_element(child):
            continue
        child_style = getattr(child, "_chromonic_native_style", {})
        if child_style.get("position") in ("absolute", "fixed"):
            continue
        # CSS 2.1 10.6.3: an ordinary block's own auto height is the
        # distance to its *last in-flow child's* bottom margin edge --
        # floats are explicitly out of flow for this purpose, the same
        # category as the `position:absolute`/`fixed` children already
        # excluded just above (10.6.7's own float-inclusive auto-height
        # algorithm is for a block that *establishes a new block
        # formatting context*, which plain `<body>` -- what this whole
        # function is scoped to -- never does here). Taffy has no `float`
        # concept at all (see `_is_floated`'s own docstring) -- a floated
        # child that `_approximate_inline_flow` didn't turn into a flex
        # row (its own 80%-of-children threshold, unrelated to this
        # function) still reaches Taffy as an ordinary in-flow block
        # child, and without this it fully counted toward `bottom` like
        # any other real content. Found on `wpt/css/CSS2/margin-padding-
        # clear/padding-right-001.xht`: a single `float:left` `<div>`
        # `96px` tall after a `<p>` inflated body to `130px` instead of
        # Chrome's `18px` (the `<p>`'s own line, with the float
        # contributing nothing).
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and _is_floated(resolved[0]):
            continue
        box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        # A `child` that dissolved into a CSS 2.1 9.2.1.1 split (its own
        # children became direct Taffy siblings of `child` here, not real
        # descendants of it -- `_split_wrapping_inline_element`) reports
        # its own `_layout_box` as the *visual* union of every generated
        # fragment, decoration included -- which can be taller than the
        # real vertical space those fragments actually occupy in ordinary
        # block flow (an edge fragment's own border can visually overlap
        # an adjoining one). Body's own auto-height must track real flow
        # advancement, not that visual overlap -- substitute the
        # decoration-free flow extent computed below when one exists.
        flow_box = getattr(child, "_chromonic_flow_extent_box", None)
        if flow_box is not None:
            box = flow_box
        boxes.append(box)
        # A CSS-empty box (no border/padding/height and no *in-flow*
        # content of its own -- an absolutely-positioned-only wrapper
        # still counts as empty here, since an out-of-flow descendant
        # contributes nothing to its parent's own auto-height either)
        # doesn't stop a preceding margin from collapsing straight through
        # it -- its own Taffy `y` is only where that margin *would have*
        # landed had the box actually rendered something there, not real
        # content extent. Counting it toward `bottom` double-counts that
        # same margin as literal separation space inside body's box
        # instead of letting it escape past this empty child the way
        # Chrome does. Found on the CSS2.1 suite's `margin-*`/`padding-*`
        # tests (`wpt/css/CSS2/margin-padding-clear`): an absolutely-
        # positioned-only wrapper `<div>` after a `<p>` added the `<p>`'s
        # own collapsed-through bottom margin to `body`'s height a second
        # time.
        #
        # `box.height == 0` alone is *not* enough to conclude "no in-flow
        # content" the way it used to be, though: a wrapper can have real
        # in-flow children and still compute to zero height, when a
        # child's own negative margin pulls it (and the wrapper's own
        # auto-height, CSS 2.1 10.6.3's "last in-flow child's margin
        # edge") back up past zero -- found on `wpt/css/CSS2/margin-
        # padding-clear/margin-collapse-004.xht`: a wrapper around a
        # `height:20px` `#div1` and a `margin-top:-40px` `#div2` legitimately
        # computes to `0px` (matching Chrome exactly, its own bottom now at
        # `#div2`'s own, pulled-up edge), but still needed to anchor
        # `body`'s own height at *its* position, not be skipped as if it
        # held nothing at all.
        has_in_flow_content = any(
            _is_element(node) and getattr(node, "_chromonic_resolved_style", None) is not None
            and not _is_absolutely_positioned(node._chromonic_resolved_style[1])
            for node in (child.childNodes or [])
        )
        if box.height == 0 and not has_in_flow_content and not any(
            value not in (0.0, "auto") for name in ("padding", "border")
            for value in child_style.get(name, ())
        ):
            continue
        visible_boxes.append(box)
    if not boxes:
        return
    if visible_boxes:
        boxes = visible_boxes
    # CSS 2.1 10.6.3: auto height is anchored to the *first* and *last*
    # in-flow child's own margin edges specifically -- not the extent of
    # whichever child happens to reach furthest. `boxes` is already in DOM
    # order, so those are literally the first/last entries here. This only
    # differs from a plain min/max when a negative margin makes an earlier
    # sibling's box visually stick out past a later one (`box.y + box.height`
    # for that earlier child now exceeds the true last child's bottom edge) --
    # found on `wpt/css/CSS2/margin-padding-clear/padding-bottom-003.xht`
    # (`#div2 { margin-top: -3px }` pulling it back over `#div1`'s own
    # bottom edge): Chrome's body height tracks `#div2` (the real last
    # child) regardless, ignoring `#div1`'s now-irrelevant extra 1px.
    top = boxes[0].y
    bottom = boxes[-1].y + boxes[-1].height
    old = root_element.__dict__.get("_chromonic_pristine_box") or root_element.__dict__.get("_layout_box")
    if old is not None:
        # The escaped final margin still contributes to the document's scroll
        # extent even though it is outside body.getBoundingClientRect() --
        # and unlike the rendered height above, the scrollable area *does*
        # need the true max over every child, first/last or not.
        explicit_height = style.get("height") != "auto"
        # CSS 2.1 10.6.3's own auto-height calculation can go negative when
        # a child's negative margin pulls `bottom` back above `top` (found
        # on `wpt/css/CSS2/positioning/top-032.xht`: a `-6pc`/`-96px`
        # margin did exactly this) -- a used `height` is never negative
        # (CSS 2.1 8.1/10.5's own "auto" case is defined as the content
        # height, and a negative content height isn't a real height at
        # all; Chrome clamps to `0`), so this is clamped the same way an
        # explicit negative `height` value would already be handled
        # elsewhere, not left to leak through to the box actually painted.
        corrected_height = old.height if explicit_height else max(0.0, bottom - top)
        corrected_client_height = old.client_height if explicit_height else corrected_height
        root_element.__dict__["_chromonic_scroll_extent"] = max(
            old.y + old.height, bottom, *(box.y + box.height for box in boxes)
        )
        if top > 0:
            # top > 0 means a child margin collapsed through the root, and
            # _adjust_body_collapsed_margins already accounts for it by
            # anchoring body's y to boxes[0].y.  Signal this so
            # _apply_root_margin_offset doesn't add the margin a second time.
            root_element.__dict__["_chromonic_margin_collapsed"] = True
        root_element.__dict__["_layout_box"] = LayoutBox(
            x=old.x, y=top, width=old.width, height=corrected_height,
            client_width=old.client_width, client_height=corrected_client_height,
            border_top=old.border_top, border_left=old.border_left,
        )


def _is_root_anchored(element) -> bool:
    """Whether `element` is `position:absolute`/`fixed` with no positioned
    ancestor -- its containing block is the document root/viewport itself,
    not a real ancestor's box. Shared by `_fix_viewport_anchored_positioning`
    (which corrects such elements against the true viewport) and
    `_apply_root_margin_offset` (which must *not* shift them, since the
    viewport's origin is unaffected by the root element's own margin)."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is None or not _is_absolutely_positioned(resolved[1]):
        return False
    return _find_containing_block_ancestor(element) is None


def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning (see `_establishes_containing_block`),
    or `None` if none exists -- meaning `element`'s containing block is the
    document root itself."""
    parent = getattr(element, "parentElement", None)
    while parent is not None:
        resolved = getattr(parent, "_chromonic_resolved_style", None)
        if resolved is not None and _establishes_containing_block(resolved[1]):
            return parent
        parent = getattr(parent, "parentElement", None)
    return None


def _resolve_inset(value, basis: float) -> "float | None":
    if value == "auto":
        return None
    if isinstance(value, tuple):  # ("pct", fraction)
        return value[1] * basis
    return float(value)


def _resolve_viewport_anchored_box(style: dict, box, viewport_height: float):
    """`(new_y, new_height)` for an element whose containing block is the
    document root, resolved against the *true* `viewport_height` instead of
    whatever height the root's own Taffy box happened to compute to --
    either may be `None`, meaning "leave that one as Taffy already computed
    it" (see `_fix_viewport_anchored_positioning`'s docstring for why)."""
    top, _right, bottom, _left = style["inset"]
    top_v = _resolve_inset(top, viewport_height)
    bottom_v = _resolve_inset(bottom, viewport_height)
    margin_top, _mr, margin_bottom, _ml = style["margin"]
    mt = _resolve_inset(margin_top, viewport_height) or 0.0
    mb = _resolve_inset(margin_bottom, viewport_height) or 0.0
    height = style["height"]
    if isinstance(height, tuple) and height[0] == "pct":
        # A percentage height resolves against the containing block's own
        # height regardless of whether `bottom` is also set -- unlike the
        # top+bottom-both-set case below, this doesn't need the opposite
        # inset to become definite (`abspos-containing-block-004.xht`:
        # `top:0; height:100%`, no `bottom` at all -- Taffy had nothing to
        # resolve `100%` against but the root's own, too-small Taffy box).
        new_height = height[1] * viewport_height
        if top_v is not None:
            return top_v + mt, new_height
        if bottom_v is not None:
            return viewport_height - bottom_v - mb - new_height, new_height
        return None, new_height
    if bottom_v is None:
        # `top` alone (or neither) determines this element's position --
        # independent of the containing block's height either way, so
        # whatever Taffy already computed is already correct.
        return None, None
    if top_v is None:
        # bottom-anchored, top:auto -- box.height is already right (an
        # explicit or intrinsic height never depends on the containing
        # block's own height), only the box's *position* needs correcting.
        return viewport_height - bottom_v - mb - box.height, None
    # Both top and bottom are definite. If height is also definite, this is
    # over-constrained -- top (+height) alone already fully determine the
    # box, same as the top-only case above, so leave it alone. If height is
    # auto, the box stretches to fill the gap between top and bottom, which
    # *does* depend on the containing block's height.
    if height != "auto":
        return None, None
    return top_v + mt, viewport_height - top_v - mt - bottom_v - mb


def _resolve_viewport_anchored_box_x(style: dict, box, viewport_width: float):
    """`(new_x, new_width)` for a root-anchored element, the same horizontal
    counterpart to `_resolve_viewport_anchored_box`'s vertical correction --
    see that function's docstring for the shape of the logic, mirrored here
    across `left`/`right`/`margin-left`/`margin-right`/`width` instead of
    `top`/`bottom`/`margin-top`/`margin-bottom`/`height`. Needed because the
    root's own Taffy box, unlike its height, is *not* always the true
    viewport width either: `_apply_root_margin_offset` documents that the
    root correctly shrinks for its own margin, and a body with horizontal
    margin (`abspos-containing-block-001.xht`: `body { margin: 100px }`, a
    `position:fixed` box with `left:0; right:0`) shrinks the root's width by
    that margin the same way it shrinks height -- previously uncorrected."""
    _top, right, _bottom, left = style["inset"]
    left_v = _resolve_inset(left, viewport_width)
    right_v = _resolve_inset(right, viewport_width)
    _mt, margin_right, _mb, margin_left = style["margin"]
    ml = _resolve_inset(margin_left, viewport_width) or 0.0
    mr = _resolve_inset(margin_right, viewport_width) or 0.0
    width = style["width"]
    if isinstance(width, tuple) and width[0] == "pct":
        # Same percentage-independent-of-the-opposite-inset resolution as
        # `_resolve_viewport_anchored_box`'s height branch -- also catches
        # the synthetic `("pct", 1.0)` `tree.build()` assigns a block box
        # with inline content and `width:auto` (line ~1253) to stretch it
        # to its containing block, which for a root-anchored box must be
        # the true viewport width, not the root's own (possibly
        # margin-shrunk) Taffy box.
        new_width = width[1] * viewport_width
        if left_v is not None:
            return left_v + ml, new_width
        if right_v is not None:
            return viewport_width - right_v - mr - new_width, new_width
        return None, new_width
    if right_v is None:
        # `left` alone (or neither) determines this element's position.
        # Unlike the vertical counterpart's equivalent branch, this can
        # *not* just be left as Taffy already computed it: `left_v` may be
        # a percentage, which Taffy resolved against its own (possibly
        # margin-shrunk, body-relative) root box width instead of the true
        # containing-block/viewport width. Only the position needs
        # correcting -- `width` is shrink-to-fit here (`right`/`width` both
        # `auto`), already independent of the containing block's width.
        # Found on `wpt/css/CSS2/positioning/abspos-023.xht`: `.container
        # { position: absolute; left: 50% }` (`width`/`right` both `auto`,
        # no positioned ancestor) measured `x:392` (`50%` of the `784px`
        # body content box) instead of Chrome's `x:400` (`50%` of the true
        # `800px` viewport).
        return (None if left_v is None else left_v + ml), None
    if left_v is None:
        return viewport_width - right_v - mr - box.width, None
    if width != "auto":
        return None, None
    return left_v + ml, viewport_width - left_v - ml - right_v - mr


def _fix_absolute_horizontal_auto_margins(node_map: dict) -> None:
    """CSS 2.1 10.3.7: for a `position:absolute` non-replaced box whose
    `left`, `width`, and `right` are all definite (not `auto`), any
    `margin-left`/`margin-right` left as `auto` absorbs the remaining slack
    of the constraint equation `left + margin-left + width + margin-right +
    right == containing block width` -- split evenly between both margins
    when both are auto; the one definite margin is honoured and the other
    auto margin absorbs everything left over when only one is auto.

    Taffy's own absolute-positioning implementation does not solve this --
    found on `wpt/css/CSS2/positioning/absolute-non-replaced-width-003.xht`
    (`left:100px; width:100px; right:-200px; margin-left:auto; margin-
    right:auto` on a `400px` containing block: Chrome resolves both margins
    to `200px`; Taffy leaves the box at `left`'s own position, as if both
    margins were `0`) -- so this corrects the box's `x` (and everything
    painted inside it, via `_shift_subtree`) after the fact, the same way
    `_fix_viewport_anchored_positioning` already corrects a different gap
    in Taffy's own absolute-positioning coverage. Only the case with a real
    containing-block *ancestor* (`_find_containing_block_ancestor` finds
    one) is handled here -- a root-anchored box's containing width is the
    viewport, already corrected by `_fix_viewport_anchored_positioning`,
    called separately and only when a real `viewport_height` is known.

    A second, distinct case is handled too: exactly one of `left`/`right`
    is `auto` (`width` still definite) -- CSS 2.1 10.3.7's case 3/5 ("'left'
    is 'auto' ..."/"'right' is 'auto' ...'"). Here any `auto` margin
    resolves to plain `0`, same as a definite margin would, and the missing
    inset is solved from the constraint equation instead -- auto margins
    only ever get to *center* the box in the fully-constrained case above.
    Found on `wpt/css/CSS2/positioning/abspos-009.xht`: `width:10em;
    right:0; margin:auto` (`left` left as its default `auto`) -- Taffy
    centered the box within the whole containing block the way it would an
    in-flow block with `margin:auto`, giving `x=320`; Chrome solves `left`
    from `right`, giving `x=632`."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        margin = style.get("margin")
        inset = style.get("inset")
        if not margin or not inset:
            continue
        margin_left, margin_right = margin[3], margin[1]
        if margin_left != "auto" and margin_right != "auto":
            continue  # nothing left for this fix-up to solve
        left, right = inset[3], inset[1]
        left_auto, right_auto = left == "auto", right == "auto"
        if style.get("width") == "auto" or (left_auto and right_auto):
            continue  # under-constrained differently -- not this equation
        containing = _find_containing_block_ancestor(element)
        if containing is None:
            continue  # root-anchored -- handled by the viewport-anchored fix instead
        cb_box = containing.__dict__.get("_layout_box")
        if cb_box is None:
            continue
        cb_width = cb_box.client_width
        cb_content_x = cb_box.x + cb_box.border_left
        if not left_auto and not right_auto:
            left_v = _resolve_inset(left, cb_width) or 0.0
            right_v = _resolve_inset(right, cb_width) or 0.0
            remaining = cb_width - left_v - box.width - right_v
            ml = None if margin_left == "auto" else (_resolve_inset(margin_left, cb_width) or 0.0)
            mr = None if margin_right == "auto" else (_resolve_inset(margin_right, cb_width) or 0.0)
            if ml is None and mr is None:
                ml = mr = remaining / 2.0
            elif ml is None:
                ml = remaining - mr
            else:
                mr = remaining - ml
            new_x = cb_content_x + left_v + ml
        elif left_auto:
            right_v = _resolve_inset(right, cb_width) or 0.0
            new_x = cb_content_x + cb_width - right_v - box.width
        else:
            left_v = _resolve_inset(left, cb_width) or 0.0
            new_x = cb_content_x + left_v
        dx = new_x - box.x
        if abs(dx) > 1e-6:
            _shift_subtree(element, dx, 0.0)


def _publish_used_horizontal_margins(node_map: dict) -> None:
    """CSS 2.1 10.3.3: an in-flow block box's own `margin-left`/`margin-
    right`, when declared `auto`, resolves during layout to whatever pixel
    value centers/aligns the box -- Taffy already computes that resolution
    internally (box positions coming out of `tree.compute()` reflect it
    correctly), but never hands the resolved value back to Python at all
    (`_write_boxes`'s box tuple carries no margin fields, so `LayoutBox`
    default its `margin_left`/`margin_right` to `0`). domonic's own
    `getComputedStyle()` already knows how to *report* a used `auto` margin
    -- `ComputedStyleDeclaration._to_used_length` reads `element._layout_
    box.margin_left`/`.margin_right` whenever the declared value is the
    literal string `auto` (`_AUTO_BOX_FIELDS`) -- it just needs those two
    fields to actually hold something. This derives them from the box's
    own (already-correct) position relative to its parent's content-box
    edges: for an ordinary block-level box alone on its own line (normal
    block flow, not a flex/grid item sharing a row with siblings, where the
    gap on either side isn't attributable to *this* box's own margin
    alone), the horizontal gap on each side *is* exactly the used margin,
    `auto` or not -- so this also naturally reports the right value for an
    explicit (non-`auto`) margin, though domonic never actually consults it
    for that case.

    Deliberately narrow, matching CSS 2.1 10.3.3's own scope: only ordinary
    in-flow, block-level boxes, excluded when floated or out-of-flow
    positioned (their own boxes' horizontal placement isn't governed by
    this equation at all) or when the parent is a flex/grid container
    (there each item shares its row with siblings; the gap on one child's
    side isn't necessarily its own margin). Pure reporting -- never moves a
    box (no `_shift_subtree` call anywhere here)."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        resolved = getattr(element, "_chromonic_resolved_style", None)
        if resolved is None:
            continue
        computed, style_obj = resolved
        if _is_floated(computed) or _is_absolutely_positioned(style_obj):
            continue
        if _is_inline_level(element, style_obj):
            continue
        parent = getattr(element, "parentNode", None)
        if parent is None or not _is_element(parent):
            continue
        parent_box = parent.__dict__.get("_layout_box")
        parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
        if parent_box is None or parent_resolved is None:
            continue
        parent_display = parent_resolved[1].display
        parent_display = getattr(parent_display, "value", parent_display)
        if parent_display in ("flex", "inline-flex", "grid", "inline-grid"):
            continue
        parent_padding = parent.__dict__.get("_chromonic_padding", (0.0, 0.0, 0.0, 0.0))
        content_left = parent_box.x + parent_box.border_left + parent_padding[3]
        content_right = parent_box.x + parent_box.border_left + parent_box.client_width - parent_padding[1]
        margin_left = box.x - content_left
        margin_right = content_right - (box.x + box.width)
        element.__dict__["_layout_box"] = dataclasses.replace(
            box, margin_left=margin_left, margin_right=margin_right,
        )


def _fix_absolute_static_position_fallback(node_map: dict) -> None:
    """CSS 2.1 10.3.7/10.6.4: an absolutely-positioned box whose `top`/
    `right`/`bottom`/`left` are *all* `auto` falls back to its *static
    position* -- where it would have landed had `position` stayed
    `static`. Taffy has no concept of this at all (an all-auto inset
    simply resolves to `0` on both axes, landing the box at its
    containing block's origin) -- found on `wpt/css/CSS2/positioning/
    position-005.xht`: a `position:absolute` `#wrapper` with no insets set
    at all, body's only child, measured `x:0` instead of Chrome's `x:8`
    (body's own default UA margin -- exactly where an ordinary, in-flow
    `#wrapper` would have started).

    Only a reasonably common approximation is implemented, not full normal-
    flow layout: the static position is the literal DOM parent's own
    content-box origin when there is no earlier in-flow sibling, or
    (approximating ordinary block stacking, not inline flow) directly
    below the last earlier in-flow sibling's own margin box otherwise. Real
    static-position resolution needs a full shadow layout pass computing
    where the box would land as if it were never taken out of flow at all
    -- a substantially bigger feature, not attempted here."""
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None or style.get("position") != "absolute":
            continue
        inset = style.get("inset")
        if not inset or any(value != "auto" for value in inset):
            continue  # only the "every inset auto" case falls back at all
        # CSS 2.1 9.2.1.1/10.3.7: mixed into inline content (`_build_text_
        # runs_from_nodes`'s "escapee" runs, e.g. `wpt/css/CSS2/
        # positioning/abspos-007.xht`'s `<div class="test">` sitting
        # between plain text and a following in-flow block, all inside a
        # `display:inline` wrapper), `element`'s real static position is
        # wherever the surrounding text's own layout placed it -- not
        # simply "its literal DOM parent's content-box origin" (the
        # fallback below), which is also usually unusable here anyway: the
        # literal parent is commonly an inline wrapper never built as a
        # Taffy node at all (no `_layout_box`), unlike an ordinary block
        # parent this function already handles. `_InlineFormattingPlan.
        # measure()`/`.publish()` compute this directly (the only place
        # that actually knows the inline formatting context's own cursor
        # position) and stash it here.
        inline_static_position = getattr(element, "_chromonic_static_position", None)
        if inline_static_position is not None:
            static_x, static_y = inline_static_position
        else:
            parent = getattr(element, "parentElement", None)
            parent_box = parent.__dict__.get("_layout_box") if parent is not None else None
            if parent_box is None:
                continue
            # The static position is where `element` would sit as an ordinary
            # `position:static` box -- which, like any block box, is pushed
            # down by its own `margin-top` (collapsing rules with whatever
            # precedes it aside -- not attempted here, see the loop below for
            # the one collapsing case this function *does* approximate).
            # Omitting this was the bug on `wpt/css/CSS2/positioning/
            # top-019.xht`: `#div2` (first child of its containing block,
            # `margin-top:72pt`, every inset `auto`) measured a full `96px`
            # (72pt) above Chrome -- exactly its own unapplied margin-top.
            own_margin_top = _numeric_edge((style.get("margin") or (0.0,) * 4)[0])
            # The static position is the parent's *content*-box origin, not
            # its border box -- padding was missing here entirely (only
            # `border_left`/`border_top` were added), so a padded
            # containing block (most commonly `<body>` itself, since it's
            # this function's most common "no preceding sibling" case) put
            # every all-auto-inset absolutely-positioned child flush against
            # its border edge instead of past its padding. Found on
            # `wpt/css/CSS2/positioning/abspos-001.xht`: `body { padding:
            # 16px }`, its only child `position:absolute` with every inset
            # `auto` -- measured `x:0` instead of Chrome's `x:16`.
            parent_pad_top, _parent_pad_right, _parent_pad_bottom, parent_pad_left = (
                parent.__dict__.get("_chromonic_padding", (0.0,) * 4))
            static_x = parent_box.x + parent_box.border_left + parent_pad_left
            static_y = parent_box.y + parent_box.border_top + parent_pad_top + own_margin_top
            for sibling in parent.childNodes or ():
                if sibling is element:
                    break
                if not _is_element(sibling):
                    continue
                sibling_style = getattr(sibling, "_chromonic_native_style", None)
                sibling_box = sibling.__dict__.get("_layout_box")
                if sibling_style is None or sibling_box is None:
                    continue
                if sibling_style.get("position") == "absolute":
                    continue  # out of flow -- doesn't move the static-position cursor
                # The sibling's own trailing margin still separates it from
                # whatever follows in normal flow -- `sibling_box` (like any
                # `_layout_box`) never includes margin, only border+padding+
                # content, so it has to be added back explicitly here or the
                # static position lands flush against the sibling's border
                # instead of past its margin too (found on `position-005.xht`:
                # a `<p>` ahead of the absolutely-positioned element, real
                # UA-stylesheet `margin-bottom`, measured `16px` short).
                # Approximated as ordinary sibling margin collapsing would
                # give -- the larger of the sibling's own margin-bottom and
                # this element's margin-top -- rather than just adding the
                # sibling's alone, which would double-count when this
                # element's own margin-top is the larger of the two.
                #
                # A CSS-empty sibling (zero height, no border/padding of its
                # own) is the one exception: Taffy already resolves *its*
                # margin collapsing internally, including recursively with
                # its own descendants -- `sibling_box.y` is already the
                # fully-collapsed resting position of that whole nested
                # chain, not merely "this sibling's own top margin". Adding
                # its raw declared `margin-bottom` on top double-counts a
                # margin Taffy already folded in. Found on `wpt/css/CSS2/
                # positioning/abspos-022.xht`: `<div class="c1"><div
                # class="c2"><div class="c3"/></div></div>` (`margin:2px` /
                # `-4px 20px` / `0 0 14px`) collapses to Taffy's correct
                # `y:10` (`max(14, 0) + min(-4) = 10`), but re-adding `c1`'s
                # own raw `margin-bottom` (`2px`) on top measured `y:12`.
                sibling_empty = sibling_box.height == 0 and not any(
                    value not in (0.0, "auto") for name in ("padding", "border")
                    for value in sibling_style.get(name, ())
                )
                if sibling_empty:
                    static_y = sibling_box.y + sibling_box.height + own_margin_top
                else:
                    sibling_margin_bottom = _numeric_edge((sibling_style.get("margin") or (0.0,) * 4)[2])
                    static_y = sibling_box.y + sibling_box.height + max(sibling_margin_bottom, own_margin_top)
        dx = static_x - box.x
        dy = static_y - box.y
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_subtree(element, dx, dy)


def _shift_box(node, dx: float, dy: float) -> None:
    box = node.__dict__.get("_layout_box")
    if box is not None:
        node.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=box.width, height=box.height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )


def _shift_subtree(element, dx: float, dy: float) -> None:
    """Shift `element` and everything painted inside it (nested elements,
    retained inline text fragments) by `(dx, dy)` -- used to carry a
    corrected element's own position through to its descendants, whose
    boxes Taffy computed as offsets from `element`'s own (now-corrected)
    origin. A uniform shift preserves every internal relationship Taffy
    already got right; only the subtree's absolute origin moves.

    Skips `element` (and, since there's nothing to descend into, its own
    subtree) entirely when it's currently `display:none` -- such an
    element generates no box at all in real CSS, was never given a real
    Taffy node this pass, and so was never repositioned by whatever
    correction is calling this function in the first place. Its own
    `_layout_box` is simply whatever was last published for it, potentially
    several relayouts ago; shifting it anyway compounds this same
    correction on top of the last one, forever, since -- unlike every
    sibling Taffy *did* just lay out fresh -- nothing ever resets it back
    to a pristine baseline first. Found via a live `chromonic.App` run
    (`examples/kanban.py`): repeatedly clicking a `.filter` button that
    leaves a `.card` at `display:none` (already `none`, reset to the same
    `none` every click) walked its stale box further up by a few pixels on
    every single click, unbounded."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is not None and not _renders(resolved[1]):
        return
    _shift_box(element, dx, dy)
    for fragment in getattr(element, "_chromonic_inline_fragments", None) or ():
        _shift_box(fragment, dx, dy)
    for child in getattr(element, "childNodes", None) or ():
        if _is_element(child):
            _shift_subtree(child, dx, dy)


def _fix_viewport_anchored_positioning(node_map: dict, viewport_height: float,
                                        viewport_width: "float | None" = None) -> None:
    """Correct the vertical position (and, when stretched, height) Taffy
    computed for any `position:absolute`/`fixed` element whose containing
    block is the document root itself -- i.e. no ancestor establishes one
    (`_find_containing_block_ancestor` finds none).

    Taffy resolves such an element's `top`/`bottom`/`height` insets against
    the root element's *own* Taffy box -- which, for a scrollable page laid
    out with `height=None` (size-to-content, see `layout()`), is the full
    *document* height, not the viewport. That's backwards for exactly this
    fallback case: CSS's real rule is that an absolutely/fixed-positioned
    element with no positioned ancestor resolves against the *initial
    containing block*, which has the viewport's dimensions, not the
    document's -- confirmed directly against real Chrome (`bottom:0` on a
    plain `position:absolute` with no positioned ancestor, on a page taller
    than the viewport, lands at the *viewport's* bottom edge, not the
    document's -- the same place a `position:fixed` element with the same
    `bottom:0` lands). Found running `chromonic`'s Chrome-comparison layout
    harness (`chromonic/tests/layout/`, `position_stack.html`): a
    `position:fixed` element anchored with `bottom:18px` on a `900px`-tall
    body measured `300px` too low, exactly the gap between the body's own
    height and the real (fixed, `600px`) viewport.

    Only called when a caller supplies a real `viewport_height` distinct
    from "whatever height the root computed to" (see `layout()`'s
    docstring) -- every existing caller that doesn't pass one keeps today's
    behaviour exactly, at zero extra cost. `viewport_width`, when given,
    applies the same correction horizontally -- the root's own width is
    usually already the true viewport width (chromonic has no
    horizontal-scroll model), but not when the root itself carries
    horizontal margin (`_apply_root_margin_offset` shrinks the root's own
    box for that margin the same way it does for height); every existing
    caller passes it, so there is no untouched-behaviour case to preserve
    here the way there is for `viewport_height`."""
    seen = set()
    for element in list(node_map.values()):
        if id(element) in seen or not _is_element(element):
            continue
        seen.add(id(element))
        if not _is_root_anchored(element):
            continue
        style = getattr(element, "_chromonic_native_style", None)
        box = element.__dict__.get("_layout_box")
        if style is None or box is None:
            continue
        new_y, new_height = _resolve_viewport_anchored_box(style, box, viewport_height)
        new_x, new_width = ((None, None) if viewport_width is None
                             else _resolve_viewport_anchored_box_x(style, box, viewport_width))
        if new_y is None and new_height is None and new_x is None and new_width is None:
            continue
        dy = (new_y - box.y) if new_y is not None else 0.0
        dx = (new_x - box.x) if new_x is not None else 0.0
        height = new_height if new_height is not None else box.height
        width = new_width if new_width is not None else box.width
        element.__dict__["_layout_box"] = LayoutBox(
            x=box.x + dx, y=box.y + dy, width=width, height=height,
            client_width=box.client_width, client_height=box.client_height,
            border_top=box.border_top, border_left=box.border_left,
        )
        if dx or dy:
            for fragment in getattr(element, "_chromonic_inline_fragments", None) or ():
                _shift_box(fragment, dx, dy)
            for child in getattr(element, "childNodes", None) or ():
                if _is_element(child):
                    _shift_subtree(child, dx, dy)


def _apply_root_margin_offset(root_element, node_map: dict) -> None:
    """Shift the whole laid-out tree by the compute root's own margin.

    Taffy's root-compute entry point has no parent context for the root
    node, so a root with `width`/`height:auto` correctly *shrinks* to leave
    room for its own margin (ordinary block sizing: available space minus
    margin) but never actually *offsets* its own box by that margin the
    way a normal child would within a real parent's content box -- the
    root always comes back positioned at `(0, 0)` regardless of its own
    margin.

    `chromonic` hands Taffy `<body>` as this root, but CSS-wise `<body>`
    isn't really the document's root -- `<html>` is, and `<body>`'s own
    margin (`8px` by every real UA stylesheet, including this one's --
    `ua_style.py`) genuinely offsets it within `<html>`'s content box.
    Found running the layout harness (`ua_defaults.html`, a page with no
    author CSS at all, so nothing but this default margin in play): *every*
    element measured `8px` left of where Chrome puts it -- the whole
    document was shifted, not any one element, since everything's absolute
    position is ultimately `<body>`'s own position plus offsets from it.

    Root-anchored `position:absolute`/`fixed` elements (`_is_root_anchored`)
    are deliberately excluded: their containing block is the *viewport*,
    anchored at the true canvas origin, which `<body>`'s own margin has no
    effect on in real CSS either -- `position:fixed; top:0` sits at the
    literal viewport top regardless of `<body>`'s margin.

    The vertical axis is skipped when `_adjust_body_collapsed_margins`
    already moved the root's own top edge by folding its top margin into a
    collapsed-margin-derived position (see that function's own docstring)
    -- otherwise this would add the same top margin a second time."""
    style = getattr(root_element, "_chromonic_native_style", None)
    box = root_element.__dict__.get("_layout_box")
    if style is None or box is None:
        return
    margin_top, _margin_right, _margin_bottom, margin_left = style["margin"]
    # CSS: a percentage margin resolves against the containing block's
    # *width* on every side, vertical included -- not a typo.
    dx = _resolve_inset(margin_left, box.width) or 0.0
    already_collapsed = "_chromonic_margin_collapsed" in root_element.__dict__
    dy = 0.0 if already_collapsed else (_resolve_inset(margin_top, box.width) or 0.0)
    # `<html>`'s own padding/border offsets `<body>` within it exactly the
    # way `<body>`'s own margin does within `<html>`'s content box -- same
    # root-anchored exclusion applies (a root-anchored element's containing
    # block is the viewport itself, unaffected by either). See
    # `_document_element_box_edges`.
    html_edges = _document_element_box_edges(root_element)
    if html_edges is not None:
        html_left, _html_right, html_top, _html_bottom = html_edges
        dx += html_left
        dy += html_top
    if not dx and not dy:
        return
    root_anchored_ids: set = set()
    for element in node_map.values():
        if _is_element(element) and _is_root_anchored(element):
            root_anchored_ids.add(id(element))
    seen = set()
    for node in list(node_map.values()):
        if id(node) in seen:
            continue
        seen.add(id(node))
        owner = node if _is_element(node) else getattr(node, "parent", None)
        # A root-anchored element's *containing block* is the viewport, not
        # `<body>` -- unaffected by `<body>`'s own margin, hence this whole
        # function existing. Everything painted *inside* that element is
        # positioned relative to *it*, not independently against the
        # viewport, so it must be excluded too, not just the root-anchored
        # element's own box -- checking only `owner` itself (not the rest
        # of its ancestor chain) missed this: a normal (not itself root-
        # anchored) descendant of a root-anchored element, e.g. a
        # `position:absolute` child using that element as its own real
        # containing block, still got this shift applied on top of a
        # position already correct relative to its (excluded, unshifted)
        # container -- double-counting `<body>`'s margin. Found on `wpt/
        # css/CSS2/positioning/position-005.xht` once `#wrapper` (root-
        # anchored, all insets `auto`, positioned via the static-position
        # fallback above) was itself finally correct: `#div1` inside it
        # measured 8px (exactly `<body>`'s margin) further right than
        # Chrome.
        while owner is not None:
            if id(owner) in root_anchored_ids:
                break
            owner = getattr(owner, "parentElement", None)
        else:
            _shift_box(node, dx, dy)


def layout(root_element, *, width: float, height: "float | None" = None, reuse_styles: bool = False,
           viewport_height: "float | None" = None) -> dict:
    """Build a fresh Taffy tree from `root_element` down, compute layout at
    `width` x `height` (`height=None` sizes to content), and write every
    node's box back onto its domonic element. Returns `{node_id: element}`
    for anyone (painting, hit-testing) who wants to walk the same tree
    without re-discovering it.

    This is the *whole* invalidation story for the POC: call `layout()`
    again after any mutation (`element.style.width = ...`, adding/removing
    children, ...) and every affected box is recomputed and rewritten.

    `reuse_styles=False` (the default, and what every mutation above needs)
    re-resolves every element's CSS from scratch, exactly as always. Pass
    `reuse_styles=True` only when nothing about *any* element's class,
    inline style, or stylesheets could have changed since the last
    `layout()` call -- see `_describe`'s docstring for why that's worth
    doing (domonic's CSS cascade resolution is the dominant cost of a
    relayout on a real page) and for the full safety contract. Currently
    the only caller that qualifies is `native_browser.py`'s image-arrival
    poll: a newly-decoded `<img>`'s intrinsic size is applied independently
    of the CSS cache (see `_apply_image_intrinsic_size`), so skipping CSS
    re-resolution there is safe. A viewport resize or a real DOM event
    handler are not -- both must keep the default.

    `viewport_height`, when given, corrects `position:absolute`/`fixed`
    elements with no positioned ancestor to resolve against the *real*
    viewport height rather than `root_element`'s own (possibly
    content-grown, when `height=None`) box -- see
    `_fix_viewport_anchored_positioning`'s docstring. Leave it `None` for
    callers with no separate "viewport vs. document" distinction at all
    (a fixed-size, non-scrolling `Interaction`, or any `height` that's
    already the whole story) -- passing the real, distinct viewport height
    only matters once a page's document can grow taller than what's
    actually visible.
    """
    from . import webfonts
    if webfonts.prepare_layout(root_element):
        reuse_styles = False
    tree = Tree()
    node_map: dict[int, object] = {}
    with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
        root_id = build(tree, root_element, node_map, reuse_styles=reuse_styles)
    available_width = _constrain_root_to_document_element(tree, root_element, root_id, width)
    compute_height = _root_compute_height(root_element, height, viewport_height)
    boxes = tree.compute(root_id, available_width, compute_height)
    _write_boxes(boxes, node_map)
    return _finish_layout_pass(tree, node_map, root_element, width=width, viewport_height=viewport_height)


def _finish_layout_pass(tree_obj, node_map, root_element, *, width, viewport_height):
    """The post-`tree.compute()` correction pipeline, shared by every entry
    point that computes real Taffy geometry (this module's own `layout()`,
    plus `LayoutProjection.layout()`/`.compute()`) -- previously duplicated
    verbatim across all three. That duplication is exactly how three
    separate post-layout fixes (`_apply_linebox_strut_height`,
    `_apply_empty_inline_block_min_height`, `_fix_float_flow_after_block_
    sibling`) ended up wired into only the first of them and silently never
    ran at all for a real, incrementally-updated page -- `LayoutProjection`,
    not this module-level function, is what `native_browser.py` actually
    uses. Found via `wpt/css/CSS2/linebox/crashtests/inline-block-baseline-
    crash.html`: fixed by editing this function alone, verified `0`, still
    measured `0` through `LayoutProjection.layout()` -- the fix was real but
    two of the three entry points never called it."""
    # `_adjust_body_collapsed_margins` runs more than once in this same
    # pass (below, again after the float-flow height fixes) -- its own
    # `_chromonic_scroll_extent` computation needs Taffy's real, still-
    # uncorrected box (specifically `old.y + old.height`, which is where
    # a root-escaped trailing margin Taffy's own native collapsing already
    # folded in still shows up) as its baseline; on a *second* call that
    # box would otherwise already be the first call's own corrected
    # (smaller) one, silently losing that escaped margin from the
    # document's scroll extent. Stashed once, here, before either call.
    root_element.__dict__["_chromonic_pristine_box"] = root_element.__dict__.get("_layout_box")
    _fix_nested_split_flow_extent(node_map)
    _adjust_body_collapsed_margins(root_element)
    _apply_root_margin_offset(root_element, node_map)
    # Before the absolute-positioning fixups below: an inline-context
    # escapee's real static position (CSS 2.1 10.3.7/10.6.4, an absolutely-
    # positioned element mixed into inline content with `top`/`left:auto`)
    # is only known once `_InlineFormattingPlan.publish()` has actually run
    # -- `_fix_absolute_static_position_fallback` reads `element._chromonic_
    # static_position`, which this sets.
    _publish_inline_formatting(node_map)
    _apply_linebox_strut_height(node_map)
    _apply_empty_inline_block_min_height(node_map)
    _fix_float_shrink_to_fit_width(tree_obj, node_map)
    _fix_float_flow_after_block_sibling(node_map)
    _fix_float_flow_container_auto_height(node_map)
    _fix_nested_bfc_float_auto_height(node_map)
    # Re-anchor body's own auto-height now that a float-flow BFC child's
    # height (and every later sibling's position) may have just shifted --
    # `_adjust_body_collapsed_margins` ran once already, above, using
    # Taffy's original (stale, pre-fix) child boxes; it's idempotent
    # (always recomputes from whatever's currently in `node_map`), so
    # calling it again here re-derives body's height from the now-final
    # positions instead of leaving it anchored to the stale ones.
    _adjust_body_collapsed_margins(root_element)
    _fix_absolute_horizontal_auto_margins(node_map)
    _fix_absolute_static_position_fallback(node_map)
    if viewport_height is not None:
        _fix_viewport_anchored_positioning(node_map, viewport_height, width)
    _publish_used_horizontal_margins(node_map)
    return node_map
