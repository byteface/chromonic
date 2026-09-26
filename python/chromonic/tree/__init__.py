"""Walk a live domonic DOM, build a mirroring Taffy tree, run layout, and
write geometry back onto the domonic elements via `element.set_layout_box(...)`.

No dirty-bit tracking: `layout()` is meant to be called again, in full,
after any mutation (see PLAN.md).

Style resolution, not Taffy itself, dominates relayout cost, so `dom._describe`
builds exactly one `ComputedStyleDeclaration` per element per pass and shares
it across style-dict-building and `paint.py`'s paint-style extraction.

Split from a single 11,260-line tree.py into this package by concern (dom
plumbing, anonymous-box synthesis, inline formatting, table layout,
positioning, flex/grid, shared box-model/geometry primitives, and the
recursive `builder.build()`). Block, flex and grid layout are Taffy's, floats
included (`float`/`clear` reach Taffy's block formatting context); inline
formatting is chromonic's own node kind (`inline_formatting`, driven from
`src/lib.rs`'s `compute_inline_layout`). Cross-submodule calls go through qualified
module access (`from . import x` then `x.name(...)`), never `from .x import
name` -- several of these modules depend on each other in both directions
(e.g. dom <-> anonymous_boxes <-> inline_formatting), and only the qualified
form survives that: it only needs the *module* to exist when the import
statement runs, not the specific attribute, so it tolerates the cycle as
long as nothing at module or class body level (as opposed to inside a
function) needs another module's attribute before that module has finished
initializing. It also keeps monkeypatching (tests/test_chromonic.py patches
a couple of private names directly, e.g. `_shift_later_siblings_for_height_delta`)
working the same way it did when everything lived in one module: a patch to
`geometry._shift_later_siblings_for_height_delta` is seen by every caller,
because every caller looks it up through `geometry` at call time rather than
holding its own bound copy from `from .geometry import ...`."""

from __future__ import annotations

import functools
import logging

from domonic import bs4 as domonic_bs4
from domonic.dom import Element
from domonic.layout import LayoutBox, LayoutStyle
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from .. import style_bridge
from .._native import Tree, layout_text
from . import anonymous_boxes, box_model, builder, dom, flex_grid, geometry, inline_finalize, inline_formatting, positioning, replaced_elements, table_layout
from .box import box_of



_log = logging.getLogger(__name__)



