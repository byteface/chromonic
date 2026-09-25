from __future__ import annotations

import re

from domonic import _fontmetrics
from domonic.layout import LayoutBox

from .. import fonts, style_bridge
from .._native import Tree, layout_text
from . import box_model, builder, dom, flex_grid, inline_formatting, positioning



def _form_control_display_text(element) -> str:
    tag_name = (getattr(element, "tagName", "") or "").lower()
    if tag_name == "textarea":
        return getattr(element, "value", "") or element.textContent or ""
    input_type = (element.getAttribute("type") or "text").lower()
    if input_type in {"checkbox", "radio", "hidden", "color"}:
        return ""  # colour has its own swatch fill (paint.py) -- no text on top of it
    if input_type in {"submit", "reset", "button"}:
        default = {"submit": "Submit", "reset": "Reset"}.get(input_type, "")
        return element.getAttribute("value") or default
    if input_type == "file":
        value = getattr(element, "value", "") or ""
        return value.rsplit("/", 1)[-1] if value else "Choose File"
    value = getattr(element, "value", "") or ""
    if value and input_type == "password":
        text = "•" * len(str(value))
    else:
        text = str(value) if value else element.getAttribute("placeholder") or ""
    style = element.__dict__.get("_chromonic_paint_style", {})
    return dom._apply_text_transform(text, style.get("text_transform"))



#: Shared by `builder.py` (box height), `paint.py` (row painting), and
#: `native_browser.py` (click row hit-testing) so all three always agree.
LISTBOX_ROW_HEIGHT = 18.0


