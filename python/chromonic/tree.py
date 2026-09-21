"""Walk a live domonic DOM, build a mirroring Taffy tree, run layout, and
write geometry back onto the domonic elements via `element.set_layout_box(...)`.

No dirty-bit tracking: `layout()` is meant to be called again, in full,
after any mutation (see PLAN.md).

Style resolution, not Taffy itself, dominates relayout cost, so `_describe`
below builds exactly one `ComputedStyleDeclaration` per element per pass and
shares it across style-dict-building and `paint.py`'s paint-style extraction."""

from __future__ import annotations

import dataclasses
import functools
import re
import math

import skia

from domonic import _fontmetrics
from domonic import bs4 as domonic_bs4
from domonic.dom import Element
from domonic.layout import LayoutBox, LayoutStyle, Length, _parse_length_or_percent
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from . import fonts, style_bridge, ua_style
from ._native import Tree, layout_text

# `Utils.case_kebab` backs every `ComputedStyleDeclaration` property read and
# is a pure string transform -- memoized process-wide since it was the
# single largest cost after `ComputedStyleDeclaration` construction itself
# under profiling.
Utils.case_kebab = staticmethod(functools.lru_cache(maxsize=2048)(Utils.case_kebab))

# Selector matchers are pure but re-parse selector text on every candidate
# element; cache them so each distinct selector is parsed once.
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
    """A `::before`/`::after` generated box -- not a real DOM node, just
    enough surface for `build()`/`paint.py` to treat it like a childless
    element. Never appears in real `childNodes` -- reached only via
    `_inline_mixed_content`'s synthesized items and `_chromonic_inline_fragments`."""

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
        self.rtl = _element_direction(element, computed) == "rtl"
        # `text-align`/`text-align-last` inherit normally through domonic's
        # own cascade -- `element` here is the block establishing this
        # formatting context (the split wrapper, or the plan's own element
        # for a non-split plan), so its computed value already reflects
        # whatever an ancestor (e.g. the real containing block a split
        # wrapper's own anonymous-block pieces belong to) declared.
        self.text_align = (getattr(computed, "textAlign", "") or "start").strip().lower()
        self.text_align_last = (getattr(computed, "textAlignLast", "") or "auto").strip().lower()
        # CSS 2.1 16.1: `text-indent` inherits normally and, same as
        # `text-align`, applies to *this* plan's own first formatted line
        # -- each CSS 2.1 9.2.1.1 split segment is its own anonymous block
        # box, so its own first line gets indented independently, not just
        # the wrapper's overall first one.
        self.text_indent = _resolve_text_indent(computed)

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
        # CSS 2.1 16.1: `text-indent` only ever offsets the block's own
        # first formatted line -- every later line (after a wrap or a
        # `<br>`) resets `x` to `0.0` already, unaffected.
        x = self.text_indent
        # A `<br>`'s own reported position sits right after the preceding
        # text's advance, never including that run's own trailing border/
        # padding/margin-end -- confirmed directly against `left-rtl-
        # ref.xht`'s real geometry: a `direction:rtl` first fragment's own
        # *end*-side package (`padding-right`/`margin-right`) genuinely
        # inflates that fragment's own box width, but a `<br>` immediately
        # following the text is positioned before that decoration, not
        # after it (the decoration renders past the wrap point instead).
        # Tracked separately from `x` since `x` itself must still carry the
        # trailing/margin-end forward for whatever follows on the same line.
        x_pre_trailing = x
        y = 0.0
        line_margin_start = 0.0  # how much of the current line's `x` is `margin_start`, not content
        line_leading_total = 0.0  # and how much is a leading border/padding edge
        above, below = base_above, base_below
        self._line_baselines = {}
        self._line_belows = {}
        self._line_has_content = {}
        line_has_content = False
        # run index -> (x, y, line height, line's own margin_start, line's own leading edge)
        self._break_positions = {}
        # For a `direction:rtl` plan, a `<br>` mirrors to the *start* (post-
        # mirror box `x`, not the un-mirrored text-end point) of whichever
        # real run immediately preceded it -- tracked here so it can be
        # resolved once `placed` itself has been mirrored, below.
        break_precedes_run: dict = {}
        last_real_run = None
        # id(escapee element) -> (x, y): an out-of-flow `top/left:auto`
        # absolutely-positioned element mixed into inline content still has
        # a real CSS 2.1 10.3.7/10.6.4 "static position" wherever it falls
        # in the surrounding text; `publish()` turns this into a page position.
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
                self._line_belows[y] = below
                self._line_has_content[y] = line_has_content
                self._break_positions[run_index] = (x_pre_trailing, y, above + below, line_margin_start, line_leading_total)
                break_precedes_run[run_index] = last_real_run
                y += above + below
                x = 0.0
                x_pre_trailing = 0.0
                last_real_run = None
                line_margin_start = 0.0
                line_leading_total = 0.0
                above, below = base_above, base_below
                line_has_content = False
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
                    self._line_belows[y] = below
                    self._line_has_content[y] = line_has_content
                    y += above + below
                    x = 0.0
                    x_pre_trailing = 0.0
                    line_margin_start = 0.0
                    line_leading_total = 0.0
                    above, below = base_above, base_below
                    line_has_content = False
                token_height = run["box_height"]
                above = max(above, run["above"])
                below = max(below, run["below"])
                # A CSS 2.1 9.2.1.1 split segment's own decoration-only
                # marker (`_empty_decoration_only_run`) never counts as
                # real content by itself -- only an actual text/strut run
                # sharing its line does, which is what `publish()` uses to
                # decide whether the marker should inherit that line's real
                # geometry instead of staying an isolated 0x0.
                if not (run.get("empty_strut") and run["ascent"] == 0.0
                        and run["above"] == 0.0 and run["below"] == 0.0):
                    line_has_content = True
                placed.append((run, text, x + leading, y, token_width, token_height,
                               leading, trailing, advance_width))
                x_pre_trailing = x + leading + advance_width
                last_real_run = run
                x += total
                if index == len(tokens) - 1:
                    # A non-replaced inline's horizontal margins are real,
                    # non-collapsing spacing (CSS 2.1 10.3.1/10.3.3) --
                    # `margin_start` already shifted the cursor before this
                    # run's first token; this is margin-end, added once
                    # after the last, kept out of the run's own width.
                    x += run.get("margin_end", 0.0)
        self._line_baselines[y] = above
        self._line_belows[y] = below
        self._line_has_content[y] = line_has_content
        # CSS 2.1 9.4.2: a line box collapses to zero height when every run
        # on it is a zero-edge empty strut (no text, no border/padding/
        # margin) -- any real content alongside one keeps normal height.
        is_all_zero_edge_empty = placed and all(
            run.get("empty_strut") and run["leading"] == 0.0 and run["trailing"] == 0.0
            and run["box_height"] <= run["glyph_height"] + 1e-6 and run.get("margin_start", 0.0) == 0.0
            for run in self.runs if not run.get("break") and not run.get("escapee")
        )
        self.height = 0.0 if is_all_zero_edge_empty else (y + above + below if placed else 0.0)
        if is_all_zero_edge_empty:
            # Each such strut's own fragment reports zero height too, not
            # its font-metrics `box_height` -- the line it sits on doesn't exist.
            placed = [
                (run, text, x, y, token_width, 0.0, leading, trailing, advance_width)
                for run, text, x, y, token_width, _token_height, leading, trailing, advance_width in placed
            ]
        content_width = min(width, max(
            (px + advance for _r, _t, px, _y, _pw, _h, _l, _tr, advance in placed), default=0.0))
        # An explicit physical `text-align:left` overrides `direction:rtl`'s
        # own default right-mirroring below -- CSS 2.1 9.10's own initial
        # `start` value is what resolves to "right" for rtl, not `left`.
        rtl_mirror_suppressed = self.text_align == "left"
        if self.rtl and not rtl_mirror_suppressed and placed:
            # `direction:rtl` mirrors each line, as a rigid group, against
            # the same `width` it was placed within. Each fragment's own
            # `margin_end` (physical-right margin, already attached to
            # whichever fragment actually owns it -- see the direct-child
            # bidi-box-model edge-swap in `_make_inline_formatting_plan`)
            # shifts its mirrored box left by that amount, same as it would
            # shift a plain LTR box's cursor rightward before mirroring --
            # confirmed directly against `right-rtl-ref.xht`'s real
            # geometry: only subtracting on the whole line's last placed
            # entry (the pre-mixed-direction-support original here) is
            # wrong whenever that entry isn't the one actually carrying the
            # margin (e.g. a bidi-box-model split's first, not last,
            # fragment owns the trailing package for `direction:rtl`).
            # A CSS 2.1 9.2.1.1 block-in-inline split (`_split_wrapping_
            # inline_element`) never threads its own trailing margin through
            # a run's `margin_end` field at all (only `margin_start`, on its
            # first segment) -- so for that case, the wrapper's own overall
            # last placed fragment still needs its raw CSS margin-right
            # applied directly here, same as before mixed-direction support
            # existed. Skipped whenever `margin_end` is already nonzero
            # (this plan's own bidi-box-model build already threaded it
            # correctly there -- see `_make_inline_formatting_plan`), to
            # avoid double-counting it.
            is_final_segment = getattr(self, "_chromonic_final_split_fragment", True)
            last_index_for_owner: dict = {}
            for i, entry in enumerate(placed):
                last_index_for_owner[id(entry[0].get("owner"))] = i
            mirrored = []
            for index, (run, text, px, y, token_width, token_height, leading, trailing, advance) in enumerate(placed):
                margin_start = run.get("margin_start", 0.0)
                margin_end = run.get("margin_end", 0.0)
                if (not margin_end and not run.get("_bidi_margin_resolved") and is_final_segment
                        and index == last_index_for_owner.get(id(run.get("owner")))):
                    owner_style = getattr(run.get("owner"), "_chromonic_native_style", None) or {}
                    owner_margin = owner_style.get("margin") or (0.0, 0.0, 0.0, 0.0)
                    margin_end = _numeric_edge(owner_margin[1])
                outer_left = px - leading - margin_start
                outer_width = leading + advance + trailing
                new_px = (width - outer_left - outer_width - margin_end) + leading
                mirrored.append((run, text, new_px, y, token_width, token_height, leading, trailing, advance))
            placed = mirrored
            # A `<br>`'s own position mirrors to the *text* start (the
            # mirrored `px`, not the box's own outer `x`) of whichever real
            # run immediately preceded it -- confirmed against both `left-
            # rtl-ref.xht` (a zero-leading fragment, where box `x` and text
            # `x` coincide) and `right-ltr-ref.xht` (a nonzero-leading
            # nested `ltr` fragment inside a `rtl` base, where only the text
            # position -- not the box's outer edge including its own
            # leading decoration -- matches real Chrome).
            run_text_x = {id(run): new_px for run, _t, new_px, _y, _tw, _th, _l, _tr, _a in placed}
            for run_index, preceding_run in break_precedes_run.items():
                if preceding_run is not None and id(preceding_run) in run_text_x:
                    old = self._break_positions[run_index]
                    self._break_positions[run_index] = (run_text_x[id(preceding_run)],) + old[1:]
        elif not self.rtl and placed:
            # This plan's own base direction is `ltr` (no plan-wide mirror
            # applies). A *nested* `direction:rtl` element's own start/end
            # edge assignment was already resolved at build time (see
            # `_build_text_runs_from_nodes`'s nested-element branch, which
            # attaches the nested element's border/padding/margin package to
            # the physically-correct side per its own direction) -- each
            # fragment's build position is therefore already correct, and no
            # position-mirroring step is needed here at all for a nested
            # override; only ordinary physical `text-align` applies.
            placed = self._apply_text_align(placed, width)
        self._placed = placed
        return (content_width, self.height)

    def _apply_text_align(self, placed, width):
        """CSS Text 3 `text-align`/`text-align-last`: shift each line's
        placed tokens to reflect the block's own alignment, physical
        `left`/`right`/`center` only (no `direction`-aware `start`/`end`
        remapping -- not needed for LTR, and `direction:rtl` is handled
        separately above, via the mirror). `justify` distributes leftover
        space as extra spacing at each token boundary that ends in real
        whitespace, on every line but the last -- CSS's own line box is
        never justified unless `text-align-last:justify` says otherwise;
        each split segment is its own anonymous block box (CSS 2.1
        9.2.1.1), so its own last physical line gets this treatment
        independently of any other segment's."""
        self._justified_lines = set()
        if not placed:
            return placed
        text_align = "left" if self.text_align in ("start", "") else (
            "right" if self.text_align == "end" else self.text_align)
        text_align_last = self.text_align_last
        if text_align_last in ("auto", ""):
            # CSS Text 3: `auto` means "ordinary `text-align`", except a
            # `justify` block's own last line is never force-justified by
            # this default -- it aligns `start` (left) instead.
            text_align_last = "left" if text_align == "justify" else text_align
        text_align_last = "left" if text_align_last in ("start", "") else (
            "right" if text_align_last == "end" else text_align_last)
        if text_align == "left" and text_align_last == "left":
            return placed
        lines: list = []
        current: list = []
        current_y = None
        for entry in placed:
            if current_y is None or abs(entry[3] - current_y) > 0.01:
                if current:
                    lines.append(current)
                current = []
                current_y = entry[3]
            current.append(entry)
        if current:
            lines.append(current)
        result: list = []
        for line_index, line_entries in enumerate(lines):
            align = text_align_last if line_index == len(lines) - 1 else text_align
            if align == "left":
                result.extend(line_entries)
                continue
            line_start = min(e[2] - e[6] for e in line_entries)
            line_end = max(e[2] + e[8] + e[7] for e in line_entries)
            slack = width - (line_end - line_start)
            if align == "justify":
                gap_after = [i for i, e in enumerate(line_entries[:-1]) if e[1][-1:].isspace()]
                if not gap_after or slack <= 0:
                    result.extend(line_entries)
                    continue
                extra_per_gap = slack / len(gap_after)
                gap_set = set(gap_after)
                cumulative = 0.0
                # `publish()`'s own trailing-whitespace collapse (correct
                # for an ordinary, un-justified line) would otherwise trim
                # this same slack right back off the last token's reported
                # width -- indistinguishable there from a line's ordinary
                # trailing space once distributed. This line's own real,
                # filled extent needs recording so `publish()` can skip it.
                self._justified_lines.add(line_entries[0][3])
                for index, (run, text, px, y, token_width, token_height, leading, trailing, advance) in enumerate(line_entries):
                    result.append((run, text, px + cumulative, y, token_width, token_height, leading, trailing, advance))
                    if index in gap_set:
                        cumulative += extra_per_gap
                continue
            shift = max(0.0, slack) if align == "right" else max(0.0, slack) / 2.0
            result.extend(
                (run, text, px + shift, y, token_width, token_height, leading, trailing, advance)
                for run, text, px, y, token_width, token_height, leading, trailing, advance in line_entries
            )
        return result

    def publish(self, box, padding, owner_accum, element_fragments_accum):
        origin_x = box.x + box.border_left + padding[3]
        origin_y = box.y + box.border_top + padding[0]
        escapee_positions = getattr(self, "_escapee_positions", None)
        if escapee_positions:
            # A later pass applies each escapee's real page-coordinate
            # static position once every element's box has been written.
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
            # A `text-align:justify` line's own trailing space was already
            # redistributed into real, visible inter-word gaps -- nothing
            # natural is left over there to collapse (see `_apply_text_
            # align`'s own `_justified_lines`).
            collapsed_space = (run["space_width"]
                               if text[-1:].isspace() and ends_line
                               and y not in getattr(self, "_justified_lines", ()) else 0.0)
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
            # A CSS 2.1 9.2.1.1 split segment's own decoration-only marker
            # run (`_empty_decoration_only_run`: no font/line-height
            # contribution of its own -- see its docstring) normally sits
            # alone on its own dedicated zero-height line. But when real
            # sibling content (e.g. the text pending before a leading
            # segment) shares that same line, the marker doesn't get a
            # second, independent line of its own -- it's simply nowhere
            # (no ascent/above/below of its own to place a baseline
            # against), and belongs at the *top* of the real line, sized to
            # that line's own real height, not its own zero one.
            is_decoration_only_marker = (
                run.get("empty_strut") and run["ascent"] == 0.0
                and run["above"] == 0.0 and run["below"] == 0.0
            )
            if is_decoration_only_marker:
                # No ascent of its own to place a baseline against --
                # always the top of whatever line it's on, whether that's
                # a real shared line (sized to that line's own height) or
                # its own isolated, genuinely-empty one (0x0, unchanged).
                owner_y = origin_y + y
                marker_height = (self._line_baselines[y] + self._line_belows[y]
                                  if self._line_has_content.get(y) else token_height)
            else:
                owner_y = origin_y + (y if run["atomic_width"] else glyph_y - run["top_edge"])
                marker_height = token_height
            rect = (origin_x + x - leading, owner_y,
                    visual_advance + leading + trailing, marker_height)
            # Split/document-order segment index (`None` for a non-split
            # owner), so `_finalize_inline_owner_boxes` can place
            # interruption-marker rects logically, not via a geometric sort.
            owner_rects.setdefault(owner, []).append((rect, run.get("split_group")))
        # `inline-block`/block owners keep their atomic Taffy box; a real
        # `display:inline` owner's rects come from `owner_rects[self.element]`
        # instead, merged per-line then unioned for getBoundingClientRect().
        #
        # Accumulated into `owner_accum`, not finalized here: a split owner
        # (CSS 2.1 9.2.1.1) publishes from multiple independent plans, one
        # per segment -- `_finalize_inline_owner_boxes` unions them all once,
        # after every plan sharing `owner_accum` has run, instead of the
        # last plan to publish overwriting the earlier ones.
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
                br_x, br_y, line_h, _line_margin_start, _line_leading_total = self._break_positions.get(
                    run_index, (0.0, 0.0, 0.0, 0.0, 0.0))
                # For `self.rtl`, `measure()` itself already resolved `br_x`
                # to its mirrored position (the preceding real run's own
                # post-mirror box start) -- no further adjustment needed here.
                run["element"].__dict__["_layout_box"] = LayoutBox(
                    x=origin_x + br_x, y=origin_y + br_y,
                    width=0.0, height=line_h,
                    client_width=0.0, client_height=line_h,
                )
                run["element"]._chromonic_has_layout_children = False
        # Same accumulate-not-overwrite reasoning as `owner_accum` above,
        # for `self.element`'s own painted fragments.
        elem_entry = element_fragments_accum.get(id(self.element))
        if elem_entry is None:
            elem_entry = element_fragments_accum[id(self.element)] = (self.element, [])
        elem_entry[1].extend(self.fragments)

