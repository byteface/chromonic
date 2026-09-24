from __future__ import annotations

import math
import re

from domonic import _fontmetrics
from domonic.layout import LayoutBox, LayoutStyle, Length, _parse_length_or_percent

from .. import fonts, style_bridge
from .._native import layout_text
from . import anonymous_boxes, box_model, dom, inline_finalize




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
        self.rtl = dom._element_direction(element, computed) == "rtl"
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

    def measure(self, available_width, _available_height, _known_width=None, _known_height=None):
        width = float(available_width or 0.0)
        if width < 0:
            # `src/lib.rs`'s `MinContent` sentinel: lay out at (almost)
            # zero width so every break opportunity is taken and the
            # reported `content_width` is the widest unbreakable piece.
            width = 1.0
        elif width <= 0 or width > 1_000_000:
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
                # was and move on, unlike a forced line-break above. A
                # block-level escapee (CSS 2.1 10.3.7: its static position
                # is where a block would have started -- on the line
                # after the current one, at the line's start) goes below
                # the line in progress (abspos-007.xht: `<div class="test">`
                # between text and a block, at x 8 / y 26, not at the text's
                # end); an inline-level one sits where the text is.
                tag = (getattr(run["element"], "tagName", "") or "").lower()
                if tag in box_model._USUALLY_INLINE_TAGS:
                    self._escapee_positions[id(run["element"])] = (x, y)
                else:
                    self._escapee_positions[id(run["element"])] = (
                        0.0, y + (above + below) if last_real_run is not None else y)
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
        if last_real_run is None and any(run.get("break") for run in self.runs):
            # Nothing placed after the final forced break: the line it
            # opened holds no content and collapses (CSS 2.1 9.4.2) --
            # `a<br>` is one line, `a<br><br>` two, and a `<br>` alone
            # still one (the break sits on the line it ends). Chrome on
            # table-height-algorithm-004.xht: ten `X<br />` lines make a
            # 200px cell, not 220.
            self.height = y
        else:
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
                    margin_end = box_model._numeric_edge(owner_margin[1])
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
                fragment = anonymous_boxes._AnonymousTextFragment(run["source"], self.element)
                fragment.owner = run["owner"]
                fragment._chromonic_paint_style = run["paint_style"]
                fragment._chromonic_text_lines = [text]
                fragment._chromonic_text_line_widths = [visual_width]
                fragment._chromonic_line_height = glyph_height
                # CSS 2.1 9.4.3: a `position: relative` inline (or inline
                # ancestor) moves its fragments by its offsets, layout
                # otherwise untouched (position-relative-002.xht: a
                # `top: 25px` span's text sits 25px below its line).
                rel_dx, rel_dy = inline_finalize._inline_relative_offset(run["owner"], self.element, box)
                fragment._layout_box = LayoutBox(
                    x=origin_x + x + rel_dx, y=origin_y + glyph_y + rel_dy,
                    width=visual_width, height=glyph_height,
                    client_width=visual_width, client_height=glyph_height,
                )
                grouped[key] = fragment
                self.fragments.append(fragment)
            else:
                entry._chromonic_text_lines[0] += text
                old = entry._layout_box
                rel_dx, _rel_dy = inline_finalize._inline_relative_offset(run["owner"], self.element, box)
                combined_width = origin_x + x + rel_dx + visual_width - old.x
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
            rel_dx, rel_dy = inline_finalize._inline_relative_offset(owner, self.element, box)
            rect = (origin_x + x - leading + rel_dx, owner_y + rel_dy,
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
                # The box is the `<br>`'s own inline box -- its font's
                # glyph height (ascent + descent), sat on the line's
                # baseline -- not the line box: Chrome reports a `<br>` in
                # `font: 20px/1 serif` as 23px tall starting 2px above its
                # 20px line (separated-border-model-004a.xht).
                br_paint = getattr(run["element"], "_chromonic_paint_style", None) or self.element._chromonic_paint_style
                br_size = _fontmetrics.parse_length(br_paint.get("font_size"), default=16.0)
                br_family = "" if br_paint.get("font_family") in (None, "none") else br_paint.get("font_family")
                br_ascent, br_descent, _normal = fonts.text_metrics(
                    br_family, br_size, _parse_font_weight(br_paint.get("font_weight")) >= 600,
                    fonts.is_italic(br_paint.get("font_style")))
                br_height = br_ascent + br_descent
                br_top = br_y + self._line_baselines.get(br_y, br_ascent) - br_ascent
                run["element"].__dict__["_layout_box"] = LayoutBox(
                    x=origin_x + br_x, y=origin_y + br_top,
                    width=0.0, height=br_height,
                    client_width=0.0, client_height=br_height,
                )
                run["element"]._chromonic_has_layout_children = False
        # Same accumulate-not-overwrite reasoning as `owner_accum` above,
        # for `self.element`'s own painted fragments.
        elem_entry = element_fragments_accum.get(id(self.element))
        if elem_entry is None:
            elem_entry = element_fragments_accum[id(self.element)] = (self.element, [])
        elem_entry[1].extend(self.fragments)



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



# CSS 2.1 16.6.1 whitespace collapsing only touches ASCII space/tab/newline/
# CR/form-feed, never U+00A0 (nbsp) -- unlike Python's own `str.strip()`/`\s`,
# which treats nbsp as whitespace too and would collapse an nbsp-only node away.
_CSS_COLLAPSIBLE_WHITESPACE_RE = re.compile(r"[ \t\n\r\f]+")

_CSS_WHITESPACE_STRIP_CHARS = " \t\n\r\f"



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
    # The normalized view (`anonymous_boxes._normalized_child_nodes`): text a CSS 2.1
    # 9.2.1.1 anonymous block took over is no longer this element's own,
    # and an anonymous inline-table (17.2.1) generated around loose cells
    # stands in for them as one inline-level item.
    child_nodes = (element.__dict__.get("_chromonic_normalized_children")
                   if hasattr(element, "__dict__") else None)
    if child_nodes is None:
        child_nodes = dom._child_nodes(element)
    has_direct_text = any(
        getattr(node, "nodeType", None) == dom.TEXT_NODE and dom._collapsed_text_node(node).strip()
        for node in child_nodes
    )
    # Also qualifies when all children are inline-level (or <br>), even with
    # no text anywhere -- CSS 2.1 9.2.1.1/10.8: a genuinely empty inline
    # still contributes its own line-box height/baseline, width 0.
    def _child_qualifies(child, style_obj) -> bool:
        return (
            box_model._is_inline_level(child, style_obj)
            or box_model._is_absolutely_positioned(style_obj)
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
    # CSS 2.1 9.5: a float doesn't break an inline formatting run either --
    # like an absolutely positioned child, it's out of flow -- but *only*
    # once real direct text already earned this container a plan above
    # (`box_model._is_floated`, unlike the other `_child_qualifies` cases, is never
    # consulted for `has_inline_only_children`: a *pure*-float sibling
    # group with no text at all must keep going through the older,
    # more capable `elif children:` -> `_approximate_inline_flow` ->
    # `_fix_float_flow_after_block_sibling` block-packing path exactly as
    # before -- confirmed directly on `zero-available-space-float-
    # positioning.html`, two floats and no text, regressed by this
    # exact function accepting them here too). With real text present,
    # though, rejecting the container outright loses every text sibling
    # to that same fallback, which has no notion of direct text at all --
    # found on any `<p>text <img style="float:left"> more text</p>`-
    # shaped fixture throughout `CSS2/floats`: the *entire paragraph's*
    # text silently vanished, not just the float's own position.
    if any(not (_child_qualifies(child, style_obj) or (has_direct_text and box_model._is_floated(computed)))
           for child, computed, style_obj in children):
        return None
    items = []
    pending_space = False
    previous_was_element = False
    # CSS 2.1 16.6.1: whitespace at the very *start* of a block's content
    # collapses to nothing, same as at a line's start -- only *interior*
    # whitespace (between two real pieces of content) collapses to a
    # single space. `pending_space` must therefore never fire until real
    # content has actually been emitted (`has_content`); otherwise a
    # purely-formatting leading newline/indentation before this
    # container's first child (`<div>\n  <span>...`, extremely common in
    # any hand-formatted or templated HTML) would wrongly manufacture a
    # visible leading space on that first child. Confirmed directly on
    # `position-relative-003.xht`: `<div>\n<span>Filler Text</span>\n
    # </div>` measured the span 4px too wide, a spurious leading space
    # baked into its own first (and only) run.
    has_content = False
    for node in child_nodes:
        if getattr(node, "nodeType", None) == dom.TEXT_NODE:
            text = dom._collapsed_text_node(node)
            if text:
                fragment = getattr(node, "_chromonic_fragment", None)
                if fragment is None:
                    fragment = anonymous_boxes._AnonymousTextFragment(node, element)
                    node._chromonic_fragment = fragment
                # Only a real (collapsed-away) whitespace node earns the
                # leading space -- a text node butted right up against
                # the preceding inline element has none (CSS 2.1 16.6.1;
                # column-visibility-004.xht's `<span>F</span>P` is "FP",
                # 200px in Ahem, not "F P").
                fragment._chromonic_leading_collapsed_space = has_content and pending_space
                items.append(("text", fragment, text, None, None))
                pending_space = False
                previous_was_element = False
                has_content = True
            elif (getattr(node, "textContent", "") or ""):
                pending_space = True
        elif id(node) in by_id:
            child, computed, style_obj = by_id[id(node)]
            if (getattr(child, "tagName", "") or "").lower() == "br":
                items.append(("break", child, None, computed, style_obj))
                pending_space = False
                previous_was_element = False
                # A forced line break starts a fresh line -- whitespace
                # right after it collapses to nothing too, not a real
                # space, the same as at this container's own start.
                has_content = False
            else:
                # Mirrors the text-fragment case just above: a whitespace-
                # only text node collapses to nothing of its own (no run
                # ever represents it), so the fact a space belongs *before*
                # this element has to be carried forward the same way, or
                # it's lost entirely once this element flattens its own
                # content into runs below. Confirmed directly: `<a>Blue
                # stein</a> <a>1 hour ago</a>` (a plain space between two
                # adjacent elements) rendered as "Bluestein1 hour ago",
                # the space silently dropped.
                if box_model._is_absolutely_positioned(style_obj):
                    # Out of flow: invisible to whitespace collapsing --
                    # neither content that makes a following space real
                    # nor something a pending space attaches to (height-
                    # width-inline-table-001.xht: an inline-table after an
                    # absolutely positioned div and a newline starts at
                    # the line's start, no 4px space).
                    child._chromonic_leading_collapsed_space = False
                    items.append(("element", child, None, computed, style_obj))
                    continue
                child._chromonic_leading_collapsed_space = has_content and pending_space
                items.append(("element", child, None, computed, style_obj))
                pending_space = False
                previous_was_element = True
                has_content = True

    def pseudo_item(which, info):
        pseudo_computed, text = info
        pseudo = dom._get_pseudo_object(element, which)
        pseudo.text = text
        pseudo_style_obj = LayoutStyle.from_computed(pseudo_computed)
        # A synthetic pseudo never goes through `dom._describe()` (no real DOM
        # node to resolve a `ComputedStyleDeclaration` for), so its paint
        # style must be set explicitly here, `@font-face` substitution included.
        pseudo._chromonic_paint_style = dom._extract_paint_style(pseudo_computed)
        pseudo._chromonic_computed_style = pseudo_computed
        from .. import webfonts
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
        # Never the parent's own basis: a table cell's is its whole
        # column width, and a text fragment inheriting it took a full
        # line to itself, stacking a cell's `<img/>B<img/>Y...` content
        # one item per line (border-conflict-element-001d.xht).
        "flex_basis": "auto",
    })
    return style



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
    if any(box_model._numeric_edge(v) != 0.0 for name in ("border", "padding") for v in native.get(name, ())):
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