def _listbox_row_count(element) -> int:
    """0 means an ordinary closed dropdown (`_select_display_text`'s single
    line); otherwise the number of always-visible option rows a real
    `<select multiple>`/`size` attribute renders as -- HTML: `size` wins
    when given (even without `multiple`); `multiple` alone with no `size`
    defaults to 4."""
    size = 0
    size_attr = element.getAttribute("size")
    if size_attr:
        try:
            size = int(size_attr)
        except ValueError:
            size = 0
    if size > 1:
        return size
    if element.hasAttribute("multiple"):
        return size if size > 0 else 4
    return 0


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
    """<img> is a replaced element: an auto width/height is sized from its
    intrinsic width/height, baked into `style` before Taffy sees the tree.
    An explicit CSS size on either axis is always left alone.

    A raster image always has a complete intrinsic size. An SVG only has
    whichever of width/height/viewBox its root declares
    (`browser_images.natural_size`), each independently possibly absent --
    CSS 2.1 10.3.2 leaves sizing undefined except for a complete
    width+height pair, and real browsers fall back to the CSS default
    object size (300x150, same as canvas/iframe) otherwise -- confirmed
    against Chrome on wpt/css/CSS2/visudet/replaced-elements-width-40.html:
    an intrinsic ratio alone is not used to scale an explicit CSS
    dimension; only a complete pair drives sizing."""
    from .. import browser_images

    # Undo the auto-width block stretch this function synthesizes below for
    # a still-loading image, if a previous call already applied one --
    # otherwise it looks identical to a real explicit width even once the
    # image arrives. `style` may be the same cached dict a reuse_styles=True
    # pass hands back, not fresh from style_bridge.to_dict().
    if getattr(element, "_chromonic_image_loading_width_stretch", False):
        style["width"] = "auto"
        element._chromonic_image_loading_width_stretch = False

    src = element.getAttribute("src") or ""
    image = browser_images.load_image(src)
    if image is None:
        # No image to size from yet, but width:auto still needs ordinary
        # block behavior (stretch to fill the containing block). An <img>
        # is always built as a bare Taffy leaf, never a container Taffy's
        # own block algorithm sizes -- a leaf with no measure function
        # silently collapses to 0 instead while width stays "auto".
        if style["display"] == "block" and style["width"] == "auto":
            style["width"] = ("pct", 1.0)
            element._chromonic_image_loading_width_stretch = True
        return
    intrinsic_width, intrinsic_height, _ratio = browser_images.natural_size(src)
    has_complete_pair = intrinsic_width is not None and intrinsic_height is not None
    intrinsic_ratio = intrinsic_width / intrinsic_height if has_complete_pair and intrinsic_height else None
    # CSS Sizing 4: a declared aspect-ratio always wins over the image's
    # own natural ratio, swapped in for intrinsic_ratio here once; the rest
    # of this function's ratio-driven sizing logic applies unchanged.
    declared_ratio = style.get("aspect_ratio")
    if isinstance(declared_ratio, (int, float)):
        intrinsic_ratio = declared_ratio
    # CSS Images 3 5.2's fallback when nothing intrinsic is known -- 300x150,
    # same UA default canvas/iframe use elsewhere in this file.
    default_width, default_height = 300.0, 150.0
    if isinstance(style["height"], tuple) and style.get("position") not in ("absolute", "fixed"):
        # CSS 2.1 10.5: a percentage height whose containing block has no
        # definite height computes to auto, then comes from width and
        # intrinsic ratio -- flex-aspect-ratio-img-column-004.html.
        parent = dom._layout_parent(element)
        parent_native = getattr(parent, "_chromonic_native_style", None) if parent is not None else None
        if parent_native is not None and not isinstance(parent_native.get("height"), (int, float)):
            style["height"] = "auto"
    width_auto = style["width"] == "auto"
    height_auto = style["height"] == "auto"
    if width_auto and height_auto and intrinsic_ratio and _stretched_replaced_flex_item(element, style):
        # CSS Flexbox 9.2.3 rule C / 9.4: a replaced flex item stretched
        # across a row with a definite height takes that stretched cross
        # size, main size following its own ratio --
        # flex-cross-size-border-box-001.html. Taffy resolves both from
        # the ratio once left auto.
        style["aspect_ratio"] = intrinsic_ratio
        return
    element.__dict__.pop("_chromonic_img_measure", None)
    if width_auto and height_auto:
        if has_complete_pair:
            width, height = intrinsic_width, intrinsic_height
            if False and intrinsic_ratio and style.get("flex_basis") == "auto" and flex_grid._is_flex_or_grid_item(element):
                # Disabled: Taffy sizes a measured leaf's cross axis from
                # its style, never the flexed main size, so this bought
                # nothing over the explicit sizes below and lost the
                # min/max ratio transfer -- image-as-flexitem-size-001.
                iw, ih, ratio = intrinsic_width, intrinsic_height, intrinsic_ratio

                def measure(_available_width, _available_height, known_width=None, known_height=None,
                            iw=iw, ih=ih, ratio=ratio):
                    # Only a known size drives the ratio -- available space
                    # is merely offered.
                    if known_width is not None:
                        return (known_width, known_height if known_height is not None else known_width / ratio)
                    if known_height is not None:
                        return (known_height * ratio, known_height)
                    return (iw, ih)

                element.__dict__["_chromonic_img_measure"] = (measure, ("img-measure", src, iw, ih))
                style["aspect_ratio"] = intrinsic_ratio
                return
            if (intrinsic_ratio and isinstance(style.get("flex_basis"), (int, float))
                    and flex_grid._is_flex_or_grid_item(element)):
                # A numeric flex-basis is the image's main size; cross size
                # follows the ratio -- image-as-flexitem-size-001.html.
                # Taffy's aspect_ratio only reads the style width, never
                # the flexed size, so both are resolved here
                # (flex-grow/shrink not modelled).
                parent_native = (dom._layout_parent(element).__dict__.get("_chromonic_native_style") or {})
                if (parent_native.get("flex_direction") or "row").startswith("row"):
                    width = float(style["flex_basis"])
                    height = width / intrinsic_ratio
                else:
                    height = float(style["flex_basis"])
                    width = height * intrinsic_ratio
            if intrinsic_ratio:
                # CSS 2.1 10.4: a min/max constraint on one axis of an
                # auto-sized replaced element transfers to the other
                # through the intrinsic ratio -- image-as-flexitem-size-001.html.
                for key, pick, axis in (("max_width", min, "w"), ("max_height", min, "h"),
                                        ("min_width", max, "w"), ("min_height", max, "h")):
                    bound = style.get(key)
                    if not isinstance(bound, (int, float)):
                        continue
                    if axis == "w" and pick(width, bound) != width:
                        width = bound
                        height = width / intrinsic_ratio
                    elif axis == "h" and pick(height, bound) != height:
                        height = bound
                        width = height * intrinsic_ratio
            style["width"], style["height"] = width, height
        else:
            style["width"], style["height"] = default_width, default_height
    elif height_auto and isinstance(style["width"], (int, float)) and _stretched_replaced_flex_item(element, style):
        # An explicit-width image stretched across a definite-height flex
        # row keeps that width and takes the row's height --
        # flexbox-whitespace-handling-001a.xhtml -- height left auto for Taffy.
        pass
    elif height_auto and isinstance(style["width"], (int, float)):
        # CSS 2.1 10.4: height comes from width after its own min/max-width
        # clamp (Taffy's aspect_ratio applies the ratio before clamping,
        # so the clamp is resolved here) -- flex-aspect-ratio-img-column-005.html.
        clamped = style["width"]
        parent = dom._layout_parent(element)
        parent_native = getattr(parent, "_chromonic_native_style", None) if parent is not None else None
        parent_width = parent_native.get("width") if parent_native is not None else None
        for key, pick in (("max_width", min), ("min_width", max)):
            bound = style.get(key)
            if isinstance(bound, tuple) and isinstance(parent_width, (int, float)):
                bound = bound[1] * parent_width
            if isinstance(bound, (int, float)):
                clamped = pick(clamped, bound)
        style["height"] = clamped * (1.0 / intrinsic_ratio) if intrinsic_ratio else default_height
    elif width_auto and isinstance(style["height"], (int, float)):
        style["width"] = style["height"] * intrinsic_ratio if intrinsic_ratio else default_width
    elif (height_auto or width_auto) and intrinsic_ratio:
        # width/height isn't a plain pixel length on either side (most
        # commonly a percentage) -- its resolved pixel value isn't known
        # until Taffy lays out the box. The measure callback sees that
        # resolved, min/max-clamped size (CSS 2.1 10.4) and derives the
        # auto side from the ratio -- max-width-109.xht (`width:200%;
        # max-width:100px` must give a 100px-tall square). A style
        # aspect ratio would not do: Taffy applies it before clamping.
        ratio = intrinsic_ratio
        natural = (intrinsic_width, intrinsic_height) if has_complete_pair else (default_width, default_height)

        def measure(available_width, available_height, known_width=None, known_height=None,
                    ratio=ratio, natural=natural, width_auto=width_auto, height_auto=height_auto):
            width = known_width if known_width is not None else (
                available_width if available_width is not None and available_width >= 0 else None)
            height = known_height if known_height is not None else (
                available_height if available_height is not None and available_height >= 0 else None)
            # The side the author specified drives the other through the
            # ratio (CSS 2.1 10.3.2/10.6.2); `available_*` for an auto side
            # is only offered space, never a size to adopt.
            if not width_auto and width is not None:
                return (width, width / ratio)
            if not height_auto and height is not None:
                return (height * ratio, height)
            return natural

        element.__dict__["_chromonic_img_measure"] = (measure, ("img-ratio-measure", src, ratio, natural))
    if isinstance(style["width"], (int, float)):
        # A resolved replaced-element width (intrinsic, ratio-derived, or
        # the 300px UA default) is never subject to flex-shrink -- it's
        # not a genuine flex item, just an atomic run inside whatever
        # flex-row this project's own inline-formatting approximation
        # wraps it in alongside surrounding text. Flexbox's plain default
        # `flex-shrink:1` would otherwise squeeze it down to fit that
        # row's available width instead of overflowing/wrapping as a
        # whole unit the way a real inline-replaced element does.
        # Confirmed directly on replaced-elements-min-width-40.html: six
        # SVGs with no complete intrinsic size (falling back to the
        # 300x150 UA default here) were squeezed down to 200px -- the
        # 200px-wide containing `<div>`'s own width, not theirs -- every
        # one of them, instead of overflowing it at their real 300px.
        style["flex_shrink"] = 0.0