def is_rust_panic(error: BaseException) -> bool:
    """Whether `error` is pyo3_runtime.PanicException -- what a genuine
    Rust-side panic (invariant violation inside Taffy/`_native`) surfaces
    as in Python. Pyo3 derives it from BaseException, not Exception, so
    every "a bad page must not crash the browser" `except Exception:`
    handler in this codebase has never been able to catch one. A caller
    that wants a Rust panic included catches BaseException and calls this.
    Matched by class name/module since pyo3 only creates that module
    lazily, on first panic -- not reliably importable up front."""
    return type(error).__module__ == "pyo3_runtime" and type(error).__name__ == "PanicException"


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
        self.root_ids = {}
        self._seen = set()

    def begin(self):
        self._seen.clear()
        # Deliberately not `self.node_map = {}` -- node_map is this
        # projection's only strong Python reference to each tracked element
        # (id(element), a bare memory address). Clearing it here would drop
        # that reference for a soon-to-be-stale element before finish()
        # prunes the Taffy side; since domonic elements hold parent/child
        # back-references, losing the last strong ref doesn't free it
        # immediately -- it becomes eligible for cyclic GC mid-build(), and
        # a new element's address landing on a just-collected one aliases
        # onto the stale entry in upsert(), a reproduced "invalid SlotMap
        # key used" panic. Old entries stay referenced (address unreusable)
        # until finish() explicitly removes them.

    def measure_changed(self, element, measure_key):
        previous = self.state.get(id(element))
        return previous is None or previous[2] != measure_key

    def upsert(self, element, style, children, measure, measure_key, kind="auto"):
        """`kind`: "auto" (Taffy dispatches on display), "table" (`Tree.new_table`) or "inline" (an
        inline formatting context, `Tree.new_inline`). A node's kind is
        fixed at creation, so an element that changes kind between
        layouts gets a fresh native node."""
        key = id(element)
        self._seen.add(key)
        children = tuple(children)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        if node is not None and previous is not None and previous[3] != kind:
            self.tree.remove(node)
            self.node_map.pop(node, None)
            node = None
            previous = None
        if node is None:
            if kind == "inline":
                node = self.tree.new_inline(style, measure, list(children))
            elif kind == "table":
                node = self.tree.new_table(style, measure, list(children))
            elif children:
                node = self.tree.new_with_children(style, list(children))
            elif measure is not None:
                node = self.tree.new_text_leaf(style, measure)
            else:
                node = self.tree.new_leaf(style)
            self.nodes[key] = node
        else:
            old_style, old_children, old_measure_key, _old_kind = previous
            if old_style != style:
                self.tree.set_style(node, style)
            if old_children != children:
                self.tree.set_children(node, list(children))
            if old_measure_key != measure_key:
                self.tree.set_measure(node, measure)
        # Keep an independent value snapshot. Image intrinsic sizing
        # mutates its cached dictionary in place; retaining that object would
        # make the next dirty comparison miss the change.
        self.state[key] = (_snapshot_style(style), children, measure_key, kind)
        self.node_map[node] = element
        return node

    def finish(self):
        stale = set(self.nodes) - self._seen
        for key in stale:
            node = self.nodes.pop(key)
            try:
                self.tree.remove(node)
            except BaseException as error:
                if not is_rust_panic(error):
                    raise
                # Some path not fully understood leaves `node` already
                # invalid in the Rust tree by the time this runs (begin()'s
                # GC-timing fix closes one way to reach this, evidently not
                # the only one). The intent here is just "make sure Taffy
                # doesn't still have this node" -- an already-invalid key
                # means that's already true, so treated as a no-op rather
                # than taking the whole browser process down; logged so a
                # recurrence leaves a trail.
                _log.exception(
                    "chromonic: LayoutProjection.finish() could not remove "
                    "an already-stale Taffy node (id=%r, tag=%r) -- treating "
                    "it as already gone",
                    key, box_of(self.node_map.get(node)).tag_name,
                )
            self.state.pop(key, None)
            # Drops this stale element's last strong reference -- see
            # begin() for why that must not happen any earlier than this.
            self.node_map.pop(node, None)

    def patch_style(self, element, **changes):
        """Apply known layout-field changes after their Domonic mutation.

        This is an explicit incremental bridge for callers that know exactly
        which translated Taffy fields their authoritative DOM write changed.
        Unknown CSS mutations must use ``layout()`` to reconcile normally.
        """
        key = id(element)
        node = self.nodes.get(key)
        previous = self.state.get(key)
        style = box_of(element).native_style
        if node is None or previous is None or style is None:
            raise KeyError("element is not present in this layout projection")
        style = dict(style)
        style.update(changes)
        box_of(element).native_style = style
        self.tree.set_style(node, style)
        _old_style, children, measure_key, kind = previous
        self.state[key] = (_snapshot_style(style), children, measure_key, kind)

    def patch_insets(self, updates):
        """Batch known ``(element, top, right, bottom, left)`` changes."""
        native_updates = []
        for element, top, right, bottom, left in updates:
            key = id(element)
            node = self.nodes.get(key)
            previous = self.state.get(key)
            style = box_of(element).native_style
            if node is None or previous is None or style is None:
                raise KeyError("element is not present in this layout projection")
            inset = [float(top), float(right), float(bottom), float(left)]
            native_updates.append((node, *inset))
            # Cached native style and its retained snapshot are independent;
            # update only the one changed field instead of copying a
            # ~50-property dict per animated element.
            style["inset"] = inset
            snapshot, _children, _measure_key, _kind = previous
            snapshot["inset"] = list(inset)
        self.tree.set_insets(native_updates)

    def compute(self, root_element, *, width, height=None, viewport_height=None):
        """Compute and publish geometry after explicit projection patches.
        `viewport_height`: see `layout()`'s own parameter of the same name."""
        root_id = self.root_ids[id(root_element)]
        _compute_root(self.tree, root_id, self.node_map, root_element, width, height, viewport_height)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )

    def layout(self, root_element, *, width, height=None, reuse_styles=False, viewport_height=None):
        """See the module-level `layout()` function for what every
        parameter here means -- this is the same operation, just against a
        retained projection that reuses native nodes by element identity
        instead of rebuilding the whole Taffy tree from scratch."""
        from .. import webfonts
        if webfonts.prepare_layout(root_element):
            reuse_styles = False
        self.begin()
        with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
            root_id = _build_root(self.tree, root_element, self.node_map,
                                  reuse_styles=reuse_styles, projection=self,
                                  viewport=(width, viewport_height) if viewport_height is not None else None)
        self.finish()
        self.root_ids[id(root_element)] = root_id
        _compute_root(self.tree, root_id, self.node_map, root_element, width, height, viewport_height)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )



