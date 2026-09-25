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
from domonic.layout import LayoutBox
from domonic.style import ComputedStyleDeclaration
from domonic.utils import Utils

from .. import style_bridge
from .._native import Tree, layout_text
from . import anonymous_boxes, box_model, builder, dom, flex_grid, geometry, inline_finalize, inline_formatting, positioning, replaced_elements, table_layout



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
                    key, getattr(self.node_map.get(node), "_chromonic_tag_name", None),
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
        style = getattr(element, "_chromonic_native_style", None)
        if node is None or previous is None or style is None:
            raise KeyError("element is not present in this layout projection")
        style = dict(style)
        style.update(changes)
        element._chromonic_native_style = style
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
            style = getattr(element, "_chromonic_native_style", None)
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
        root_id = self.nodes[id(root_element)]
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        geometry._write_boxes(boxes, self.node_map)
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
            root_id = builder.build(
                self.tree, root_element, self.node_map,
                reuse_styles=reuse_styles, projection=self,
            )
        self.finish()
        available_width = _constrain_root_to_document_element(self.tree, root_element, root_id, width)
        compute_height = _root_compute_height(root_element, height, viewport_height)
        boxes = self.tree.compute(root_id, available_width, compute_height)
        geometry._write_boxes(boxes, self.node_map)
        return _finish_layout_pass(
            self.tree, self.node_map, root_element, width=width, viewport_height=viewport_height,
        )



def _snapshot_style(style):
    # style_bridge emits primitives/tuples and top-level lists of those.
    # Copy lists so later intrinsic-image mutation can't alias the snapshot.
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
    """Corrects `root_element` (<body>)'s Taffy style for <html>'s box-model
    edges (`_document_element_box_edges`) before compute() runs, and returns
    the available width <body> must be computed against (width minus
    <html>'s horizontal edges).

    <body>'s width:auto is always forced to a definite content-box number
    rather than left for Taffy to resolve -- a root node with only
    out-of-flow children would otherwise shrink-to-fit to 0.
    box-sizing:border-box is left alone, since there width already means
    the border-box total."""
    edges = _document_element_box_edges(root_element)
    html_left, html_right = edges[0:2] if edges is not None else (0.0, 0.0)
    style = root_element.__dict__.get("_chromonic_native_style")
    body_margin = style.get("margin") if style is not None else None
    body_margin_left = positioning._resolve_inset((body_margin or (0.0,) * 4)[3], width) or 0.0
    body_margin_right = positioning._resolve_inset((body_margin or (0.0,) * 4)[1], width) or 0.0
    available_width = max(0.0, width - html_left - html_right)
    if style is not None and style.get("width") == "auto":
        outer_width = max(0.0, available_width - body_margin_left - body_margin_right)
        if style.get("box_sizing") != "border-box":
            padding = style.get("padding") or (0.0,) * 4
            border = style.get("border") or (0.0,) * 4
            outer_width = max(0.0, outer_width
                               - box_model._numeric_edge(padding[1]) - box_model._numeric_edge(padding[3])
                               - box_model._numeric_edge(border[1]) - box_model._numeric_edge(border[3]))
        style["width"] = outer_width
        tree_obj.set_style(root_id, style)
    return available_width