def _stretched_replaced_flex_item(element, style: dict) -> bool:
    """Whether `element` is an in-flow item of a row flex container with a
    definite height whose effective `align-self` is `stretch`/`normal`
    (so its cross size is the container's, per Flexbox 9.4)."""
    if style.get("position") in ("absolute", "fixed") or not flex_grid._is_flex_or_grid_item(element):
        return False
    parent = dom._layout_parent(element)
    parent_native = getattr(parent, "_chromonic_native_style", None) or {}
    parent_inset = parent_native.get("inset") or ("auto",) * 4
    parent_definite_height = (
        isinstance(parent_native.get("height"), (int, float))
        # An absolutely positioned container with `top` and `bottom` set
        # is as definite (flex-abspos-inset-nested-001.html).
        or (parent_native.get("position") == "absolute"
            and parent_inset[0] != "auto" and parent_inset[2] != "auto"))
    if (parent_native.get("display") != "flex"
            or not (parent_native.get("flex_direction") or "row").startswith("row")
            or not parent_definite_height):
        return False
    resolved = getattr(element, "_chromonic_resolved_style", None)
    align, _safe = box_model._alignment_parts(getattr(resolved[0], "alignSelf", "auto") if resolved else "auto")
    if align == "auto":
        parent_resolved = getattr(parent, "_chromonic_resolved_style", None)
        align, _safe = box_model._alignment_parts(getattr(parent_resolved[0], "alignItems", "normal")
                                        if parent_resolved else "normal")
    return align in ("stretch", "normal")



