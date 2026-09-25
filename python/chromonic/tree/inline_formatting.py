from __future__ import annotations

import math
import re

from domonic import _fontmetrics
from domonic.layout import LayoutBox, LayoutStyle, Length, _parse_length_or_percent

from .. import fonts, style_bridge
from .._native import layout_text
from . import anonymous_boxes, box_model, dom, geometry, inline_finalize




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
        # CSS 2.1 9.10: a direction:rtl block's line boxes start from its
        # right edge (position mirrors; glyph order within a same-direction
        # run stays untouched, no full bidi). `element` is the block
        # establishing this formatting context, so its own computed
        # direction governs every plan built for it, split or not.
        computed = getattr(element, "_chromonic_computed_style", None)
        self.rtl = dom._element_direction(element, computed) == "rtl"
        # text-align/text-align-last inherit normally -- `element`'s
        # computed value already reflects whatever ancestor declared it.
        self.text_align = box_model._text_align(getattr(computed, "textAlign", "") or "start")
        self.text_align_last = (getattr(computed, "textAlignLast", "") or "auto").strip().lower()
        # CSS 2.1 16.1: text-indent applies to this plan's own first
        # formatted line -- each 9.2.1.1 split segment is its own anonymous
        # block, indented independently.
        self.text_indent = _resolve_text_indent(computed)

    def measure(self, available_width, _available_height, _known_width=None, _known_height=None,
                atomics=(), bands=None, placed_floats=(), owns_bfc=False):
        """Lay this context's lines. Called by `_native.Tree` for an inline
        node (see `compute_inline_layout`'s protocol in src/lib.rs):
        `atomics[k]` is the border-box size, baseline and margins of the
        atomic child whose run has `atomic_index == k`; `bands` is the
        float-free horizontal space per vertical range as content-box
        `(top, bottom, left, width)` tuples (None while this node's width
        is indefinite -- an intrinsic-size probe); `placed_floats` is every
        float already placed this measure as `(k, x, y)`. Returns
        `("float", k, y)` to have child k's float placed no higher than
        content-box `y`, after which the lines are laid again from scratch
        against the updated bands, or the final
        `(width, height, first_baseline, [(k, x, y), ...])` with each
        non-float atomic's border-box position."""
        self._atomic_info = {info[0]: info for info in atomics}
        self._placed_floats = {k: (x, y) for k, x, y in placed_floats}
        width = float(available_width or 0.0)
        if width < 0:
            # src/lib.rs's MinContent sentinel: lay out at ~zero width so
            # every break opportunity is taken.
            width = 1.0
        elif width <= 0 or width > 1_000_000:
            width = sum(self._intrinsic_width(run) for run in self.runs)
        self._measured_width = width  # `publish()` needs this to mirror a `<br>`'s own box for RTL
        band_list = [tuple(float(v) for v in band) for band in bands] if bands else [(0.0, math.inf, 0.0, width)]
        base_height = _resolved_line_height(self.parent_style["line_height"])
        base_font = _fontmetrics.parse_length(self.parent_style["font_size"], default=16.0)
        base_ascent, base_descent, normal = fonts.text_metrics(
            self.parent_style["font_family"], base_font,
            _parse_font_weight(self.parent_style["font_weight"]) >= 600,
            fonts.is_italic(self.parent_style["font_style"]))
        base_height = base_height or normal
        base_above = base_ascent + math.floor((base_height - base_ascent - base_descent) / 2)
        base_below = base_height - base_above
        # CSS 2.1 10.8: how each atomic box sits on its line, per its own
        # vertical-align, from the sizes Rust just measured.
        self._atomic_metrics = {
            run["atomic_index"]: self._atomic_line_metrics(run, base_ascent, base_descent, base_height, base_font)
            for run in self.runs if run.get("atomic") and "atomic_index" in run
        }

        def band_for(y_pos):
            """(left, right, bottom) of the float-free space at `y_pos`."""
            for top, bottom, left, band_width in band_list:
                if y_pos < bottom:
                    if y_pos >= top:
                        return left, left + band_width, bottom
                    return left, left, top  # a gap before this band: no room until `top`
            top, bottom, left, band_width = band_list[-1]
            return left, left + band_width, math.inf

        state = _LineState()
        self._line_baselines = {}
        self._line_belows = {}
        self._line_has_content = {}
        self._line_bands = {}
        # run index -> (x, y, line height, line's own margin_start, line's own leading edge)
        self._break_positions = {}
        # For an rtl plan, a <br> mirrors to the start (post-mirror box x)
        # of whichever real run immediately preceded it -- resolved once
        # `placed` itself has been mirrored, below.
        break_precedes_run: dict = {}
        # id(escapee element) -> (x, y): an out-of-flow element mixed into
        # inline content still has a real CSS 2.1 10.3.7/10.6.4 static
        # position wherever it falls; `publish()` turns this into a page position.
        self._escapee_positions = {}
        placed = []
        first_line_y = [None]
        runs = self.runs
        # A line is laid against the band at its top, then checked against
        # the bands its final height crosses (CSS 2.1 9.5: a line box next
        # to a float is shortened over its whole height); if its content no
        # longer fits, it is laid again from the top of the offending band.
        # `cursor` is the next (run, token) to place; `checkpoint` is the
        # cursor and `placed` length at the current line's first token.
        cursor = [0, 0]
        checkpoint = [0, 0, 0]
        relaid = [0]

        def band_for(y_pos):
            """(left, right, bottom) of the float-free space at `y_pos`."""
            for top, bottom, left, band_width in band_list:
                if y_pos < bottom:
                    if y_pos >= top:
                        return left, left + band_width, bottom
                    return left, left, top  # a gap before this band: no room until `top`
            top, bottom, left, band_width = band_list[-1]
            return left, left + band_width, math.inf

        def start_line(new_y, *, first=False):
            state.y = new_y
            state.line_left, state.line_right, state.line_bottom = band_for(new_y)
            # CSS 2.1 16.1: text-indent only offsets the first formatted line.
            state.x = state.line_left + (self.text_indent if first else 0.0)
            state.x_pre_trailing = state.x
            state.last_real_run = None
            state.line_margin_start = 0.0
            state.line_leading_total = 0.0
            state.above, state.below = base_above, base_below
            state.line_has_content = False
            state.top_aligned = []
            state.bottom_aligned = []
            checkpoint[0], checkpoint[1], checkpoint[2] = cursor[0], cursor[1], len(placed)

        def settle_line_metrics():
            # top/bottom-aligned atomics only ever make the line taller
            # (CSS 2.1 10.8.1), never move its baseline.
            for extent in state.top_aligned:
                state.below = max(state.below, extent - state.above)
            for extent in state.bottom_aligned:
                state.above = max(state.above, extent - state.below)

        def line_retry_y():
            """The y to lay this line again from, when a band it crosses
            below its top leaves less room than its content took; None
            when it fits as placed."""
            entries = placed[checkpoint[2]:]
            if not entries:
                return None
            height = state.above + state.below
            content_left = min(px - leading - entry_run.get("margin_start", 0.0)
                               for entry_run, _t, px, _y, _tw, _th, leading, _tr, _a in entries)
            content_right = max(px + advance + trailing
                                for _r, _t, px, _y, _tw, _th, _l, trailing, advance in entries)
            for top, _bottom, band_left, band_width in band_list:
                if top <= state.y + 1e-6 or top >= state.y + height - 1e-6:
                    continue
                if content_left < band_left - 1e-6 or content_right > band_left + band_width + 1e-6:
                    return top
            return None

        def close_line() -> bool:
            """Finalize the current line, or roll it back to be laid lower
            down (True): `placed` and `cursor` return to its checkpoint."""
            settle_line_metrics()
            retry_y = line_retry_y() if relaid[0] < 64 else None
            if retry_y is not None:
                del placed[checkpoint[2]:]
                cursor[0], cursor[1] = checkpoint[0], checkpoint[1]
                relaid[0] += 1
                start_line(retry_y)
                return True
            self._line_baselines[state.y] = state.above
            self._line_belows[state.y] = state.below
            self._line_has_content[state.y] = state.line_has_content
            self._line_bands[state.y] = (state.line_left, state.line_right)
            if first_line_y[0] is None:
                first_line_y[0] = state.y
            return False

        start_line(0.0, first=True)
        while True:
            while cursor[0] < len(runs):
                run_index = cursor[0]
                run = runs[run_index]
                if run.get("escapee"):
                    # Doesn't occupy space -- record where the cursor already
                    # was. A block-level escapee's CSS 2.1 10.3.7 static
                    # position is where a block would start (the line after
                    # this one) -- abspos-007.xht; an inline-level one sits
                    # where the text is.
                    tag = (getattr(run["element"], "tagName", "") or "").lower()
                    if tag in box_model._USUALLY_INLINE_TAGS:
                        self._escapee_positions[id(run["element"])] = (state.x, state.y)
                    else:
                        # x stays None: a block's hypothetical position is
                        # flush to the containing block's start edge, which
                        # depends on direction and its own margins -- the
                        # positioning pass's block rule resolves it.
                        self._escapee_positions[id(run["element"])] = (
                            None, state.y + (state.above + state.below) if state.last_real_run is not None else state.y)
                    cursor[0] += 1
                    cursor[1] = 0
                    continue
                if run.get("break"):
                    # Forced line-break: flush the current line and move to the next.
                    if close_line():
                        continue
                    self._break_positions[run_index] = (
                        state.x_pre_trailing, state.y, state.above + state.below,
                        state.line_margin_start, state.line_leading_total)
                    break_precedes_run[run_index] = state.last_real_run
                    next_y = state.y + state.above + state.below
                    cursor[0] += 1
                    cursor[1] = 0
                    start_line(next_y)
                    continue
                metrics = None
                if run.get("atomic"):
                    metrics = self._atomic_metrics.get(run.get("atomic_index"))
                    if run.get("float"):
                        if bands is not None:
                            if run["atomic_index"] in self._placed_floats:
                                cursor[0] += 1
                                cursor[1] = 0
                                continue  # already in the bands
                            float_width = metrics["advance"] if metrics else 0.0
                            # CSS 2.1 9.5: a float met mid-line goes at this
                            # line's top when it fits beside what's already on
                            # it (or the line is still empty and the float
                            # context decides), else below this line.
                            if state.x <= state.line_left + 1e-6 or state.x + float_width <= state.line_right + 1e-6:
                                return ("float", run["atomic_index"], state.y)
                            return ("float", run["atomic_index"], state.y + state.above + state.below)
                        # Intrinsic sizing: a float contributes its margin-box
                        # width like an atomic inline, and no height.
                    tokens = [("￼", metrics["advance"] if metrics else 0.0)]
                else:
                    tokens = run["tokens"]
                rolled_back = False
                while cursor[1] < len(tokens):
                    index = cursor[1]
                    text, token_width = tokens[index]
                    leading = run["leading"] if index == 0 else 0.0
                    trailing = run["trailing"] if index == len(tokens) - 1 else 0.0
                    # margin-start shifts the whole run right on the first token
                    # only -- not carried onto subsequent lines after a <br>.
                    if index == 0:
                        state.x += run.get("margin_start", 0.0)
                        state.line_margin_start += run.get("margin_start", 0.0)
                    advance_width = (max(token_width, run["atomic_width"])
                                     if len(tokens) == 1 else token_width)
                    total = leading + advance_width + trailing
                    fit_total = total - (run["space_width"] if text[-1:].isspace() else 0.0)
                    following_space = 0.0
                    if index == len(tokens) - 1 and run["owner"] is not self.element:
                        for later in runs[run_index + 1:]:
                            if later.get("break"):
                                break
                            if later.get("escapee") or later.get("float"):
                                # Out of flow -- contributes no text/tokens of
                                # its own, so it can't be "the next token" for
                                # trailing-space purposes; skip past it to
                                # whatever real run actually follows.
                                continue
                            if later["tokens"]:
                                if not later["tokens"][0][0].strip():
                                    following_space = later["tokens"][0][1]
                                break
                    is_content = bool(text.strip())
                    line_empty = state.last_real_run is None
                    guard = 0
                    while (is_content and state.x + fit_total + following_space > state.line_right + 1e-6
                           and guard < 64):
                        guard += 1
                        margin_carry = run.get("margin_start", 0.0) if index == 0 else 0.0
                        if not line_empty:
                            # Wrap: this token starts the next line.
                            if close_line():
                                rolled_back = True
                                break
                            next_y = state.y + state.above + state.below
                            start_line(next_y)
                            state.x += margin_carry
                            state.line_margin_start += margin_carry
                            line_empty = True
                            continue
                        if state.line_bottom < math.inf and state.line_right - state.line_left < width - 1e-6:
                            # CSS 2.1 9.5: a line box shortened by a float that
                            # can't fit any content shifts down until it can.
                            start_line(state.line_bottom)
                            state.x += margin_carry
                            state.line_margin_start += margin_carry
                            continue
                        break  # nothing narrower below: overflow this line
                    if rolled_back:
                        break
                    state.line_leading_total += leading
                    token_height = run["box_height"]
                    if metrics is not None:
                        token_height = metrics["extent"]
                        if metrics["mode"] == "top":
                            state.top_aligned.append(metrics["extent"])
                        elif metrics["mode"] == "bottom":
                            state.bottom_aligned.append(metrics["extent"])
                        elif not run.get("float"):
                            state.above = max(state.above, metrics["above"])
                            state.below = max(state.below, metrics["below"])
                    else:
                        state.above = max(state.above, run["above"])
                        state.below = max(state.below, run["below"])
                    # A 9.2.1.1 split segment's decoration-only marker
                    # (`_empty_decoration_only_run`) never counts as real content
                    # by itself -- `publish()` uses this to decide whether it
                    # should inherit its line's real geometry instead of 0x0.
                    if not (run.get("empty_strut") and run["ascent"] == 0.0
                            and run["above"] == 0.0 and run["below"] == 0.0):
                        state.line_has_content = True
                    placed.append((run, text, state.x + leading, state.y, token_width, token_height,
                                   leading, trailing, advance_width))
                    state.x_pre_trailing = state.x + leading + advance_width
                    state.last_real_run = run
                    state.x += total
                    if index == len(tokens) - 1:
                        # CSS 2.1 10.3.1/10.3.3: non-collapsing margin-end,
                        # added once after the last token, kept out of the run's
                        # own width (margin_start already shifted the cursor
                        # before the first token).
                        state.x += run.get("margin_end", 0.0)
                    cursor[1] += 1
                if rolled_back:
                    continue
                cursor[0] += 1
                cursor[1] = 0
            if not close_line():
                break
        above, below, y, last_real_run = state.above, state.below, state.y, state.last_real_run
        # CSS 2.1 9.4.2: a line box collapses to zero height when every run
        # on it is a zero-edge empty strut (no text, no border/padding/
        # margin) -- any real content alongside one keeps normal height.
        is_all_zero_edge_empty = placed and all(
            run.get("empty_strut") and run["leading"] == 0.0 and run["trailing"] == 0.0
            and run["box_height"] <= run["glyph_height"] + 1e-6 and run.get("margin_start", 0.0) == 0.0
            for run in self.runs if not run.get("break") and not run.get("escapee") and not run.get("float"))
        if last_real_run is None and any(run.get("break") for run in self.runs):
            # Nothing placed after the final forced break: that line holds
            # no content and collapses (CSS 2.1 9.4.2) -- `a<br>` is one
            # line, `a<br><br>` two -- table-height-algorithm-004.xht.
            self.height = y
        else:
            self.height = 0.0 if is_all_zero_edge_empty else (y + above + below if placed else 0.0)
        if is_all_zero_edge_empty:
            # Each such strut reports zero height too, not its font-metrics
            # box_height -- the line it sits on doesn't exist.
            placed = [
                (run, text, x, y, token_width, 0.0, leading, trailing, advance_width)
                for run, text, x, y, token_width, _token_height, leading, trailing, advance_width in placed
            ]
        if owns_bfc and getattr(self.element, "_chromonic_tag_name", None) != "body":
            # CSS 2.1 10.6.7: this node is the root of its block formatting
            # context, so its auto height reaches the bottom of its floats.
            # (`<body>` is the tree's root here but not a BFC root in CSS.)
            for k, (_fx, fy) in self._placed_floats.items():
                metrics = self._atomic_metrics.get(k)
                if metrics is not None:
                    self.height = max(self.height, fy + metrics["extent"])
        content_width = min(width, max(
            (px + advance for _r, _t, px, _y, _pw, _h, _l, _tr, advance in placed), default=0.0))
        # An explicit physical text-align:left overrides direction:rtl's
        # default right-mirroring -- CSS 2.1 9.10's initial `start` value
        # resolves to "right" for rtl, not `left`.
        rtl_mirror_suppressed = self.text_align == "left"
        if self.rtl and not rtl_mirror_suppressed and placed:
            # direction:rtl mirrors each line, as a rigid group, against the
            # space it was placed within. Each fragment's own margin_end
            # (already attached to whichever fragment owns it, see the
            # bidi-box-model edge-swap in `_make_inline_formatting_plan`)
            # shifts its mirrored box left by that amount --
            # right-rtl-ref.xht. A 9.2.1.1 block-in-inline split
            # (`_split_wrapping_inline_element`) never threads its trailing
            # margin through margin_end at all, so that case still needs
            # its raw CSS margin-right applied directly here. Skipped
            # whenever margin_end is already nonzero
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
                line_left, line_right = self._line_bands.get(y, (0.0, width))
                outer_left = px - leading - margin_start
                outer_width = leading + advance + trailing
                new_px = (line_left + line_right - outer_left - outer_width - margin_end) + leading
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
            # This plan's base direction is ltr (no plan-wide mirror
            # applies). A nested direction:rtl element's start/end edge
            # assignment was already resolved at build time
            # (`_build_text_runs_from_nodes`'s nested-element branch), so no
            # mirroring is needed here; only physical text-align applies.
            placed = self._apply_text_align(placed, width)
        self._placed = placed
        placements = []
        for run, _text, px, y, _token_width, _token_height, _leading, _trailing, _advance in placed:
            if not run.get("atomic") or run.get("float"):
                continue
            metrics = self._atomic_metrics.get(run.get("atomic_index"))
            if metrics is None:
                continue
            line_baseline = self._line_baselines.get(y, 0.0)
            line_height = line_baseline + self._line_belows.get(y, 0.0)
            if metrics["mode"] == "top":
                border_top = y + metrics["mt"]
            elif metrics["mode"] == "bottom":
                border_top = y + line_height - metrics["extent"] + metrics["mt"]
            else:
                border_top = y + line_baseline - metrics["top_to_baseline"] + metrics["mt"]
            placements.append((run["atomic_index"], px + metrics["ml"], border_top))
        for run in runs:
            if run.get("escapee") and "anchor_index" in run:
                position = self._escapee_positions.get(id(run["element"]))
                if position is not None:
                    x = position[0]
                    if x is None:
                        x = self._measured_width if self.rtl else 0.0
                    placements.append((run["anchor_index"], x, position[1]))
        self._placements = placements
        # CSS 2.1 9.4.2/10.8.1: a line box that collapsed to nothing is
        # treated as not existing, so it lends no baseline -- an
        # inline-block holding only such a "phantom" line sits on its
        # bottom margin edge instead (inline-block-baseline-015.html).
        line_ys = sorted(self._line_baselines) if placed and not is_all_zero_edge_empty else []
        baseline = (line_ys[0] + self._line_baselines[line_ys[0]]) if line_ys else None
        last_baseline = (line_ys[-1] + self._line_baselines[line_ys[-1]]) if line_ys else None
        return (content_width, self.height, baseline, last_baseline, placements)

    def _intrinsic_width(self, run) -> float:
        """A run's contribution to this context's max-content width."""
        width = run.get("intrinsic_width", 0.0)
        if run.get("atomic"):
            metrics = self._atomic_metrics_for(run)
            if metrics is not None:
                width += metrics["advance"]
        return width

    def _atomic_metrics_for(self, run):
        info = self._atomic_info.get(run.get("atomic_index"))
        if info is None:
            return None
        _k, w, _h, _baseline, mt, mr, mb, ml, _float = info
        return {"advance": ml + w + mr, "extent": mt + _h + mb}

    def _atomic_line_metrics(self, run, base_ascent, base_descent, base_height, base_font):
        """CSS 2.1 10.8.1 vertical-align for one atomic inline-level box:
        its extent above and below the line's baseline (`above`/`below`),
        or a line-relative `mode` ("top"/"bottom"). `top_to_baseline` is
        the distance from the box's border-box top to the line baseline,
        which is what positions the box once the line's baseline is known."""
        info = self._atomic_info.get(run.get("atomic_index"))
        if info is None:
            return None
        _k, w, h, baseline, mt, mr, mb, ml, _float = info
        extent = mt + h + mb
        advance = ml + w + mr
        # CSS 2.1 10.8.1: an inline-block's baseline is its last line box's
        # unless it has no in-flow line boxes or overflow other than
        # visible; a replaced box's is its bottom margin edge.
        if baseline is None or not run.get("overflow_visible", True):
            box_baseline = h + mb
        else:
            box_baseline = baseline
        align = run.get("vertical_align") or "baseline"
        mode = "baseline"
        shift = 0.0  # positive raises the box
        if align in ("top", "bottom"):
            mode = align
        elif align == "middle":
            # Midpoint of the margin box on the parent's baseline plus half
            # its x-height (approximated as half an em).
            shift = extent / 2.0 - (mt + box_baseline) + base_font * 0.25
        elif align == "text-top":
            shift = base_ascent - (mt + box_baseline)
        elif align == "text-bottom":
            shift = (extent - (mt + box_baseline)) - base_descent
        elif align == "sub":
            shift = -base_font / 5.0
        elif align == "super":
            shift = base_font / 3.0
        elif align.endswith("%"):
            try:
                shift = float(align[:-1]) / 100.0 * base_height
            except ValueError:
                shift = 0.0
        elif align not in ("baseline", ""):
            shift = _fontmetrics.parse_length(align, default=0.0) or 0.0
        top_to_baseline = mt + box_baseline + shift
        return {
            "advance": advance, "extent": extent, "mode": mode,
            "above": top_to_baseline, "below": extent - top_to_baseline,
            "top_to_baseline": top_to_baseline, "mt": mt, "ml": ml,
        }

    def _apply_text_align(self, placed, width):
        """CSS Text 3 text-align/text-align-last: shift each line's placed
        tokens for the block's alignment, physical left/right/center only
        (direction:rtl is handled separately, via the mirror). `justify`
        distributes leftover space at each whitespace-ending token
        boundary, every line but the last -- each 9.2.1.1 split segment is
        its own anonymous block, so its own last line is independent."""
        self._justified_lines = set()
        if not placed:
            return placed
        text_align = "left" if self.text_align in ("start", "") else (
            "right" if self.text_align == "end" else self.text_align)
        text_align_last = self.text_align_last
        if text_align_last in ("auto", ""):
            # CSS Text 3: auto means ordinary text-align, except a justify
            # block's own last line is never force-justified by this
            # default -- it aligns start (left) instead.
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
            band_left, band_right = self._line_bands.get(line_entries[0][3], (0.0, width))
            slack = (band_right - band_left) - (line_end - line_start)
            if align == "justify":
                gap_after = [i for i, e in enumerate(line_entries[:-1]) if e[1][-1:].isspace()]
                if not gap_after or slack <= 0:
                    result.extend(line_entries)
                    continue
                extra_per_gap = slack / len(gap_after)
                gap_set = set(gap_after)
                cumulative = 0.0
                # `publish()`'s trailing-whitespace collapse would otherwise
                # trim this same slack back off the last token -- recorded
                # here so `publish()` can skip it for a justified line.
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
        # A table cell's vertical-align moves its content down
        # (`table_formatting` sets the offset; Rust moves the atomic boxes).
        origin_y = (box.y + box.border_top + padding[0]
                    + float(self.element.__dict__.get("_chromonic_content_offset_y", 0.0) or 0.0))
        escapee_positions = getattr(self, "_escapee_positions", None)
        if escapee_positions:
            # A later pass applies each escapee's real page-coordinate
            # static position once every box has been written.
            for run in self.runs:
                if run.get("escapee"):
                    position = escapee_positions.get(id(run["element"]))
                    if position is not None:
                        run["element"]._chromonic_static_position = (
                            None if position[0] is None else origin_x + position[0], origin_y + position[1])
        self.fragments = []
        owner_rects = {}
        grouped = {}
        placed = getattr(self, "_placed", ())
        first_of_run: dict = {}
        last_of_run: dict = {}
        for placed_index, entry in enumerate(placed):
            first_of_run.setdefault(id(entry[0]), placed_index)
            last_of_run[id(entry[0])] = placed_index
        for placed_index, (run, text, x, y, width, token_height, leading, trailing, advance) in enumerate(placed):
            ends_line = placed_index + 1 == len(placed) or placed[placed_index + 1][3] != y
            # A justified line's trailing space was already redistributed
            # into real inter-word gaps -- nothing left to collapse
            # (see `_apply_text_align`'s `_justified_lines`).
            collapsed_space = (run["space_width"]
                               if text[-1:].isspace() and ends_line
                               and y not in getattr(self, "_justified_lines", ()) else 0.0)
            visual_width = max(0.0, width - collapsed_space)
            visual_advance = max(0.0, advance - collapsed_space)
            glyph_height = min(token_height, run["glyph_height"])
            glyph_y = y + self._line_baselines[y] - run["ascent"]
            key = (id(run["source"]), y)
            entry = grouped.get(key)
            # A genuinely empty inline's strut run (`_empty_inline_strut_run`,
            # one ("", 0.0) token) places a real element rect via
            # `owner_rects` below but is never a text-range fragment --
            # there's no source text node, and Chrome's getClientRects()
            # reports zero text fragments for it.
            if run.get("atomic") or (entry is None and text == ""):
                pass
            elif entry is None:
                fragment = anonymous_boxes._AnonymousTextFragment(run["source"], self.element)
                fragment.owner = run["owner"]
                fragment._chromonic_paint_style = run["paint_style"]
                fragment._chromonic_text_lines = [text]
                fragment._chromonic_text_line_widths = [visual_width]
                fragment._chromonic_line_height = glyph_height
                # CSS 2.1 9.4.3: a position:relative inline moves its
                # fragments by its offsets, layout otherwise untouched --
                # position-relative-002.xht.
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
            # A 9.2.1.1 split segment's decoration-only marker run
            # (`_empty_decoration_only_run`, no font/line-height of its own)
            # normally sits alone on its own zero-height line, but when real
            # sibling content shares that line it belongs at the top of the
            # real line instead, sized to that line's real height.
            is_decoration_only_marker = (
                run.get("empty_strut") and run["ascent"] == 0.0
                and run["above"] == 0.0 and run["below"] == 0.0
            )
            if is_decoration_only_marker:
                # No ascent of its own to place a baseline against -- always
                # the top of whatever line it's on.
                owner_y = origin_y + y
                marker_height = (self._line_baselines[y] + self._line_belows[y]
                                  if self._line_has_content.get(y) else token_height)
            else:
                owner_y = origin_y + (y if run["atomic_width"] else glyph_y - run["top_edge"])
                marker_height = token_height
            rel_dx, rel_dy = inline_finalize._inline_relative_offset(owner, self.element, box)
            rect = (origin_x + x - leading + rel_dx, owner_y + rel_dy,
                    visual_advance + leading + trailing, marker_height)
            # Split/document-order segment index (None for a non-split
            # owner), so `_finalize_inline_owner_boxes` can place
            # interruption-marker rects logically, not via a geometric sort.
            # ...and the same fragment's margin box, which is what an
            # enclosing inline box spans (CSS 2.1 9.2.1: the wrapper's
            # content area holds its children's margin boxes) --
            # inline-formatting-context-002.xht.
            margin_before = (run.get("own_margin_start", run.get("margin_start", 0.0))
                             if first_of_run.get(id(run)) == placed_index else 0.0)
            margin_after = (run.get("own_margin_end", run.get("margin_end", 0.0))
                            if last_of_run.get(id(run)) == placed_index else 0.0)
            outer_rect = (rect[0] - margin_before, rect[1], rect[2] + margin_before + margin_after, rect[3])
            owner_rects.setdefault(owner, []).append((rect, run.get("split_group"), outer_rect))
            if run.get("atomic") and not run.get("float") and (rel_dx or rel_dy) and hasattr(run["element"], "__dict__"):
                _apply_inline_rel_offset(run["element"], rel_dx, rel_dy)
        # inline-block/block owners keep their atomic Taffy box; a real
        # display:inline owner's rects come from owner_rects[self.element]
        # instead, merged per-line then unioned for getBoundingClientRect().
        #
        # Accumulated into owner_accum, not finalized here: a split owner
        # (CSS 2.1 9.2.1.1) publishes from multiple independent plans, one
        # per segment -- `_finalize_inline_owner_boxes` unions them all once
        # every plan sharing owner_accum has run.
        owner_is_inline = self.owner_display == "inline"
        for owner, rect_group_pairs in owner_rects.items():
            if owner is self.element and not owner_is_inline:
                continue
            entry = owner_accum.get(id(owner))
            if entry is None:
                entry = owner_accum[id(owner)] = (owner, {}, [], {})
            groups = entry[1]
            for rect, group, outer_rect in rect_group_pairs:
                groups.setdefault(group, []).append(rect)
                entry[3].setdefault(group, []).append(outer_rect)
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
        # Same accumulate-not-overwrite reasoning as owner_accum above, for
        # self.element's own painted fragments.
        elem_entry = element_fragments_accum.get(id(self.element))
        if elem_entry is None:
            elem_entry = element_fragments_accum[id(self.element)] = (self.element, [])
        elem_entry[1].extend(self.fragments)



