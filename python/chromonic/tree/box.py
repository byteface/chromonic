"""The layout state of one node: its `Box`.

Everything the layout tree knows about a DOM node -- its resolved styles,
how it was built into the native tree, the lines and fragments its inline
content produced, its table bookkeeping -- lives on one `Box`, reached with
`box_of(node)`, instead of as loose attributes stashed on the domonic node.
Paint, hit-testing and the harness read the same object.

The node's own geometry stays on domonic's `_layout_box` (a `LayoutBox`),
which domonic's DOM APIs (`getBoundingClientRect()` and friends) read.

Every field defaults to None, meaning "not set by this layout".
"""

from __future__ import annotations


class Box:
    """One node's layout state; see the module docstring."""

    __slots__ = (
        # -- style ------------------------------------------------------------
        "tag_name",                # lower-case tag, or a "#..." name for a synthetic box
        "computed_style",          # domonic ComputedStyleDeclaration
        "resolved_style",          # (computed, LayoutStyle), as `dom._describe` resolved them
        "native_style",            # the style dict handed to `_native.Tree`
        "paint_style",             # the flat dict paint.py draws from
        "synthetic_style",         # an anonymous box's stand-in computed style
        "padding",                 # used (top, right, bottom, left) padding from Taffy

        # -- the native tree -------------------------------------------------
        "node_id",                 # this node's id in `_native.Tree`
        "has_layout_children",     # False: paint draws the node's own text
        "normalized_children",     # childNodes with anonymous boxes generated around them
        "anonymous_parent",        # the anonymous box a node was wrapped in
        "anonymous_table_boxes",   # {key: anonymous box} generated inside this node
        "root_boxes",              # {"html"|"icb": box} above a page's <body>
        "string_text_nodes",       # text-node shims for bare str children

        # -- generated content --------------------------------------------------
        "before_pseudo",           # (computed, text) for ::before, if it generates a box
        "after_pseudo",
        "before_text",             # ::before/::after text folded into a leaf's own text
        "after_text",
        "pseudo_objs",             # {"before"|"after": _PseudoElement}

        # -- text and inline formatting ------------------------------------------
        "text_lines",              # the lines this node's own text wrapped into
        "text_line_widths",
        "text_line_x",             # per-line offsets inside the content box, when floats
        "text_line_y",             #   shortened some lines (None: x=0, stacked)
        "text_line_avail",         # per-line available width, same condition
        "line_height",
        "content_offset_y",        # a table cell's vertical-align shift of its content
        "inline_plan",             # the `_InlineFormattingPlan` laying this node's lines
        "inline_owner",            # the anonymous inline node under a containing block
        "inline_fragments",        # retained text/generated-content fragments to paint
        "owned_fragments",         # the fragments of this inline's own direct text
        "inline_boxes",            # getClientRects() rects of an inline box
        "marker_positions",        # indices of block-interruption rects in inline_boxes
        "fragment",                # a text node's retained `_AnonymousTextFragment`
        "flattened_inline",        # laid out as runs of an enclosing plan, no node of its own
        "leading_collapsed_space", # collapsible white space before it survives as a space
        "inline_rel_offset",       # the position:relative offset applied inside a line

        # -- CSS 2.1 9.2.1.1 block-in-inline splits ---------------------------------
        "split_container",         # the block container whose flow the split pieces join
        "split_self_edges",        # (left, right, top) edges of a self-splitting container
        "split_plan_owners",       # {index: anonymous owner} per inline piece
        "interruption_blocks",     # the blocks this inline was split around
        "final_split_fragment",    # the plan holds the inline's trailing piece

        # -- out-of-flow boxes -------------------------------------------------------
        "static_position",         # (x, y) static position recorded by an inline plan
        "static_anchor_obj",       # the `_StaticAnchor` placeholder left in the flow
        "static_anchored",         # `src/lib.rs` places this box at its static position

        # -- replaced elements --------------------------------------------------------
        "img_measure",             # (measure, key) for an image sized by its measure
        "image_loading_width_stretch",
        "listbox_rows",

        # -- tables ---------------------------------------------------------------------
        "is_table_root",
        "table_rows",
        "table_grid_cells",        # [(cell, row, column, rowspan, colspan)]
        "table_columns",           # [(col element, colgroup element)]
        "table_captions",
        "table_sections",          # row groups in display order (thead, tbody..., tfoot)
        "table_bottom_captions",
        "table_collapsed_columns",
        "table_columns_max",       # used column widths (the `<col>` boxes)
        "table_fixed",
        "table_rtl",
        "table_specified_width",
        "table_specified_height",
        "table_cells",             # a row's cells
        "row_collapsed",           # visibility: collapse
        "cell_specified_width",
        "cell_specified_height",
        "border_collapse",
        "border_spacing",
        "collapsed_cell_borders",
    )

    def __init__(self):
        for name in self.__slots__:
            setattr(self, name, None)

    def get(self, name: str, default=None):
        """The field, or `default` when it isn't set."""
        value = getattr(self, name)
        return default if value is None else value

    def pop(self, name: str, default=None):
        """Clear the field, returning what it held (or `default`)."""
        value = getattr(self, name)
        setattr(self, name, None)
        return default if value is None else value

    def setdefault(self, name: str, default):
        """The field, first set to `default` if it isn't set."""
        value = getattr(self, name)
        if value is None:
            setattr(self, name, default)
            return default
        return value


def box_of(node) -> Box:
    """`node`'s Box, created on first use."""
    state = node.__dict__
    box = state.get("_chromonic_box")
    if box is None:
        box = state["_chromonic_box"] = Box()
    return box