def _resolve_replaced_percent_height(style: dict, element, height_attr: str) -> "float | None":
    """CSS 2.1 10.6.2: a replaced element's percentage intrinsic height
    (an HTML height="N%" attribute, e.g. on svg/iframe) resolves against
    its containing block's height, but only when that's itself definite;
    otherwise the percentage "is treated as '0'", never resolved
    circularly against a still-undetermined auto height."""
    containing_block = positioning._find_containing_block_ancestor(element) if (
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



def _apply_video_intrinsic_size(style: dict, element) -> None:
    """<video> is a replaced element sized from the decoded stream's own
    dimensions (`video_backend.py`, via ffprobe) -- the same "auto axis
    takes the intrinsic size, one auto axis scales by the intrinsic ratio"
    shape `_apply_image_intrinsic_size` uses for <img>, just simplified:
    doesn't yet handle a declared `aspect-ratio` override or a percentage
    height against an indefinite containing block the way that function
    does. No/undecodable source: `ua_style.py`'s 300x150 default (same as
    canvas/iframe) stands, untouched."""
    if style["width"] != "auto" and style["height"] != "auto":
        return
    from .. import video_backend
    decoder = video_backend.decoder_for(element)
    width_auto, height_auto = style["width"] == "auto", style["height"] == "auto"
    if decoder is None or not decoder.ready:
        # CSS Images 3 5.2's fallback when nothing intrinsic is known --
        # 300x150, same UA default canvas/iframe use (there as a real CSS
        # rule; done here in code since -- unlike those -- an auto axis
        # here should still prefer the decoded size the moment it's ready).
        if width_auto:
            style["width"] = 300.0
        if height_auto:
            style["height"] = 150.0
        return
    intrinsic_ratio = decoder.width / decoder.height
    if width_auto and height_auto:
        style["width"], style["height"] = float(decoder.width), float(decoder.height)
    elif width_auto and isinstance(style["height"], (int, float)):
        style["width"] = style["height"] * intrinsic_ratio
    elif height_auto and isinstance(style["width"], (int, float)):
        style["height"] = style["width"] / intrinsic_ratio



def _apply_iframe_intrinsic_size(style: dict, element) -> None:
    """<iframe> is a replaced element with no intrinsic ratio -- CSS 2.1
    10.3.2/10.6.2's fallback is a UA-defined 300x150 default, same as
    canvas's default bitmap (an explicit width/height HTML attribute or
    CSS size still wins)."""
    # style["width"]/["height"] can already be the UA stylesheet's 300/150
    # default rather than literal "auto" (`ua_style.py`) -- an HTML
    # width/height attribute is a real presentational hint that should
    # still win over that UA default.
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
            if resolved is None and style.get("position") in ("absolute", "fixed"):
                # CSS 2.1 10.5: an absolutely positioned box's containing
                # block always has a resolvable height -- left as a
                # percentage for Taffy to resolve against --
                # absolute-replaced-height-007.xht.
                try:
                    style["height"] = ("pct", float(height_attr[:-1]) / 100.0)
                    resolved = style["height"]
                except ValueError:
                    pass
            if not isinstance(resolved, tuple):
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
    intrinsic_width = float(element.getAttribute("width") or 300)
    intrinsic_height = float(element.getAttribute("height") or 150)
    if (style["width"] == "auto" and style["height"] == "auto" and intrinsic_height
            and _stretched_replaced_flex_item(element, style)):
        # Stretched across a definite-height flex row (Flexbox 9.4/9.2.3
        # rule C) -- flexbox-flex-basis-content-001a.html -- both left
        # auto with the ratio for Taffy.
        style["aspect_ratio"] = intrinsic_width / intrinsic_height
        return
    if style["width"] == "auto":
        style["width"] = intrinsic_width
    if style["height"] == "auto":
        style["height"] = intrinsic_height



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
    # CSS 2.1 10.3.2/10.6.2: with no intrinsic width/height/ratio at all, a
    # replaced element still isn't sized like an ordinary block -- it gets
    # the same UA-defined 300x150 default iframe/canvas use, even
    # overflowing a narrower containing block -- confirmed against Chrome:
    # a width:auto SVG with only height="50" and no viewBox comes out
    # 300px wide inside a 288px container.
    if style["width"] == "auto":
        style["width"] = 300.0
    if style["height"] == "auto":
        if _stretched_replaced_flex_item(element, style):
            # CSS Flexbox 9.4: a replaced flex item stretched across a row
            # with a definite height takes that stretched cross size -- the
            # 150px UA default is only a fallback for "no intrinsic size
            # and no flex context" and must not override a real stretch --
            # display-flex-svg-overflow-default.html. Left auto here so
            # Taffy's cross-axis stretch fills it in.
            return
        style["height"] = 150.0



def _numeric_or_zero(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0



def _publish_svg_shape_boxes(node_map: dict) -> None:
    """An <svg>'s own content isn't laid out here (the root is one replaced
    box), but Chrome still answers getBoundingClientRect() for a shape
    inside it: a <rect> reports its x/y/width/height offset from the svg
    root's box, unclipped, unscaled when the svg has no viewBox --
    absolute-replaced-width-002.xht. Published purely so the element
    reports that rect."""
    for element in list(node_map.values()):
        if not dom._is_element(element):
            continue
        tag = (getattr(element, "tagName", "") or "").lower()
        if tag not in ("svg", "svg:svg"):
            continue
        box = element.__dict__.get("_layout_box")
        if box is None or element.getAttribute("viewBox") is not None:
            continue

        def length(node, name, default=0.0) -> float:
            raw = node.getAttribute(name)
            try:
                return float(str(raw).strip().rstrip("px")) if raw not in (None, "") else default
            except ValueError:
                return default

        for child in dom._child_nodes(element):
            if not dom._is_element(child):
                continue
            child_tag = (getattr(child, "tagName", "") or "").lower()
            if child_tag not in ("rect", "svg:rect"):
                continue
            width, height = length(child, "width"), length(child, "height")
            child.__dict__["_layout_box"] = LayoutBox(
                x=box.x + box.border_left + length(child, "x"), y=box.y + box.border_top + length(child, "y"),
                width=width, height=height, client_width=width, client_height=height)