def _make_collapsed_space_run(paint_style, owner, top_edge_val: float, extra_height: float) -> dict:
    """A standalone run for a whitespace-only text node that collapsed to
    nothing of its own but still needs to occupy a real (wrappable,
    collapsible-at-line-end) slot between two elements -- built the same
    way an ordinary text run is, just for a single space token, and kept
    as its *own* run rather than merged into a neighboring element's own
    run. Merging it into the neighbor's first token was tried first and
    is exactly as wide, but confirmed wrong directly on `inline-
    formatting-context-013.xht`: when that neighbor's own content didn't
    fit on the space's line and had to wrap, the merged token pair wrapped
    as a unit strangely, leaving a stray zero-width space fragment
    dangling on the *previous* line real Chrome never reports at all. A
    standalone run collapses at a wrap boundary the same well-tested way
    any other adjacent whitespace-only run already does elsewhere in this
    file, instead of taking on a new, untested code path."""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    resolved_lh = _resolved_line_height(paint_style["line_height"])
    used_lh = resolved_lh if resolved_lh is not None else normal_height
    above = ascent + math.floor((used_lh - glyph_height) / 2)
    below = used_lh - above
    one_w = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
    spaced_w = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
    space_width = max(0.0, spaced_w - 2.0 * one_w)
    return {
        "source": owner, "owner": owner, "paint_style": paint_style,
        "font_size": font_size, "tokens": [(" ", space_width)],
        "leading": 0.0, "trailing": 0.0, "box_height": glyph_height + extra_height,
        "glyph_height": glyph_height, "ascent": ascent,
        "above": above, "below": below, "top_edge": top_edge_val,
        "space_width": space_width, "atomic_width": 0.0,
        "margin_start": 0.0, "margin_end": 0.0, "intrinsic_width": space_width,
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
        if node_type == dom.TEXT_NODE:
            return bool(dom._collapsed_text_node(node).strip())
        if not dom._is_element(node):
            return False
        if (getattr(node, "tagName", "") or "").lower() == "br":
            return False
        if computed_cache is not None:
            _node_computed, node_style_obj = dom._describe(node, computed_cache)
            if box_model._is_absolutely_positioned(node_style_obj):
                return False  # an escapee -- out of flow, no text-bearing slot
        return bool((getattr(node, "textContent", "") or "").strip())

    text_node_indices = [i for i, n in enumerate(child_nodes) if is_text_bearing(n)]
    if not text_node_indices:
        return []
    runs = []
    # Whitespace collapses across sibling-node boundaries the same way it
    # does across run boundaries in `_make_inline_formatting_plan`'s own
    # top-level text handling (`raw[:1]`/`raw[-1:]`) -- but unlike that
    # one, every text run built below unconditionally strips *both* ends
    # of its own text (`t.strip(...)`), with no equivalent restoration.
    # `pending_space` carries a boundary space forward (from a text node
    # that collapsed to nothing of its own, or a real node's own trailing
    # whitespace) to whatever the *next* run turns out to be -- a plain
    # text token, or a nested element's own first run. Confirmed directly
    # rendering `news.ycombinator.com`: a subtext line's own `<span class=
    # "subline">` wrapper (nested one level inside `<td>`, so its content
    # is flattened here, not via that other, already-correct top-level
    # path) rendered `Bluestein2 hours ago` -- an `<a>` immediately
    # followed by a `<span>` with no space token anywhere between them.
    pending_space = False
    # Same CSS 2.1 16.6.1 reasoning as `_inline_mixed_content`'s own
    # `has_content` guard: whitespace collapses to nothing at the very
    # *start* of this content (never to a real space), so neither a prior
    # sibling's trailing whitespace nor this node's own leading whitespace
    # should manufacture a leading-space token until something real has
    # already been emitted.
    has_content = False
    for node_index, child_node in enumerate(child_nodes):
        node_tag = (getattr(child_node, "tagName", "") or "").lower()
        if getattr(child_node, "nodeType", None) == dom.TEXT_NODE:
            node_raw = getattr(child_node, "textContent", None)
            if node_raw is None:
                node_raw = getattr(child_node, "data", "") or ""
            raw_text = dom._collapsed_text_node(child_node)
            if not raw_text:
                if node_raw:
                    pending_space = True
                continue
            has_leading_space = has_content and (pending_space or bool(
                node_raw[:1] and node_raw[:1] in _CSS_WHITESPACE_STRIP_CHARS))
            has_trailing_space = bool(node_raw[-1:] and node_raw[-1:] in _CSS_WHITESPACE_STRIP_CHARS)
            pending_space = False
            has_content = True
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            run_leading = leading_edge if is_first_text else 0.0
            run_trailing = trailing_edge if is_last_text else 0.0
            t = dom._apply_text_transform(
                _CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw_text),
                paint_style.get("text_transform"),
            )
            if not t.strip(_CSS_WHITESPACE_STRIP_CHARS):
                continue
            t = ((" " if has_leading_space else "")
                 + t.strip(_CSS_WHITESPACE_STRIP_CHARS)
                 + (" " if has_trailing_space else ""))
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
            pending_space = False
            # A forced line break starts a fresh line the same way the
            # whole container's own start does -- whitespace right after
            # it collapses to nothing too, not to a real space.
            has_content = False
            runs.append({"break": True, "element": child_node})
        elif dom._is_element(child_node) and computed_cache is not None:
            child_computed, child_style = dom._describe(child_node, computed_cache)
            if box_model._is_absolutely_positioned(child_style):
                # Out of flow -- doesn't occupy an inline-content slot of
                # its own, so any space pending before it isn't consumed
                # here; it still belongs before whatever real content
                # comes next.
                runs.append({"escapee": True, "element": child_node,
                             "computed": child_computed, "style": child_style})
                continue
            non_br_element_children = [
                node for node in dom._child_nodes(child_node)
                if dom._is_element(node) and (getattr(node, "tagName", "") or "").lower() != "br"
            ]
            if non_br_element_children and not _is_genuine_inline_wrapper(child_node, child_style):
                # A nested block, `inline-block`, or replaced descendant
                # needs its own dedicated treatment (the CSS 2.1 9.2.1.1
                # split, or an atomic box with real intrinsic sizing) --
                # this function only ever flattens plain text into runs,
                # so it has no way to represent one and silently drops it,
                # same as it already did for a one-level-nested one (a bare
                # replaced/block element directly here, with no element
                # children of its own, always recursed into a trivially-
                # empty call and vanished the same way). Same reasoning as
                # the escapee case just above: dropped, not consumed, so a
                # pending space still belongs before whatever comes next.
                continue
            needs_leading_space = has_content and pending_space
            pending_space = False
            has_content = True
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            nested_native = style_bridge.to_dict(child_style)
            nested_left = box_model._numeric_edge(nested_native["padding"][3]) + box_model._numeric_edge(nested_native["border"][3])
            nested_right = box_model._numeric_edge(nested_native["padding"][1]) + box_model._numeric_edge(nested_native["border"][1])
            nested_top = box_model._numeric_edge(nested_native["padding"][0]) + box_model._numeric_edge(nested_native["border"][0])
            nested_extra = (nested_top + box_model._numeric_edge(nested_native["padding"][2])
                             + box_model._numeric_edge(nested_native["border"][2]))
            nested_margin_left = box_model._numeric_edge(nested_native["margin"][3])
            nested_margin_right = box_model._numeric_edge(nested_native["margin"][1])
            child_node.__dict__["_chromonic_flattened_inline"] = True  # see `_is_flattened_inline`
            nested_runs = _build_text_runs_from_nodes(
                list(dom._child_nodes(child_node)), child_node._chromonic_paint_style, child_node,
                leading_edge=leading_edge if is_first_text else 0.0,
                trailing_edge=trailing_edge if is_last_text else 0.0,
                top_edge_val=top_edge_val + nested_top,
                extra_height=extra_height + nested_extra,
                margin_start=margin_start if is_first_text else 0.0,
                margin_end=margin_end if is_last_text else 0.0,
                computed_cache=computed_cache,
            )
            if needs_leading_space and nested_runs:
                runs.append(_make_collapsed_space_run(paint_style, owner, top_edge_val, extra_height))
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
                is_rtl_nested = dom._element_direction(child_node, child_computed) == "rtl"
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
        tag_name not in box_model._REPLACED_OR_CONTROL_TAGS
        and getattr(style_obj.display, "value", "") == "inline"
        and box_model._trusts_computed_inline(node, tag_name)
    )