def warm_text_layout() -> None:
    """Pay Parley's one-time FontContext setup cost (~100ms, font
    enumeration) now, not during the first real page's first text --
    later calls reuse the process-lifetime context and are near-free."""
    layout_text("warm", "sans-serif", 16.0)



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
    # A genuine author flexbox body is left alone: real flex containers
    # don't collapse margins with children.
    if style.get("display") != "block":
        return
    if any(value not in (0.0, "auto") for name in ("padding", "border")
           for value in style.get(name, ())):
        return
    # CSS 2.1 8.3.1: a block's own top margin collapses with its first
    # in-flow child's only when the block doesn't establish a new BFC --
    # and any overflow other than visible does that. Taffy has no such
    # concept, so leaving it alone here and letting
    # `_apply_root_margin_offset` add body's margin normally already gives
    # the correct, uncollapsed result.
    if False and any(value != "visible" for value in style.get("overflow", ("visible", "visible"))):
        # Disabled: CSS 2.1 8.3.1's adjoining-margins list doesn't name a
        # block's own overflow as a blocking condition (only border/padding
        # or clearance) -- overflow establishing a BFC stops a grandchild's
        # margin from escaping, a different pairing than this element's own
        # margin against its direct child's. body{overflow:hidden}'s
        # first-child margin still collapsed with body's own in Chrome --
        # css-grid/layout-algorithm/grid-as-flex-item-should-not-shrink-to-fit-001.html.
        return
    boxes = []
    visible_boxes = []
    float_top = None
    # The layout tree's own view of the children: a 9.2.1.1 anonymous
    # block around leading loose text is the real first in-flow child here
    # -- table-anonymous-objects-093.xht.
    for child in (root_element.__dict__.get("_chromonic_normalized_children")
                  or dom._child_nodes(root_element)):
        if not dom._is_element(child):
            continue
        child_style = getattr(child, "_chromonic_native_style", {})
        if child_style.get("position") in ("absolute", "fixed"):
            continue
        # CSS 2.1 10.6.3: an ordinary block's auto height is the distance to
        # its last in-flow child's bottom margin edge -- floats are out of
        # flow for this (plain body never establishes a BFC, so 10.6.7's
        # float-inclusive algorithm doesn't apply). Taffy has no float
        # concept, so an un-flex-rowed floated child would otherwise count
        # fully toward bottom like real content.
        resolved = getattr(child, "_chromonic_resolved_style", None)
        if resolved is not None and box_model._is_floated(resolved[0]):
            # A leading float sits at body's content top; the first in-flow
            # box may still be below it at this point (its <br clear> line
            # is only placed beside the float later, in
            # `_fix_float_flow_after_block_sibling`), so the float's top
            # bounds body's -- image-as-flexitem-size-001.html.
            float_box = child.__dict__.get("_layout_box")
            if float_box is not None and not boxes:
                float_top = float_box.y if float_top is None else min(float_top, float_box.y)
            continue
        # A child that dissolved into a 9.2.1.1 split reports its
        # `_chromonic_flow_extent_box` (decoration-free, available earlier in
        # the pipeline) rather than its own `_layout_box`, which for such a
        # child isn't written until `inline_finalize._publish_inline_formatting`
        # runs, several passes later. Checking _layout_box first meant this
        # function silently skipped a still-mid-split child and never set
        # `_chromonic_margin_collapsed`, letting `_apply_root_margin_offset`
        # wrongly add the root's margin a second time.
        box = getattr(child, "_chromonic_flow_extent_box", None)
        if box is None:
            box = child.__dict__.get("_layout_box")
        if box is None:
            continue
        boxes.append(box)
        # A CSS-empty box (no border/padding/height, no in-flow content)
        # doesn't stop a preceding margin from collapsing through it;
        # counting it toward bottom would double-count that margin.
        # box.height == 0 alone isn't enough to conclude "no in-flow
        # content" -- a negative child margin can pull a non-empty
        # wrapper's auto-height back to zero too.
        has_in_flow_content = any(
            dom._is_element(node) and getattr(node, "_chromonic_resolved_style", None) is not None
            and not box_model._is_absolutely_positioned(node._chromonic_resolved_style[1])
            and not box_model._is_floated(node._chromonic_resolved_style[0])
            for node in dom._child_nodes(child)
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
    # CSS 2.1 10.6.3: auto height is anchored to the first and last in-flow
    # child's own margin edges -- not the extent of whichever child reaches
    # furthest. boxes is already in DOM order, so those are literally the
    # first/last entries. This only differs from a plain min/max when a
    # negative margin makes an earlier sibling's box stick out past a
    # later one -- Chrome still tracks the real last child regardless.
    top = boxes[0].y if float_top is None else min(boxes[0].y, float_top)
    bottom = boxes[-1].y + boxes[-1].height
    old = root_element.__dict__.get("_chromonic_pristine_box") or root_element.__dict__.get("_layout_box")
    # `old` (the pristine, pre-offset box) is only right for the
    # scroll-extent baseline below -- on the second pass
    # `_apply_root_margin_offset` has since shifted the real box
    # horizontally, and rebuilding from the stale pristine x would
    # silently discard that shift.
    current = root_element.__dict__.get("_layout_box") or old
    if old is not None:
        # The escaped final margin still contributes to the document's
        # scroll extent even though it's outside
        # body.getBoundingClientRect() -- the scrollable area needs the
        # true max over every child, first/last or not.
        explicit_height = style.get("height") != "auto"
        # Auto-height can go negative when a child's negative margin pulls
        # bottom back above top -- a used height is never negative (CSS
        # 2.1 8.1/10.5), so this clamps to 0 like Chrome does.
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



def _in_root_anchored_subtree(node, root_anchored_ids) -> bool:
    while node is not None:
        if id(node) in root_anchored_ids:
            return True
        node = getattr(node, "parentElement", None)
    return False



def _apply_root_margin_offset(root_element, node_map: dict) -> None:
    """Shift the whole laid-out tree by the compute root's own margin.

    Taffy's root-compute has no parent context, so a root with
    width/height:auto correctly shrinks to leave room for its own margin
    but never offsets its own box by it -- the root always comes back at
    (0, 0) regardless of margin.

    chromonic hands Taffy <body> as this root, but CSS-wise <html> is the
    real root, and <body>'s margin genuinely offsets it within <html>'s
    content box.

    Root-anchored position:absolute/fixed elements are excluded: their
    containing block is the viewport, unaffected by <body>'s margin. The
    vertical axis is skipped when `_adjust_body_collapsed_margins` already
    folded the root's top margin into its position."""
    style = getattr(root_element, "_chromonic_native_style", None)
    box = root_element.__dict__.get("_layout_box")
    if style is None or box is None:
        return
    margin_top, _margin_right, _margin_bottom, margin_left = style["margin"]
    # CSS: a percentage margin resolves against the containing block's
    # *width* on every side, vertical included -- not a typo.
    dx = positioning._resolve_inset(margin_left, box.width) or 0.0
    already_collapsed = "_chromonic_margin_collapsed" in root_element.__dict__
    dy = 0.0 if already_collapsed else (positioning._resolve_inset(margin_top, box.width) or 0.0)
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
        if dom._is_element(element) and positioning._is_root_anchored(element):
            root_anchored_ids.add(id(element))
    seen = set()
    for node in list(node_map.values()):
        if id(node) in seen:
            continue
        seen.add(id(node))
        owner = node if dom._is_element(node) else getattr(node, "parent", None)
        # A root-anchored element's containing block is the viewport,
        # unaffected by <body>'s margin -- and so is everything painted
        # inside it, so the whole ancestor chain must be checked.
        while owner is not None:
            if id(owner) in root_anchored_ids:
                break
            owner = getattr(owner, "parentElement", None)
        else:
            geometry._shift_box(node, dx, dy)
            continue
        if owner.__dict__.get("_chromonic_static_anchored") and not _in_root_anchored_subtree(
                getattr(owner, "parentElement", None), root_anchored_ids):
            # Its static position came from its placeholder in the flow,
            # which did move: on each axis whose insets are both auto, it
            # moves with it -- unless the placeholder itself sits inside a
            # viewport-anchored subtree that the margin doesn't move
            # (abspos-023.xht).
            inset = (getattr(owner, "_chromonic_native_style", None) or {}).get("inset") or ("auto",) * 4
            sx = dx if inset[1] == "auto" and inset[3] == "auto" else 0.0
            sy = dy if inset[0] == "auto" and inset[2] == "auto" else 0.0
            if sx or sy:
                geometry._shift_box(node, sx, sy)



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
        root_id = builder.build(tree, root_element, node_map, reuse_styles=reuse_styles)
    available_width = _constrain_root_to_document_element(tree, root_element, root_id, width)
    compute_height = _root_compute_height(root_element, height, viewport_height)
    boxes = tree.compute(root_id, available_width, compute_height)
    geometry._write_boxes(boxes, node_map)
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
    has_split_wrapper = False
    has_absolute = False
    has_absolute_or_fixed = False
    has_table = False
    has_auto_horizontal_margin = False
    has_rtl = False
    for element in node_map.values():
        d = getattr(element, "__dict__", None)
        if d is None:
            continue
        if d.get("_chromonic_is_table_root"):
            has_table = True
        if d.get("_chromonic_split_wrapper_ref") is not None:
            has_split_wrapper = True
        style = d.get("_chromonic_native_style")
        if style is not None:
            margin = style.get("margin") or ()
            if len(margin) == 4 and (margin[1] == "auto" or margin[3] == "auto"):
                has_auto_horizontal_margin = True
            position = style.get("position")
            if position == "absolute":
                has_absolute = True
                has_absolute_or_fixed = True
            elif position == "fixed":
                has_absolute_or_fixed = True
        paint_style = d.get("_chromonic_paint_style") or {}
        if (paint_style.get("direction") or "").strip().lower() == "rtl":
            has_rtl = True
        elif getattr(element, "getAttribute", None) is not None:
            if (element.getAttribute("dir") or "").strip().lower() == "rtl":
                has_rtl = True
    return {
        "split_wrapper": has_split_wrapper,
        "absolute": has_absolute,
        "absolute_or_fixed": has_absolute_or_fixed,
        "table": has_table,
        "auto_horizontal_margin": has_auto_horizontal_margin,
        "rtl": has_rtl,
    }



def _finish_layout_pass(tree_obj, node_map, root_element, *, width, viewport_height):
    """The post-tree.compute() correction pipeline, shared by every entry
    point that computes real Taffy geometry (layout(),
    LayoutProjection.layout()/.compute()) -- previously duplicated verbatim
    across all three, which is how fixes wired into only one of them
    silently never ran for a real, incrementally-updated page."""
    features = _scan_layout_pass_features(node_map)
    # `_adjust_body_collapsed_margins` runs twice in this pass -- its
    # _chromonic_scroll_extent needs Taffy's real, uncorrected box as its
    # baseline, which the second call would otherwise only see already
    # corrected (and smaller). Stashed once, before either call.
    root_element.__dict__["_chromonic_pristine_box"] = root_element.__dict__.get("_layout_box")
    if features["split_wrapper"]:
        inline_finalize._fix_nested_split_flow_extent(node_map)
    _adjust_body_collapsed_margins(root_element)
    _apply_root_margin_offset(root_element, node_map)
    flex_grid._fix_flex_baseline_alignment(node_map)
    flex_grid._fix_flex_safe_alignment(node_map)
    flex_grid._fix_flex_rtl_mirroring(node_map)
    if features["rtl"]:
        positioning._fix_rtl_block_positioning(node_map)
    positioning._fix_relative_rtl_insets(node_map)
    # Before the absolute-positioning fixups below: an inline-context
    # escapee's real static position is only known once
    # `_InlineFormattingPlan.publish()` has run --
    # `positioning._fix_absolute_static_position_fallback` reads
    # element._chromonic_static_position, which this sets.
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
    inline_finalize._resync_interruption_marker_heights(node_map)
    # A nested split wrapper's _layout_box doesn't exist until
    # `inline_finalize._publish_inline_formatting` (above) unions its fragments,
    # and the interruption blocks' boxes it reads have since moved
    # (`_apply_root_margin_offset`) -- recomputed fresh now that both are
    # finally real and final.
    if features["split_wrapper"]:
        inline_finalize._fix_nested_split_flow_extent(node_map)
    # Re-anchor body's auto-height now that the split-wrapper extents are
    # final -- idempotent, re-derives from final positions.
    _adjust_body_collapsed_margins(root_element)
    if features["absolute"]:
        positioning._fix_absolute_horizontal_auto_margins(node_map)
        positioning._fix_absolute_vertical_auto_margins(node_map)
        positioning._fix_absolute_width_against_containing_block(node_map)
        positioning._fix_absolute_static_position_fallback(node_map)
    if viewport_height is not None and features["absolute_or_fixed"]:
        positioning._fix_viewport_anchored_positioning(node_map, viewport_height, width)
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
    _element_own_baseline, _establishes_bfc, _establishes_containing_block, _first_baseline,
    _is_absolutely_positioned, _is_floated, _is_inline_level, _numeric_edge,
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
    _fix_flex_baseline_alignment, _fix_flex_rtl_mirroring,
    _fix_flex_safe_alignment, _is_flex_or_grid_item, _parse_grid_area, _parse_grid_area_token
)
from .geometry import (
    _grow_and_reflow, _grow_box_height, _needed_ancestor_growth, _shift_anonymous_boxes,
    _shift_box, _shift_later_siblings_for_height_delta, _shift_recomputed_subtree,
    _shift_subtree, _write_boxes
)
from .inline_finalize import (
    _finalize_inline_owner_boxes, _fix_nested_split_flow_extent,
    _fix_split_inline_relative_offset, _inline_relative_offset, _is_flattened_inline,
    _merge_adjacent_same_line_rects, _publish_inline_formatting,
    _resync_interruption_marker_heights
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
    _fix_absolute_horizontal_auto_margins,
    _fix_absolute_static_position_fallback, _fix_absolute_vertical_auto_margins,
    _fix_absolute_width_against_containing_block, _fix_relative_rtl_insets,
    _fix_rtl_block_positioning, _fix_viewport_anchored_positioning,
    _flex_container_static_position, _is_root_anchored,
    _resolve_inset, _resolve_viewport_anchored_box, _resolve_viewport_anchored_box_x
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