# Metadata/logic tags a real browser hardcodes as never painting a box;
# domonic's cascade gives these no such default on its own.
_NON_RENDERING_TAGS = frozenset({"script", "style", "head", "title", "meta", "link", "noscript", "template"})


def _is_element(node) -> bool:
    return getattr(node, "nodeType", None) == ELEMENT_NODE


def _element_direction(element, computed=None) -> str:
    """CSS `direction`'s real used value for `element`. An explicit author
    CSS declaration (inherited normally -- domonic's own cascade already
    handles that correctly) always wins. But domonic's cascade has no
    UA-stylesheet mapping for HTML's own `dir` attribute at all (a real UA
    rule every browser has, `[dir=rtl] { direction: rtl }`), and no
    attribute-selector support to add one via `ua_style.py` either
    (verified directly: `[dir="rtl"] {...}` never matches in domonic) --
    so a plain `<section dir="rtl">`, with no CSS `direction` anywhere,
    resolves `ltr` here, silently wrong. Falls back to walking the DOM for
    the attribute directly (nearest ancestor-or-self, mirroring its own
    real inheritance) only when the cascade resolved plain `ltr` -- which
    it only ever does when no CSS rule set `direction` anywhere up the
    chain (proven by the explicit-CSS case correctly resolving `rtl`
    through this same inheritance), so this can't shadow a real author
    declaration except the rare, self-contradictory case of *also* writing
    literal `direction: ltr` on top of a `dir="rtl"` attribute."""
    if computed is None:
        computed = getattr(element, "_chromonic_computed_style", None)
    resolved = (getattr(computed, "direction", "ltr") or "ltr").strip().lower() if computed is not None else "ltr"
    if resolved == "rtl":
        return "rtl"
    node = element
    while node is not None:
        if _is_element(node) and hasattr(node, "getAttribute"):
            dir_attr = (node.getAttribute("dir") or "").strip().lower()
            if dir_attr in ("rtl", "ltr"):
                return dir_attr
        node = getattr(node, "parentElement", None)
    return resolved


def _resolve_text_indent(computed) -> float:
    """`text-indent` in real px -- `ComputedStyleDeclaration.getPropertyValue`
    has no used-length conversion for it (unlike `width`), so a `ch`/`em`
    value would otherwise reach here as the literal, unresolved CSS text.
    `domonic.layout`'s own length parser (already relied on for `width`
    etc. via `LayoutStyle`) resolves it the same way real `width:10ch`
    layout already does elsewhere in this file -- percentages aren't
    supported (need the containing block, layout's job, not this early)."""
    if computed is None:
        return 0.0
    raw = (getattr(computed, "textIndent", "") or "0").strip()
    if not raw or raw == "0":
        return 0.0
    value = _parse_length_or_percent(raw, computed, allow_percent=False, allow_auto=False)
    return value.px if isinstance(value, Length) else 0.0


def _extract_paint_style(computed) -> dict:
    """The handful of paint-only properties `paint.py` needs, read out of
    `computed` exactly once here and cached as plain strings -- a
    `ComputedStyleDeclaration` attribute access re-resolves from the
    underlying style text on every access, so without this every repaint
    (not just relayout) re-parses every element's colours/fonts from scratch."""
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
        "text_align_last": computed.textAlignLast,
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
    """Whether a `::before`/`::after` rule's raw `content` generates a box
    at all, distinct from resolving to empty text -- `content: ""` still
    generates a real (icon-only) box; no matching rule, `none`, or the
    initial `normal` does not. domonic's own unset value is `"none"`."""
    text = (raw_content or "").strip().lower()
    return text not in ("", "none", "normal", "initial", "inherit")


def _extract_generated_content(element, computed_cache):
    """`(before_text, after_text, before_info, after_info)` -- the text
    pair is the plain generated-content strings; the info pair is `None`
    or `(pseudo_computed, text)` when the pseudo-element should become a
    real box (`_pseudo_generates_box`), consulted by `_inline_mixed_content`."""
    document = getattr(element, "ownerDocument", None)

    # No document/stylesheets -> no authored pseudo-element content possible;
    # skip the cascade resolutions entirely.
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
    `ComputedStyleDeclaration` and its extracted paint style on the element
    itself so `paint.py`'s later walk, and a repaint with no relayout in
    between, don't re-resolve either.

    `reuse_styles=True` skips resolving CSS entirely if a prior resolution
    (`element._chromonic_resolved_style`) exists, reusing it as-is -- CSS
    resolution, not Taffy, dominates relayout cost (domonic re-parses every
    selector from scratch, uncached), so a relayout triggered by something
    that can't have changed any element's class/inline-style/stylesheets
    (an image arriving, a same-bucket resize) passes this to skip it.

    `computed_cache` holds our own `(computed, style_obj)` tuples keyed by
    `id(element)`, kept separate from domonic's own `_chain_cache` (which
    expects bare `ComputedStyleDeclaration` values) under a nested dict."""
    cache = {} if computed_cache is None else computed_cache
    cached = cache.get(id(element))
    if cached is not None:
        return cached

    if reuse_styles:
        prior = getattr(element, "_chromonic_resolved_style", None)
        if prior is not None:
            cache[id(element)] = prior
            return prior

    # Share ancestor resolution across this layout pass, not across frames --
    # domonic's default cache only spans one element's ancestor walk.
    chain_cache = cache.setdefault("_chromonic_chain_cache", {})
    computed = ComputedStyleDeclaration(element, _chain_cache=chain_cache)
    # domonic's `_parent_computed()` only reads `chain_cache`, never
    # registers itself in it -- without this, a child resolved right after
    # `element` would build a second, separate `ComputedStyleDeclaration` for it.
    chain_cache[id(element)] = computed
    style_obj = LayoutStyle.from_computed(computed)
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
    """`element` just resolved to `display:none` -- clear its (and its
    subtree's) `_layout_box`/`_chromonic_inline_fragments` rather than
    leaving them from whenever it last rendered, or `paint_tree`/hit-
    testing/`getBoundingClientRect()` (which all walk the real DOM, not
    `node_map`) keep reading it as still having a box."""
    element.__dict__.pop("_layout_box", None)
    element.__dict__.pop("_chromonic_inline_fragments", None)
    for child in element.childNodes or []:
        if _is_element(child):
            _clear_stale_layout_geometry(child)


# CSS 2.1 16.6.1 whitespace collapsing only touches ASCII space/tab/newline/
# CR/form-feed, never U+00A0 (nbsp) -- unlike Python's own `str.strip()`/`\s`,
# which treats nbsp as whitespace too and would collapse an nbsp-only node away.
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
    # Mixed content (text alongside child elements) is out of scope -- only
    # a childless element's own text is measured. `_rendering_text_content`,
    # not raw `.textContent`, since a "childless" element can still have a
    # `<style>`/`<script>` descendant whose raw source text isn't real prose.
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
    when all children are inline-level elements (spans, links, etc.).

    Block children opt out, except when `element` is itself a genuine
    `display:inline` element -- CSS 2.1 9.2.1.1: an in-flow block child of
    an inline forces that inline to split around it, which needs the block
    to reach `_split_inline_flow_around_blocks`/`_contains_in_flow_block`
    as an ordinary item here rather than being rejected up front. An
    ordinary (non-inline) block `element` still opts a block child out
    entirely -- no anonymous-block implementation for that case."""
    has_direct_text = any(
        getattr(node, "nodeType", None) == TEXT_NODE and _collapsed_text_node(node).strip()
        for node in (element.childNodes or [])
    )
    # Also qualifies when all children are inline-level (or <br>), even with
    # no text anywhere -- CSS 2.1 9.2.1.1/10.8: a genuinely empty inline
    # still contributes its own line-box height/baseline, width 0.
    def _child_qualifies(child, style_obj) -> bool:
        return (
            _is_inline_level(child, style_obj)
            or _is_absolutely_positioned(style_obj)
            or (getattr(child, "tagName", "") or "").lower() == "br"
            # A direct in-flow block child of an inline `element` is the
            # CSS 2.1 9.2.1.1 split trigger, not grounds to reject it.
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
        pseudo_style_obj = LayoutStyle.from_computed(pseudo_computed)
        # A synthetic pseudo never goes through `_describe()` (no real DOM
        # node to resolve a `ComputedStyleDeclaration` for), so its paint
        # style must be set explicitly here, `@font-face` substitution included.
        pseudo._chromonic_paint_style = _extract_paint_style(pseudo_computed)
        pseudo._chromonic_computed_style = pseudo_computed
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
    """CSS 2.1 8.3.1: adjoining margins collapse into one -- the largest
    positive value plus the largest-magnitude negative one. An empty list
    collapses to no margin at all."""
    positive = max((m for m in margins if m > 0), default=0.0)
    negative = min((m for m in margins if m < 0), default=0.0)
    return positive + negative


def _block_margins_collapse_through(child, child_box) -> bool:
    """CSS 2.1 8.3.1: an empty in-flow block (no border/padding, `auto`
    height, no content) doesn't stop its own top/bottom margins from
    collapsing through it. Zero height alone isn't enough -- an explicit
    `height: 0` still stops collapse-through; only `auto` qualifies."""
    if child_box.height != 0.0:
        return False
    native = getattr(child, "_chromonic_native_style", None) or {}
    if native.get("height") != "auto":
        return False
    if any(_numeric_edge(v) != 0.0 for name in ("border", "padding") for v in native.get(name, ())):
        return False
    return True


def _empty_inline_strut_run(owner, leading_edge, trailing_edge, top_edge_val, extra_height, margin_start):
    """CSS 2.1 9.2.1.1/10.8: a genuinely empty, non-replaced inline still
    generates one zero-width inline box participating in the line --
    contributing its own font/line-height to the line's height/baseline
    like a real text run (`above`/`below`), while its own vertical
    padding/border/margin only grow its own box (`box_height`), never the
    line's. One empty `("", 0.0)` token so `measure()` places it without
    treating it as wrappable content."""
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
    real text still needs a fragment for its own border/padding decoration,
    but unlike `_empty_inline_strut_run` it's never a real inline-flow
    participant (it's on its own dedicated block-flow line) -- so it gets
    no font/line-height contribution at all, just its own border/padding."""
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
    content of `child_nodes` (DOM order): `leading_edge`/`trailing_edge`
    (border+padding) go on the first/last text run, `top_edge_val`/
    `extra_height` on every run's box height, `margin_start`/`margin_end`
    applied once each around the whole sequence. `[]` if no non-empty text.

    Shared by `_make_inline_formatting_plan` and `_split_wrapping_inline_
    element` (CSS 2.1 9.2.1.1's split fragments, each passing 0 for
    whichever edge it doesn't own).

    `computed_cache`, when given, also handles two node kinds a split
    segment can carry: a *simple* nested inline (text/`<br>` only, no
    further nesting) is flattened in place via a recursive call with its
    own paint style/edges; an absolutely-positioned element becomes an
    "escapee" run -- no width/height of its own, but its position in
    `runs` marks its CSS 2.1 10.3.7/10.6.4 static position for later use.
    Anything deeper is silently skipped."""

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
            # An explicit `line-height: 0` must not be treated as unset.
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
            nested_margin_left = _numeric_edge(nested_native["margin"][3])
            nested_margin_right = _numeric_edge(nested_native["margin"][1])
            nested_runs = _build_text_runs_from_nodes(
                list(child_node.childNodes or ()), child_node._chromonic_paint_style, child_node,
                leading_edge=leading_edge if is_first_text else 0.0,
                trailing_edge=trailing_edge if is_last_text else 0.0,
                top_edge_val=top_edge_val + nested_top,
                extra_height=extra_height + nested_extra,
                margin_start=margin_start if is_first_text else 0.0,
                margin_end=margin_end if is_last_text else 0.0,
                computed_cache=computed_cache,
            )
            # CSS 2.1's bidi box model (box.html#bidi-box-model, confirmed
            # directly against `left-rtl-ref.xht`'s own reference markup:
            # `<span style="border-left-style:none; padding-right:10px;
            # margin-right:60px">One</span>` for the DOM-first fragment of a
            # `direction:rtl` box split by a line break) attaches this
            # element's own *start*-side package (border+padding+margin
            # together, on one physical side, nothing on the other) to the
            # DOM-first non-break run and the *end*-side package to the
            # DOM-last one -- start=left/end=right for `ltr`, start=right/
            # end=left for `rtl`. `leading_edge`/`trailing_edge`/
            # `margin_start`/`margin_end` above carry only outer-ancestor
            # contributions (always physical, unswapped -- an ancestor's own
            # side was already resolved at its own level); this element's
            # own package is added directly onto the correct physical field
            # of the correct run here, after the recursive call returns,
            # since the recursive call's own `leading_edge`/`trailing_edge`
            # params are structurally physical-left/physical-right and can't
            # express "this element's start edge is physically on the right".
            non_break_runs = [r for r in nested_runs if not r.get("break")]
            if non_break_runs:
                first_run, last_run = non_break_runs[0], non_break_runs[-1]
                is_rtl_nested = _element_direction(child_node, child_computed) == "rtl"
                # Tells `_InlineFormattingPlan.measure()`'s whole-line RTL
                # mirror not to fall back to the owner's raw CSS margin-right
                # on this fragment even when its own `margin_end` is zero --
                # zero is a real, direction-aware answer here (an `rtl`
                # box's end fragment legitimately owns margin-*left*
                # instead), not a sign the field was never threaded (which
                # is what that fallback exists for, elsewhere).
                first_run["_bidi_margin_resolved"] = True
                last_run["_bidi_margin_resolved"] = True
                if first_run is last_run:
                    # Unsplit -- the sole run owns both edges, physically,
                    # regardless of direction (a box's own border/padding
                    # doesn't change because its content is `rtl`; only a
                    # line-break split invokes the start/end swap at all).
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_right
                                                      + nested_margin_left + nested_margin_right)
                elif is_rtl_nested:
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_right + nested_margin_right)
                    last_run["leading"] = last_run.get("leading", 0.0) + nested_left
                    last_run["margin_start"] = last_run.get("margin_start", 0.0) + nested_margin_left
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_left + nested_margin_left)
                else:
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_margin_left)
                    last_run["trailing"] = last_run.get("trailing", 0.0) + nested_right
                    last_run["margin_end"] = last_run.get("margin_end", 0.0) + nested_margin_right
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_right + nested_margin_right)
            runs.extend(nested_runs)
    return runs


def _is_genuine_inline_wrapper(node, style_obj) -> bool:
    """Whether `node` is a genuinely `display:inline` wrapper, as opposed
    to an atomic inline-level box (`inline-block`, replaced) that merely
    happens to also be inline-level. Only a genuine wrapper's content is
    reachable "through" it for CSS 2.1 9.2.1.1 (`_contains_in_flow_block`)
    -- an `inline-block` owns its own BFC/descendants entirely, so a block
    child inside one must never look like a direct block child of whatever
    merely contains that inline-block."""
    tag_name = (getattr(node, "tagName", "") or "").lower()
    return (
        tag_name not in _REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and _trusts_computed_inline(node, tag_name)
    )


def _contains_in_flow_block(element, computed_cache) -> bool:
    """Whether `element`'s subtree contains a genuine in-flow, block-level
    descendant reachable by walking only inline-level elements -- the CSS
    2.1 9.2.1.1 "anonymous block box" split trigger.

    `select`/`svg` are never walked into, matching `build()`'s own
    treatment of their real children as not real layout content."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
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
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return True
        if _is_genuine_inline_wrapper(node, child_style) and _contains_in_flow_block(node, computed_cache):
            return True
    return False


def _first_reachable_in_flow_block(element, computed_cache):
    """Like `_contains_in_flow_block`, but returns the first descendant
    node itself rather than a bool -- used so a *nested* wrapper's own
    marker fragment (`_finalize_inline_owner_boxes`) has a real box to
    read geometry from. `None` if nothing qualifies."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
        return None
    for node in element.childNodes or ():
        if not _is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(node, computed_cache)
        if not _renders(child_style):
            continue
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return node
        if _is_genuine_inline_wrapper(node, child_style):
            found = _first_reachable_in_flow_block(node, computed_cache)
            if found is not None:
                return found
    return None