def _snapshot_style(style):
    # style_bridge emits primitives/tuples and top-level lists of those.
    # Copy lists so later intrinsic-image mutation can't alias the snapshot.
    return {key: list(value) if isinstance(value, list) else value
            for key, value in style.items()}












def _document_element(root_element):
    """`<html>` when `root_element` is its `<body>`, else None. (domonic's
    `<body>.parentElement` is unreliable; `parentNode` works.)"""
    if box_of(root_element).tag_name != "body" and (
            getattr(root_element, "tagName", "") or "").lower() != "body":
        return None
    html = getattr(root_element, "parentNode", None)
    return html if (getattr(html, "tagName", "") or "").lower() == "html" else None


def _build_root(tree, root_element, node_map, *, reuse_styles=False, projection=None,
                viewport=None) -> int:
    """Build the layout tree for `root_element`. For a page's `<body>`:

    - the root is the initial containing block (CSS 2.1 10.1), a
      viewport-sized box when `viewport` = (width, height) is given, holding
      `<html>` plus every box whose containing block it is -- `position:
      fixed` boxes and absolutely positioned boxes with no positioned
      ancestor -- so Taffy resolves their insets against the viewport;
    - `<html>` is a real block box with `<body>` as an ordinary block child,
      so `<body>`'s margins, their collapsing with its children, and floats
      reaching past it are Taffy's block layout.

    Anything else is its own root."""
    html = _document_element(root_element)
    computed_cache: dict = {}
    if html is None:
        return builder.build(tree, root_element, node_map, computed_cache=computed_cache,
                             reuse_styles=reuse_styles, projection=projection)
    computed, style_obj = dom._describe(root_element, computed_cache, reuse_styles=reuse_styles)
    viewport_children: list = []
    body_is_cb = box_model._establishes_containing_block(style_obj)
    previous_sink = builder._viewport_sink
    builder._viewport_sink = viewport_children if viewport is not None else None
    try:
        body_id = builder.build(tree, root_element, node_map, computed=computed, style_obj=style_obj,
                                computed_cache=computed_cache, is_containing_block=body_is_cb,
                                escapees=None if body_is_cb else viewport_children,
                                reuse_styles=reuse_styles, projection=projection)
    finally:
        builder._viewport_sink = previous_sink
    boxes = box_of(root_element).setdefault("root_boxes", {})
    html_box = boxes.get("html")
    if html_box is None:
        html_box = boxes["html"] = anonymous_boxes._DocumentRootBox(html)
    from domonic.style import ComputedStyleDeclaration
    html_style = style_bridge.to_dict(LayoutStyle.from_computed(ComputedStyleDeclaration(html)))
    # CSS 2.1 9.4.1: the root element establishes a block formatting
    # context -- its children's margins stay inside it.
    html_style.update({"display": "block", "position": "relative", "inset": ["auto"] * 4,
                       "establishes_bfc": True})
    box_of(html_box).native_style = html_style
    html_children = [body_id] + ([] if viewport is not None else viewport_children)
    html_id = (projection.upsert(html_box, html_style, html_children, None, None)
               if projection else tree.new_with_children(html_style, html_children))
    node_map[html_id] = html_box
    if viewport is None:
        return html_id
    icb = boxes.get("icb")
    if icb is None:
        icb = boxes["icb"] = anonymous_boxes._InitialContainingBlock()
    icb_style = {"display": "block", "position": "relative", "width": float(viewport[0]),
                 "height": float(viewport[1]), "overflow": ("visible", "visible")}
    icb_children = [html_id] + viewport_children
    icb_id = (projection.upsert(icb, icb_style, icb_children, None, None)
              if projection else tree.new_with_children(icb_style, icb_children))
    node_map[icb_id] = icb
    return icb_id