def _resolve_text_indent(computed) -> float:
    """text-indent in real px -- ComputedStyleDeclaration.getPropertyValue
    has no used-length conversion for it, so a ch/em value would otherwise
    reach here as literal unresolved CSS text. Percentages aren't
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
    display:inline element -- CSS 2.1 9.2.1.1: an in-flow block child of an
    inline forces that inline to split around it, so the block must reach
    `_split_inline_flow_around_blocks`/`_contains_in_flow_block` as an
    ordinary item here. An ordinary block `element` opts a block child out
    entirely -- no anonymous-block implementation for that case."""
    # The normalized view (`anonymous_boxes._normalized_child_nodes`): text a 9.2.1.1
    # anonymous block took over is no longer this element's own, and an
    # anonymous inline-table (17.2.1) stands in for loose cells.
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
    def _child_qualifies(child, computed, style_obj) -> bool:
        return (
            box_model._is_inline_level(child, style_obj)
            or box_model._is_absolutely_positioned(style_obj)
            # CSS 2.1 9.5: a float mixed into inline content is placed by
            # the inline formatting context itself (an atomic run).
            or box_model._is_floated(computed)
            or (getattr(child, "tagName", "") or "").lower() == "br"
            # A direct in-flow block child of an inline `element` is the
            # CSS 2.1 9.2.1.1 split trigger, not grounds to reject it.
            or element_is_inline
        )

    has_inline_only_children = (
        not has_direct_text
        and bool(children)
        and all(_child_qualifies(child, computed, style_obj) for child, computed, style_obj in children)
    )
    before_info = getattr(element, "_chromonic_before_pseudo", None)
    after_info = getattr(element, "_chromonic_after_pseudo", None)
    if not has_direct_text and not has_inline_only_children and before_info is None and after_info is None:
        return None
    by_id = {id(child): (child, computed, style_obj) for child, computed, style_obj in children}
    # Out-of-flow positioned children and <br> don't break an inline
    # formatting run, and stay in the retained projection so Taffy can
    # anchor them. CSS 2.1 9.5: a float doesn't break one either, but only
    # once real direct text already earned this container a plan -- a
    # pure-float sibling group with no text must keep going through the
    # older `elif children:` -> `_approximate_inline_flow` ->
    # `_fix_float_flow_after_block_sibling` path instead --
    # zero-available-space-float-positioning.html. With real text present,
    # rejecting the container loses every text sibling to that fallback,
    # which has no notion of direct text -- any `CSS2/floats` fixture
    # shaped like `<p>text <img style="float:left"> more text</p>`.
    if any(not _child_qualifies(child, computed, style_obj) for child, computed, style_obj in children):
        return None
    items = []
    pending_space = False
    previous_was_element = False
    preserves_white_space = ((getattr(element, "_chromonic_paint_style", None) or {}).get("white_space")
                             or "normal").strip().lower() in _PRESERVING_WHITE_SPACE
    # CSS 2.1 16.6.1: whitespace at the very start of a block's content
    # collapses to nothing, like at a line's start -- only interior
    # whitespace collapses to a single space. `pending_space` must never
    # fire until real content has been emitted (`has_content`), or a
    # purely-formatting leading newline before the first child would
    # manufacture a spurious visible space -- position-relative-003.xht.
    has_content = False
    for node in child_nodes:
        if getattr(node, "nodeType", None) == dom.TEXT_NODE:
            text = dom._collapsed_text_node(node)
            raw = getattr(node, "textContent", None) or getattr(node, "data", "") or ""
            if text or (preserves_white_space and raw):
                fragment = getattr(node, "_chromonic_fragment", None)
                if fragment is None:
                    fragment = anonymous_boxes._AnonymousTextFragment(node, element)
                    node._chromonic_fragment = fragment
                # Only a real collapsed-away whitespace node earns the
                # leading space -- text butted right against the preceding
                # inline element has none (CSS 2.1 16.6.1) --
                # column-visibility-004.xht.
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
                # right after it collapses too, same as at this
                # container's own start.
                has_content = False
            else:
                # Mirrors the text-fragment case above: a whitespace-only
                # text node collapses to nothing of its own, so the pending
                # space before this element must be carried forward the
                # same way or it's lost -- `<a>Bluestein</a> <a>1 hour
                # ago</a>` rendered as "Bluestein1 hour ago" without this.
                if box_model._is_absolutely_positioned(style_obj) or box_model._is_floated(computed):
                    # Out of flow: invisible to whitespace collapsing --
                    # height-width-inline-table-001.xht; a float likewise
                    # (CSS 2.1 9.5), so a space after one at a line's start
                    # still collapses -- floats-placement-001.html.
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
        # node for a ComputedStyleDeclaration), so its paint style must be
        # set explicitly here, @font-face substitution included.
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
        # Never the parent's own basis: a table cell's is its whole column
        # width, and a fragment inheriting it stacked content one item per
        # line -- border-conflict-element-001d.xht.
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
    contributing its font/line-height to the line's height/baseline like a
    real text run, while its own vertical padding/border/margin only grow
    its own box, never the line's. One empty ("", 0.0) token so measure()
    places it without treating it as wrappable content."""
    paint_style = owner._chromonic_paint_style
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    # An explicit line-height:0 is a real authored value, not "unset" --
    # `_resolved_line_height` returns None for genuinely-unset/normal, so
    # `... or normal_height` here would wrongly treat 0.0 the same way.
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
        "own_margin_start": margin_start, "own_margin_end": 0.0,
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



class _LineState:
    """Mutable cursor for one `_InlineFormattingPlan.measure` pass."""

    __slots__ = ("x", "y", "x_pre_trailing", "line_left", "line_right", "line_bottom", "above", "below",
                 "line_has_content", "line_margin_start", "line_leading_total", "last_real_run",
                 "top_aligned", "bottom_aligned")

    def __init__(self):
        self.x = self.y = self.x_pre_trailing = 0.0
        self.line_left = self.line_right = 0.0
        self.line_bottom = math.inf
        self.above = self.below = 0.0
        self.line_has_content = False
        self.line_margin_start = self.line_leading_total = 0.0
        self.last_real_run = None
        self.top_aligned = []
        self.bottom_aligned = []



class _GeneratedTextNode:
    """The text of a ::before/::after box, shaped like a DOM text node so
    `_build_text_runs_from_nodes` can run it like any other."""

    def __init__(self, text: str):
        self.nodeType = dom.TEXT_NODE
        self.textContent = text
        self.data = text
        self.childNodes = ()



def _is_atomic_inline(node, computed, style_obj) -> bool:
    """CSS 2.1 9.2.2/10.3.9: an inline-level box that is its own formatting
    context -- replaced, inline-block, inline-table, inline-flex/grid --
    so it takes part in a line as one opaque box rather than as text."""
    if isinstance(node, anonymous_boxes._AnonymousTableBox):
        return node.kind == "inline-table"
    if isinstance(node, dom._PseudoElement):
        return False
    tag = (getattr(node, "tagName", "") or "").lower()
    if tag in box_model._REPLACED_OR_CONTROL_TAGS:
        return True
    display = getattr(getattr(style_obj, "display", None), "value", "") if style_obj is not None else ""
    if isinstance(display, str):
        match = style_bridge._SIMPLE_VAR_FALLBACK.match(display.strip())
        if match:
            display = match.group(1).strip()
    return display in ("inline-block", "inline-flex", "inline-grid", "-webkit-inline-flex", "inline-table")



def _atomic_run(item, computed, style_obj, owner, paint_style, *, leading=0.0, trailing=0.0,
                top_edge=0.0, extra_height=0.0, margin_start=0.0, margin_end=0.0) -> dict:
    """A run standing for one atomic inline-level box (or float) built as a
    real child of the inline node. Its size arrives at measure time from
    Rust (`atomics[atomic_index]`); the font metrics recorded here are the
    *owner's*, for the owner's own line-box fragment around it."""
    font_size = _fontmetrics.parse_length(paint_style["font_size"], default=16.0)
    family = "" if paint_style["font_family"] == "none" else paint_style["font_family"]
    weight = _parse_font_weight(paint_style["font_weight"])
    italic = fonts.is_italic(paint_style["font_style"])
    ascent, descent, normal_height = fonts.text_metrics(family, font_size, weight >= 600, italic)
    glyph_height = ascent + descent
    resolved_line_height = _resolved_line_height(paint_style["line_height"])
    used_line_height = resolved_line_height if resolved_line_height is not None else normal_height
    above = ascent + math.floor((used_line_height - glyph_height) / 2)
    below = used_line_height - above
    float_value = (getattr(computed, "float", "") or "").strip().lower() if computed is not None else ""
    clear_value = (getattr(computed, "clear", "") or "").strip().lower() if computed is not None else ""
    overflow_x = (getattr(computed, "overflowX", "") or "visible").strip().lower() if computed is not None else "visible"
    overflow_y = (getattr(computed, "overflowY", "") or "visible").strip().lower() if computed is not None else "visible"
    return {
        "atomic": True, "element": item, "computed": computed, "style": style_obj,
        "source": item, "owner": owner, "paint_style": paint_style,
        "font_size": font_size, "tokens": [("￼", 0.0)],
        "leading": leading, "trailing": trailing,
        "box_height": glyph_height + extra_height,
        "glyph_height": glyph_height, "ascent": ascent,
        "above": above, "below": below, "top_edge": top_edge,
        "space_width": 0.0, "atomic_width": 0.0,
        "margin_start": margin_start, "margin_end": margin_end, "intrinsic_width": 0.0,
        "vertical_align": (getattr(computed, "verticalAlign", "") or "baseline").strip().lower()
        if computed is not None else "baseline",
        "float": float_value if float_value in ("left", "right") else None,
        "clear": clear_value if clear_value in ("left", "right", "both") else "none",
        "overflow_visible": overflow_x in ("visible", "") and overflow_y in ("visible", ""),
    }



def _pseudo_object_for(element, which: str, info):
    """The `_PseudoElement` for `element`'s ::before/::after, configured
    the way `_inline_mixed_content` does for a top-level one."""
    pseudo_computed, text = info
    pseudo = dom._get_pseudo_object(element, which)
    pseudo.text = text
    pseudo_style_obj = LayoutStyle.from_computed(pseudo_computed)
    pseudo._chromonic_paint_style = dom._extract_paint_style(pseudo_computed)
    pseudo._chromonic_computed_style = pseudo_computed
    from .. import webfonts
    webfonts.resolve_style(element, pseudo._chromonic_paint_style)
    return pseudo, pseudo_computed, pseudo_style_obj



def _pseudo_runs(pseudo, pseudo_computed, pseudo_style, owner, computed_cache) -> list:
    """Runs for one ::before/::after box in inline flow. A display:inline
    pseudo with text is ordinary inline text in its own font, with its own
    border/padding/margin as edges (CSS 2.1 12.1: generated content is
    laid out as if it were a child inline box); one with no text keeps an
    empty inline box for its decoration; any other display -- or an empty
    box given explicit dimensions (an icon-only box) -- is atomic and is
    built as a real child of the inline node."""
    native = style_bridge.to_dict(pseudo_style)
    pseudo._chromonic_native_style = native
    display = (getattr(pseudo_computed, "display", "") or "inline").strip().lower()
    text = pseudo.text or ""
    has_explicit_size = isinstance(native.get("width"), (int, float)) or isinstance(native.get("height"), (int, float))
    if display not in ("inline", "") or (not text.strip(_CSS_WHITESPACE_STRIP_CHARS) and has_explicit_size):
        return [_atomic_run(pseudo, pseudo_computed, pseudo_style, owner, owner._chromonic_paint_style)]
    left_edge = box_model._numeric_edge(native["padding"][3]) + box_model._numeric_edge(native["border"][3])
    right_edge = box_model._numeric_edge(native["padding"][1]) + box_model._numeric_edge(native["border"][1])
    top_edge = box_model._numeric_edge(native["padding"][0]) + box_model._numeric_edge(native["border"][0])
    extra_height = (top_edge + box_model._numeric_edge(native["padding"][2])
                    + box_model._numeric_edge(native["border"][2]))
    margin_start = box_model._numeric_edge(native["margin"][3])
    margin_end = box_model._numeric_edge(native["margin"][1])
    pseudo.__dict__["_chromonic_flattened_inline"] = True
    if text.strip(_CSS_WHITESPACE_STRIP_CHARS):
        return _build_text_runs_from_nodes(
            [_GeneratedTextNode(text)], pseudo._chromonic_paint_style, pseudo,
            leading_edge=left_edge, trailing_edge=right_edge, top_edge_val=top_edge,
            extra_height=extra_height, margin_start=margin_start, margin_end=margin_end,
            computed_cache=None,
        )
    if left_edge or right_edge or margin_start or margin_end or extra_height:
        return [_empty_inline_strut_run(pseudo, left_edge, right_edge, top_edge, extra_height, margin_start)]
    return []



def _with_pseudo_runs(item, child_runs, computed_cache) -> list:
    """`child_runs` (a flattened inline element's own runs) with the
    element's ::before/::after runs around them. The element's leading
    edge/margin-start moves onto the ::before (it is the first inline box
    inside the element), the trailing ones onto the ::after."""
    before_info = getattr(item, "_chromonic_before_pseudo", None)
    after_info = getattr(item, "_chromonic_after_pseudo", None)
    if before_info is None and after_info is None:
        return child_runs

    def transfer(source, target, edge_key, margin_key):
        if source is None or target is None:
            return
        moved_edge = source.get(edge_key, 0.0)
        moved_margin = source.get(margin_key, 0.0)
        source[edge_key] = 0.0
        source[margin_key] = 0.0
        source["intrinsic_width"] = max(0.0, source.get("intrinsic_width", 0.0) - moved_edge - moved_margin)
        target[edge_key] = target.get(edge_key, 0.0) + moved_edge
        target[margin_key] = target.get(margin_key, 0.0) + moved_margin
        target["intrinsic_width"] = target.get("intrinsic_width", 0.0) + moved_edge + moved_margin

    real = [run for run in child_runs if not run.get("break") and not run.get("escapee")]
    before_runs = after_runs = []
    if before_info is not None:
        pseudo, pseudo_computed, pseudo_style = _pseudo_object_for(item, "before", before_info)
        before_runs = _pseudo_runs(pseudo, pseudo_computed, pseudo_style, item, computed_cache)
        before_real = [run for run in before_runs if not run.get("break")]
        if before_real and real:
            transfer(real[0], before_real[0], "leading", "margin_start")
    if after_info is not None:
        pseudo, pseudo_computed, pseudo_style = _pseudo_object_for(item, "after", after_info)
        after_runs = _pseudo_runs(pseudo, pseudo_computed, pseudo_style, item, computed_cache)
        after_real = [run for run in after_runs if not run.get("break")]
        if after_real and real:
            transfer(real[-1], after_real[-1], "trailing", "margin_end")
    return list(before_runs) + list(child_runs) + list(after_runs)



def _apply_inline_rel_offset(element, dx: float, dy: float) -> None:
    """CSS 2.1 9.4.3: an atomic box inside a position:relative inline
    moves with it. Idempotent per laid-out box: `geometry._write_boxes`
    clears the record whenever the box is written fresh."""
    state = element.__dict__
    previous = state.get("_chromonic_inline_rel_offset") or (0.0, 0.0)
    delta_x, delta_y = dx - previous[0], dy - previous[1]
    if delta_x or delta_y:
        geometry._shift_subtree(element, delta_x, delta_y)
    state["_chromonic_inline_rel_offset"] = (dx, dy)



_PRESERVING_WHITE_SPACE = frozenset({"pre", "pre-wrap", "pre-line", "break-spaces"})


class _PreservedNewline:
    """A forced line break from a preserved newline (`white-space: pre`,
    `pre-wrap`, `pre-line`, `break-spaces`) -- a `<br>`-shaped stand-in
    for the plan's break runs."""

    def __init__(self, owner, paint_style):
        self.parentElement = owner
        self.tagName = "#newline"
        self.childNodes = ()
        self._chromonic_paint_style = paint_style


def _run_font_metrics(paint_style) -> dict:
    """The per-font numbers every text run of `paint_style` shares."""
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
    one_width = layout_text("a", family, font_size, font_weight=weight, italic=italic)[0]
    spaced_width = layout_text("a a", family, font_size, font_weight=weight, italic=italic)[0]
    return {
        "font_size": font_size, "family": family, "weight": weight, "italic": italic,
        "ascent": ascent, "descent": descent, "glyph_height": glyph_height,
        "above": above, "below": used_line_height - above,
        "space_width": max(0.0, spaced_width - 2.0 * one_width),
        "letter_spacing": _fontmetrics.parse_length(paint_style["letter_spacing"], default=0.0),
        "word_spacing": _fontmetrics.parse_length(paint_style["word_spacing"], default=0.0),
    }


def _text_run(source, owner, paint_style, metrics, token_texts, *, leading=0.0, trailing=0.0,
              top_edge=0.0, extra_height=0.0, margin_start=0.0, margin_end=0.0, no_wrap=False,
              own_margin_start=None, own_margin_end=None) -> dict:
    """One text run: `token_texts` measured in the run's own font.
    `own_margin_*` is the part of `margin_*` that is the owner's own (the
    rest belongs to enclosing inline wrappers flattened into this run)."""
    tokens = []
    for token in token_texts:
        _measured, _height, lines = layout_text(
            token, metrics["family"], metrics["font_size"], font_weight=metrics["weight"], italic=metrics["italic"],
            letter_spacing=metrics["letter_spacing"], word_spacing=metrics["word_spacing"],
        )
        tokens.append((token, sum(line[1] for line in lines)))
    return {
        "source": source, "owner": owner, "paint_style": paint_style,
        "font_size": metrics["font_size"], "tokens": tokens,
        "leading": leading, "trailing": trailing,
        "box_height": metrics["glyph_height"] + extra_height,
        "glyph_height": metrics["glyph_height"], "ascent": metrics["ascent"],
        "above": metrics["above"], "below": metrics["below"], "top_edge": top_edge,
        "space_width": metrics["space_width"], "atomic_width": 0.0,
        "margin_start": margin_start, "margin_end": margin_end, "no_wrap": no_wrap,
        "own_margin_start": margin_start if own_margin_start is None else own_margin_start,
        "own_margin_end": margin_end if own_margin_end is None else own_margin_end,
        "intrinsic_width": margin_start + leading + sum(width for _text, width in tokens) + trailing + margin_end,
    }


def _runs_for_text(raw, paint_style, owner, source, *, has_leading_space=False, leading=0.0, trailing=0.0,
                   top_edge=0.0, extra_height=0.0, margin_start=0.0, margin_end=0.0,
                   margins_are_own=True) -> list:
    """The runs for one DOM text node under `paint_style`'s `white-space`
    (CSS Text 3 4.1). The collapsing values (`normal`, `nowrap`) give one
    run of word tokens with at most one leading/trailing space; the
    preserving values keep runs of spaces (`pre`, `pre-wrap`,
    `break-spaces`) or collapse them (`pre-line`), and every newline is a
    forced break. `nowrap`/`pre` text never wraps: each segment is one
    token. `leading`/`margin_start` land on the first run, `trailing`/
    `margin_end` on the last."""
    if not raw:
        return []
    white_space = (paint_style.get("white_space") or "normal").strip().lower()
    preserve_spaces = white_space in ("pre", "pre-wrap", "break-spaces")
    no_wrap = white_space in ("pre", "nowrap")
    transform = paint_style.get("text_transform")
    metrics = _run_font_metrics(paint_style)
    if white_space not in _PRESERVING_WHITE_SPACE:
        text = dom._apply_text_transform(_CSS_COLLAPSIBLE_WHITESPACE_RE.sub(" ", raw), transform)
        if not text.strip(_CSS_WHITESPACE_STRIP_CHARS):
            return []
        # Whitespace collapses across run boundaries. Keep a single leading
        # or trailing space only when the source actually contains one.
        text = ((" " if (raw[:1] and raw[:1] in _CSS_WHITESPACE_STRIP_CHARS) or has_leading_space else "")
                + text.strip(_CSS_WHITESPACE_STRIP_CHARS)
                + (" " if raw[-1:] and raw[-1:] in _CSS_WHITESPACE_STRIP_CHARS else ""))
        return [_text_run(source, owner, paint_style, metrics, [text] if no_wrap else re.findall(r"\S+\s*|\s+", text),
                          leading=leading, trailing=trailing, top_edge=top_edge, extra_height=extra_height,
                          margin_start=margin_start, margin_end=margin_end, no_wrap=no_wrap,
                          own_margin_start=margin_start if margins_are_own else 0.0,
                          own_margin_end=margin_end if margins_are_own else 0.0)]
    text = dom._apply_text_transform(raw, transform).replace("\r\n", "\n").replace("\r", "\n")
    runs = []
    for index, segment in enumerate(text.split("\n")):
        if index > 0:
            runs.append({"break": True, "element": _PreservedNewline(owner, paint_style)})
        if preserve_spaces:
            segment = segment.replace("\t", " " * 8)
        else:
            # pre-line: spaces collapse, and collapsible spaces at a line's
            # start and end are removed.
            segment = re.sub(r"[ \t\f]+", " ", segment).strip(" ")
        if not segment:
            continue
        runs.append(_text_run(source, owner, paint_style, metrics,
                              [segment] if no_wrap else re.findall(r"\S+\s*|\s+", segment),
                              top_edge=top_edge, extra_height=extra_height, no_wrap=no_wrap))
    real = [run for run in runs if not run.get("break")]
    if real:
        real[0]["leading"] += leading
        real[0]["margin_start"] += margin_start
        real[0]["own_margin_start"] += margin_start if margins_are_own else 0.0
        real[0]["intrinsic_width"] += leading + margin_start
        real[-1]["trailing"] += trailing
        real[-1]["margin_end"] += margin_end
        real[-1]["own_margin_end"] += margin_end if margins_are_own else 0.0
        real[-1]["intrinsic_width"] += trailing + margin_end
    return runs


def _build_text_runs_from_nodes(child_nodes, paint_style, owner, *,
                                 leading_edge=0.0, trailing_edge=0.0,
                                 top_edge_val=0.0, extra_height=0.0,
                                 margin_start=0.0, margin_end=0.0, computed_cache=None,
                                 margins_are_own=True):
    """Build inline-formatting-plan `runs` entries for the text/`<br>`
    content of `child_nodes` (DOM order): `leading_edge`/`trailing_edge`
    (border+padding) go on the first/last text run, `top_edge_val`/
    `extra_height` on every run's box height, `margin_start`/`margin_end`
    applied once each around the whole sequence. `[]` if no non-empty text.

    Shared by `_make_inline_formatting_plan` and `_split_wrapping_inline_element`
    (CSS 2.1 9.2.1.1's split fragments, each passing 0 for whichever edge
    it doesn't own).

    `computed_cache`, when given, also handles two node kinds a split
    segment can carry: a simple nested inline (text/`<br>` only) is
    flattened in place via a recursive call with its own paint style/edges;
    an absolutely-positioned element becomes an "escapee" run -- no
    width/height of its own, but its position in `runs` marks its CSS 2.1
    10.3.7/10.6.4 static position. Anything deeper is silently skipped."""

    def is_text_bearing(node, depth: int = 0) -> bool:
        """Whether `node` puts something on the line: text, an atomic box
        or a float, directly or through nested inline wrappers -- the
        nodes that own the first/last edges and count for spacing."""
        node_type = getattr(node, "nodeType", None)
        if node_type == dom.TEXT_NODE:
            if (paint_style.get("white_space") or "normal").strip().lower() in _PRESERVING_WHITE_SPACE:
                return bool(getattr(node, "textContent", None) or getattr(node, "data", ""))
            return bool(dom._collapsed_text_node(node).strip())
        if not dom._is_element(node):
            return False
        if (getattr(node, "tagName", "") or "").lower() == "br":
            return False
        if computed_cache is not None:
            _node_computed, node_style_obj = dom._describe(node, computed_cache)
            if box_model._is_absolutely_positioned(node_style_obj):
                return False  # an escapee -- out of flow, no text-bearing slot
            if _is_atomic_inline(node, _node_computed, node_style_obj) or box_model._is_floated(_node_computed):
                return True  # an atomic box: a real slot on the line, owns edges like text
            if depth < 32 and _is_genuine_inline_wrapper(node, node_style_obj):
                return any(is_text_bearing(child, depth + 1) for child in dom._child_nodes(node))
        return bool((getattr(node, "textContent", "") or "").strip())

    text_node_indices = [i for i, n in enumerate(child_nodes) if is_text_bearing(n)]
    if not text_node_indices:
        return []
    runs = []
    # Whitespace collapses across sibling-node boundaries the same way it
    # does across run boundaries in `_make_inline_formatting_plan`, but every
    # text run built below unconditionally strips both ends of its own
    # text, with no equivalent restoration. `pending_space` carries a
    # boundary space forward to whatever the next run turns out to be --
    # found rendering news.ycombinator.com: a nested `<span class=
    # "subline">` (flattened here, not via the top-level path) rendered
    # "Bluestein2 hours ago" without this.
    pending_space = False
    # Same CSS 2.1 16.6.1 reasoning as `_inline_mixed_content`'s has_content
    # guard: no leading-space token until real content has been emitted.
    has_content = False
    for node_index, child_node in enumerate(child_nodes):
        node_tag = (getattr(child_node, "tagName", "") or "").lower()
        if getattr(child_node, "nodeType", None) == dom.TEXT_NODE:
            node_raw = getattr(child_node, "textContent", None)
            if node_raw is None:
                node_raw = getattr(child_node, "data", "") or ""
            preserves = (paint_style.get("white_space") or "normal").strip().lower() in _PRESERVING_WHITE_SPACE
            raw_text = dom._collapsed_text_node(child_node)
            if not raw_text and not (preserves and node_raw):
                if node_raw:
                    pending_space = True
                continue
            has_leading_space = has_content and (pending_space or bool(
                node_raw[:1] and node_raw[:1] in _CSS_WHITESPACE_STRIP_CHARS))
            pending_space = False
            has_content = True
            is_first_text = node_index == text_node_indices[0]
            is_last_text = node_index == text_node_indices[-1]
            runs.extend(_runs_for_text(
                node_raw, paint_style, owner, child_node, has_leading_space=has_leading_space,
                leading=leading_edge if is_first_text else 0.0,
                trailing=trailing_edge if is_last_text else 0.0,
                top_edge=top_edge_val, extra_height=extra_height,
                margin_start=margin_start if is_first_text else 0.0,
                margin_end=margin_end if is_last_text else 0.0,
                margins_are_own=margins_are_own,
            ))
        elif node_tag == "br":
            pending_space = False
            # A forced line break starts a fresh line, same as this
            # container's own start -- whitespace right after it collapses too.
            has_content = False
            runs.append({"break": True, "element": child_node})
        elif dom._is_element(child_node) and computed_cache is not None:
            child_computed, child_style = dom._describe(child_node, computed_cache)
            if box_model._is_absolutely_positioned(child_style):
                # Out of flow -- doesn't occupy an inline-content slot, so a
                # pending space isn't consumed here, and still belongs
                # before whatever comes next.
                runs.append({"escapee": True, "element": child_node,
                             "computed": child_computed, "style": child_style})
                continue
            if _is_atomic_inline(child_node, child_computed, child_style) or box_model._is_floated(child_computed):
                # An atomic inline-level box (replaced, inline-block, ...)
                # or a float: one opaque slot on the line, built as a real
                # child of the inline node. A float is out of flow and
                # invisible to white-space collapsing, so a pending space
                # carries past it to whatever real content follows.
                floated = box_model._is_floated(child_computed)
                is_first_text = node_index == text_node_indices[0]
                is_last_text = node_index == text_node_indices[-1]
                if not floated and has_content and pending_space:
                    runs.append(_make_collapsed_space_run(paint_style, owner, top_edge_val, extra_height))
                if not floated:
                    pending_space = False
                    has_content = True
                run_leading = leading_edge if is_first_text else 0.0
                run_trailing = trailing_edge if is_last_text else 0.0
                run_margin_start = margin_start if is_first_text else 0.0
                run_margin_end = margin_end if is_last_text else 0.0
                if floated and (run_leading or run_trailing or run_margin_start or run_margin_end):
                    # The float is out of flow; its wrapper's edges stay on
                    # the line as an empty inline box (CSS 2.1 9.5,
                    # float-in-inline-002.html).
                    strut = _empty_inline_strut_run(owner, run_leading, run_trailing, top_edge_val, extra_height,
                                                    run_margin_start)
                    strut["margin_end"] = run_margin_end
                    strut["own_margin_start"] = run_margin_start if margins_are_own else 0.0
                    strut["own_margin_end"] = run_margin_end if margins_are_own else 0.0
                    runs.append(strut)
                    run_leading = run_trailing = run_margin_start = run_margin_end = 0.0
                atomic = _atomic_run(
                    child_node, child_computed, child_style, owner, paint_style,
                    leading=run_leading, trailing=run_trailing,
                    top_edge=top_edge_val, extra_height=extra_height,
                    margin_start=run_margin_start, margin_end=run_margin_end,
                )
                atomic["own_margin_start"] = run_margin_start if margins_are_own else 0.0
                atomic["own_margin_end"] = run_margin_end if margins_are_own else 0.0
                runs.append(atomic)
                continue
            non_br_element_children = [
                node for node in dom._child_nodes(child_node)
                if dom._is_element(node) and (getattr(node, "tagName", "") or "").lower() != "br"
            ]
            if non_br_element_children and not _is_genuine_inline_wrapper(child_node, child_style):
                # A nested block/inline-block/replaced descendant needs its
                # own dedicated treatment (a 9.2.1.1 split, or an atomic
                # box) -- this function only flattens plain text, so it
                # silently drops it, same reasoning as the escapee case:
                # dropped, not consumed, a pending space still carries forward.
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
                margins_are_own=False,
            )
            if needs_leading_space and nested_runs:
                runs.append(_make_collapsed_space_run(paint_style, owner, top_edge_val, extra_height))
            # CSS 2.1's bidi box model (box.html#bidi-box-model) attaches
            # this element's own start-side package (border+padding+margin)
            # to the DOM-first non-break run and the end-side package to
            # the DOM-last one -- start=left/end=right for ltr, start=right/
            # end=left for rtl -- left-rtl-ref.xht. `leading_edge`/
            # `trailing_edge`/`margin_start`/`margin_end` above carry only
            # outer-ancestor contributions (always physical); this
            # element's own package is added directly onto the correct
            # physical field here since the recursive call's own params
            # can't express "this element's start edge is physically on the right".
            non_break_runs = [r for r in nested_runs if not r.get("break")]
            if non_break_runs:
                first_run, last_run = non_break_runs[0], non_break_runs[-1]
                is_rtl_nested = dom._element_direction(child_node, child_computed) == "rtl"
                # Tells measure()'s whole-line RTL mirror not to fall back
                # to the owner's raw CSS margin-right even when margin_end
                # is zero -- zero is a real, direction-aware answer here.
                first_run["_bidi_margin_resolved"] = True
                last_run["_bidi_margin_resolved"] = True
                if first_run is last_run:
                    # Unsplit -- the sole run owns both edges physically,
                    # regardless of direction; only a line-break split
                    # invokes the start/end swap.
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["own_margin_start"] = first_run.get("own_margin_start", 0.0) + nested_margin_left
                    first_run["own_margin_end"] = first_run.get("own_margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_right
                                                      + nested_margin_left + nested_margin_right)
                elif is_rtl_nested:
                    first_run["trailing"] = first_run.get("trailing", 0.0) + nested_right
                    first_run["margin_end"] = first_run.get("margin_end", 0.0) + nested_margin_right
                    first_run["own_margin_end"] = first_run.get("own_margin_end", 0.0) + nested_margin_right
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_right + nested_margin_right)
                    last_run["leading"] = last_run.get("leading", 0.0) + nested_left
                    last_run["margin_start"] = last_run.get("margin_start", 0.0) + nested_margin_left
                    last_run["own_margin_start"] = last_run.get("own_margin_start", 0.0) + nested_margin_left
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_left + nested_margin_left)
                else:
                    first_run["leading"] = first_run.get("leading", 0.0) + nested_left
                    first_run["margin_start"] = first_run.get("margin_start", 0.0) + nested_margin_left
                    first_run["own_margin_start"] = first_run.get("own_margin_start", 0.0) + nested_margin_left
                    first_run["intrinsic_width"] = (first_run.get("intrinsic_width", 0.0)
                                                      + nested_left + nested_margin_left)
                    last_run["trailing"] = last_run.get("trailing", 0.0) + nested_right
                    last_run["margin_end"] = last_run.get("margin_end", 0.0) + nested_margin_right
                    last_run["own_margin_end"] = last_run.get("own_margin_end", 0.0) + nested_margin_right
                    last_run["intrinsic_width"] = (last_run.get("intrinsic_width", 0.0)
                                                     + nested_right + nested_margin_right)
            runs.extend(nested_runs)
    return runs