def _has_direct_in_flow_block_child(element, computed_cache) -> bool:
    """Like `_contains_in_flow_block`, but shallow -- true only for a
    block that's `element`'s own immediate child, not one further nested.
    Distinguishes CSS 2.1 9.2.1.1's two shapes: `element` itself directly
    parenting a block (splits itself) vs. a nested wrapper doing so (only
    that wrapper splits; `element` stays an ordinary block container)."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
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
        if _is_absolutely_positioned(child_style) or _is_floated(child_computed):
            continue
        if not _is_inline_level(node, child_style):
            return True
    return False


def _split_wrapping_inline_element(wrapper, computed_cache, container):
    """CSS 2.1 9.2.1.1: `wrapper`, an inline element containing an in-flow
    block, splits into a sequence of fragments around each such block --
    yields `("run", runs)` or `("block", child, child_computed, child_style)`
    in DOM order. Only the first run gets `wrapper`'s own left border/
    padding/margin; only the last gets its right border/padding; an
    interior fragment gets neither. `wrapper` itself is never built as a
    Taffy node -- the split exists only in the layout projection.

    `container` (the real ancestor whose block-flow children the split
    pieces become) is stashed on `wrapper` for two post-layout corrections
    needing its finished geometry: the block-interruption marker rect
    (`_finalize_inline_owner_boxes`) and the percentage `top`/`left` basis
    (`_fix_split_inline_relative_offset`)."""
    wrapper._chromonic_split_container = container
    wrapper_computed, wrapper_style_obj = _describe(wrapper, computed_cache)
    native = style_bridge.to_dict(wrapper_style_obj)
    wrapper._chromonic_native_style = native
    left_edge = _numeric_edge(native["padding"][3]) + _numeric_edge(native["border"][3])
    right_edge = _numeric_edge(native["padding"][1]) + _numeric_edge(native["border"][1])
    # The split's leading/trailing fragments carry the wrapper's *logical*
    # start/end edge, not always its physical left/right -- in
    # `direction:rtl`, the first-generated fragment owns the right
    # border/padding/margin and the last owns the left, swapped from `ltr`.
    # `left_edge`/`right_edge`/`margin_left`/`margin_right` themselves stay
    # physical below (unswapped); which segment (first vs last) each gets
    # routed to is decided per-segment further down instead, since a plain
    # value-swap here would just move the *magnitude* without moving it to
    # the physically-correct side (`_build_text_runs_from_nodes`'s own
    # `leading_edge`/`trailing_edge` params are always physical-left/
    # physical-right, tied structurally to first/last segment respectively).
    is_rtl = _element_direction(wrapper, wrapper_computed) == "rtl"
    top_edge_val = _numeric_edge(native["padding"][0]) + _numeric_edge(native["border"][0])
    extra_height = (top_edge_val + _numeric_edge(native["padding"][2])
                    + _numeric_edge(native["border"][2]))
    margin_left = _numeric_edge(native["margin"][3])
    margin_right = _numeric_edge(native["margin"][1])
    if wrapper is container:
        # The direct-child shape: `wrapper` is a real Taffy node, so Taffy
        # already physically shifted its text-leaf children by this same
        # border/padding/margin -- zero them here to avoid double-counting
        # horizontal position; `top_edge_val` stays (one-way addition, safe),
        # its own vertical position corrected via `_chromonic_split_self_edges`
        # in `_finalize_inline_owner_boxes` instead.
        wrapper._chromonic_split_self_edges = (
            (right_edge if is_rtl else left_edge), (left_edge if is_rtl else right_edge), top_edge_val)
        left_edge = right_edge = margin_left = margin_right = 0.0
    else:
        wrapper.__dict__.pop("_chromonic_split_self_edges", None)
    paint_style = wrapper._chromonic_paint_style

    segments: list = [[]]
    blocks: list = []
    # A child that's itself an inline wrapper reaching a block only through
    # further nesting (not directly) must not fall through to `segments[-1].
    # append(node)` -- that treats it as ordinary content for
    # `_build_text_runs_from_nodes`, which has no notion of a nested block
    # and drops it. Recorded by segment index instead; resolved to a real
    # block for the marker rect below, and split via recursive delegation
    # at yield time rather than being flattened into `blocks` directly.
    nested_wrapper_at: dict = {}
    for node in wrapper.childNodes or ():
        if _is_element(node):
            tag = (getattr(node, "tagName", "") or "").lower()
            if tag in _NON_RENDERING_TAGS:
                continue
            if tag != "br":
                child_computed, child_style = _describe(node, computed_cache)
                if not _renders(child_style):
                    continue
                # CSS 2.1 9.2.1.1 only ever applies to an *in-flow* block
                # child -- a `float:left`/`right` one is out of flow (still
                # computed block-level, per 9.7's blockification, but never
                # forces the wrapper to split around it): it stays ordinary
                # segment content instead, built as its own atomic subtree
                # below, the same way an `inline-block` already is.
                if (not _is_absolutely_positioned(child_style) and not _is_floated(child_computed)
                        and not _is_inline_level(node, child_style)):
                    # `_fix_block_in_inline_rtl_position` needs the same
                    # real containing block `wrapper` itself tracks --
                    # CSS 2.1 9.2.1.1's split doesn't change this block's
                    # own normal-flow containing block or positioning
                    # rules, `direction:rtl` included.
                    node._chromonic_split_container = container
                    blocks.append((node, child_computed, child_style))
                    segments.append([])
                    continue
                if (_is_genuine_inline_wrapper(node, child_style)
                        and _contains_in_flow_block(node, computed_cache)):
                    nested_wrapper_at[len(blocks)] = node
                    blocks.append((None, child_computed, child_style))
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
    # merged rects are visible. A nested-wrapper interruption reports the
    # first *real* block reachable through it -- the same element its own
    # recursive split (below) will itself report a marker for -- so every
    # ancestor level between the real block and the line gets its own
    # marker rect at that same position, matching real Chrome.
    wrapper._chromonic_interruption_blocks = [
        block if block is not None else _first_reachable_in_flow_block(nested_wrapper_at[index], computed_cache)
        for index, (block, _computed, _style) in enumerate(blocks)
    ]
    wrapper._chromonic_atomic_segment_elements = {}
    wrapper._chromonic_split_edge_flow_height = {}
    # `wrapper` itself is never built as a real Taffy node at all when it's
    # a *nested* wrapper (see this function's own docstring) -- it never
    # ends up in `node_map`, so nothing that only ever walks `node_map.
    # values()` (e.g. `_fix_nested_split_flow_extent`) can discover it
    # directly. Each real interruption block *is* a genuine node, though,
    # so a back-reference on it is a reliable way back to its wrapper.
    for block, _computed, _style in blocks:
        if block is not None:
            block.__dict__["_chromonic_split_wrapper_ref"] = wrapper

    for index, seg_nodes in enumerate(segments):
        is_first, is_last = index == 0, index == len(segments) - 1
        # `direction:rtl`: the first segment owns the *start* (physical-
        # right) package, the last owns the *end* (physical-left) one --
        # swapped from `ltr`'s first=left/last=right (CSS 2.1's own bidi box
        # model, confirmed against `left-rtl-ref.xht`'s real geometry).
        seg_leading = (left_edge if is_last else 0.0) if is_rtl else (left_edge if is_first else 0.0)
        seg_trailing = (right_edge if is_first else 0.0) if is_rtl else (right_edge if is_last else 0.0)
        seg_margin_start = (margin_left if is_last else 0.0) if is_rtl else (margin_left if is_first else 0.0)
        seg_margin_end = (margin_right if is_first else 0.0) if is_rtl else 0.0
        runs = _build_text_runs_from_nodes(
            seg_nodes, paint_style, wrapper,
            leading_edge=seg_leading,
            trailing_edge=seg_trailing,
            top_edge_val=top_edge_val, extra_height=extra_height,
            margin_start=seg_margin_start, margin_end=seg_margin_end,
            computed_cache=computed_cache,
        )
        if not runs and (seg_leading or seg_trailing):
            # A leading/trailing segment with no real text still needs a
            # fragment when it has real inline extent (own padding making
            # it non-zero-width) -- CSS 2.1 9.2.1.1's split generates an
            # anonymous inline box there even with no text, at the font's
            # line-height plus the wrapper's vertical border/padding. A
            # segment with *no* inline extent is genuinely `0x0` instead.
            runs = [_empty_inline_strut_run(
                wrapper, seg_leading, seg_trailing, top_edge_val, extra_height, seg_margin_start,
            )]
            # Ancestor auto-height must read only the real line box
            # (`above`+`below`), not this fragment's visual height (which
            # includes the wrapper's vertical border/padding) -- stashed
            # per edge for `_fix_nested_split_flow_extent` to correct with.
            wrapper.__dict__.setdefault("_chromonic_split_edge_flow_height", {})[
                "leading" if is_first else "trailing"
            ] = runs[0]["above"] + runs[0]["below"]
        if not runs:
            # A segment can also come up empty because it holds one or
            # more atomic inline-level elements (`inline-block`/replaced)
            # with no text -- `_build_text_runs_from_nodes` can't build a
            # subtree for those, so represent each as its own real,
            # recursively-built block-flow piece instead.
            atomic_candidates = [
                (node,) + _describe(node, computed_cache)
                for node in seg_nodes if _is_element(node)
            ]
            if atomic_candidates and all(
                (_is_inline_level(node, node_style) and not _is_genuine_inline_wrapper(node, node_style))
                or _is_floated(node_computed)
                for node, node_computed, node_style in atomic_candidates
            ):
                wrapper._chromonic_atomic_segment_elements[index] = [node for node, _c, _s in atomic_candidates]
                for node, node_computed, node_style in atomic_candidates:
                    yield ("block", node, node_computed, node_style)
                # A zero-edge marker run, not a real fragment -- every
                # segment needs at least one run tagged to it or
                # `_finalize_inline_owner_boxes` skips its marker rect too.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
            else:
                # Genuinely nothing on this side -- still an explicit `0x0`
                # fragment (Chrome reports one), but no height/font/flow
                # contribution of its own.
                runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
        if runs:
            # Tags each run with its segment index (document order), so
            # `_finalize_inline_owner_boxes` places interruption markers
            # logically rather than via a geometric sort.
            for run in runs:
                if not run.get("break"):
                    run["split_group"] = index
                    if is_rtl and (is_first or is_last):
                        # A zero `margin_end`/`margin_start` on this segment
                        # is a real, direction-aware answer here (see the
                        # identical tag elsewhere) -- suppress `measure()`'s
                        # owner-raw-margin fallback, which exists only for
                        # the untagged, non-`rtl` case above where that
                        # fallback is genuinely needed (margin-right is
                        # never threaded there at all).
                        run["_bidi_margin_resolved"] = True
            yield ("run", runs)
        if index < len(blocks):
            if index in nested_wrapper_at:
                # Not a real block child -- delegate to this nested
                # wrapper's own split (one anonymous-block level per
                # genuine inline ancestor, CSS 2.1 9.2.1.1 applied recursively).
                yield from _split_wrapping_inline_element(
                    nested_wrapper_at[index], computed_cache, container)
            else:
                yield ("block",) + blocks[index]


def _split_inline_flow_around_blocks(element, inline_items, style, css_display, computed_cache):
    """`inline_items` contains, at some depth reachable only through
    inline-level elements, a genuine in-flow block -- CSS 2.1 9.2.1.1's
    "anonymous block box" case (`<span>One<div/>Two</span>`: the block
    forces `span` to split into a "One" fragment, the block, and a "Two"
    fragment, siblings in normal block flow, not one flex row or a single
    measured text leaf).

    Returns an ordered list of pieces -- `("plan", _InlineFormattingPlan)`
    or `("block", child, child_computed, child_style)` -- ready to become
    `element`'s ordinary block-stack Taffy children. `None` if nothing
    needs splitting -- caller falls back to its existing handling.

    Two shapes reach this function (`_has_direct_in_flow_block_child`
    distinguishes them): `element` itself directly parenting the block
    (splits itself, dispatched immediately below), or a plain block
    `element` merely containing a nested inline item that wraps a block
    deeper (only that nested item splits; `element` stays an ordinary
    container, handled by the per-item loop below)."""
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
            # CSS 2.1 9.2.1.1: the wrapper's own leading fragment isn't
            # itself a line break -- any text already pending before it
            # belongs on the *same* line box, right up until the first
            # real block interruption actually forces a split. Seed
            # `runs_acc` from `pending`'s own runs instead of flushing it
            # as an independent, separately-positioned plan (which would
            # otherwise stack it as its own zero-height row above the
            # wrapper's leading segment rather than sharing its line).
            runs_acc: list = []
            if pending:
                pending_plan = _make_inline_formatting_plan(element, list(pending), style, css_display)
                if pending_plan is not None:
                    runs_acc.extend(pending_plan.runs)
                pending.clear()
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
            or getattr(item, "_chromonic_after_pseudo", None) is not None
            # `inline-block` (CSS 2.1 10.3.10) is always an atomic box with
            # its own formatting context, own explicit/auto width+height,
            # never flattened text runs sharing this plan's line metrics --
            # the "element" branch below only ever tries to flatten a
            # nested element's own *text* into runs, which for a genuinely
            # empty `inline-block` (no text, no children at all) produces
            # zero runs and *no fallback strut either* (that fallback is
            # deliberately `display:inline`-only, since only a plain empty
            # inline collapses to nothing -- an empty inline-block still
            # keeps its own explicit box, CSS 2.1 9.2.1.1/10.8) -- silently
            # dropping the element from the plan's own `runs` entirely, and
            # therefore from `node_map`, painted or not. Confirmed on `wpt/
            # css/CSS2/visudet/inline-block-baseline-011.xht`: an empty
            # `<span style="display:inline-block">` between two text runs
            # never got a Taffy node at all. Bailing here routes it through
            # the flex-row fallback instead, which already builds every
            # element item as its own real recursive `build()` subtree.
            or getattr(child_style.display, "value", "") == "inline-block"
            # A replaced element (`<img>`, `<canvas>`, `<svg>`, `<iframe>`,
            # a form control) is exactly as atomic as `inline-block` above
            # -- its own box is a real Taffy leaf sized from its own
            # intrinsic/authored width+height, never flattened text runs --
            # and one is typically a void element with no `childNodes` of
            # its own at all, so it hits the exact same silent-drop gap:
            # zero runs, no empty-strut fallback (also deliberately
            # excluded for these tags, since they don't collapse to
            # nothing like a plain empty inline can). Confirmed directly on
            # `wpt/css/CSS2/visudet/replaced-elements-width-40.html`: every
            # `<img>` meant to flow inline with the comma-separated text
            # between them vanished from layout entirely once that text
            # started actually being recognised as inline-mixed content
            # (see `_USUALLY_INLINE_TAGS` picking up `img`/`canvas`/`svg`/
            # `iframe`).
            or (getattr(item, "tagName", "") or "").lower() in _REPLACED_OR_CONTROL_TAGS)
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
            # `item` is on the ordinary (non-split) path this layout --
            # clear any stale interruption-block bookkeeping a previous
            # layout's split may have left on it.
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
            # CSS 2.1's bidi box model (box.html#bidi-box-model, confirmed
            # against `left-rtl-ref.xht`'s own reference markup) attaches
            # the *start*-side package (border+padding+margin together) to
            # the DOM-first fragment and the *end*-side package to the
            # DOM-last one -- start=left/end=right for `ltr`, start=right/
            # end=left for `rtl`. `_build_text_runs_from_nodes`'s own
            # `leading_edge`/`trailing_edge` params are always physical-left/
            # physical-right, so for a `direction:rtl` item the two edge
            # packages are built unswapped here and then swapped onto the
            # correct fragment below, once the actual (possibly `<br>`-
            # split) fragments are known.
            is_rtl_item = _element_direction(item, child_computed) == "rtl"
            child_runs = _build_text_runs_from_nodes(
                list(item.childNodes or []), item._chromonic_paint_style, item,
                leading_edge=0.0 if is_rtl_item else left_edge,
                trailing_edge=0.0 if is_rtl_item else right_edge,
                top_edge_val=top_edge_val, extra_height=extra_height,
                margin_start=0.0 if is_rtl_item else margin_start,
                margin_end=0.0 if is_rtl_item else margin_end,
            )
            if is_rtl_item:
                non_break_runs = [r for r in child_runs if not r.get("break")]
                if non_break_runs:
                    first_run, last_run = non_break_runs[0], non_break_runs[-1]
                    # See the identical tag in `_build_text_runs_from_nodes`'s
                    # nested-element branch: a zero `margin_end` here is a
                    # real, direction-aware answer, not an unthreaded field.
                    first_run["_bidi_margin_resolved"] = True
                    last_run["_bidi_margin_resolved"] = True
                    if first_run is last_run:
                        # Unsplit -- the sole run owns both edges, physically,
                        # regardless of direction.
                        first_run["leading"] = first_run.get("leading", 0.0) + left_edge
                        first_run["trailing"] = first_run.get("trailing", 0.0) + right_edge
                        first_run["margin_start"] = first_run.get("margin_start", 0.0) + margin_start
                        first_run["margin_end"] = first_run.get("margin_end", 0.0) + margin_end
                        first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                          + left_edge + right_edge
                                                          + margin_start + margin_end)
                    else:
                        first_run["trailing"] = first_run.get("trailing", 0.0) + right_edge
                        first_run["margin_end"] = first_run.get("margin_end", 0.0) + margin_end
                        first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                          + right_edge + margin_end)
                        last_run["leading"] = last_run.get("leading", 0.0) + left_edge
                        last_run["margin_start"] = last_run.get("margin_start", 0.0) + margin_start
                        last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                         + left_edge + margin_start)
            item_display = (getattr(child_computed, "display", "") or "").strip().lower()
            item_tag = (getattr(item, "tagName", "") or "").lower()
            if (not child_runs and not (item.childNodes or [])
                    and item_display == "inline" and item_tag not in _REPLACED_OR_CONTROL_TAGS):
                # CSS 2.1 9.2.1.1/10.8's empty-inline strut applies only to
                # a plain, non-replaced `display:inline` -- an `inline-
                # block`/replaced element keeps its own explicit width/
                # height even with no content (CSS 2.1 10.3.10).
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
        # An explicit `line-height: 0` must not be treated as unset.
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


def _needs_inline_flow_grouping(child, child_style) -> bool:
    """Whether `child` still needs `_group_inline_element_runs`'s flex-row
    simulation of real inline flow. An inline-level *tag* (`_is_inline_
    level`) that CSS 2.1 9.2.1.1 has already split around an in-flow block
    child (`_split_wrapping_inline_element` marks this on the element
    itself via `_chromonic_split_container`, set every time it runs) no
    longer needs it: `build()` already represents that element as an
    ordinary block-flow Taffy node (ordinary sibling block pieces, not
    real horizontal inline content), so wrapping it in a flex row here
    would be both redundant and actively wrong -- CSS margins never
    collapse across a flex formatting context, so a plain block sibling
    after it could never collapse through the wrapper with the split's
    own trailing margin, even though nothing here is really "inline"
    content anymore. Confirmed directly: `<div class="container"><span>
    <div class="first"></div></span><div class="second"></div></div>`
    only collapsed margin-bottom/margin-top additively (70px) instead of
    the correct `max()` (40px) until this exemption was added -- the
    anonymous flex wrapper `_group_inline_element_runs` built around the
    already-dissolved `<span>` was the collapse barrier."""
    return _is_inline_level(child, child_style) and getattr(child, "_chromonic_split_container", None) is None


def _group_inline_element_runs(tree, parent, entries, parent_style, node_map, projection):
    """Wrap consecutive inline siblings in retained anonymous flex rows."""
    if parent_style["display"] != "block":
        return [node_id for _child, _style, node_id in entries]
    output, run_index, index = [], 0, 0
    cache = parent.__dict__.setdefault("_chromonic_inline_runs", {})
    while index < len(entries):
        child, child_style, node_id = entries[index]
        if not _needs_inline_flow_grouping(child, child_style):
            output.append(node_id)
            index += 1
            continue
        run = []
        run_elements = []
        while index < len(entries) and _needs_inline_flow_grouping(entries[index][0], entries[index][1]):
            run.append(entries[index][2])
            run_elements.append(entries[index][0])
            index += 1
        wrapper = cache.get(run_index)
        if wrapper is None:
            wrapper = cache[run_index] = _AnonymousInlineRun(None, parent)
        run_index += 1
        wrapper_style = _inline_text_style(parent_style)
        # A real inline formatting context's default cross-axis alignment
        # is the text baseline, not flex's own initial "normal"/stretch.
        wrapper_style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap",
                              "align_items": "baseline"})
        wrapper._chromonic_native_style = wrapper_style
        # `_fix_flex_row_baseline_alignment` only corrects Taffy's own
        # (sometimes wrong -- see that function's docstring) baseline
        # cross-axis placement for a row it can find via this attribute;
        # never set here before, so a run of consecutive real inline-level
        # element siblings (not the separate mixed-text-and-elements `elif
        # inline_items:` approximation, which already sets it) got no such
        # correction at all. Confirmed on `wpt/css/CSS2/visudet/content-
        # height-001.html`: three sibling `display:inline-block` divs with
        # different `line-height`s (so genuinely different heights) landed
        # at the wrong `y` relative to each other, silently unfixed.
        wrapper._chromonic_flex_row_members = run_elements
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
    matching, shaping, and genuine Unicode line-breaking. `font_family` is
    read from `paint_style` (already extracted by `_describe`), not a
    fresh `computed.fontFamily` access, to avoid re-resolving it. Parley's
    job is strictly layout (line breaks, space needed), not painting --
    `chromonic.fonts` separately resolves the actual Skia typeface."""
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
            # Chrome exposes integral line-box heights for platform fonts
            # while Parley's raw metrics are fractional -- normalize the
            # implicit `normal` line box before it accumulates down a page.
            lines = [(line_text, line_width, normal_height)
                     for line_text, line_width, _height in lines]
            height = normal_height * len(lines)
        # stashed for paint.py -- the *only* record of how this text wrapped
        # (and, now, its real per-line height); paint draws exactly these
        # lines rather than re-wrapping or re-measuring itself.
        element._chromonic_text_lines = [line_text for line_text, _line_width, _line_height in lines]
        line_widths = [line_width for _line_text, line_width, _line_height in lines]
        # CSS Text 3 `text-align: justify`: every line but the last (unless
        # `text-align-last` says otherwise) stretches to fill the line box
        # -- paint.py doesn't yet re-space individual words to visually
        # match (a real leaf here has no per-word gap bookkeeping the way
        # `_InlineFormattingPlan` does), but the *reported* line width
        # (hit-testing/`getClientRects()`) must still reflect the real,
        # filled extent, not each line's own natural shrink-to-fit one.
        if (len(line_widths) > 1 and (paint_style.get("text_align") or "").strip().lower() == "justify"
                and available_width and 0 < available_width < 1_000_000):
            text_align_last = (paint_style.get("text_align_last") or "auto").strip().lower()
            stretch_last = text_align_last not in ("auto", "left", "start", "")
            last_index = len(line_widths) - 1
            line_widths = [
                available_width if (index != last_index or stretch_last) else line_width
                for index, line_width in enumerate(line_widths)
            ]
        element._chromonic_text_line_widths = line_widths
        element._chromonic_line_height = lines[0][2] if lines else font_size * 1.2
        # `_fix_flex_row_baseline_alignment` needs this to re-assert the
        # real measured content height onto a member of an `align-items:
        # baseline` row -- Taffy's own cross-axis sizing for a custom
        # `MeasureFunc` leaf doesn't reliably keep this method's own
        # returned `height` once that alignment mode is in play (the same
        # quirk `_InlineFormattingPlan.measure()`'s own leaves have,
        # confirmed on `wpt/css/CSS2/visudet/inline-block-baseline-001.xht`:
        # a `display:inline-block` `<span>`'s `line-height:5`-driven `75px`
        # height came back `17px` -- this method itself, called directly
        # with the same arguments, correctly returns `75`).
        element._chromonic_measured_height = height
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
    """`<select>` is a native, closed dropdown -- shows only the selected
    option's text, not every `<option>` stacked as visible content. Picks
    the first `<option selected>`, falling back to the first option, or
    `""` if it has none."""
    options = element.getElementsByTagName("option")
    if not options:
        return ""
    for opt in options:
        if opt.hasAttribute("selected"):
            return " ".join((opt.textContent or "").split())
    return " ".join((options[0].textContent or "").split())