def _compute_root(tree, root_id, node_map, root_element, width, height, viewport_height) -> None:
    """Lay the tree out in a `width` x viewport initial containing block and
    publish every box. Percentage heights on the root resolve against the
    viewport; the document's own height is its content's."""
    available_height = viewport_height if viewport_height is not None else height
    boxes = tree.compute(root_id, width, available_height)
    geometry._write_boxes(boxes, node_map)
    html_box = (box_of(root_element).root_boxes or {}).get("html")
    html = getattr(html_box, "element", None)
    if html is not None and hasattr(html, "__dict__"):
        html.__dict__["_layout_box"] = html_box.__dict__.get("_layout_box")


def warm_text_layout() -> None:
    """Pay Parley's one-time FontContext setup cost (~100ms, font
    enumeration) now, not during the first real page's first text --
    later calls reuse the process-lifetime context and are near-free."""
    layout_text("warm", "sans-serif", 16.0)












def layout(root_element, *, width: float, height: "float | None" = None, reuse_styles: bool = False,
           viewport_height: "float | None" = None) -> dict:
    """Build a fresh Taffy tree from `root_element` down, compute layout at
    width x height (height=None sizes to content), and write every node's
    box back onto its domonic element. Returns {node_id: element} for
    anyone (painting, hit-testing) who wants to walk the same tree.

    This is the whole invalidation story for the POC: call layout() again
    after any mutation and every affected box is recomputed and rewritten.

    reuse_styles=False (default) re-resolves every element's CSS from
    scratch. Pass reuse_styles=True only when nothing about any element's
    class/inline style/stylesheets could have changed since the last call
    (see `_describe`'s docstring) -- currently only native_browser.py's
    image-arrival poll qualifies.

    viewport_height, when given, corrects position:absolute/fixed elements
    with no positioned ancestor to resolve against the real viewport
    height rather than root_element's own (possibly content-grown) box."""
    from .. import webfonts
    if webfonts.prepare_layout(root_element):
        reuse_styles = False
    tree = Tree()
    node_map: dict[int, object] = {}
    with style_bridge.viewport(width, viewport_height if viewport_height is not None else height):
        root_id = _build_root(tree, root_element, node_map, reuse_styles=reuse_styles,
                              viewport=(width, viewport_height) if viewport_height is not None else None)
    _compute_root(tree, root_id, node_map, root_element, width, height, viewport_height)
    return _finish_layout_pass(tree, node_map, root_element, width=width, viewport_height=viewport_height)