def _is_genuine_inline_wrapper(node, style_obj) -> bool:
    """Whether `node` is a genuinely display:inline wrapper, as opposed to
    an atomic inline-level box (inline-block, replaced). Only a genuine
    wrapper's content is reachable "through" it for CSS 2.1 9.2.1.1
    (`_contains_in_flow_block`) -- an inline-block owns its own BFC entirely."""
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
    """Like `_contains_in_flow_block`, but returns the first descendant node
    itself rather than a bool -- used so a nested wrapper's own marker
    fragment (`_finalize_inline_owner_boxes`) has a real box to read
    geometry from. `None` if nothing qualifies."""
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
    # The split's leading/trailing fragments carry the wrapper's logical
    # start/end edge, not always its physical left/right -- in
    # direction:rtl, the first-generated fragment owns the right
    # border/padding/margin and the last owns the left, swapped from ltr.
    # left_edge/right_edge/margin_left/margin_right stay physical below;
    # which segment (first vs last) each routes to is decided per-segment
    # further down instead.
    is_rtl = dom._element_direction(wrapper, wrapper_computed) == "rtl"
    top_edge_val = box_model._numeric_edge(native["padding"][0]) + box_model._numeric_edge(native["border"][0])
    extra_height = (top_edge_val + box_model._numeric_edge(native["padding"][2])
                    + box_model._numeric_edge(native["border"][2]))
    margin_left = box_model._numeric_edge(native["margin"][3])
    margin_right = box_model._numeric_edge(native["margin"][1])
    if wrapper is container:
        # The direct-child shape: `wrapper` is a real Taffy node, so Taffy
        # already physically shifted its text-leaf children by this same
        # border/padding/margin -- zeroed here to avoid double-counting
        # horizontal position; vertical position is corrected via
        # `_chromonic_split_self_edges` in `_finalize_inline_owner_boxes` instead.
        wrapper._chromonic_split_self_edges = (
            (right_edge if is_rtl else left_edge), (left_edge if is_rtl else right_edge), top_edge_val)
        left_edge = right_edge = margin_left = margin_right = 0.0
    else:
        wrapper.__dict__.pop("_chromonic_split_self_edges", None)
    paint_style = wrapper._chromonic_paint_style

    segments: list = [[]]
    blocks: list = []
    # A child that's itself an inline wrapper reaching a block only through
    # further nesting must not fall through to `segments[-1].append(node)`
    # -- `_build_text_runs_from_nodes` has no notion of a nested block and
    # drops it. Recorded by segment index instead, split via recursive
    # delegation at yield time.
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
                # CSS 2.1 9.2.1.1 only applies to an in-flow block child --
                # a float:left/right one is out of flow (still blockified
                # per 9.7, but never forces a split): it stays ordinary
                # segment content, built as its own atomic subtree below,
                # the same way an inline-block already is.
                if (not box_model._is_absolutely_positioned(child_style) and not box_model._is_floated(child_computed)
                        and not box_model._is_inline_level(node, child_style)):
                    # The split doesn't change this block's own normal-flow
                    # containing block or positioning rules, direction:rtl included.
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

    # getClientRects(): Chrome exposes one extra, zero-height rect per
    # interruption, positioned where the interrupting block sits -- a
    # 2-fragment split's element.getClientRects() returns 3 rects in real
    # Chrome, not 2. `_finalize_inline_owner_boxes` adds these once boxes are
    # final; recorded here (overwritten fresh each relayout) rather than
    # recomputed there, where only owner_accum's already-merged rects are
    # visible. A nested-wrapper interruption reports the first real block
    # reachable through it, so every ancestor level gets its own marker at
    # that same position, matching Chrome.
    wrapper._chromonic_interruption_blocks = [
        block if block is not None else _first_reachable_in_flow_block(nested_wrapper_at[index], computed_cache)
        for index, (block, _computed, _style) in enumerate(blocks)
    ]
    wrapper._chromonic_split_edge_flow_height = {}
    # `wrapper` itself is never built as a real Taffy node when it's a
    # nested wrapper -- it never ends up in `node_map`, so a walk of
    # `node_map.values()` (e.g. `_fix_nested_split_flow_extent`) can't discover
    # it directly. Each real interruption block is a genuine node, so a
    # back-reference on it is a reliable way back to its wrapper.
    for block, _computed, _style in blocks:
        if block is not None:
            block.__dict__["_chromonic_split_wrapper_ref"] = wrapper

    for index, seg_nodes in enumerate(segments):
        is_first, is_last = index == 0, index == len(segments) - 1
        # direction:rtl: the first segment owns the start (physical-right)
        # package, the last owns the end (physical-left) one -- swapped
        # from ltr's first=left/last=right (CSS 2.1's bidi box model) --
        # left-rtl-ref.xht.
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
            # fragment when it has real inline extent (own padding) -- CSS
            # 2.1 9.2.1.1's split generates an anonymous inline box there
            # even with no text. A segment with no inline extent is 0x0.
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
            # Genuinely nothing on this side -- still an explicit 0x0
            # fragment (Chrome reports one), no height/font/flow of its own.
            runs = [_empty_decoration_only_run(wrapper, 0.0, 0.0, 0.0, 0.0, 0.0)]
        if runs:
            # Tags each run with its segment index (document order), so
            # `_finalize_inline_owner_boxes` places interruption markers
            # logically rather than via a geometric sort.
            for run in runs:
                if not run.get("break"):
                    run["split_group"] = index
                    if is_rtl and (is_first or is_last):
                        # Zero margin_end/margin_start on this segment is a
                        # real, direction-aware answer -- suppress
                        # measure()'s owner-raw-margin fallback.
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
    anonymous block box case (`<span>One<div/>Two</span>`: the block forces
    `span` to split into a "One" fragment, the block, and a "Two" fragment,
    siblings in normal block flow).

    Returns an ordered list of pieces -- `("plan", _InlineFormattingPlan)`
    or `("block", child, child_computed, child_style)` -- ready to become
    `element`'s ordinary block-stack Taffy children. `None` if nothing
    needs splitting.

    Two shapes reach this function (`_has_direct_in_flow_block_child`
    distinguishes them): `element` itself directly parenting the block
    (splits itself), or a plain block `element` containing a nested inline
    item that wraps a block deeper (only that nested item splits)."""
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
            # CSS 2.1 9.2.1.1: the wrapper's leading fragment isn't itself a
            # line break -- pending text before it belongs on the same line
            # box until the first real block interruption forces a split.
            # Seeded from `pending`'s runs instead of flushed as an
            # independent plan, which would stack it as its own row.
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
                        # leading/interior segment, never the wrapper's true
                        # trailing one. measure()'s RTL mirror must not
                        # apply the wrapper's margin-right on that false signal.
                        plan._chromonic_final_split_fragment = False
                        pieces.append(("plan", plan))
                        runs_acc = []
                    pieces.append(sub)
            if runs_acc:
                # Nothing follows this plan for `item` -- the real trailing
                # segment, where measure() applies the wrapper's
                # margin-right normally (the default for a non-split plan).
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
    Every item becomes a run: text, a flattened nested inline, an
    "escapee" marker (an absolutely positioned item's static position), an
    atomic inline-level box or float (built as a real child of the inline
    node, see `_atomic_run`) or a ::before/::after box. `allow_escapees`
    is accepted for compatibility and no longer changes anything."""
    runs = []
    for kind, item, collapsed, child_computed, child_style in inline_items:
        if kind == "break":
            # Forced line-break: store a sentinel run so measure() can end the line.
            runs.append({"break": True, "element": item})
            continue
        if kind == "element" and box_model._is_absolutely_positioned(child_style):
            runs.append({"escapee": True, "element": item, "computed": child_computed, "style": child_style})
            continue
        if kind == "element" and isinstance(item, dom._PseudoElement):
            runs.extend(_pseudo_runs(item, child_computed, child_style, element, computed_cache))
            continue
        if kind == "element" and (box_model._is_floated(child_computed)
                                  or _is_atomic_inline(item, child_computed, child_style)):
            # An atomic inline-level box or a float: a real child of the
            # inline node, sized by its own formatting context, placed by
            # measure() (CSS 2.1 9.2.2, 9.5, 10.3.9).
            item.__dict__.pop("_chromonic_interruption_blocks", None)
            item.__dict__.pop("_chromonic_split_container", None)
            if (not box_model._is_floated(child_computed)
                    and getattr(item, "_chromonic_leading_collapsed_space", False)):
                runs.append(_make_collapsed_space_run(element._chromonic_paint_style, element, 0.0, 0.0))
            runs.append(_atomic_run(item, child_computed, child_style, element, element._chromonic_paint_style))
            continue
        if kind == "text":
            source = item.source
            owner = element
            paint_style = element._chromonic_paint_style
            native = None
        else:
            # Nested markup is flattened into this plan when it contains
            # text and <br> breaks, recursively including further genuine
            # inline nesting (`_build_text_runs_from_nodes`). A nested block/
            # inline-block/replaced descendant can't be a flattened text
            # run, so it's silently dropped rather than bailing the whole
            # plan to the flex-row fallback, which mismeasures mixed text +
            # nested-inline content -- confirmed on news.ycombinator.com's
            # subtext line.
            # `item` is on the ordinary (non-split) path this layout --
            # clear any stale interruption-block bookkeeping a previous
            # layout's split may have left on it.
            item.__dict__.pop("_chromonic_interruption_blocks", None)
            item.__dict__.pop("_chromonic_split_container", None)
            # Walk childNodes to collect text segments and <br> breaks,
            # decorating them with the child element's border+padding edges
            # (first fragment gets left edge, last gets right) -- see
            # `_build_text_runs_from_nodes`, also reused by
            # `_split_wrapping_inline_element`.
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
            # fragment only; CSS 2.1 10.3.1/10.3.3: real spacing, never part
            # of either fragment's own rect, and never collapses with an
            # adjoining element's margin.
            margin_start = box_model._numeric_edge(native["margin"][3])
            margin_end = box_model._numeric_edge(native["margin"][1])
            # CSS 2.1's bidi box model attaches the start-side package
            # (border+padding+margin) to the DOM-first fragment and the
            # end-side package to the DOM-last one -- start=left/end=right
            # for ltr, swapped for rtl -- left-rtl-ref.xht.
            # `_build_text_runs_from_nodes`'s leading_edge/trailing_edge params
            # are always physical, so for a direction:rtl item the packages
            # are built unswapped here and swapped onto the correct
            # fragment below, once the real (possibly <br>-split) fragments
            # are known.
            is_rtl_item = dom._element_direction(item, child_computed) == "rtl"
            # Flattened into this plan: no Taffy box of its own this pass
            # (build() clears the mark when it does build the element).
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
            child_runs = _with_pseudo_runs(item, child_runs, computed_cache)
            leading_space_run = None
            if getattr(item, "_chromonic_leading_collapsed_space", False) and child_runs:
                # A whitespace-only text node right before this element
                # collapsed to nothing of its own (set in
                # `_inline_mixed_content`) -- restored as its own standalone
                # run rather than merged into this element's first token --
                # merged, a wrap point between the space and this element's
                # content left a stray zero-width space on the previous
                # line -- inline-formatting-context-013.xht.
                leading_space_run = _make_collapsed_space_run(
                    element._chromonic_paint_style, element, top_edge_val, extra_height)
            if is_rtl_item:
                non_break_runs = [r for r in child_runs if not r.get("break")]
                if non_break_runs:
                    first_run, last_run = non_break_runs[0], non_break_runs[-1]
                    # See the identical tag in `_build_text_runs_from_nodes`'s
                    # nested-element branch: a zero margin_end here is a
                    # real, direction-aware answer, not an unthreaded field.
                    first_run["_bidi_margin_resolved"] = True
                    last_run["_bidi_margin_resolved"] = True
                    if first_run is last_run:
                        # Unsplit -- the sole run owns both edges physically,
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
                # CSS 2.1 9.2.1.1/10.8's empty-inline strut applies only to a
                # plain non-replaced display:inline -- inline-block/replaced
                # keeps its own explicit width/height even empty (10.3.10).
                child_runs = [_empty_inline_strut_run(
                    item, left_edge, right_edge, top_edge_val, extra_height, margin_start,
                )]
            if leading_space_run is not None:
                runs.append(leading_space_run)
            runs.extend(child_runs)
            continue
        raw = getattr(source, "textContent", None)
        if raw is None:
            raw = getattr(source, "data", None) or collapsed or ""
        runs.extend(_runs_for_text(
            raw, paint_style, owner, source,
            has_leading_space=getattr(item, "_chromonic_leading_collapsed_space", False),
        ))
    # DOM boundaries with identical shaping properties are not kerning
    # boundaries. Preserve the pair adjustment across adjacent text owners.
    shaping_keys = ("font_family", "font_size", "font_weight", "font_style",
                    "letter_spacing", "word_spacing")
    # CSS 2.1 16.6.1: collapsible spaces collapse across element boundaries
    # too -- a trailing-space run followed by a leading-space one keeps
    # just one -- abspos-inline-001.xht.
    previous = None
    for run in runs:
        if run.get("break") or run.get("escapee") or run.get("float"):
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
        if left.get("atomic") or right.get("atomic") or left.get("empty_strut") or right.get("empty_strut"):
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
    line height rather than guessing. domonic resolves an explicit
    line-height to a plain "Npx" string, so this is just
    `_fontmetrics.parse_length` guarded against the unset/normal case."""
    if not value or value == "normal":
        return None
    return _fontmetrics.parse_length(value, default=None)



def _make_measure(paint_style: dict, text: str, element):
    """Real text layout via Parley (`chromonic._native.layout_text`) -- font
    matching, shaping, and genuine Unicode line-breaking. `font_family` is
    read from `paint_style` (already extracted by `dom._describe`), not
    re-resolved from `computed.fontFamily`. Parley's job is strictly
    layout; `chromonic.fonts` separately resolves the actual Skia typeface."""
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
    # CSS Sizing 3's min-content carve-out: overflow-wrap:break-word
    # (unlike `anywhere`) must not shrink the min-content contribution
    # below "widest whole word" -- it only breaks a word that would
    # overflow its line, which never happens at min-content's own
    # infinitely-narrow constraint. word-break:break-all has no such
    # carve-out. Matches Parley's OverflowWrap::BreakWord doc note.
    breaks_within_words_at_min_content = word_break == "break-all" or overflow_wrap == "anywhere"
    ascent, descent, normal_height = fonts.text_metrics(font_family, font_size, font_weight >= 600, italic)

    nowrap = paint_style.get("white_space") in ("pre", "nowrap")
    glyph_height = ascent + descent
    used_line_height = line_height if line_height is not None else normal_height
    first_above = ascent + math.floor((used_line_height - glyph_height) / 2)
    text_kwargs = dict(font_weight=font_weight, italic=italic, letter_spacing=letter_spacing,
                       word_spacing=word_spacing, line_height=line_height,
                       word_break=word_break, overflow_wrap=overflow_wrap)

    def lay_out(chunk, max_width):
        width, height, lines = layout_text(chunk, font_family, font_size, max_width=max_width, **text_kwargs)
        if line_height is None and lines:
            # Chrome exposes integral line-box heights for platform fonts
            # while Parley's raw metrics are fractional -- normalize the
            # implicit normal line box before it accumulates down a page.
            lines = [(line_text, line_width, normal_height) for line_text, line_width, _height in lines]
            height = normal_height * len(lines)
        return width, height, lines

    def lay_out_in_bands(band_list, available_width):
        """CSS 2.1 9.5: line boxes beside a float are shortened. Every line
        of a text leaf is the same height, so the room for the line at `y`
        is the intersection of the bands its height crosses; consecutive
        lines with the same room are laid together in one Parley call, and
        a line with no room moves down to the next band edge."""
        def window(y):
            left, right, next_edge = 0.0, available_width, math.inf
            for top, bottom, band_left, band_width in band_list:
                if bottom <= y + 1e-6 or top >= y + used_line_height - 1e-6:
                    if top > y + 1e-6:
                        next_edge = min(next_edge, top)
                    continue
                left = max(left, band_left)
                right = min(right, band_left + band_width)
                if bottom < math.inf:
                    next_edge = min(next_edge, bottom)
            return left, right - left, next_edge

        lines_out, xs, ys, avail = [], [], [], []
        remaining = text
        y = 0.0
        guard = 0
        while remaining and guard < 4096:
            guard += 1
            left, room, next_edge = window(y)
            if room <= 1e-6 and next_edge < math.inf:
                y = next_edge
                continue
            # how many lines share this window before it changes
            count = 1
            while next_edge < math.inf and y + (count + 1) * used_line_height <= next_edge + 1e-6:
                count += 1
            if next_edge == math.inf:
                count = None
            _w, _h, lines = lay_out(remaining, None if nowrap else max(room, 1.0))
            if not lines:
                break
            taken = lines if count is None else lines[:count]
            for line_text, line_width, line_h in taken:
                lines_out.append((line_text, line_width, line_h))
                xs.append(left)
                ys.append(y)
                avail.append(room)
                y += line_h
            remaining = remaining[sum(len(line_text) for line_text, _lw, _lh in taken):]
        width = max((line_width for _t, line_width, _h in lines_out), default=0.0)
        return width, y, lines_out, xs, ys, avail

    def measure(available_width, available_height, _known_width=None, _known_height=None,
                atomics=(), bands=None, placed_floats=(), owns_bfc=False):
        # -1.0 is src/lib.rs's sentinel for Taffy's MinContent request: wrap
        # at every opportunity, so width is the widest unbreakable piece
        # (white-space:nowrap/pre has no break opportunities).
        min_content = available_width is not None and available_width < 0
        if min_content:
            available_width = None
        band_list = None
        if bands and available_width is not None and not min_content:
            band_list = [tuple(float(v) for v in band) for band in bands]
            if len(band_list) == 1 and band_list[0][2] <= 1e-6 and band_list[0][3] >= available_width - 1e-6:
                band_list = None  # nothing narrows any line
        line_xs = line_ys = line_avail = None
        if band_list is not None:
            width, height, lines, line_xs, line_ys, line_avail = lay_out_in_bands(band_list, available_width)
        else:
            width, height, lines = lay_out(text, None if nowrap else 1.0 if min_content else available_width)
        if (min_content and not nowrap and not breaks_within_words_at_min_content):
            # A wrapped line's width from Parley keeps its trailing space;
            # min-content is the widest word alone. Skipped when
            # word-break/overflow-wrap allow breaking within a word --
            # `width` already reflects that there, and re-measuring whole
            # words would overstate min-content.
            words = [word for word in re.split(r"[ \t\n\r\f]+", text) if word]
            if words:
                width = max(layout_text(word, font_family, font_size, font_weight=font_weight, italic=italic,
                                        letter_spacing=letter_spacing, word_spacing=word_spacing,
                                        line_height=line_height)[0] for word in words)
        # Stashed for paint.py -- the only record of how this text wrapped;
        # paint draws exactly these lines rather than re-wrapping itself.
        element._chromonic_text_lines = [line_text for line_text, _line_width, _line_height in lines]
        line_widths = [line_width for _line_text, line_width, _line_height in lines]
        # CSS Text 3 text-align:justify: every line but the last stretches
        # to fill the line box -- paint.py doesn't re-space individual
        # words, but the reported line width (hit-testing/getClientRects())
        # must still reflect the real filled extent, not shrink-to-fit.
        if (len(line_widths) > 1 and (paint_style.get("text_align") or "").strip().lower() == "justify"
                and available_width and 0 < available_width < 1_000_000):
            text_align_last = (paint_style.get("text_align_last") or "auto").strip().lower()
            stretch_last = text_align_last not in ("auto", "left", "start", "")
            last_index = len(line_widths) - 1
            line_widths = [
                (line_avail[index] if line_avail else available_width)
                if (index != last_index or stretch_last) else line_width
                for index, line_width in enumerate(line_widths)
            ]
        element._chromonic_text_line_widths = line_widths
        # Per-line offsets within the content box (only when floats
        # shortened some lines; None means every line starts at x=0 and
        # they stack `line_height` apart).
        element._chromonic_text_line_x = line_xs
        element._chromonic_text_line_y = line_ys
        element._chromonic_text_line_avail = line_avail
        element._chromonic_line_height = lines[0][2] if lines else font_size * 1.2
        if lines:
            first_baseline = (line_ys[0] if line_ys else 0.0) + first_above
            last_top = line_ys[-1] if line_ys else sum(line_h for _t, _w, line_h in lines[:-1])
            last_baseline = last_top + first_above
        else:
            first_baseline = last_baseline = None
        return (width, height, first_baseline, last_baseline, [])

    return measure