def _apply_image_intrinsic_size(style: dict, element) -> None:
    """`<img>` is a replaced element: an `auto` width/height is sized from
    its own intrinsic width/height -- built here since Taffy has no concept
    of an image's intrinsic properties (only the plain rectangle `width`/
    `height`/`aspect_ratio` this function bakes into `style` before Taffy
    ever sees the tree, as a block child's own width isn't `measure`'s to
    give). An explicit CSS size on either axis is always left alone.

    A raster image (PNG/JPEG/GIF/...) always has a complete intrinsic size.
    An SVG only has whichever of `width`/`height`/`viewBox` its root
    element actually declares (`browser_images.natural_size`), each
    independently possibly absent -- CSS 2.1 10.3.2 leaves a replaced
    element's sizing genuinely undefined for every case *except* a
    complete intrinsic width+height pair (this fixture's own `<meta
    name="flags" content="should">` notes exactly that), and real browsers
    resolve the undefined cases by falling back to the CSS default object
    size (300x150, the same UA default `<canvas>`/`<iframe>` already use)
    outright -- confirmed directly against Chrome on `wpt/css/CSS2/
    visudet/replaced-elements-width-40.html`'s own seven SVGs: an
    intrinsic ratio alone (from `viewBox`, with or without one matching
    dimension) is *not* used to scale an explicit CSS dimension the way
    CSS Images 3 5.2's idealised algorithm would suggest -- only a
    complete width+height pair ever drives sizing; every partial case
    (ratio-only, one dimension only, or nothing at all) gets the default
    150 height/300 width regardless."""
    from . import browser_images

    src = element.getAttribute("src") or ""
    image = browser_images.load_image(src)
    if image is None:
        return  # no image to size from -- left as whatever the cascade said (probably auto -> an empty box)
    intrinsic_width, intrinsic_height, _ratio = browser_images.natural_size(src)
    has_complete_pair = intrinsic_width is not None and intrinsic_height is not None
    intrinsic_ratio = intrinsic_width / intrinsic_height if has_complete_pair and intrinsic_height else None
    # CSS Images 3 5.2's own fallback when nothing intrinsic is known at
    # all on the needed axis -- 300x150, the same UA default `<canvas>`/
    # `<iframe>` already use elsewhere in this file.
    default_width, default_height = 300.0, 150.0
    width_auto = style["width"] == "auto"
    height_auto = style["height"] == "auto"
    if width_auto and height_auto:
        if has_complete_pair:
            style["width"], style["height"] = intrinsic_width, intrinsic_height
        else:
            style["width"], style["height"] = default_width, default_height
    elif height_auto and isinstance(style["width"], (int, float)):
        style["height"] = style["width"] * (1.0 / intrinsic_ratio) if intrinsic_ratio else default_height
    elif width_auto and isinstance(style["height"], (int, float)):
        style["width"] = style["height"] * intrinsic_ratio if intrinsic_ratio else default_width
    elif (height_auto or width_auto) and intrinsic_ratio:
        # `width`/`height` isn't a plain pixel length on either side (most
        # commonly a percentage, e.g. `width:100%` on a responsive `<img>`)
        # -- its resolved pixel value isn't known until Taffy itself lays
        # out the box, so this function (which only ever runs once, before
        # Taffy sees the tree at all) can't precompute the scaled auto side
        # the way the plain-pixel branches above do. Taffy's own native
        # `aspect_ratio` support (added to its `Style` for exactly this)
        # picks it up after resolving whichever side has a real value, on
        # its own. Confirmed directly on bbc.com: every `<img
        # style="width:100%">` measured a real width and `height:auto` but
        # a literal `0` height -- nothing here had ever handled a
        # percentage width/height at all, so the image simply never
        # painted (zero-height box), despite genuinely finishing loading.
        style["aspect_ratio"] = intrinsic_ratio


def _resolve_replaced_percent_height(style: dict, element, height_attr: str) -> "float | None":
    """CSS 2.1 10.6.2: a replaced element's own percentage intrinsic
    height (an HTML `height="N%"` attribute, e.g. on `<svg>`/`<iframe>`)
    resolves against its containing block's height -- but only when that
    containing block's own height is itself definite (an explicit
    length, not `auto`); otherwise the percentage "is treated as '0'" (no
    intrinsic height at all, same as never having one), never resolved
    circularly against the containing block's own (still-undetermined)
    auto height."""
    containing_block = _find_containing_block_ancestor(element) if (
        style.get("position") in ("absolute", "fixed")) else getattr(element, "parentElement", None)
    cb_native = (getattr(containing_block, "_chromonic_native_style", None)
                 if containing_block is not None else None)
    cb_height = cb_native.get("height") if cb_native is not None else None
    if not isinstance(cb_height, (int, float)):
        return None
    try:
        return cb_height * float(height_attr[:-1]) / 100.0
    except ValueError:
        return None


def _apply_iframe_intrinsic_size(style: dict, element) -> None:
    """`<iframe>` is a replaced element with no intrinsic ratio -- CSS 2.1
    10.3.2/10.6.2's fallback for that case is a UA-defined default,
    300 x 150 in every real browser, same as `<canvas>`'s own default
    bitmap (an explicit `width`/`height` HTML attribute, or CSS size,
    still wins over it as always)."""
    # `style["width"]`/`["height"]` can already be the UA stylesheet's own
    # `300`/`150` default rather than literal `"auto"` (`ua_style.py` gives
    # every `<iframe>` an explicit fallback size, CSS 2.1 10.3.2/10.6.2's
    # own UA-defined default) -- an HTML `width`/`height` attribute is a
    # real, if legacy, presentational hint that should still win over that
    # UA default (though never over genuine author CSS, indistinguishable
    # here from that same UA default -- accepted as a rare edge case).
    if style["width"] in ("auto", 300.0):
        width_attr = (element.getAttribute("width") or "").strip()
        if width_attr and not width_attr.endswith("%"):
            try:
                style["width"] = float(width_attr)
            except ValueError:
                pass
        elif style["width"] == "auto":
            style["width"] = 300.0
    if style["height"] in ("auto", 150.0):
        height_attr = (element.getAttribute("height") or "").strip()
        if height_attr.endswith("%"):
            resolved = _resolve_replaced_percent_height(style, element, height_attr)
            style["height"] = resolved if resolved is not None else (
                style["height"] if style["height"] != "auto" else 150.0)
        elif height_attr:
            try:
                style["height"] = float(height_attr)
            except ValueError:
                pass
        elif style["height"] == "auto":
            style["height"] = 150.0


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
    if height is None and height_attr.endswith("%"):
        height = _resolve_replaced_percent_height(style, element, height_attr)
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
    # CSS 2.1 10.3.2/10.6.2: with no intrinsic width/height/ratio to fall
    # back on at all (no `width`/`height` attribute, no `viewBox`), a
    # replaced element still isn't sized like an ordinary block -- it
    # gets the same UA-defined 300 x 150 default `<iframe>`/`<canvas>`
    # already do, even where that means overflowing a narrower
    # containing block (confirmed directly against Chrome: a
    # `width:auto` SVG with only `height="50"` and no `viewBox` comes out
    # `300px` wide inside a `288px` container, not shrunk to fit it).
    if style["width"] == "auto":
        style["width"] = 300.0
    if style["height"] == "auto":
        style["height"] = 150.0


def _measure_intrinsic_width(element, computed_cache) -> "float | None":
    """The natural (max-content) width `element` would take with no line
    wrapping -- computed in a disposable Taffy tree so real layout does the
    measuring rather than a hand-rolled approximation. `None` on any
    failure; caller falls back to today's behaviour."""
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
    a `_NON_RENDERING_TAGS` tag -- plain `.textContent` includes their raw
    source text verbatim, which is never actually rendered.

    No `childNodes` at all (e.g. a `_PseudoElement`'s generated-content
    text) falls back to `element.textContent` directly."""
    child_nodes = getattr(element, "childNodes", None)
    if not child_nodes:
        return getattr(element, "textContent", None) or ""
    parts = []

    def walk(node):
        # A plain-string child (domonic's programmatic constructors never
        # wrap one in a Text node) has no `nodeType` at all, so it must be
        # checked for explicitly or it's silently dropped.
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


def _is_table_row_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() == "table-row"


def _is_table_cell_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() == "table-cell"


def _is_table_root_display(computed) -> bool:
    return (getattr(computed, "display", "") or "").strip().lower() in ("table", "inline-table")


_ROW_GROUP_TAG_KIND = {"thead": "header", "tfoot": "footer", "tbody": "body"}


def _row_group_kind(tag: str, computed) -> "str | None":
    """`None` if `tag`/`computed` isn't a table row-group at all (a plain
    wrapper `walk` should recurse through transparently); otherwise which
    of the three CSS 2.1 17.5.3 row-group buckets it is."""
    if tag in _ROW_GROUP_TAG_KIND:
        return _ROW_GROUP_TAG_KIND[tag]
    display = (getattr(computed, "display", "") or "").strip().lower()
    if display == "table-header-group":
        return "header"
    if display == "table-footer-group":
        return "footer"
    if display == "table-row-group":
        return "body"
    return None


def _table_rows(table_element, computed_cache) -> list:
    """Every table-row belonging to `table_element`'s own table -- a
    literal `<tr>`, or any element computing `display:table-row` --
    reached transparently through any depth of row-group wrapper
    (`table-row-group`/`-header-group`/`-footer-group`, or a literal
    `<tbody>`/`<thead>`/`<tfoot>`). Stops at a nested table's own
    boundary -- its rows belong to it, not this one.

    CSS 2.1 17.5.3: however many header/body/footer row-groups appear,
    and in whatever source order, they're always *displayed* in a fixed
    header-groups, then body-groups, then footer-groups order -- a
    `<tfoot>` authored first (a common pattern, so its totals row can
    reach the network before the body finishes loading) still renders
    last. A row with no row-group ancestor at all counts as an (implicit)
    body row, same as a `table-row-group`. Row order *within* each bucket,
    and row-group order within `header`/`footer` (multiple of either is
    non-conforming markup, but not fatal here), stays DOM order."""
    buckets: dict[str, list] = {"header": [], "body": [], "footer": []}

    def walk(node, kind: str):
        for child in node.childNodes or ():
            if not _is_element(child):
                continue
            tag = (getattr(child, "tagName", "") or "").lower()
            if tag in _NON_RENDERING_TAGS:
                continue
            child_computed, child_style = _describe(child, computed_cache)
            if not _renders(child_style):
                continue
            if tag == "tr" or _is_table_row_display(child_computed):
                buckets[kind].append(child)
                continue
            if tag == "table" or _is_table_root_display(child_computed):
                continue  # a nested table's own rows aren't this table's
            group_kind = _row_group_kind(tag, child_computed)
            walk(child, group_kind if group_kind is not None else kind)

    walk(table_element, "body")
    return buckets["header"] + buckets["body"] + buckets["footer"]


def _row_cells(row_element, computed_cache) -> list:
    """Every table-cell directly inside `row_element` -- a literal `<td>`/
    `<th>`, or any element computing `display:table-cell`. CSS 2.1
    17.2.1 only ever generates a cell as a row's *direct* child (an
    anonymous cell wraps other content instead), so this deliberately
    doesn't recurse."""
    cells: list = []
    for child in row_element.childNodes or ():
        if not _is_element(child):
            continue
        tag = (getattr(child, "tagName", "") or "").lower()
        if tag in _NON_RENDERING_TAGS:
            continue
        child_computed, child_style = _describe(child, computed_cache)
        if not _renders(child_style):
            continue
        if tag in ("td", "th") or _is_table_cell_display(child_computed):
            cells.append(child)
    return cells