def _scan_layout_pass_features(node_map: dict) -> dict:
    """One combined O(node count) pre-scan, collecting cheap presence flags
    that let `_finish_layout_pass` skip a downstream correction pass's own
    O(node count) scan when a page has none of what that pass looks for,
    instead of every one of its ~15 passes unconditionally re-walking the
    tree even on pages with no floats, absolute/fixed positioning, or
    split-inline/flex-row-approximation machinery. Each flag mirrors the
    exact marker attribute its pass already gates on internally, so this
    changes nothing about what any pass does, only whether it runs."""
    has_absolute = False
    has_absolute_or_fixed = False
    has_table = False
    for element in node_map.values():
        if getattr(element, "__dict__", None) is None:
            continue
        state = box_of(element)
        if state.is_table_root:
            has_table = True
        style = state.native_style
        if style is not None:
            position = style.get("position")
            if position == "absolute":
                has_absolute = True
                has_absolute_or_fixed = True
            elif position == "fixed":
                has_absolute_or_fixed = True
    return {
        "absolute": has_absolute,
        "absolute_or_fixed": has_absolute_or_fixed,
        "table": has_table,
    }



def _finish_layout_pass(tree_obj, node_map, root_element, *, width, viewport_height):
    """The post-tree.compute() correction pipeline, shared by every entry
    point that computes real Taffy geometry (layout(),
    LayoutProjection.layout()/.compute()) -- previously duplicated verbatim
    across all three, which is how fixes wired into only one of them
    silently never ran for a real, incrementally-updated page."""
    features = _scan_layout_pass_features(node_map)
    # Before the absolute-positioning fixups below: an inline-context
    # escapee's real static position is only known once
    # `_InlineFormattingPlan.publish()` has run --
    # `positioning._fix_absolute_static_position_fallback` reads
    # box_of(element).static_position, which this sets.
    inline_finalize._publish_inline_formatting(node_map)
    shrink_to_fit_shifted = False
    # Both shrink-to-fit fixes above reposition their whole subtree via
    # `geometry._write_boxes` (a fresh, isolated tree.compute()) + `_shift_subtree`
    # -- but `geometry._write_boxes` only touches elements with a real Taffy
    # node, never a plain inline element whose box comes from
    # `_publish_inline_formatting`'s fragment-based tracking
    # (`_finalize_inline_owner_boxes`). `_shift_subtree` still walks and
    # shifts those too, double-applying the correction on top of a box
    # that was never reset. Re-publishing here recomputes every such
    # element's box fresh from its now-correct container instead of
    # shifting a stale one -- confirmed on a one-cell auto-width table
    # whose <span> landed a full shrink-to-fit correction off from its <td>.
    #
    # Only worth its own O(node count) pass when a shift actually
    # happened -- `_finish_layout_pass` also runs on every high-frequency
    # incremental LayoutProjection.compute() call, where an unconditional
    # second full-tree republish was a measurable per-frame cost on pages
    # with no floats or auto-width tables.
    if shrink_to_fit_shifted:
        inline_finalize._publish_inline_formatting(node_map)
    # After the shrink-to-fit pass: that recomputes an auto-width table's
    # whole subtree from scratch (`geometry._write_boxes`), which would discard
    # any row heights distributed before it.
    if features["table"]:
        table_layout._publish_table_column_boxes(node_map)
    replaced_elements._publish_svg_shape_boxes(node_map)
    if features["absolute"]:
        positioning._fix_absolute_width_against_containing_block(node_map)
        positioning._fix_absolute_static_position_fallback(node_map)
    return node_map