def _contains_in_flow_block(element, computed_cache) -> bool:
    """Whether `element`'s subtree contains a genuine in-flow, block-level
    descendant reachable by walking only inline-level elements -- the CSS
    2.1 9.2.1.1 "anonymous block box" split trigger.

    `select`/`svg` are never walked into, matching `build()`'s own
    treatment of their real children as not real layout content.

    Walks the *normalized* children: loose cells inside this inline are
    already wrapped in one anonymous inline-table (CSS 2.1 17.2.1), an
    atomic inline-level box -- not the block-level cells themselves,
    which read as a split trigger and broke the inline around each one
    (table-anonymous-objects-177.xht)."""
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name in ("select", "svg", "svg:svg"):
        return False
    for node in anonymous_boxes._normalized_child_nodes(element, computed_cache):
        if not dom._is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in dom._NON_RENDERING_TAGS:
            continue
        child_computed, child_style = dom._describe(node, computed_cache)
        if not dom._renders(child_style):
            continue
        if box_model._is_absolutely_positioned(child_style) or box_model._is_floated(child_computed):
            continue
        if not box_model._is_inline_level(node, child_style):
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
    for node in anonymous_boxes._normalized_child_nodes(element, computed_cache):
        if not dom._is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in dom._NON_RENDERING_TAGS:
            continue
        child_computed, child_style = dom._describe(node, computed_cache)
        if not dom._renders(child_style):
            continue
        if box_model._is_absolutely_positioned(child_style) or box_model._is_floated(child_computed):
            continue
        if not box_model._is_inline_level(node, child_style):
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
    for node in anonymous_boxes._normalized_child_nodes(element, computed_cache):
        if not dom._is_element(node):
            continue
        tag = (getattr(node, "tagName", "") or "").lower()
        if tag == "br" or tag in dom._NON_RENDERING_TAGS:
            continue
        child_computed, child_style = dom._describe(node, computed_cache)
        if not dom._renders(child_style):
            continue
        if box_model._is_absolutely_positioned(child_style) or box_model._is_floated(child_computed):
            continue
        if not box_model._is_inline_level(node, child_style):
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
    wrapper_computed, wrapper_style_obj = dom._describe(wrapper, computed_cache)
    native = style_bridge.to_dict(wrapper_style_obj)
    wrapper._chromonic_native_style = native
    left_edge = box_model._numeric_edge(native["padding"][3]) + box_model._numeric_edge(native["border"][3])
    right_edge = box_model._numeric_edge(native["padding"][1]) + box_model._numeric_edge(native["border"][1])
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
    is_rtl = dom._element_direction(wrapper, wrapper_computed) == "rtl"
    top_edge_val = box_model._numeric_edge(native["padding"][0]) + box_model._numeric_edge(native["border"][0])
    extra_height = (top_edge_val + box_model._numeric_edge(native["padding"][2])
                    + box_model._numeric_edge(native["border"][2]))
    margin_left = box_model._numeric_edge(native["margin"][3])
    margin_right = box_model._numeric_edge(native["margin"][1])
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
    for node in dom._child_nodes(wrapper):
        if dom._is_element(node):
            tag = (getattr(node, "tagName", "") or "").lower()
            if tag in dom._NON_RENDERING_TAGS:
                continue
            if tag != "br":
                child_computed, child_style = dom._describe(node, computed_cache)
                if not dom._renders(child_style):
                    continue
                # CSS 2.1 9.2.1.1 only ever applies to an *in-flow* block
                # child -- a `float:left`/`right` one is out of flow (still
                # computed block-level, per 9.7's blockification, but never
                # forces the wrapper to split around it): it stays ordinary
                # segment content instead, built as its own atomic subtree
                # below, the same way an `inline-block` already is.
                if (not box_model._is_absolutely_positioned(child_style) and not box_model._is_floated(child_computed)
                        and not box_model._is_inline_level(node, child_style)):
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
                (node,) + dom._describe(node, computed_cache)
                for node in seg_nodes if dom._is_element(node)
            ]
            if atomic_candidates and all(
                (box_model._is_inline_level(node, node_style) and not _is_genuine_inline_wrapper(node, node_style))
                or box_model._is_floated(node_computed)
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
            plan = _make_inline_formatting_plan(element, list(pending), style, css_display, computed_cache,
                                                allow_escapees=True)
            if plan is not None:
                pieces.append(("plan", plan))
            pending.clear()

    for kind, item, text, child_computed, child_style in inline_items:
        if (kind == "element" and not box_model._is_absolutely_positioned(child_style)
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
                pending_plan = _make_inline_formatting_plan(element, list(pending), style, css_display, computed_cache)
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



def _make_inline_formatting_plan(element, inline_items, style, css_display, computed_cache=None,
                                 allow_escapees: bool = False):
    """Build styled text runs for a shared inline formatting context.
    `allow_escapees`: a top-level absolutely positioned item becomes an
    "escapee" marker run (its static position) instead of making this
    function bail to the flex-row fallback -- the CSS 2.1 9.2.1.1 split
    path has no such fallback for a segment (abspos-029.html: an abs div
    alone between two blocks inside an inline span was simply dropped)."""
    if any(kind == "element" and (
            (box_model._is_absolutely_positioned(child_style) and not allow_escapees)
            # CSS 2.1 9.5: a float, like an abs-pos item, is out of flow --
            # but unlike abs-pos this plan has no escapee/static-position
            # handling for one (real float positioning needs the
            # block-level packer, `_fix_float_flow_after_block_sibling`-
            # style logic, not a text-flow static position) -- always
            # bails to the `elif inline_items:` flex-row fallback, which
            # `build()` gives its own float handling
            # (`_chromonic_inline_floats`/`_fix_inline_float_position`).
            or box_model._is_floated(child_computed)
            or isinstance(item, dom._PseudoElement)
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
            or getattr(child_style.display, "value", "") in (
                "inline-block", "inline-flex", "inline-grid", "-webkit-inline-flex")
            # An `inline-table` (CSS 2.1 17.4) -- a real element's, or a
            # CSS 2.1 17.2.1 anonymous one generated around loose cells
            # inside this inline (`_AnonymousTableBox`) -- is exactly as
            # atomic: one box, laid out by its own table algorithm.
            # Flattened as if it were a plain inline, its cells (never
            # inline-level themselves) read as in-flow blocks that split
            # this inline around them, one full-width row per cell
            # (table-anonymous-objects-177.xht).
            or getattr(child_style.display, "value", "") == "inline-table"
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
            # (see `box_model._USUALLY_INLINE_TAGS` picking up `img`/`canvas`/`svg`/
            # `iframe`).
            or (getattr(item, "tagName", "") or "").lower() in box_model._REPLACED_OR_CONTROL_TAGS)
           for kind, item, _text, child_computed, child_style in inline_items):
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
        if kind == "element" and box_model._is_absolutely_positioned(child_style):
            runs.append({"escapee": True, "element": item, "computed": child_computed, "style": child_style})
            continue
        if kind == "text":
            source = item.source
            owner = element
            paint_style = element._chromonic_paint_style
            native = None
        else:
            # Nested markup is flattened into this plan when it contains
            # text and `<br>` forced line-breaks -- and, recursively, any
            # further plain (`_is_genuine_inline_wrapper`) inline nesting
            # too (`_build_text_runs_from_nodes`, called below, recurses
            # into each non-`<br>` element child the same way). A nested
            # block, `inline-block`, or replaced descendant still can't be
            # represented as a flattened text run, so it's silently
            # dropped rather than bailing this whole plan to the flex-row
            # fallback -- confirmed directly that fallback mismeasures
            # mixed text + nested-inline content (real Chrome vs chromonic
            # on `news.ycombinator.com`'s subtext line, `<span class="age">
            # <a>1 hour ago</a></span>` sitting among plain text and other
            # `<a>`s: the whole line was scrambled -- the nested `<a>`'s
            # own text wrapped internally into two lines and landed out of
            # DOM order relative to its siblings).
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
            left_edge = (box_model._numeric_edge(native["padding"][3])
                         + box_model._numeric_edge(native["border"][3]))
            right_edge = (box_model._numeric_edge(native["padding"][1])
                          + box_model._numeric_edge(native["border"][1]))
            top_edge_val = (box_model._numeric_edge(native["padding"][0])
                            + box_model._numeric_edge(native["border"][0]))
            extra_height = (top_edge_val
                            + box_model._numeric_edge(native["padding"][2])
                            + box_model._numeric_edge(native["border"][2]))
            # margin-left/-right apply before the first/after the last LTR
            # fragment only; CSS 2.1 10.3.1/10.3.3: real spacing, but never
            # part of either fragment's own rect, and -- unlike a block's
            # vertical margins -- never collapses with an adjoining
            # element's own margin (`_InlineFormattingPlan.measure()`'s own
            # `margin_end` handling adds both sides independently).
            margin_start = box_model._numeric_edge(native["margin"][3])
            margin_end = box_model._numeric_edge(native["margin"][1])
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
            is_rtl_item = dom._element_direction(item, child_computed) == "rtl"
            # Flattened into this plan: no Taffy box of its own this pass
            # (`build()` clears the mark when it does build the element).
            item.__dict__["_chromonic_flattened_inline"] = True
            child_runs = _build_text_runs_from_nodes(
                list(dom._child_nodes(item)), item._chromonic_paint_style, item,
                leading_edge=0.0 if is_rtl_item else left_edge,
                trailing_edge=0.0 if is_rtl_item else right_edge,
                top_edge_val=top_edge_val, extra_height=extra_height,
                margin_start=0.0 if is_rtl_item else margin_start,
                margin_end=0.0 if is_rtl_item else margin_end,
                computed_cache=computed_cache,
            )
            leading_space_run = None
            if getattr(item, "_chromonic_leading_collapsed_space", False) and child_runs:
                # A whitespace-only text node right before this element
                # collapsed to nothing of its own (see where this flag is
                # set, in `_inline_mixed_content`) -- restore it as its
                # own standalone run, owned by this shared plan's own
                # `element` (the same as any other collapsed-whitespace
                # text node would be), the same collapsed space a plain
                # text item's own `inferred_leading` (just below) restores
                # for itself. Kept as a *separate* run rather than merged
                # into this element's own first token -- confirmed wrong
                # directly on `inline-formatting-context-013.xht`: merged,
                # a wrap point landing between the space and this
                # element's own content left a stray zero-width space
                # fragment on the previous line real Chrome never reports.
                leading_space_run = _make_collapsed_space_run(
                    element._chromonic_paint_style, element, top_edge_val, extra_height)
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
            if (not child_runs and not dom._child_nodes(item)
                    and item_display == "inline" and item_tag not in box_model._REPLACED_OR_CONTROL_TAGS):
                # CSS 2.1 9.2.1.1/10.8's empty-inline strut applies only to
                # a plain, non-replaced `display:inline` -- an `inline-
                # block`/replaced element keeps its own explicit width/
                # height even with no content (CSS 2.1 10.3.10).
                child_runs = [_empty_inline_strut_run(
                    item, left_edge, right_edge, top_edge_val, extra_height, margin_start,
                )]
            if leading_space_run is not None:
                runs.append(leading_space_run)
            runs.extend(child_runs)
            continue
        raw = getattr(source, "textContent", "") or collapsed or ""
        text = dom._apply_text_transform(_CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw), paint_style.get("text_transform"))
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
            top_edge = box_model._numeric_edge(native["padding"][0]) + box_model._numeric_edge(native["border"][0])
            leading = box_model._numeric_edge(native["padding"][3]) + box_model._numeric_edge(native["border"][3])
            trailing = box_model._numeric_edge(native["padding"][1]) + box_model._numeric_edge(native["border"][1])
            box_height += (box_model._numeric_edge(native["padding"][0]) + box_model._numeric_edge(native["padding"][2])
                           + box_model._numeric_edge(native["border"][0]) + box_model._numeric_edge(native["border"][2]))
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
    # CSS 2.1 16.6.1: collapsible spaces collapse across element
    # boundaries too -- a run ending in a space followed by one starting
    # with a space keeps just one (abspos-inline-001.xht: `<span>...text.
    # </span>\n<span> The test...`, one 7.8px space in Chrome, not two).
    previous = None
    for run in runs:
        if run.get("break") or run.get("escapee"):
            previous = run if run.get("break") else previous
            continue
        tokens = run["tokens"]
        if (previous is not None and not previous.get("break") and tokens and previous["tokens"]
                and previous["tokens"][-1][0][-1:] in _CSS_WHITESPACE_STRIP_CHARS
                and tokens[0][0] and not tokens[0][0].strip(_CSS_WHITESPACE_STRIP_CHARS)):
            run["tokens"] = tokens[1:] or [("", 0.0)]
            run["intrinsic_width"] = max(0.0, run.get("intrinsic_width", 0.0) - tokens[0][1])
        previous = run
    for left, right in zip(runs, runs[1:]):
        if left.get("break") or right.get("break") or left.get("escapee") or right.get("escapee"):
            continue  # a forced break or an out-of-flow escapee has no glyphs to kern against
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
    return box_model._is_inline_level(child, child_style) and getattr(child, "_chromonic_split_container", None) is None



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
            wrapper = cache[run_index] = anonymous_boxes._AnonymousInlineRun(None, parent)
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
    read from `paint_style` (already extracted by `dom._describe`), not a
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
    word_break = (paint_style.get("word_break") or "normal").strip().lower()
    overflow_wrap = (paint_style.get("overflow_wrap") or "normal").strip().lower()
    # CSS Sizing 3's min-content carve-out: `overflow-wrap: break-word`
    # (unlike `anywhere`) must *not* shrink the min-content contribution
    # below "widest whole word" -- it only breaks a word that would
    # otherwise overflow its line, which by definition never happens at
    # a min-content query's own (infinitely narrow) constraint. `word-
    # break: break-all` has no such carve-out; it always may break
    # anywhere. Matches Parley's own `OverflowWrap::BreakWord` doc note
    # ("treated differently for min-content sizing"); handled here
    # rather than assumed inside Parley itself, since the `max_width:
    # 1.0` call below is this function's own proxy for "give me min-
    # content", not a dedicated min-content request Parley can key off.
    breaks_within_words_at_min_content = word_break == "break-all" or overflow_wrap == "anywhere"
    ascent, descent, normal_height = fonts.text_metrics(font_family, font_size, font_weight >= 600, italic)

    def measure(available_width, available_height, _known_width=None, _known_height=None):
        # `-1.0` is `src/lib.rs`'s sentinel for Taffy's `MinContent`
        # request: wrap at every opportunity, so the reported width is
        # the widest unbreakable piece (`white-space: nowrap`/`pre` text
        # has no break opportunities -- its min-content is its max-content).
        min_content = available_width is not None and available_width < 0
        if min_content:
            available_width = None
        width, height, lines = layout_text(
            text, font_family, font_size,
            font_weight=font_weight, italic=italic,
            max_width=(None if paint_style.get("white_space") in ("pre", "nowrap")
                       else 1.0 if min_content else available_width),
            letter_spacing=letter_spacing, word_spacing=word_spacing, line_height=line_height,
            word_break=word_break, overflow_wrap=overflow_wrap,
        )
        if (min_content and paint_style.get("white_space") not in ("pre", "nowrap")
                and not breaks_within_words_at_min_content):
            # A wrapped line's width from Parley keeps its trailing space
            # (`"IT "` = 150px in 50px Ahem); the min-content width is the
            # widest *word* (`"IT"` = 100px, Chrome's answer). Skipped
            # above when `word-break`/`overflow-wrap` allow breaking
            # *within* a word -- there, `width` already reflects that
            # (the `max_width: 1.0` call above forces every break Parley
            # will take), and re-measuring whole whitespace-delimited
            # words here would silently ignore it, overstating min-content.
            words = [word for word in re.split(r"[ \t\n\r\f]+", text) if word]
            if words:
                width = max(layout_text(word, font_family, font_size, font_weight=font_weight, italic=italic,
                                        letter_spacing=letter_spacing, word_spacing=word_spacing,
                                        line_height=line_height)[0] for word in words)
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