def _compute_table_column_widths(table_element, computed_cache) -> dict:
    """`{id(cell_element): resolved_width}` for every cell, colspan'd or
    not -- a deliberately minimal CSS 2.1 17.5.2.2 "auto" table-layout
    pass, enough for ordinary HTML tables (and `display:table`-styled
    arbitrary elements, see `_table_rows`/`_row_cells`).

    1. Each colspan-1 cell's max-content width; a column's width is the
       widest same-column cell across every row.
    2. A colspan'd cell's width is the sum of its columns; if its own
       *minimum* content width needs more, spread the shortfall evenly
       across just the columns it spans (never the whole table).
    3. Resolve every cell to one definite pixel width from the final
       column widths, before `build()` ever measures inline content --
       Taffy's own flex-measurement guessing never enters into it."""
    per_column: dict[int, float] = {}
    single_cells: dict[int, int] = {}  # id(cell) -> col_index, colspan == 1
    span_cells: list = []  # (cell, start_col, colspan)
    rows = _table_rows(table_element, computed_cache)
    for row in rows:
        col_index = 0
        for cell in _row_cells(row, computed_cache):
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

    # Grow only the columns a colspan'd cell covers, measured against the
    # base column widths (not ones already grown by an earlier colspan) --
    # the largest shortfall wins (`max`), not an accumulating sum, or a run
    # of same-span rows would compound into a wildly inflated column.
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


# Fallback tag guess for a raw DOM tree built without `browser.load()`
# (skipping the UA stylesheet), where every tag's un-cascaded default is
# "inline" and tag-name is the only signal left.
_USUALLY_INLINE_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "small", "code", "label", "abbr",
    "cite", "mark", "sub", "sup", "time", "kbd", "samp", "var", "q", "u", "s",
    "button", "input", "select", "textarea",
    # Every real HTML UA stylesheet gives these `display:inline` by default
    # too, same as the form controls just above -- missing here left `img`/
    # `canvas`/`svg`/`iframe` untrusted by `_trusts_computed_inline`, so
    # `_is_inline_level` always said `False` for them regardless of their
    # own genuinely-inline computed style, which made `_inline_mixed_
    # content`'s `_child_qualifies` reject any container mixing one with
    # real text -- the whole container fell out of inline flow entirely
    # (`_make_inline_formatting_plan` *and* the flex-row fallback both
    # bail together, since neither ever got a chance to run at all),
    # putting each image on its own block-level line instead of flowing
    # with its surrounding text. Confirmed directly on `wpt/css/CSS2/
    # visudet/replaced-elements-width-40.html`: seven `<img>`s meant to
    # flow with comma-separated text between them each landed alone on
    # its own row.
    "img", "canvas", "svg", "svg:svg", "iframe",
})