from .anonymous_boxes import (
    _ANONYMOUS_COMPUTED_DEFAULTS, _AnonymousInlineRun, _AnonymousTableBox,
    _AnonymousTextFragment, _RowspanPlaceholder, _SyntheticComputed,
    _TABLE_INTERNAL_KINDS, _TABLE_PART_DISPLAYS, _TABLE_PART_TAGS, _normalized_child_nodes,
    _synthesize_anonymous_style, _table_part_kind, _wrap_inline_runs,
    _wrap_missing_table_boxes
)
from .box_model import (
    _BASELINE_ALIGNMENTS, _REPLACED_OR_CONTROL_TAGS, _USUALLY_INLINE_TAGS, _alignment_parts,
    _establishes_bfc, _establishes_containing_block, _is_absolutely_positioned, _is_floated, _is_inline_level, _numeric_edge,
    _trusts_computed_inline, _ua_stylesheet_applied
)
from .dom import (
    ELEMENT_NODE, TEXT_NODE, _NON_RENDERING_DISPLAYS, _NON_RENDERING_TAGS,
    _NO_GENERATED_CONTENT_TAGS, _PseudoElement, _apply_text_transform, _child_elements,
    _child_nodes, _clear_stale_layout_geometry, _collapsed_text_node,
    _css_generated_content_text, _describe, _element_direction, _extract_generated_content,
    _extract_paint_style, _get_pseudo_object, _is_element, _layout_parent, _own_text,
    _pseudo_generates_box, _rendering_text_content, _renders
)
from .flex_grid import (
    _FLEX_DISPLAYS, _GRID_AREA_LINE_RE, _GRID_AREA_SPAN_RE, _css_order,
    _is_flex_or_grid_item, _parse_grid_area, _parse_grid_area_token
)
from .geometry import (
    _grow_and_reflow, _grow_box_height, _needed_ancestor_growth, _shift_anonymous_boxes,
    _shift_box, _shift_later_siblings_for_height_delta, _shift_recomputed_subtree,
    _shift_subtree, _write_boxes
)
from .inline_finalize import (
    _finalize_inline_owner_boxes, _fix_split_inline_relative_offset, _inline_relative_offset, _is_flattened_inline,
    _merge_adjacent_same_line_rects, _publish_inline_formatting
)
from .inline_formatting import (
    _CSS_COLLAPSIBLE_WHITESPACE_RE, _CSS_WHITESPACE_STRIP_CHARS, _InlineFormattingPlan,
    _block_margins_collapse_through, _build_text_runs_from_nodes, _collapse_margin_set,
    _contains_in_flow_block, _empty_decoration_only_run, _empty_inline_strut_run,
    _first_reachable_in_flow_block,
    _has_direct_in_flow_block_child, _inline_mixed_content, _inline_text_style,
    _is_atomic_inline, _is_genuine_inline_wrapper, _make_collapsed_space_run,
    _make_inline_formatting_plan, _make_measure, _parse_font_weight, _resolve_text_indent,
    _resolved_line_height, _split_inline_flow_around_blocks, _split_wrapping_inline_element
)
from .positioning import (
    _find_containing_block_ancestor,
    _fix_absolute_static_position_fallback, _fix_absolute_width_against_containing_block, _flex_container_static_position, _resolve_inset
)
from .replaced_elements import (
    _apply_canvas_intrinsic_size,
    _apply_iframe_intrinsic_size, _apply_image_intrinsic_size, _apply_svg_intrinsic_size,
    _form_control_display_text, _numeric_or_zero, _publish_svg_shape_boxes,
    _resolve_replaced_percent_height, _select_display_text,
    _stretched_replaced_flex_item
)
from .table_layout import (
    _BORDER_ORIGIN_PRIORITY, _BORDER_SIDE_ATTR, _BORDER_STYLE_PRIORITY, _ROW_GROUP_TAG_KIND,
    _TABLE_INTERNAL_DISPLAYS, _border_candidate, _cell_span, _column_elements, _compute_fixed_column_widths, _is_inline_table_box, _is_table_cell_display,
    _is_table_root_display, _is_table_row_display, _publish_table_column_boxes, _resolve_collapsed_border,
    _resolve_collapsed_table_borders, _row_cells, _row_group_kind, _table_cell_has_content, _table_columns, _table_grid, _table_rows
)
from .builder import (
    _build_inline_node, _measure_key, build
)