# Replaced elements and form controls size themselves from authored
# `width`/`height` even at `display:inline` -- unlike an ordinary inline
# element, whose box is purely a function of its content.
_REPLACED_OR_CONTROL_TAGS = frozenset({
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select", "button", "iframe",
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
    "img", "canvas", "svg", "svg:svg", "input", "textarea", "select",
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
    """Whether a computed `display: inline`/`inline-block` on `element`
    can be trusted as real author intent rather than domonic's un-cascaded
    default -- true for a tag assumed usually-inline, or one `ua_style.py`
    gives an explicit `block` default when that stylesheet actually ran."""
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
    if value not in ("inline", "inline-block", "inline-table"):
        return False
    tag_name = (getattr(element, "tagName", "") or "").lower()
    return _trusts_computed_inline(element, tag_name)


def _is_floated(child_computed) -> bool:
    """Whether an author explicitly gave this element `float: left`/`right`
    -- unlike `display`, `float`'s initial value is always `none` regardless
    of tag, so any non-`none` value is unambiguous author intent, no tag
    gate needed (contrast `_is_inline_level`)."""
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
    # A majority (not unanimity) qualifies, since each individual check is
    # already narrow. A single floated child is enough on its own though --
    # `float` is always explicit author intent (never a domonic tag-default
    # ambiguity), and CSS 2.1 9.5 has any float narrow its container
    # regardless of how small a fraction of the children it is.
    if not any(_is_floated(cc) for cc in child_computeds) and sum(qualifies) < len(child_elements) * 0.8:
        return
    style["display"] = "flex"
    style["flex_direction"] = "row"
    style["flex_wrap"] = "wrap"
    # `_fix_float_flow_after_block_sibling` needs to know which children
    # were real ordinary blocks -- plain flex-wrap has no notion that a
    # block sibling must force every later floated child onto a fresh line.
    element._chromonic_float_flow_children = list(child_elements)
    element._chromonic_float_flow_qualifies = list(qualifies)
    # An ordinary block child (CSS 2.1 9.2.1) always fills the containing
    # block's full width, `width:auto` or not -- plain flex-wrap would
    # shrink-to-fit it instead, so `build()` forces `flex_basis:100%` for
    # it here; explicit-width blocks are left alone.
    for child, ok in zip(child_elements, qualifies):
        if not ok:
            child._chromonic_force_full_row_width = True
    inline_tag_qualifies = any(
        _is_inline_level(child, child_style) for child, child_style in zip(child_elements, child_styles)
    )
    if inline_tag_qualifies and style["gap"] == (0.0, 0.0):
        # Real inline flow gets its spacing from whitespace text nodes
        # between elements, which this project doesn't measure -- without
        # this, flex-wrap packs elements edge to edge. Approximated as one
        # space width in the container's font; skipped for a purely
        # float-qualified group, which already gets spacing from margins.
        font_size = _fontmetrics.parse_length(computed.fontSize, default=16.0)
        bold = _fontmetrics.is_bold(computed.fontWeight)
        space_width = _fontmetrics.advance_width(" ", font_size, bold)
        style["gap"] = (0.0, space_width)


def _is_absolutely_positioned(style_obj) -> bool:
    position = style_obj.position
    return getattr(position, "value", position) in ("absolute", "fixed")


def _establishes_bfc(computed) -> bool:
    """CSS 2.1 9.4.1: whether this box establishes its own new block
    formatting context -- float/absolute/fixed positioning/`flow-root`/
    `inline-block`/table-cell/table-caption, or any non-`visible`
    overflow. Plain `overflow:visible` must NOT establish one -- only a
    real BFC box avoids a float; an ordinary block's border box may
    extend behind one."""
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
    """Whether `position` makes this element a valid containing block for
    `position:absolute`/`fixed` descendants -- anything but `static`."""
    position = style_obj.position
    return getattr(position, "value", position) != "static"


def build(
    tree: Tree, element, node_map: dict, *, computed=None, style_obj=None, computed_cache=None,
    is_containing_block: bool = True, escapees: "list | None" = None, reuse_styles: bool = False,
    projection=None, is_grid_item: bool = False,
) -> int:
    """Recursively mirror `element` and its descendants into `tree`. Returns
    the root's Taffy node id; `node_map[node_id] = element` for every node
    created. `computed`/`style_obj`, if given, are `element`'s already-
    computed style (the caller's `_child_elements` call already needed
    them), so they're never computed twice.

    `is_containing_block`/`escapees` implement CSS's real containing-block
    rule for `position:absolute`/`fixed` -- resolved against the nearest
    ancestor with `position != static`, or the viewport, never just the
    literal DOM parent. Every call defaults to `is_containing_block=True`
    (matching the CSS initial containing block); when true, it owns a
    fresh `escapees` list, and any absolutely-positioned descendant whose
    literal parent isn't a containing block is added to *that* list
    instead of its literal parent's Taffy children, landing it one edge
    from its real containing block. An intermediate `position:static`
    element passes its inherited `escapees` straight through."""
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
        # implements this correctly given `Style.overflow`; any value Rust's
        # `parse_overflow_axis` doesn't recognise falls back to "visible".
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
        # inline box -- Taffy has no inline mode (mapped to plain "block"
        # by `style_bridge._display()`), which would otherwise treat an
        # authored width/height as a hard box size.
        style["width"] = "auto"
        style["height"] = "auto"
        # CSS 2.1 10.3.1/10.6.1: vertical margins likewise don't affect a
        # non-replaced inline's height -- left as ordinary flex-item
        # margins, Taffy would inflate the anonymous inline run's height.
        margin = list(style["margin"])
        margin[0] = margin[2] = 0.0
        style["margin"] = margin
    table_internal_display = getattr(style_obj.display, "value", "")
    if table_internal_display in _TABLE_INTERNAL_DISPLAYS:
        # CSS 2.1 17.4/CSS Tables 3: margin never applies to an internal
        # table box (row/row-group/cell/column/...) on any side -- only
        # the outer `display:table` box itself keeps it.
        style["margin"] = [0.0, 0.0, 0.0, 0.0]
        if table_internal_display != "table-cell":
            # Unlike margin, padding *does* still apply to `table-cell`
            # (CSS 2.1 17.6.1 -- a cell's own padding is what visibly
            # separates its content from its border) -- every other
            # internal table box (row/row-group/column/column-group) gets
            # neither, same as margin. Confirmed directly on `wpt/css/
            # CSS2/margin-padding-clear/padding-applies-to-001.xht`
            # (`display:table-row-group; padding:50px` was still adding
            # 50px of space Chrome never does).
            style["padding"] = [0.0, 0.0, 0.0, 0.0]
    if (getattr(style_obj.display, "value", "") == "inline-block"
            and _trusts_computed_inline(element, tag_name)):
        # `inline-block` establishes its own BFC (CSS 2.1 9.2.1), so an
        # in-flow child's margin must not collapse through it -- signalled
        # to Taffy the same way as `overflow`, via `Contain::PAINT`.
        style["establishes_bfc"] = True
    is_table_root = tag_name == "table" or _is_table_root_display(computed)
    is_table_row = tag_name == "tr" or _is_table_row_display(computed)
    is_table_cell = tag_name in ("td", "th") or _is_table_cell_display(computed)
    if is_table_root:
        element._chromonic_is_table_root = True
        element._chromonic_border_collapse = computed.borderCollapse == "collapse"
        if element._chromonic_border_collapse:
            # Collapsed borders straddle the table grid edge -- reserving
            # half an outer border per side matches Chrome's inner grid.
            style.update({"box_sizing": "border-box", "padding": [0.5] * 4})
        # Real "auto" table layout (CSS 2.1 17.5.2.2, not `table-layout:
        # fixed`) sizes each column to its widest cell's own content, not
        # an equal row share -- measured once per table so every same-
        # column cell agrees. See `_compute_table_column_widths`. Applies
        # equally to a literal `<table>` and any `display:table`/`inline-
        # table` arbitrary element -- `_table_rows`/`_row_cells` (which
        # this calls) recognise a table-row/-cell by computed `display`
        # too, not just tag name.
        element._chromonic_table_column_widths = (
            _compute_table_column_widths(element, computed_cache)
            if computed.tableLayout != "fixed" else {}
        )
    if is_table_row:
        # Taffy has no table formatting mode -- a plain flex row gives
        # ordinary fixed/equal-column tables the right basic geometry.
        style.update({"display": "flex", "flex_direction": "row", "flex_wrap": "nowrap"})
    elif is_table_cell and style["width"] == "auto":
        ancestor = getattr(element, "parentElement", None)
        while ancestor is not None and not getattr(ancestor, "_chromonic_is_table_root", False):
            ancestor = getattr(ancestor, "parentElement", None)
        column_width = None
        if ancestor is not None:
            column_width = getattr(ancestor, "_chromonic_table_column_widths", {}).get(id(element))
        if column_width is not None:
            # `flex_grow` proportional to the column's own intrinsic width
            # (not uniform `1.0`) so extra room goes mostly to the column
            # that wants it, not a small fixed-content one.
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
        # Set by `_approximate_inline_flow` for a non-floated, non-inline
        # block sibling standing in for real float layout -- an explicit
        # author width is left alone; only `auto` needs correcting, since
        # real CSS block flow always fills the containing block.
        style["flex_basis"] = ("pct", 1.0)
    # `<select>`'s `<option>`s and `<iframe>`'s light-DOM children are never
    # real layout content -- treated as childless regardless of markup.
    children = [] if tag_name in ("select", "svg", "svg:svg", "iframe") else _child_elements(
        element, computed_cache, reuse_styles=reuse_styles
    )
    if is_table_root and children:
        # CSS 2.1 17.5.3: row-groups always *display* in header/body/
        # footer order regardless of source order (a `<tfoot>` authored
        # first, so its totals reach the network before the body
        # finishes loading, is common markup) -- `_table_rows` already
        # reorders for column-width *measurement*; this reorders the
        # table's own direct Taffy children the same way so the visual
        # stacking (built from ordinary DOM-order block-child handling,
        # same as any other element) matches. A non-row-group direct
        # child (a bare `<tr>`, or anything else) counts as an implicit
        # body row/group, same as `_table_rows`'s own default.
        header, body, footer = [], [], []
        for entry in children:
            child_tag = (getattr(entry[0], "tagName", "") or "").lower()
            kind = _row_group_kind(child_tag, entry[1]) or "body"
            (header if kind == "header" else footer if kind == "footer" else body).append(entry)
        children = header + body + footer
    element._chromonic_has_layout_children = bool(children)
    if tag_name == "button":
        _apply_button_intrinsic_width(style, element)
    # Replaced/control elements always run their own dedicated branch below
    # -- CSS generated content doesn't apply to them, so a stray
    # `::before`/`::after` rule must not divert them into inline formatting.
    has_pseudo = tag_name not in _NO_GENERATED_CONTENT_TAGS and (
        getattr(element, "_chromonic_before_pseudo", None) is not None
        or getattr(element, "_chromonic_after_pseudo", None) is not None
    )
    inline_items = (_inline_mixed_content(element, children, element_is_inline=is_genuinely_inline)
                    if (children or has_pseudo) else None)
    # `<td>`/`<th>` have no UA default in domonic, so their computed
    # `display` is uninformatively "inline" -- forced to "block" here so
    # `_InlineFormattingPlan`'s `owner_display` leaves Taffy's own
    # (already-correct, full column-width) box alone instead of narrowing
    # it to just the cell's own text fragment. A `display:table-cell`
    # element on an arbitrary tag has a real, already-correct computed
    # value (`is_table_cell`, from earlier in this function) -- included
    # here for the same "leave Taffy's box alone" reason, not because its
    # own computed value is uninformative too.
    css_display_value = ("block" if (tag_name in ("td", "th") or is_table_cell)
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
                # A run tagged "escapee" (an out-of-flow absolutely-
                # positioned element mixed into this segment) marks where
                # it sits for static-position purposes; building its real
                # subtree is this function's job, added to `escapees` (its
                # containing block is almost never this split wrapper
                # itself) so it lands one edge from its real one.
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
        # `style["display"]` is already Taffy-mapped ("block" for every
        # non-flex/grid box), so it can't distinguish a genuine block-level
        # element (width:auto stretches to fill its container) from an
        # inline/inline-block one recursively built as an atomic flex item
        # inside an ancestor's flex-row fallback (must stay content-sized).
        # `css_display_value`, the real pre-mapping computed display, does.
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
        # `paint.py` falls back to drawing raw `textContent` when it
        # believes there are no layout children -- a `::before`/`::after`-
        # only element (no real child elements) reaches here with that
        # flag still unset, so it must be forced True or paint would draw
        # the raw text a second time on top of the fragment built below.
        element._chromonic_has_layout_children = True
        style["display"] = "flex"
        style["flex_direction"] = "row"
        style["flex_wrap"] = "wrap"
        style["align_items"] = "baseline"
        # CSS 2.1 16.2 `text-align`: this flex-row approximation's own
        # `elif inline_items:` mixed-text-and-elements content otherwise
        # always packs to the row's physical start (Taffy's own default,
        # unset `justify_content`), silently ignoring `right`/`center` --
        # `justify-content` is a real, direct equivalent for a *single*
        # unjustified line, which is genuinely what this approximation
        # already models one of (`flex-wrap:wrap` repeats it per wrapped
        # row too, matching real `text-align` applying per line, not once
        # for the whole block). `justify`/`start`/`end` deliberately not
        # remapped here -- no simple `justify-content` distributes text
        # the way real justification does, and `start`/`end` need real
        # `direction` awareness this approximation doesn't have elsewhere
        # either (`_InlineFormattingPlan._apply_text_align` next door
        # makes the same simplification, physical `left`/`right`/`center`
        # only). Confirmed on `wpt/css/CSS2/visudet/line-height-203.html`:
        # a `position:absolute; width:300px; text-align:right` box's own
        # inline-block `<span>` landed flush left instead of at the box's
        # own right edge.
        text_align_value = (getattr(computed, "textAlign", "") or "").strip().lower()
        if text_align_value in ("right", "end"):
            style["justify_content"] = "flex-end"
        elif text_align_value == "center":
            style["justify_content"] = "center"
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
        # Document-order record of every real Taffy child this flex row
        # got (text fragments and real elements alike, `escapee`s excluded
        # -- they never occupy row space) -- `_fix_flex_row_baseline_
        # alignment` needs this exact membership/order to know which
        # children share one wrapped row, since after Taffy's own
        # (sometimes wrong, see that function) baseline placement their
        # `y` positions alone can no longer be trusted to say so.
        row_members = []
        for item_index, (kind, item, text, child_computed, child_style) in enumerate(inline_items):
            if kind == "element":
                # An absolutely-positioned item counts as "inline" for
                # `_inline_mixed_content` regardless of its real display,
                # so it can land in this flex-row fallback too -- must
                # still escape to its real containing-block ancestor when
                # `element` (its literal DOM parent) isn't one, same as
                # the `elif children:` branch below already does.
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
                    row_members.append(item)
                if isinstance(item, _PseudoElement):
                    # Not a real DOM child -- reaches paint only via this
                    # side-channel list, same as retained text fragments.
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
            row_members.append(item)
        element._chromonic_inline_fragments = fragments
        element._chromonic_flex_row_members = row_members
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
                # `element` isn't a valid containing block -- build the
                # child normally, but hand its node id to whichever real
                # ancestor `escapees` belongs to instead.
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
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        style["width"] = 0.0
        style["height"] = line_height
        element._chromonic_text_lines = []
        node_id = (projection.upsert(element, style, [], None, None)
                   if projection else tree.new_leaf(style))
    elif tag_name in ("img", "canvas", "svg", "svg:svg", "iframe"):
        element.__dict__.pop("_chromonic_inline_plan", None)
        element._chromonic_inline_fragments = []
        if tag_name == "img":
            _apply_image_intrinsic_size(style, element)
        elif tag_name == "canvas":
            _apply_canvas_intrinsic_size(style, element)
        elif tag_name == "iframe":
            _apply_iframe_intrinsic_size(style, element)
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

    `<html>` is never built into the Taffy tree -- chromonic hands Taffy
    `<body>` as its root instead, so `<html>`'s own box-model edges were
    never read anywhere. Summed together rather than kept separate --
    nothing downstream needs to tell them apart."""
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return None
    # `.parentElement` is broken for `<body>` in domonic (`.parentNode`
    # works); that object's `nodeType` is `DOCUMENT_NODE`, not
    # `ELEMENT_NODE` (domonic's `<html>` and `Document` are the same
    # underlying object), so `tagName` is the only reliable signal.
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
    box-model edges (`_document_element_box_edges`) before `compute()`
    runs, and returns the available width `<body>` must be computed
    against (`width` minus `<html>`'s horizontal edges).

    `<body>`'s own `width:auto` is always forced to a definite content-box
    number (`<html>`'s edges and `<body>`'s own margin subtracted from
    `width`) rather than left for Taffy to resolve -- a root node with
    only out-of-flow children would otherwise shrink-to-fit to `0`.
    `box-sizing: border-box` is left alone, since there `width` already
    means the border-box total."""
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
    """Pay Parley's one-time `FontContext` setup cost (~100ms, font
    enumeration) now, not during the first real page's first text --
    subsequent calls reuse the process-lifetime context and are near-free."""
    layout_text("warm", "sans-serif", 16.0)


def _write_boxes(boxes, node_map):
    """Publish native geometry back onto the authoritative Domonic nodes."""
    for node_id, box in boxes.items():
        x, y, w, h, bt, br, bb, bl, pt, pr, pb, pl = box
        element = node_map[node_id]
        state = element.__dict__
        # Same private-state assignment domonic's set_layout_box wrappers
        # ultimately do -- done directly here to skip them per node.
        state["_layout_box"] = LayoutBox(
            x=x, y=y, width=w, height=h,
            client_width=w - bl - br,
            client_height=h - bt - bb,
            border_top=bt, border_left=bl,
        )
        state["_chromonic_padding"] = (pt, pr, pb, pl)


def _fix_float_shrink_to_fit_width(tree_obj, node_map: dict) -> None:
    """CSS 2.1 10.3.5/10.3.6: a floated box with `width:auto` is sized by
    shrink-to-fit, not stretched to fill its containing block -- chromonic
    has no real float implementation, so a floated element reaches this
    point laid out as an ordinary full-width block first.

    Re-runs Taffy's `compute()` for just this element at `available_width=
    None` (max-content), re-laying-out the real subtree so descendants
    reflow into the narrower width too, then shifts the whole subtree to
    its real page position. Only ever shrinks -- nothing to correct if the
    intrinsic width isn't already smaller."""
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


def _fix_table_shrink_to_fit_width(tree_obj, node_map: dict) -> None:
    """CSS 2.1 17.5.2: an outer `display:table`/`inline-table` box with
    `width:auto` is sized by shrink-to-fit (summed column widths), the
    same as a float or `inline-block` -- not stretched to fill its
    containing block the way an ordinary block is. Chromonic's table
    root has no dedicated Taffy display mode of its own (plain "block",
    same as any other box; only its `<tr>`-equivalent children switch to
    a flex-row simulation -- see `build()`'s `is_table_row` handling), so
    it reaches this point laid out full-width first, same starting point
    `_fix_float_shrink_to_fit_width` corrects for floats -- reuses the
    identical technique (a fresh, max-content `tree.compute()` for just
    this subtree)."""
    by_id = {id(element): node_id for node_id, element in node_map.items()}
    for element in list(node_map.values()):
        if not _is_element(element):
            continue
        if not getattr(element, "_chromonic_is_table_root", False):
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
        _write_boxes(boxes, node_map)
        dx = box.x - own[0]
        dy = box.y - own[1]
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            _shift_subtree(element, dx, dy)


def _fix_float_flow_after_block_sibling(node_map: dict) -> None:
    """CSS 2.1 9.5: a float starts at or below the current block-flow
    position, at the containing block's edge, never wherever a previous
    sibling's box happened to end horizontally. `_approximate_inline_flow`
    stands in for real float layout with plain `flex-wrap`, which has no
    notion of this -- a row only wraps on width overflow, so a paragraph
    followed by floats packed them onto its own row instead of below it.

    Runs after Taffy's flex-wrap layout, using the qualifying split
    `_approximate_inline_flow` recorded on `element`. Narrow on purpose:
    only applies when at least one child is an ordinary block and every
    qualifying child is a real float (not merely inline-level) -- mixed
    groups are left to Taffy's own result. When it applies, every child's
    position is recomputed by simple left-to-right block/float packing."""
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
        # every row it overlaps, not just the one it first packed onto --
        # tracked here so a later block's `margin:auto` resolves against
        # the narrowed band, not the full content width.
        active_floats: list = []
        # `_adjust_body_collapsed_margins` may have already folded
        # `element`'s own top margin together with this first child's
        # (CSS 2.1 8.3.1 adjoining-margins collapse) -- treated as an
        # already-resolved `0`, not `mt`, here.
        first_margin_collapsed = getattr(element, "_chromonic_margin_collapsed", False)
        # CSS 2.1 8.3.1: adjoining margins collapse into one; a float
        # between two blocks doesn't break the adjoining chain.
        # `pending_margins` accumulates the current chain (an empty block
        # joins both its own margins without resolving anything); a real
        # block resolves the whole set via `_collapse_margin_set`.
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
                    # Own top/bottom margin joins the same adjoining set --
                    # nothing resolves yet, so this empty block's zero-size
                    # position is only a best-effort placement.
                    pending_margins.append(mb)
                    new_x = content_left + ml
                    new_y = block_bottom + _collapse_margin_set(pending_margins)
                    dx, dy = new_x - child_box.x, new_y - child_box.y
                    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                        _shift_subtree(child, dx, dy)
                    cursor_x = content_left
                    continue
                # An ordinary in-flow block: own row, at the containing
                # block's edge, below everything placed so far -- narrowed
                # by a still-active float (CSS 2.1 9.5) only if this child
                # establishes its own BFC (9.4.1); an ordinary block's
                # border box may extend behind one otherwise.
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
                # `float:right` packs flush to the containing block's right
                # content edge, not the left-to-right packing below (CSS
                # 2.1 9.5.1).
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
    BFC (CSS 2.1 10.6.7) -- descends through non-BFC-establishing
    descendants (an ordinary wrapper isn't a float's containing block;
    the nearest real BFC ancestor still owns it), stopping at any
    descendant that establishes its own BFC."""
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
    auto_height` applies (a `height:auto` box never counts a float unless
    it establishes a BFC) but for the cases that heuristic doesn't reach:
    a lone float (the only child of an ordinary wrapper div) reaches Taffy
    as a plain in-flow block, its full height counted toward the wrapper's
    auto-height like any other child -- Taffy has no notion it should be
    excluded, only that its margin might collapse through.

    A BFC-establishing ancestor further up needs the opposite correction,
    recursing past that same non-BFC wrapper to find the float, since its
    own auto-height counts every descendant float in its formatting
    context, not just direct children."""
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
            # `float` always computes to `none` on a flex/grid item, so
            # such a container can never actually have a floated child --
            # this function's whole premise never applies to one, even
            # though `_establishes_bfc` (correctly, as a separate CSS
            # fact) says it establishes a BFC. Its block-flow-style
            # recompute isn't equivalent to Taffy's own flex/grid sizing
            # (cross-axis extent, not the lowest child's bottom edge), so
            # it must never touch one -- Taffy's own number is already correct.
            continue
        if not getattr(element, "_chromonic_has_layout_children", False):
            # A genuine leaf (no element children) was never sized by
            # summing child contributions -- its `height:auto` is already
            # a real, correctly-measured text/line-box result, not
            # something to recompute from `childNodes` here.
            continue
        if getattr(element, "_chromonic_inline_plan", None) is not None:
            # `_chromonic_has_layout_children` is set `True` for one of
            # these too (a different purpose -- see `build()`'s own
            # comment there, stopping `paint.py` from drawing raw
            # `textContent` a second time), but it's still a genuine,
            # single Taffy leaf measured whole by `plan.measure()` -- its
            # own inline children (a `<span>`, say) were flattened into the
            # plan's runs, never built as real Taffy nodes of their own, so
            # they have no real `_layout_box` this function's "sum child
            # bottoms" logic could read. Recomputing its height from
            # `childNodes` here silently discarded the plan's own already-
            # correct measured height instead (confirmed on `wpt/css/CSS2/
            # visudet/content-height-001.html`: a `line-height:200px`
            # `display:inline-block` div, which also establishes a BFC,
            # measured `200px` correctly and then got overwritten to `129px`
            # by this exact function, right here).
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
    contributes too, but only if the element establishes a BFC (9.4.1).
    Taffy's own flex-wrap row-summing (`_approximate_inline_flow`'s
    stand-in for real float layout) instead *adds* each wrapped row's
    height together, double-counting a float row and a later normal-flow
    row that both start from the same content top.

    Runs after `_fix_float_flow_after_block_sibling` has placed every
    child at its real, float-aware position -- recomputes the container's
    height from those final positions instead."""
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
            # auto-height with extra precision this generic version
            # doesn't replicate -- recomputing it here risks regressing it.
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
            # Taffy already stacked every later sibling using this
            # element's stale, pre-fix height -- must be propagated.
            _shift_later_siblings_for_height_delta(element, delta)


def _apply_linebox_strut_height(node_map: dict) -> None:
    """CSS 2.1 10.8: a line box's height always includes its own "strut"
    (an invisible zero-width inline box using the line's font/line-height)
    even when the only real content is a single atomic inline-level box
    with no text. Taffy has no concept of a line box, so such a block's
    `height:auto` comes out exactly as tall as its tallest child.

    Deliberately conservative: only a `height:auto` block whose in-flow
    children are all atomic inline-level boxes at default `vertical-align:
    baseline`, with no text of their own, is corrected."""
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
        # An explicit `line-height: 0` must not be treated as unset.
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else normal
        half_leading = (line_height - (ascent + descent)) / 2.0
        strut_above = ascent + half_leading
        strut_below = descent + half_leading
        max_above = strut_above
        for child, child_box in atomic_children:
            child_native = getattr(child, "_chromonic_native_style", None) or {}
            margin = child_native.get("margin") or (0.0, 0.0, 0.0, 0.0)
            # `vertical-align:baseline` on an atomic box aligns its bottom
            # margin edge to the line's baseline (CSS 2.1 10.8.1).
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
    """A genuinely empty `display:inline-block` box still measures
    `height:auto` as one line's worth of its own font/line-height, not
    zero -- unlike a plain non-replaced `display:inline`, whose *shared*
    ancestor line can legitimately collapse to zero when empty (CSS 2.1
    9.4.2), an inline-block always establishes its own self-contained
    formatting context, whose line box exists even with nothing in it.
    `build()`'s childless fallback gives it no such machinery on its own."""
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
        # that flag can be set for degenerate content too. What matters is
        # only whether the box ended up shorter than one line, checked
        # below against `needed_height`.
        paint_style = getattr(element, "_chromonic_paint_style", None) or {}
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family", "") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        _ascent, _descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        # An explicit `line-height: 0` must not be treated as unset.
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


def _resync_interruption_marker_heights(node_map: dict) -> None:
    """`_finalize_inline_owner_boxes` builds each CSS 2.1 9.2.1.1
    interruption marker's rect from its block's `_layout_box` height at
    that point in the pipeline -- before the float-auto-height
    corrections (`_fix_float_flow_container_auto_height`/`_fix_nested_bfc_
    float_auto_height`, both run after `_publish_inline_formatting`) have
    had a chance to zero out a float-only block's own contribution. A
    block whose in-flow content is nothing but a float (CSS 2.1 9.5: a
    float doesn't contribute to its containing block's auto-height) still
    got its pre-correction, float-inflated height baked into the marker,
    and so did the owner's own bounding box built from it.

    Patches each marker rect's height back in sync with the block's real,
    final height now that it's known, and re-derives the owner's own
    bounding box from the corrected rects the same way `_finalize_inline_
    owner_boxes` first did -- idempotent, so a block whose height didn't
    actually change after all is simply a no-op. A *nested* split wrapper
    (CSS 2.1 9.2.1.1, see `_split_wrapping_inline_element`'s docstring)
    never appears in `node_map` itself -- reached instead the same way
    `_fix_nested_split_flow_extent` reaches it, via each interruption
    block's own `_chromonic_split_wrapper_ref` back-reference."""
    seen_ids: set = set()
    for node in list(node_map.values()) + [
        node.__dict__.get("_chromonic_split_wrapper_ref")
        for node in node_map.values()
        if _is_element(node) and node.__dict__.get("_chromonic_split_wrapper_ref") is not None
    ]:
        if not _is_element(node) or id(node) in seen_ids:
            continue
        seen_ids.add(id(node))
        owner = node
        marker_positions = owner.__dict__.get("_chromonic_marker_positions")
        rects = owner.__dict__.get("_chromonic_inline_boxes")
        if not marker_positions or rects is None:
            continue
        rects = list(rects)
        changed = False
        for index, blocks in marker_positions:
            if index >= len(rects):
                continue
            boxes = [block.__dict__.get("_layout_box") for block in blocks]
            if any(box is None for box in boxes):
                continue
            total_height = sum(box.height for box in boxes)
            old = rects[index]
            if abs(old[3] - total_height) > 0.01:
                rects[index] = (old[0], old[1], old[2], total_height)
                changed = True
        if not changed:
            continue
        owner.__dict__["_chromonic_inline_boxes"] = rects
        # Same all-degenerate fallback as `_finalize_inline_owner_boxes`
        # (see its own comment) -- kept in sync here since this can be the
        # pass that *makes* every rect degenerate (a float-only marker
        # zeroing out).
        bounding_rects = [r for r in rects if r[2] != 0.0 and r[3] != 0.0] or rects[-1:]
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right - left, height=bottom - top,
            client_width=right - left, client_height=bottom - top,
        )


def _finalize_inline_owner_boxes(owner_accum) -> None:
    """Merge each inline owner's accumulated fragment rects -- gathered
    across every `_InlineFormattingPlan` that published fragments for it
    (CSS 2.1 9.2.1.1's split contributes from multiple independent plans
    belonging to the same owner) -- into its final `_chromonic_inline_
    boxes`/`_layout_box`/`_chromonic_owned_fragments`, once per owner per pass.

    Rects are grouped by split segment (`run["split_group"]`) rather than
    merged as one re-sorted list -- `getClientRects()` preserves document
    order, not a geometric sort.

    CSS 2.1 9.2.1: an inline's line-box fragments cover nested inline
    descendants' content too -- each owner's merged rects are folded into
    every tracked inline ancestor's, deepest owner first."""
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
    # descendants, excluding `key`'s own -- kept separate since only the
    # horizontal extent folds upward, never the vertical.
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
        # CSS 2.1 9.2.1: a wrapping inline's fragments span the full
        # horizontal extent of everything nested inside on that line, but
        # its own height/position come only from its own font metrics --
        # a taller nested child extends visually without growing it or
        # splitting it into extra fragments.
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
        # block interruption (CSS 2.1 9.2.1.1) -- the anonymous block box
        # wrapping the real interrupting block, not the block's own
        # (possibly narrower) box. It's `width:auto`, 100% of `owner`'s
        # containing block, on top of (not merged with) the real leading/
        # trailing fragments, at its logical split position.
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
            # `(index into final_rects, [contributing blocks])` for every
            # marker rect appended below -- `_resync_interruption_marker_
            # heights` needs this to patch each marker's height back in
            # sync once the float-auto-height corrections (which run
            # after this) have given its block(s) their real, final
            # height; a merged marker (two blocks with a skipped, empty
            # segment between them) tracks both.
            marker_positions: list = []
            atomic_segment_elements = getattr(owner, "_chromonic_atomic_segment_elements", None) or {}
            for index, group in enumerate(merged_groups):
                atomic_nodes = atomic_segment_elements.get(group_keys[index])
                # An *interior* segment (between two interruption blocks,
                # never the wrapper's own true leading/trailing one) with
                # no real content of its own -- no text, no atomic element,
                # nothing -- gets no fragment of its own in real Chrome:
                # nothing there ever generated an anonymous inline box to
                # begin with. The leading/trailing 0x0 case is different
                # (kept as-is) -- that one *is* a real, if empty, fragment
                # of the wrapper's own remaining content on that side.
                interior_empty = (
                    not atomic_nodes and 0 < index < len(merged_groups) - 1
                    and len(group) == 1 and group[0][2] == 0.0 and group[0][3] == 0.0
                )
                if atomic_nodes:
                    # This segment's "group" is a zero-sized marker run --
                    # its real content is one or more atomic elements built
                    # as real subtrees; use their already-final boxes instead.
                    for node in atomic_nodes:
                        node_box = node.__dict__.get("_layout_box")
                        if node_box is not None:
                            final_rects.append((node_box.x, node_box.y, node_box.width, node_box.height))
                elif not interior_empty:
                    final_rects.extend(group)
                if index < len(interruption_blocks):
                    block = interruption_blocks[index]
                    block_box = block.__dict__.get("_layout_box")
                    if block_box is not None and container_box is not None:
                        block_y = block_box.y
                        if container is owner:
                            # The direct-child shape: `container` is `owner`
                            # itself, forced to `width:100%` of its own
                            # containing block, so the marker uses the
                            # whole border box, uninset by `owner`'s own
                            # edges (never carried by the anonymous block
                            # box). `block_box.y` still needs the same
                            # border-top correction the text rects got.
                            marker_x = container_box.x
                            marker_width = container_box.width
                            block_y = block_y - self_top
                        else:
                            marker_x = container_box.x + container_box.border_left + cpl
                            marker_width = container_box.client_width - cpl - cpr
                        # CSS 2.1 9.2.1.1's anonymous block box wraps the
                        # real interrupting block, but the marker rect
                        # reports the block's own border box, not a
                        # margin-inflated union -- margin still participates
                        # in block-flow spacing, but isn't part of any
                        # box's own border-box geometry or hit region.
                        marker_rect = (marker_x, block_y, marker_width, block_box.height)
                        # Two markers with a skipped, genuinely-empty
                        # interior segment between them (just above) are
                        # visually contiguous -- nothing (no line box) ever
                        # separated them, so real Chrome reports them as
                        # one merged rect, not two back to back.
                        prev = final_rects[-1] if final_rects else None
                        if (interior_empty and prev is not None
                                and abs(prev[0] - marker_rect[0]) < 0.01
                                and abs(prev[2] - marker_rect[2]) < 0.01
                                and abs(prev[1] + prev[3] - marker_rect[1]) < 0.01):
                            final_rects[-1] = (prev[0], prev[1], prev[2], prev[3] + marker_rect[3])
                            marker_positions[-1][1].append(block)
                        else:
                            final_rects.append(marker_rect)
                            marker_positions.append([len(final_rects) - 1, [block]])
                    elif block_box is not None:
                        final_rects.append((block_box.x, block_box.y, block_box.width, 0.0))
                        marker_positions.append([len(final_rects) - 1, [block]])
            owner.__dict__["_chromonic_inline_boxes"] = final_rects
            owner.__dict__["_chromonic_marker_positions"] = marker_positions
            all_merged = final_rects  # the block interruption also grows getBoundingClientRect()
        else:
            owner.__dict__["_chromonic_inline_boxes"] = all_merged
        # `getBoundingClientRect()` unions every `getClientRects()` rect
        # except zero-width/height ones -- `all_merged`/`final_rects`
        # themselves stay unfiltered (a zero-sized rect is a real fragment
        # there); only the union bounds here drop them. When *every* rect
        # is degenerate (e.g. a float-only interruption block whose marker
        # legitimately collapses to 0 height, CSS 2.1 9.5), real Chrome's
        # own bounding rect isn't a union of their differing positions --
        # confirmed empty (0x0) at the position of the *last* one, not a
        # box spanning from the first to the last.
        bounding_rects = [r for r in all_merged if r[2] != 0.0 and r[3] != 0.0] or all_merged[-1:]
        left = min(r[0] for r in bounding_rects); top = min(r[1] for r in bounding_rects)
        right = max(r[0] + r[2] for r in bounding_rects); bottom = max(r[1] + r[3] for r in bounding_rects)
        owner.__dict__["_layout_box"] = LayoutBox(
            x=left, y=top, width=right-left, height=bottom-top,
            client_width=right-left, client_height=bottom-top,
        )
        owner._chromonic_has_layout_children = True
        # Text-range fragments stay scoped to this owner's own direct text,
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
    every box an element generates -- for a split inline (CSS 2.1
    9.2.1.1), that includes the real block child's own box too. `wrapper`
    (the split inline) is never built as a real Taffy node, so Taffy's own
    `position:relative` handling never sees it -- reapplied by hand here,
    after `_finalize_inline_owner_boxes` has published `wrapper`'s own
    fragment geometry.

    Takes the owners `_finalize_inline_owner_boxes` just published rather
    than walking `node_map` -- a nested split wrapper never appears there
    at all."""
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
    """A nested CSS 2.1 9.2.1.1 split wrapper (never a real Taffy node)
    still gets a `_layout_box` published for it -- the visual union of
    every generated fragment, border/padding decoration included, which
    can be taller than the real vertical space those fragments occupy in
    ordinary block flow (an edge fragment's border can overlap an
    adjoining one). An ancestor's auto-height must not read it directly.

    Computes a second, decoration-free box instead -- the real block-flow
    extent: the interruption blocks' own final top/bottom edges, extended
    by whichever edge fragments contributed real flow height. Ordinary
    sequential stacking, so it can't overlap; `_adjust_body_collapsed_
    margins` prefers this when present."""
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
    applies) doubles as the signal `_apply_root_margin_offset` uses to
    know this pass already accounted for the root's own top margin, so it
    doesn't add that margin a second time -- cleared unconditionally up
    front so a stale value can never survive from an earlier pass.
    """
    root_element.__dict__.pop("_chromonic_scroll_extent", None)
    root_element.__dict__.pop("_chromonic_margin_collapsed", None)
    if getattr(root_element, "_chromonic_tag_name", None) != "body":
        return
    style = getattr(root_element, "_chromonic_native_style", {})
    # `_approximate_inline_flow` may have turned `body` into a `flex-wrap`
    # row standing in for real float layout -- `_chromonic_float_flow_
    # children` marks this as chromonic's own approximation, where margin
    # collapsing still applies as it would to an ordinary block body. A
    # genuine author flexbox body (no such marker) is left alone: real
    # flex containers don't collapse margins with their children.
    if (style.get("display") != "block"
            and getattr(root_element, "_chromonic_float_flow_children", None) is None):
        return
    if any(value not in (0.0, "auto") for name in ("padding", "border")
           for value in style.get(name, ())):
        return
    # CSS 2.1 8.3.1: a block's own top margin collapses with its first
    # in-flow child's (letting the child's margin "escape" outward, which
    # is what this whole correction exists to publish) only when the
    # block doesn't establish a new block formatting context -- and any
    # `overflow` other than `visible` does exactly that, same as a real
    # border/padding already excluded above. Taffy has no such concept at
    # all -- its own raw child position already reflects the child's own
    # margin applied plainly (root nodes carry no margin of their own to
    # Taffy), so simply leaving it alone here and letting `_apply_root_
    # margin_offset` add body's own margin normally on top, unmodified,
    # already gives the correct, uncollapsed result.
    if any(value != "visible" for value in style.get("overflow", ("visible", "visible"))):
        return
    boxes = []
    visible_boxes = []
    for child in root_element.childNodes or []:
        if not _is_element(child):
            continue
        child_style = getattr(child, "_chromonic_native_style", {})
        if child_style.get("position") in ("absolute", "fixed"):
            continue
        # CSS 2.1 10.6.3: an ordinary block's auto height is the distance
        # to its last in-flow child's bottom margin edge -- floats are out
        # of flow for this (plain `<body>` never establishes a BFC, so
        # 10.6.7's float-inclusive algorithm doesn't apply). Taffy has no
        # `float` concept, so an un-flex-rowed floated child would
        # otherwise count fully toward `bottom` like real content.
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and _is_floated(resolved[0]):
            continue
        # A child that dissolved into a CSS 2.1 9.2.1.1 split reports its
        # `_chromonic_flow_extent_box` (the decoration-free flow extent,
        # authoritative and available earlier in the correction pipeline)
        # rather than its own `_layout_box` -- which, for such a child, is
        # not written until `_publish_inline_formatting` runs, several
        # passes after this function. Checking `_layout_box` for `None`
        # first (the previous order) meant this function saw no box at
        # all for a still-mid-split child on its first (pre-publish) call
        # this pass, silently skipped it, and returned without ever
        # setting `_chromonic_margin_collapsed` -- letting `_apply_root_
        # margin_offset` (which runs immediately after) wrongly add the
        # root's own margin a second time on top of Taffy's already-
        # correct collapsed position. Taffy's raw output was never wrong;
        # only this substitution order was. `_chromonic_flow_extent_box`
        # is checked first and preferred for exactly this reason -- only
        # a child with neither is skipped.
        box = getattr(child, "_chromonic_flow_extent_box", None)
        if box is None:
            box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        boxes.append(box)
        # A CSS-empty box (no border/padding/height, no in-flow content --
        # an absolutely-positioned-only wrapper still counts as empty)
        # doesn't stop a preceding margin from collapsing straight through
        # it; counting it toward `bottom` would double-count that margin
        # instead of letting it escape past this empty child. `box.height
        # == 0` alone isn't enough to conclude "no in-flow content" --
        # a negative child margin can legitimately pull a non-empty
        # wrapper's own auto-height back to zero too.
        has_in_flow_content = any(
            _is_element(node) and getattr(node, "_chromonic_resolved_style", None) is not None
            and not _is_absolutely_positioned(node._chromonic_resolved_style[1])
            and not _is_floated(node._chromonic_resolved_style[0])
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
    # sibling's box visually stick out past a later one -- a negative
    # margin can pull an earlier child's own bottom edge past the real
    # last child's, but Chrome still tracks the real last child regardless.
    top = boxes[0].y
    bottom = boxes[-1].y + boxes[-1].height
    old = root_element.__dict__.get("_chromonic_pristine_box") or root_element.__dict__.get("_layout_box")
    # `old` (the pristine, pre-offset box) is only right for the scroll-
    # extent baseline below -- on the second pass `_apply_root_margin_
    # offset` has since shifted the real box horizontally, and rebuilding
    # from the stale pristine `x` would silently discard that shift.
    current = root_element.__dict__.get("_layout_box") or old
    if old is not None:
        # The escaped final margin still contributes to the document's scroll
        # extent even though it is outside body.getBoundingClientRect() --
        # and unlike the rendered height above, the scrollable area *does*
        # need the true max over every child, first/last or not.
        explicit_height = style.get("height") != "auto"
        # Auto-height can go negative when a child's negative margin pulls
        # `bottom` back above `top` -- a used height is never negative
        # (CSS 2.1 8.1/10.5), so this clamps to `0` like Chrome does.
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
            x=current.x, y=top, width=old.width, height=corrected_height,
            client_width=old.client_width, client_height=corrected_client_height,
            border_top=old.border_top, border_left=old.border_left,
        )


def _is_root_anchored(element) -> bool:
    """Whether `element` is `position:absolute`/`fixed` with no positioned
    ancestor -- its containing block is the viewport itself. Shared by
    `_fix_viewport_anchored_positioning` and `_apply_root_margin_offset`
    (which must not shift such elements)."""
    resolved = getattr(element, "_chromonic_resolved_style", None)
    if resolved is None or not _is_absolutely_positioned(resolved[1]):
        return False
    return _find_containing_block_ancestor(element) is None


def _find_containing_block_ancestor(element):
    """The nearest ancestor establishing a real containing block for
    `element`'s absolute/fixed positioning, or `None` if none exists."""
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
    """`(new_y, new_height)` for a root-anchored element, resolved against
    the true `viewport_height` instead of the root's own Taffy box --
    either may be `None`, meaning "leave as Taffy already computed it"."""
    top, _right, bottom, _left = style["inset"]
    top_v = _resolve_inset(top, viewport_height)
    bottom_v = _resolve_inset(bottom, viewport_height)
    margin_top, _mr, margin_bottom, _ml = style["margin"]
    mt = _resolve_inset(margin_top, viewport_height) or 0.0
    mb = _resolve_inset(margin_bottom, viewport_height) or 0.0
    height = style["height"]
    if isinstance(height, tuple) and height[0] == "pct":
        # A percentage height resolves against the containing block's own
        # height regardless of whether `bottom` is also set, unlike the
        # top+bottom-both-set case below.
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
    """`(new_x, new_width)` for a root-anchored element -- the horizontal
    counterpart to `_resolve_viewport_anchored_box`, needed because the
    root's own Taffy box (shrunk for its own margin) isn't always the true
    viewport width either."""
    _top, right, _bottom, left = style["inset"]
    left_v = _resolve_inset(left, viewport_width)
    right_v = _resolve_inset(right, viewport_width)
    _mt, margin_right, _mb, margin_left = style["margin"]
    ml = _resolve_inset(margin_left, viewport_width) or 0.0
    mr = _resolve_inset(margin_right, viewport_width) or 0.0
    width = style["width"]
    if isinstance(width, tuple) and width[0] == "pct":
        # Same opposite-inset-independent resolution as the height branch
        # above -- also catches the synthetic `("pct", 1.0)` `build()`
        # assigns a width:auto block with inline content, which for a
        # root-anchored box must resolve against the true viewport width.
        new_width = width[1] * viewport_width
        if left_v is not None:
            return left_v + ml, new_width
        if right_v is not None:
            return viewport_width - right_v - mr - new_width, new_width
        return None, new_width
    if right_v is None:
        # `left` alone determines position. Unlike the vertical
        # counterpart, this can't just be left as Taffy computed it --
        # `left_v` may be a percentage Taffy resolved against its own
        # (margin-shrunk) root box instead of the true viewport width.
        return (None if left_v is None else left_v + ml), None
    if left_v is None:
        return viewport_width - right_v - mr - box.width, None
    if width != "auto":
        return None, None
    return left_v + ml, viewport_width - left_v - ml - right_v - mr


def _fix_absolute_horizontal_auto_margins(node_map: dict) -> None:
    """CSS 2.1 10.3.7: for a `position:absolute` box whose `left`/`width`/
    `right` are all definite, any `auto` margin absorbs the remaining
    slack of `left + margin-left + width + margin-right + right ==
    containing block width` -- split evenly if both are auto. Taffy's own
    absolute-positioning doesn't solve this, so this corrects the box's
    `x` (and everything inside it) after the fact.

    Only handled when a real containing-block ancestor exists -- a
    root-anchored box is corrected separately by `_fix_viewport_anchored_
    positioning`.

    Also handles exactly one of `left`/`right` being `auto` (CSS 2.1
    10.3.7 case 3/5): any auto margin resolves to `0`, and the missing
    inset is solved from the constraint equation -- auto margins only
    center the box in the fully-constrained case above."""
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
    """CSS 2.1 10.3.3: an in-flow block's `auto` `margin-left`/`margin-
    right` resolves during layout, but Taffy never hands that resolved
    value back to Python (`LayoutBox` defaults both to `0`) -- domonic's
    own `getComputedStyle()` reads `_layout_box.margin_left`/`_right` for
    a declared `auto`, so this derives them from the box's own position
    relative to its parent's content-box edges.

    Narrow, matching CSS 2.1 10.3.3's scope: only ordinary in-flow block
    boxes, excluded when floated/out-of-flow or the parent is flex/grid
    (siblings share the row there). Pure reporting -- never moves a box."""
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
    """CSS 2.1 10.3.7/10.6.4: an absolutely-positioned box with all-`auto`
    insets falls back to its static position -- where it would have
    landed as `position:static`. Taffy has no concept of this (an
    all-auto inset just resolves to `0`, landing the box at its
    containing block's origin).

    Only a reasonably common approximation, not full normal-flow layout:
    the static position is the literal DOM parent's content-box origin
    when there's no earlier in-flow sibling, or (approximating ordinary
    block stacking) directly
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
        if not inset:
            continue
        # CSS 2.1 10.3.7/10.6.4 resolve each axis independently -- `top:
        # 82px; left/right: auto` still needs the *horizontal* static-
        # position fallback even though the vertical position is already
        # correctly pinned by the explicit `top`.
        inset_top, inset_right, inset_bottom, inset_left = inset
        needs_x = inset_left == "auto" and inset_right == "auto"
        needs_y = inset_top == "auto" and inset_bottom == "auto"
        if not needs_x and not needs_y:
            continue
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
            # The static position is where `element` would sit as an
            # ordinary `position:static` box -- pushed down by its own
            # margin-top (collapsing with a preceding sibling handled
            # separately below).
            own_margin = style.get("margin") or (0.0,) * 4
            own_margin_top = _numeric_edge(own_margin[0])
            own_margin_right = _numeric_edge(own_margin[1])
            own_margin_left = _numeric_edge(own_margin[3])
            # The static position is the parent's content-box origin, not
            # its border box.
            parent_pad_top, parent_pad_right, _parent_pad_bottom, parent_pad_left = (
                parent.__dict__.get("_chromonic_padding", (0.0,) * 4))
            # CSS 2.1 10.1: a block-level box's hypothetical static
            # position still stacks top-to-bottom the same regardless of
            # `direction` -- but *where* it would land horizontally, as
            # an ordinary in-flow block, follows the same `direction:rtl`
            # flush-right rule `_fix_rtl_block_positioning` already
            # applies to a real (non-absolute) sibling: flush against the
            # containing block's *right* content edge, not its left. Either
            # way this is still an ordinary block box's own margin box, so
            # its own margin-left/-right (mirroring `static_y`'s own
            # margin-top below) has to push it in from that edge same as
            # any other block -- previously omitted here, unlike the
            # vertical axis, so a `position:absolute` element with `left/
            # right:auto` (falling back to its static position) landed
            # flush against the containing block's padding edge with its
            # own declared margin silently dropped.
            if _element_direction(parent) == "rtl":
                static_x = (parent_box.x + parent_box.border_left
                            + parent_box.client_width - parent_pad_left - parent_pad_right
                            - box.width - own_margin_right)
            else:
                static_x = parent_box.x + parent_box.border_left + parent_pad_left + own_margin_left
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
                # `sibling_box` never includes margin, so the sibling's
                # trailing margin has to be added back explicitly, as the
                # larger of its own margin-bottom and this element's
                # margin-top (ordinary collapsing), not just added alone.
                #
                # A CSS-empty sibling is the one exception: Taffy already
                # resolves its own margin collapsing internally, so
                # `sibling_box.y` is already the fully-collapsed resting
                # position -- adding its raw margin-bottom on top would
                # double-count a margin Taffy already folded in.
                sibling_empty = sibling_box.height == 0 and not any(
                    value not in (0.0, "auto") for name in ("padding", "border")
                    for value in sibling_style.get(name, ())
                )
                if sibling_empty:
                    static_y = sibling_box.y + sibling_box.height + own_margin_top
                else:
                    sibling_margin_bottom = _numeric_edge((sibling_style.get("margin") or (0.0,) * 4)[2])
                    static_y = sibling_box.y + sibling_box.height + max(sibling_margin_bottom, own_margin_top)
        dx = (static_x - box.x) if needs_x else 0.0
        dy = (static_y - box.y) if needs_y else 0.0
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
    """Shift `element` and everything painted inside it by `(dx, dy)` --
    used to carry a corrected element's own position through to its
    descendants, whose boxes Taffy computed as offsets from `element`'s
    own (now-corrected) origin. A uniform shift preserves every internal
    relationship Taffy already got right.

    Skips `element` entirely when it's currently `display:none` -- it was
    never given a real Taffy node this pass, so its stale `_layout_box`
    must not keep being shifted on top of whatever was last published for
    it, or the correction compounds forever across relayouts."""
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
    block is the document root itself.

    Taffy resolves such an element's `top`/`bottom`/`height` insets against
    the root's own Taffy box -- the full document height for a scrollable
    page, not the viewport. CSS's real rule is that such an element
    resolves against the initial containing block, which has the
    viewport's dimensions, not the document's.

    Only called when a caller supplies a real `viewport_height` -- every
    caller that doesn't keeps today's behaviour at zero extra cost.
    `viewport_width`, when given, applies the same correction horizontally."""
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


def _fix_rtl_block_positioning(node_map: dict) -> None:
    """CSS 2.1 10.3.3: for an ordinary in-flow block-level box with
    `width`/`margin-left`/`margin-right` all non-`auto`, the over-
    constrained case ignores the *specified* `margin-right` and solves
    for it in `ltr` (Taffy's own default -- already flush against the
    specified `margin-left`, needing no correction) but ignores
    `margin-left` instead in `rtl`, honoring the real, specified
    `margin-right` and solving for `margin-left` -- which can come out
    smaller, larger, or even negative than whatever was actually written,
    not just `0`. Taffy has no `direction` concept at all, so it always
    positions such a block from a literal, un-recalculated `margin-left`
    regardless; this recomputes that one edge, in both directions,
    exactly as the spec's own formula would.

    Applies both to CSS 2.1 9.2.1.1's split interruption blocks (their
    real containing block is `_chromonic_split_container`, tracked
    separately from the DOM parent a non-replaced inline never
    establishes one of) and to an ordinary, un-split block child (its
    containing block is simply its own `parentElement`)."""
    for element in node_map.values():
        if not _is_element(element):
            continue
        container = element.__dict__.get("_chromonic_split_container")
        if container is None:
            native_self = element.__dict__.get("_chromonic_native_style")
            if native_self is None or native_self.get("position") in ("absolute", "fixed"):
                continue
            if native_self.get("display") != "block":
                continue
            container = getattr(element, "parentElement", None)
            if container is None:
                continue
        # CSS 2.1 10.3.3's ltr/rtl branch is decided by the *containing
        # block's* own `direction` -- the actual block formatting context
        # this block is positioned within -- not a wrapping non-replaced
        # inline's own (it never establishes one itself). A `<span
        # style="direction:ltr">` wrapping a split block inside an outer
        # `direction:rtl` container still positions the block by the
        # outer container's `rtl`, confirmed directly against Chrome.
        if _element_direction(container) != "rtl":
            continue
        native = element.__dict__.get("_chromonic_native_style") or {}
        margin = native.get("margin") or (0.0, 0.0, 0.0, 0.0)
        margin_right = margin[1]
        if not isinstance(margin_right, (int, float)):
            continue  # `margin-right:auto` -- a different CSS 2.1 10.3.3 case, not this one
        box = element.__dict__.get("_layout_box")
        container_box = container.__dict__.get("_layout_box")
        if box is None or container_box is None:
            continue
        _cpt, cpr, _cpb, cpl = container.__dict__.get("_chromonic_padding", (0.0,) * 4)
        content_left = container_box.x + container_box.border_left + cpl
        content_right = content_left + container_box.client_width - cpl - cpr
        target_x = content_right - float(margin_right) - box.width
        delta = target_x - box.x
        if abs(delta) > 0.01:
            _shift_subtree(element, delta, 0.0)


def _element_own_baseline(element) -> "float | None":
    """The offset, from `element`'s own border-box top, of the CSS 2.1
    10.8.1 baseline a `display:flex; align-items:baseline` *row* should
    align it on -- `None` if it has no real in-flow line box at all (the
    spec's own fallback: align on its bottom margin edge instead, exactly
    what Taffy's own baseline algorithm already does unprompted).

    Needed because Taffy's flex baseline alignment only ever looks at a
    node's own reported baseline, which is real for a measured text leaf
    but silently `None` (synthesized from its own bottom edge instead, see
    `taffy::compute::flexbox`) for anything built from further Taffy
    children -- including a CSS 2.1 9.2.1.1 split's own anonymous block
    boxes, whose real text lives several accumulated levels down, never
    reaching Taffy's own baseline search at all. CSS 2.1 10.8.1: the
    baseline is that of the *last* in-flow line box, not the first."""
    box = element.__dict__.get("_layout_box")
    if box is None:
        return None
    plan = getattr(element, "_chromonic_inline_plan", None)
    if plan is not None:
        baselines = getattr(plan, "_line_baselines", None)
        has_content = getattr(plan, "_line_has_content", None)
        if baselines:
            for y in sorted(baselines, reverse=True):
                if has_content is not None and not has_content.get(y):
                    continue
                return y + baselines[y]
        return None
    owners = element.__dict__.get("_chromonic_split_plan_owners")
    if owners:
        for index in sorted(owners, reverse=True):
            owner = owners[index]
            owner_plan = getattr(owner, "_chromonic_inline_plan", None)
            owner_box = owner.__dict__.get("_layout_box")
            if owner_plan is None or owner_box is None:
                continue
            baselines = getattr(owner_plan, "_line_baselines", None)
            has_content = getattr(owner_plan, "_line_has_content", None)
            if not baselines:
                continue
            for y in sorted(baselines, reverse=True):
                if has_content is not None and not has_content.get(y):
                    continue
                return (owner_box.y - box.y) + y + baselines[y]
        return None
    if not _is_element(element):
        # A plain text leaf of the `elif inline_items:` flex-row
        # approximation (`tree.new_text_leaf`, no `_InlineFormattingPlan`
        # of its own) -- its baseline is just its own font's ascent.
        paint_style = getattr(element, "_chromonic_paint_style", None)
        if not paint_style:
            return None
        font_size = _fontmetrics.parse_length(paint_style.get("font_size"), default=16.0)
        family = paint_style.get("font_family") or ""
        if family == "none":
            family = ""
        weight = _parse_font_weight(paint_style.get("font_weight"))
        italic = fonts.is_italic(paint_style.get("font_style"))
        ascent, descent, normal = fonts.text_metrics(family, font_size, weight >= 600, italic)
        resolved_line_height = _resolved_line_height(paint_style.get("line_height"))
        line_height = resolved_line_height if resolved_line_height is not None else (ascent + descent) or normal
        return ascent + math.floor((line_height - (ascent + descent)) / 2)
    return None


def _fix_flex_row_baseline_alignment(node_map: dict) -> None:
    """Correct `display:flex; align-items:baseline` rows built by the
    `elif inline_items:` mixed-text-and-elements flex-row approximation
    (`build()`) -- Taffy's own baseline alignment silently falls back to
    each item's own bottom edge whenever that item isn't a measured text
    leaf itself (see `_element_own_baseline`'s docstring), which is wrong
    for exactly the case this approximation exists to handle: real text
    sharing a row with a nested element (e.g. an `inline-block`) whose own
    text lives several Taffy levels down. Gated on `_chromonic_flex_row_
    members`, set only by that one approximation -- never a real author
    flexbox, whose own explicit `align-items:baseline` must keep Taffy's
    own (here, correct-by-definition) behavior untouched."""
    for element in node_map.values():
        # Not gated on `_is_element`: `_group_inline_element_runs`' own
        # anonymous-block wrapper (`_AnonymousInlineRun`, a pseudo-node
        # with `nodeType` 3, not a real `Element`) sets this attribute too,
        # for a run of consecutive real inline-level element siblings --
        # excluding it here silently skipped that whole case (confirmed on
        # `wpt/css/CSS2/visudet/content-height-001.html`).
        members = element.__dict__.get("_chromonic_flex_row_members") if hasattr(element, "__dict__") else None
        if not members or len(members) < 2:
            continue
        box = element.__dict__.get("_layout_box")
        if box is None:
            continue
        rows: list = []
        current: list = []
        prev_x = None
        for member in members:
            member_box = member.__dict__.get("_layout_box")
            if member_box is None:
                continue
            if prev_x is not None and member_box.x < prev_x - 0.01:
                if len(current) > 1:
                    rows.append(current)
                current = []
            current.append(member)
            prev_x = member_box.x
        if len(current) > 1:
            rows.append(current)
        for row in rows:
            # Taffy's own cross-axis sizing for a custom `MeasureFunc` leaf
            # inside an `align-items:baseline` row doesn't reliably keep
            # that leaf's own correctly-measured `height:auto` result (see
            # `_make_measure`'s own `_chromonic_measured_height` stash, and
            # `_InlineFormattingPlan.height` for a leaf with its own inline
            # plan) -- re-assert it here, before any baseline math below
            # reads a (possibly still-wrong) `member_box.height`.
            for member in row:
                member_native = member.__dict__.get("_chromonic_native_style")
                if member_native is None or member_native.get("height") != "auto":
                    continue
                plan = member.__dict__.get("_chromonic_inline_plan")
                wanted = (plan.height if plan is not None
                          else member.__dict__.get("_chromonic_measured_height"))
                if wanted is None:
                    continue
                member_box = member.__dict__.get("_layout_box")
                if member_box is None:
                    continue
                delta = wanted - member_box.height
                if abs(delta) < 0.01:
                    continue
                member.__dict__["_layout_box"] = dataclasses.replace(
                    member_box, height=member_box.height + delta,
                    client_height=member_box.client_height + delta,
                )
            # Taffy already applied its *own* (here, sometimes wrong)
            # cross-axis baseline offset to every member's current `y` --
            # using that directly as a baseline reference would double-
            # count it. Every flex item starts flush with the row's own
            # cross-start edge before any such offset is added, so the
            # member Taffy already trusted most (the one it moved least,
            # i.e. the smallest current `y`) recovers that shared,
            # un-offset row top.
            entries = [(member, member.__dict__.get("_layout_box")) for member in row]
            row_top = min(member_box.y for _m, member_box in entries)
            baselines = []
            for member, member_box in entries:
                own_baseline = _element_own_baseline(member)
                baselines.append((member, member_box,
                                  own_baseline if own_baseline is not None else member_box.height))
            row_baseline = max(own for _m, _b, own in baselines)
            for member, member_box, own_baseline in baselines:
                target = row_top + (row_baseline - own_baseline)
                delta = target - member_box.y
                if abs(delta) < 0.01:
                    continue
                if _is_element(member):
                    _shift_subtree(member, 0.0, delta)
                else:
                    _shift_box(member, 0.0, delta)


def _apply_root_margin_offset(root_element, node_map: dict) -> None:
    """Shift the whole laid-out tree by the compute root's own margin.

    Taffy's root-compute has no parent context, so a root with `width`/
    `height:auto` correctly shrinks to leave room for its own margin but
    never offsets its own box by it -- the root always comes back at
    `(0, 0)` regardless of margin.

    `chromonic` hands Taffy `<body>` as this root, but CSS-wise `<html>`
    is the real root, and `<body>`'s own margin genuinely offsets it
    within `<html>`'s content box.

    Root-anchored `position:absolute`/`fixed` elements are excluded:
    their containing block is the viewport, unaffected by `<body>`'s
    margin. The vertical axis is skipped when `_adjust_body_collapsed_
    margins` already folded the root's top margin into its position."""
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
    # `<html>`'s own padding/border offsets `<body>` within it the same
    # way `<body>`'s own margin does -- same root-anchored exclusion applies.
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
        # A root-anchored element's own containing block is the viewport,
        # unaffected by `<body>`'s margin -- and so is everything painted
        # inside it, not just its own box, so the whole ancestor chain
        # must be checked, not just `owner` itself.
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

    `reuse_styles=False` (the default) re-resolves every element's CSS
    from scratch. Pass `reuse_styles=True` only when nothing about any
    element's class/inline style/stylesheets could have changed since the
    last `layout()` call (see `_describe`'s docstring) -- currently only
    `native_browser.py`'s image-arrival poll qualifies.

    `viewport_height`, when given, corrects `position:absolute`/`fixed`
    elements with no positioned ancestor to resolve against the real
    viewport height rather than `root_element`'s own (possibly content-
    grown) box. Leave `None` for callers with no separate viewport-vs-
    document distinction."""
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
    point that computes real Taffy geometry (`layout()`, `LayoutProjection.
    layout()`/`.compute()`) -- previously duplicated verbatim across all
    three, which is how fixes wired into only one of them silently never
    ran for a real, incrementally-updated page."""
    # `_adjust_body_collapsed_margins` runs twice in this pass -- its
    # `_chromonic_scroll_extent` needs Taffy's real, uncorrected box as its
    # baseline, which the second call would otherwise only see already
    # corrected (and smaller). Stashed once, before either call.
    root_element.__dict__["_chromonic_pristine_box"] = root_element.__dict__.get("_layout_box")
    _fix_nested_split_flow_extent(node_map)
    _adjust_body_collapsed_margins(root_element)
    _apply_root_margin_offset(root_element, node_map)
    _fix_flex_row_baseline_alignment(node_map)
    _fix_rtl_block_positioning(node_map)
    # Before the absolute-positioning fixups below: an inline-context
    # escapee's real static position is only known once `_InlineFormatting
    # Plan.publish()` has run -- `_fix_absolute_static_position_fallback`
    # reads `element._chromonic_static_position`, which this sets.
    _publish_inline_formatting(node_map)
    _apply_linebox_strut_height(node_map)
    _apply_empty_inline_block_min_height(node_map)
    _fix_float_shrink_to_fit_width(tree_obj, node_map)
    _fix_table_shrink_to_fit_width(tree_obj, node_map)
    _fix_float_flow_after_block_sibling(node_map)
    _fix_float_flow_container_auto_height(node_map)
    _fix_nested_bfc_float_auto_height(node_map)
    _resync_interruption_marker_heights(node_map)
    # A nested split wrapper's own `_layout_box` doesn't exist until
    # `_publish_inline_formatting` (just above) unions its fragments --
    # this pass's first run, before that, silently skipped every such
    # wrapper, and the interruption blocks' own boxes it reads have since
    # moved (`_apply_root_margin_offset`) -- recomputed fresh now that
    # both are finally real and final, or `_adjust_body_collapsed_margins`
    # below reads a stale, pre-offset `_chromonic_flow_extent_box`.
    _fix_nested_split_flow_extent(node_map)
    # Re-anchor body's own auto-height now that a float-flow BFC child's
    # height may have just shifted -- idempotent, so this re-derives it
    # from the now-final positions instead of the stale ones above.
    _adjust_body_collapsed_margins(root_element)
    _fix_absolute_horizontal_auto_margins(node_map)
    _fix_absolute_static_position_fallback(node_map)
    if viewport_height is not None:
        _fix_viewport_anchored_positioning(node_map, viewport_height, width)
    _publish_used_horizontal_margins(node_map)
    return node_map
