//! chromonic_native -- a PyO3 binding onto the Taffy layout algorithms (block,
//! flex, grid) driven from chromonic's own node tree, plus
//! (see `layout_text` below) Parley for real text layout.
//!
//! This crate knows nothing about domonic or CSS text. Python
//! (`chromonic.style_bridge`) reduces a domonic `LayoutStyle` down to plain,
//! FFI-trivial values *before* crossing over here:
//!
//! - a length -> a Python `float` (px), the 2-tuple `("pct", fraction)`,
//!   the 2-tuple `("fr", n)` (track-sizing only), or the string `"auto"`
//! - `display` -> `"block" | "flex" | "grid"`
//! - flex/grid enums -> plain lowercase, hyphenated strings matching CSS
//!   keywords (e.g. `"flex-start"`, `"space-between"`, `"row-reverse"`)
//!
//! See `PLAN.md` for why this boundary is intentionally narrow.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use taffy::prelude::*;
use taffy::geometry::Point;
use taffy::style::{CheapCloneStr, Contain, Overflow};
use taffy::util::{MaybeMath, MaybeResolve, ResolveOrZero};
use taffy::{
    compute_block_layout, compute_cached_layout, compute_flexbox_layout, compute_grid_layout,
    compute_hidden_layout, compute_leaf_layout, compute_root_layout, AlignContent, AlignItems, Baselines,
    BlockContext, BlockFormattingContext, BoxSizing, Cache, CacheTree, Clear, Float, Layout,
    LayoutBlockContainer, LayoutFlexboxContainer, LayoutGridContainer, LayoutInput, LayoutOutput,
    LayoutPartialTree, Line, NodeId, Position, RequestedAxis, RunMode, SizingMode, TraversePartialTree,
    TraverseTree,
};

use parley::{
    Alignment, AlignmentOptions, FontContext, FontStyle as ParleyFontStyle,
    FontWeight as ParleyFontWeight, LayoutContext, LineHeight as ParleyLineHeight,
    OverflowWrap as ParleyOverflowWrap, StyleProperty, WordBreak as ParleyWordBreak,
};
use std::cell::RefCell;

// -- Python value -> Taffy value ---------------------------------------------

/// A length-like Python value: `float` (px), `("pct", frac)`, `("fr", n)`,
/// the string `"auto"`, or a CSS Sizing 3 keyword (`"min-content"`,
/// `"max-content"`, `"fit-content"`, `"stretch"`) -- sizes only.
enum RawLen {
    Px(f32),
    Pct(f32),
    Fr(f32),
    Auto,
    Sizing(Dimension),
}

fn read_raw_len(value: &Bound<PyAny>) -> PyResult<RawLen> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(match text.as_str() {
            "min-content" => RawLen::Sizing(Dimension::min_content()),
            "max-content" => RawLen::Sizing(Dimension::max_content()),
            "fit-content" => RawLen::Sizing(Dimension::fit_content()),
            "stretch" => RawLen::Sizing(Dimension::stretch()),
            // An unknown keyword is an invalid declaration: initial value.
            _ => RawLen::Auto,
        });
    }
    if let Ok(px) = value.extract::<f32>() {
        return Ok(RawLen::Px(px));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() == 2 {
            let tag: String = tuple.get_item(0)?.extract()?;
            let num: f32 = tuple.get_item(1)?.extract()?;
            return match tag.as_str() {
                "pct" => Ok(RawLen::Pct(num)),
                "fr" => Ok(RawLen::Fr(num)),
                other => Err(PyValueError::new_err(format!("unrecognised length tag: {other:?}"))),
            };
        }
    }
    Err(PyValueError::new_err("expected a float (px), \"auto\", (\"pct\", f), or (\"fr\", f)"))
}

fn dimension(value: &Bound<PyAny>) -> PyResult<Dimension> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Auto => auto(),
        RawLen::Sizing(keyword) => keyword,
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is only valid for grid track sizes")),
    })
}

fn length_percentage(value: &Bound<PyAny>) -> PyResult<LengthPercentage> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Auto | RawLen::Sizing(_) => LengthPercentage::ZERO, // padding/border/gap have no "auto"
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is not valid here")),
    })
}

fn length_percentage_auto(value: &Bound<PyAny>) -> PyResult<LengthPercentageAuto> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Auto | RawLen::Sizing(_) => auto(),
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is not valid here")),
    })
}

/// `style_bridge._tracks()`'s vocabulary for one grid track: the plain
/// `read_raw_len` shapes (px/`("pct", f)`/`("fr", f)`/`"auto"`), plus
/// `"min-content"`/`"max-content"`, `("fit-content", <length>)`, and
/// `("minmax", <min>, <max>)` (whose own `<min>`/`<max>` are each one of
/// the same shapes, minus `fr` on the min side and `fit-content` on
/// either -- CSS Grid 1 7.2.3/7.2.4).
fn min_track_sizing_function(value: &Bound<PyAny>) -> PyResult<MinTrackSizingFunction> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(match text.as_str() {
            "auto" => auto(),
            "min-content" => min_content(),
            "max-content" => max_content(),
            other => return Err(PyValueError::new_err(format!("unrecognised min track keyword: {other:?}"))),
        });
    }
    if let Ok(px) = value.extract::<f32>() {
        return Ok(length(px));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() == 2 {
            if let Ok(tag) = tuple.get_item(0)?.extract::<String>() {
                if tag == "pct" {
                    return Ok(percent(tuple.get_item(1)?.extract::<f32>()?));
                }
            }
        }
    }
    Err(PyValueError::new_err("expected a min track sizing value"))
}

fn max_track_sizing_function(value: &Bound<PyAny>) -> PyResult<MaxTrackSizingFunction> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(match text.as_str() {
            "auto" => auto(),
            "min-content" => min_content(),
            "max-content" => max_content(),
            other => return Err(PyValueError::new_err(format!("unrecognised max track keyword: {other:?}"))),
        });
    }
    if let Ok(px) = value.extract::<f32>() {
        return Ok(length(px));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() == 2 {
            let tag: String = tuple.get_item(0)?.extract()?;
            match tag.as_str() {
                "pct" => return Ok(percent(tuple.get_item(1)?.extract::<f32>()?)),
                "fr" => return Ok(fr(tuple.get_item(1)?.extract::<f32>()?)),
                "fit-content" => return Ok(fit_content(length_percentage(&tuple.get_item(1)?)?)),
                _ => {}
            }
        }
    }
    Err(PyValueError::new_err("expected a max track sizing value"))
}

fn track_sizing_function(value: &Bound<PyAny>) -> PyResult<TrackSizingFunction> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(match text.as_str() {
            "auto" => auto(),
            "min-content" => min_content(),
            "max-content" => max_content(),
            other => return Err(PyValueError::new_err(format!("unrecognised track keyword: {other:?}"))),
        });
    }
    if let Ok(px) = value.extract::<f32>() {
        return Ok(length(px));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        let tag: String = tuple.get_item(0)?.extract()?;
        match (tag.as_str(), tuple.len()) {
            ("pct", 2) => return Ok(percent(tuple.get_item(1)?.extract::<f32>()?)),
            ("fr", 2) => return Ok(fr(tuple.get_item(1)?.extract::<f32>()?)),
            ("fit-content", 2) => return Ok(fit_content(length_percentage(&tuple.get_item(1)?)?)),
            ("minmax", 3) => {
                let min = min_track_sizing_function(&tuple.get_item(1)?)?;
                let max = max_track_sizing_function(&tuple.get_item(2)?)?;
                return Ok(minmax(min, max));
            }
            _ => {}
        }
    }
    Err(PyValueError::new_err("expected a track sizing value"))
}

/// One `grid-template-columns`/`-rows` component: an ordinary track (any
/// `track_sizing_function` shape), or `("repeat", count, [track, ...])`
/// for a `repeat()` -- `count` is either an integer or `"auto-fill"`/
/// `"auto-fit"` (CSS Grid 1 7.2.3.1), left to Taffy's own explicit-grid
/// sizing to expand (an auto-repeat's real count depends on the
/// container's available space, unknowable in `style_bridge.py`).
fn grid_template_component<S: CheapCloneStr>(value: &Bound<PyAny>) -> PyResult<GridTemplateComponent<S>> {
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() == 3 {
            if let Ok(tag) = tuple.get_item(0)?.extract::<String>() {
                if tag == "repeat" {
                    let tracks_list = tuple.get_item(2)?;
                    let tracks: Vec<TrackSizingFunction> = tracks_list
                        .cast::<PyList>()?
                        .iter()
                        .map(|item| track_sizing_function(&item))
                        .collect::<PyResult<Vec<_>>>()?;
                    let count_item = tuple.get_item(1)?;
                    return Ok(if let Ok(count_str) = count_item.extract::<String>() {
                        if count_str != "auto-fill" && count_str != "auto-fit" {
                            return Err(PyValueError::new_err(format!(
                                "unrecognised repeat() count: {count_str:?}"
                            )));
                        }
                        repeat(count_str.as_str(), tracks)
                    } else {
                        repeat(count_item.extract::<u16>()?, tracks)
                    });
                }
            }
        }
    }
    Ok(track_sizing_function(value)?.into())
}

fn get<'py>(dict: &Bound<'py, PyDict>, key: &str) -> Option<Bound<'py, PyAny>> {
    dict.get_item(key).ok().flatten()
}

fn get_edges_lpa(dict: &Bound<PyDict>, key: &str) -> PyResult<Rect<LengthPercentageAuto>> {
    match get(dict, key) {
        None => Ok(Rect::zero()),
        Some(v) => {
            let list = v.cast::<PyList>().map_err(|_| PyValueError::new_err(format!("{key} must be a 4-item list")))?;
            if list.len() != 4 {
                return Err(PyValueError::new_err(format!("{key} must have exactly 4 items (top,right,bottom,left)")));
            }
            Ok(Rect {
                top: length_percentage_auto(&list.get_item(0)?)?,
                right: length_percentage_auto(&list.get_item(1)?)?,
                bottom: length_percentage_auto(&list.get_item(2)?)?,
                left: length_percentage_auto(&list.get_item(3)?)?,
            })
        }
    }
}

fn get_edges_lp(dict: &Bound<PyDict>, key: &str) -> PyResult<Rect<LengthPercentage>> {
    match get(dict, key) {
        None => Ok(Rect::zero()),
        Some(v) => {
            let list = v.cast::<PyList>().map_err(|_| PyValueError::new_err(format!("{key} must be a 4-item list")))?;
            if list.len() != 4 {
                return Err(PyValueError::new_err(format!("{key} must have exactly 4 items (top,right,bottom,left)")));
            }
            Ok(Rect {
                top: length_percentage(&list.get_item(0)?)?,
                right: length_percentage(&list.get_item(1)?)?,
                bottom: length_percentage(&list.get_item(2)?)?,
                left: length_percentage(&list.get_item(3)?)?,
            })
        }
    }
}

fn get_str(dict: &Bound<PyDict>, key: &str, default: &str) -> PyResult<String> {
    match get(dict, key) {
        Some(v) => v.extract::<String>(),
        None => Ok(default.to_string()),
    }
}

fn parse_overflow_axis(text: &str) -> PyResult<Overflow> {
    Ok(match text {
        "visible" => Overflow::Visible,
        "clip" => Overflow::Clip,
        "hidden" => Overflow::Hidden,
        // CSS `auto`'s scrollbar-on-demand has no exact Taffy equivalent;
        // `Scroll` is the closer of the two non-`visible` options for BFC
        // purposes (both are scroll containers -- see `Overflow::is_scroll_container`).
        "scroll" | "auto" => Overflow::Scroll,
        other => return Err(PyValueError::new_err(format!("unrecognised overflow: {other:?}"))),
    })
}

fn get_overflow(dict: &Bound<PyDict>) -> PyResult<Point<Overflow>> {
    match get(dict, "overflow") {
        None => Ok(Point { x: Overflow::Visible, y: Overflow::Visible }),
        Some(v) => {
            let tuple = v.cast::<PyTuple>().map_err(|_| PyValueError::new_err("overflow must be an (x, y) tuple"))?;
            if tuple.len() != 2 {
                return Err(PyValueError::new_err("overflow must be an (x, y) tuple"));
            }
            Ok(Point {
                x: parse_overflow_axis(&tuple.get_item(0)?.extract::<String>()?)?,
                y: parse_overflow_axis(&tuple.get_item(1)?.extract::<String>()?)?,
            })
        }
    }
}

fn get_bool(dict: &Bound<PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match get(dict, key) {
        Some(v) => v.extract::<bool>(),
        None => Ok(default),
    }
}

fn get_contain(dict: &Bound<PyDict>) -> PyResult<Contain> {
    // `establishes_bfc`, when true, is Python's narrower request for exactly
    // the one thing this POC needs from CSS `contain`: making the box
    // establish an independent formatting context (so a child's margin
    // can't collapse through it) -- not a real `contain` property. `PAINT`
    // (not `LAYOUT`) specifically: both establish a formatting context
    // equally, but `LAYOUT` *also* suppresses the box's baseline for its
    // parent's baseline-alignment purposes (`Contain::suppresses_baseline`),
    // which would silently undo the separate baseline-alignment fix for
    // inline-block siblings -- the first (only, currently) caller of this.
    Ok(if get_bool(dict, "establishes_bfc", false)? { Contain::PAINT } else { Contain::NONE })
}

fn get_f32(dict: &Bound<PyDict>, key: &str, default: f32) -> PyResult<f32> {
    match get(dict, key) {
        Some(v) => v.extract::<f32>(),
        None => Ok(default),
    }
}

fn get_opt_f32(dict: &Bound<PyDict>, key: &str) -> PyResult<Option<f32>> {
    match get(dict, key) {
        Some(v) if !v.is_none() => Ok(Some(v.extract::<f32>()?)),
        _ => Ok(None),
    }
}

fn parse_align_items(text: &str) -> PyResult<Option<AlignItems>> {
    Ok(match text {
        "" | "normal" | "auto" => None,
        "start" => Some(AlignItems::START),
        "end" => Some(AlignItems::END),
        "flex-start" => Some(AlignItems::FLEX_START),
        "flex-end" => Some(AlignItems::FLEX_END),
        "center" => Some(AlignItems::CENTER),
        "safe-start" => Some(AlignItems::SAFE_START),
        "safe-end" => Some(AlignItems::SAFE_END),
        "safe-flex-start" => Some(AlignItems::SAFE_FLEX_START),
        "safe-flex-end" => Some(AlignItems::SAFE_FLEX_END),
        "safe-center" => Some(AlignItems::SAFE_CENTER),
        "baseline" => Some(AlignItems::BASELINE),
        "stretch" => Some(AlignItems::STRETCH),
        other => return Err(PyValueError::new_err(format!("unrecognised align/justify-items keyword: {other:?}"))),
    })
}

fn parse_align_content(text: &str) -> PyResult<Option<AlignContent>> {
    Ok(match text {
        "" | "normal" | "auto" => None,
        "start" => Some(AlignContent::START),
        "end" => Some(AlignContent::END),
        "flex-start" => Some(AlignContent::FLEX_START),
        "flex-end" => Some(AlignContent::FLEX_END),
        "center" => Some(AlignContent::CENTER),
        "safe-start" => Some(AlignContent::SAFE_START),
        "safe-end" => Some(AlignContent::SAFE_END),
        "safe-flex-start" => Some(AlignContent::SAFE_FLEX_START),
        "safe-flex-end" => Some(AlignContent::SAFE_FLEX_END),
        "safe-center" => Some(AlignContent::SAFE_CENTER),
        "stretch" => Some(AlignContent::STRETCH),
        "space-between" => Some(AlignContent::SPACE_BETWEEN),
        "space-around" => Some(AlignContent::SPACE_AROUND),
        "space-evenly" => Some(AlignContent::SPACE_EVENLY),
        other => return Err(PyValueError::new_err(format!("unrecognised align/justify-content keyword: {other:?}"))),
    })
}

fn parse_grid_placement(value: Option<Bound<PyAny>>) -> PyResult<Line<GridPlacement>> {
    match value {
        None => Ok(Line { start: GridPlacement::Auto, end: GridPlacement::Auto }),
        Some(v) => {
            let tuple = v.cast::<PyTuple>().map_err(|_| PyValueError::new_err("grid_column/grid_row must be a (start, end) tuple"))?;
            let one = |item: Bound<PyAny>| -> PyResult<GridPlacement> {
                if item.is_none() {
                    return Ok(GridPlacement::Auto);
                }
                // `grid-column`/`grid-row`'s `span N` form -- style_bridge.py
                // passes it as the 2-tuple `("span", N)` since it has no line
                // number of its own (auto-placed, N tracks wide/tall).
                if let Ok(span_tuple) = item.cast::<PyTuple>() {
                    if span_tuple.len() == 2 {
                        let tag: String = span_tuple.get_item(0)?.extract()?;
                        if tag == "span" {
                            let n: u16 = span_tuple.get_item(1)?.extract()?;
                            return Ok(GridPlacement::Span(n));
                        }
                    }
                }
                let n: i16 = item.extract()?;
                Ok(line(n))
            };
            Ok(Line { start: one(tuple.get_item(0)?)?, end: one(tuple.get_item(1)?)? })
        }
    }
}

/// An unrecognised keyword (an author typo, or a value this level of CSS
/// doesn't define) is an invalid declaration, which CSS ignores: the
/// property keeps its initial value rather than failing the whole page.
fn parse_style(dict: &Bound<PyDict>) -> PyResult<Style> {
    let display = match get_str(dict, "display", "block")?.as_str() {
        "block" => Display::Block,
        "flex" => Display::Flex,
        "grid" => Display::Grid,
        "none" => Display::None,
        _ => Display::Block,
    };
    let position = match get_str(dict, "position", "relative")?.as_str() {
        "relative" => Position::Relative,
        "absolute" => Position::Absolute,
        _ => Position::Relative,
    };
    let box_sizing = match get_str(dict, "box_sizing", "border-box")?.as_str() {
        "border-box" => BoxSizing::BorderBox,
        "content-box" => BoxSizing::ContentBox,
        _ => BoxSizing::BorderBox,
    };
    let flex_direction = match get_str(dict, "flex_direction", "row")?.as_str() {
        "row" => FlexDirection::Row,
        "column" => FlexDirection::Column,
        "row-reverse" => FlexDirection::RowReverse,
        "column-reverse" => FlexDirection::ColumnReverse,
        _ => FlexDirection::Row,
    };
    let flex_wrap = match get_str(dict, "flex_wrap", "nowrap")?.as_str() {
        "nowrap" => FlexWrap::NoWrap,
        "wrap" => FlexWrap::Wrap,
        "wrap-reverse" => FlexWrap::WrapReverse,
        _ => FlexWrap::NoWrap,
    };
    let grid_auto_flow = match get_str(dict, "grid_auto_flow", "row")?.as_str() {
        "row" => GridAutoFlow::Row,
        "column" => GridAutoFlow::Column,
        "row-dense" => GridAutoFlow::RowDense,
        "column-dense" => GridAutoFlow::ColumnDense,
        _ => GridAutoFlow::Row,
    };

    let grid_template_columns = match get(dict, "grid_template_columns") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| grid_template_component(&item)).collect::<PyResult<Vec<_>>>()?,
    };
    let grid_template_rows = match get(dict, "grid_template_rows") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| grid_template_component(&item)).collect::<PyResult<Vec<_>>>()?,
    };
    // CSS Grid 1 7.5: an implicit track (one the grid creates on demand
    // for placement that overflows the explicit `grid-template-*`) sizes
    // from `grid-auto-rows`/`-columns`, not the explicit tracks' default
    // -- absent, Taffy's own default is a single implicit `auto` track,
    // repeated as needed, which is already correct for the common case
    // this leaves unset.
    let grid_auto_rows = match get(dict, "grid_auto_rows") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| track_sizing_function(&item)).collect::<PyResult<Vec<_>>>()?,
    };
    let grid_auto_columns = match get(dict, "grid_auto_columns") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| track_sizing_function(&item)).collect::<PyResult<Vec<_>>>()?,
    };

    let float = match get_str(dict, "float", "none")?.as_str() {
        "left" => Float::Left,
        "right" => Float::Right,
        _ => Float::None,
    };
    let clear = match get_str(dict, "clear", "none")?.as_str() {
        "left" => Clear::Left,
        "right" => Clear::Right,
        "both" => Clear::Both,
        _ => Clear::None,
    };

    Ok(Style {
        display,
        position,
        box_sizing,
        float,
        clear,
        direction: if get_str(dict, "direction", "ltr")? == "rtl" {
            taffy::Direction::Rtl
        } else {
            taffy::Direction::Ltr
        },
        text_align: match get_str(dict, "text_align", "auto")?.as_str() {
            "legacy-center" => taffy::TextAlign::LegacyCenter,
            "legacy-right" => taffy::TextAlign::LegacyRight,
            "legacy-left" => taffy::TextAlign::LegacyLeft,
            _ => taffy::TextAlign::Auto,
        },
        // A table is sized by its own algorithm, never stretched by a
        // block parent (CSS 2.1 17.5.2: its used width is shrink-to-fit).
        item_is_table: get_bool(dict, "is_table", false)?,
        overflow: get_overflow(dict)?,
        contain: get_contain(dict)?,
        size: Size { width: get_size(dict, "width")?, height: get_size(dict, "height")? },
        min_size: Size { width: get_min_max(dict, "min_width")?, height: get_min_max(dict, "min_height")? },
        max_size: Size { width: get_min_max(dict, "max_width")?, height: get_min_max(dict, "max_height")? },
        inset: get_edges_lpa(dict, "inset")?,
        margin: get_edges_lpa(dict, "margin")?,
        padding: get_edges_lp(dict, "padding")?,
        border: get_edges_lp(dict, "border")?,
        gap: get_gap(dict)?,
        flex_direction,
        flex_wrap,
        flex_grow: get_f32(dict, "flex_grow", 0.0)?,
        flex_shrink: get_f32(dict, "flex_shrink", 1.0)?,
        flex_basis: get_size(dict, "flex_basis")?,
        align_items: parse_align_items(&get_str(dict, "align_items", "")?)?,
        align_self: parse_align_items(&get_str(dict, "align_self", "")?)?,
        justify_content: parse_align_content(&get_str(dict, "justify_content", "")?)?,
        align_content: parse_align_content(&get_str(dict, "align_content", "")?)?,
        // CSS Box Alignment 3: grid's *inline*-axis counterpart to
        // `align-items`/`align-self` -- flexbox has no such axis (its
        // single cross axis is `align-items`), so this is grid-only.
        justify_items: parse_align_items(&get_str(dict, "justify_items", "")?)?,
        justify_self: parse_align_items(&get_str(dict, "justify_self", "")?)?,
        grid_auto_flow,
        grid_template_columns,
        grid_template_rows,
        grid_auto_rows,
        grid_auto_columns,
        grid_column: parse_grid_placement(get(dict, "grid_column"))?,
        grid_row: parse_grid_placement(get(dict, "grid_row"))?,
        aspect_ratio: get_opt_f32(dict, "aspect_ratio")?,
        ..Default::default()
    })
}

fn get_gap(dict: &Bound<PyDict>) -> PyResult<Size<LengthPercentage>> {
    match get(dict, "gap") {
        None => Ok(Size { width: LengthPercentage::ZERO, height: LengthPercentage::ZERO }),
        Some(v) => {
            let tuple = v.cast::<PyTuple>().map_err(|_| PyValueError::new_err("gap must be a (row, column) tuple"))?;
            if tuple.len() != 2 {
                return Err(PyValueError::new_err("gap must be a (row, column) tuple"));
            }
            Ok(Size {
                height: length_percentage(&tuple.get_item(0)?)?, // row-gap is the block/y axis
                width: length_percentage(&tuple.get_item(1)?)?,  // column-gap is the inline/x axis
            })
        }
    }
}

fn get_size(dict: &Bound<PyDict>, key: &str) -> PyResult<Dimension> {
    match get(dict, key) {
        None => Ok(auto()),
        Some(v) => dimension(&v),
    }
}

fn get_min_max(dict: &Bound<PyDict>, key: &str) -> PyResult<LengthPercentageAuto> {
    match get(dict, key) {
        None => Ok(auto()),
        Some(v) => length_percentage_auto(&v),
    }
}

// -- the tree -----------------------------------------------------------
//
// chromonic's own node store implementing Taffy's `LayoutPartialTree` family
// of traits (the `custom_tree_vec.rs` pattern from Taffy's own examples)
// rather than the batteries-included `TaffyTree`. The built-in tree can only
// dispatch on `Display`; owning the dispatch is what lets a node kind Taffy
// has no algorithm for -- an inline formatting context -- lay out its own
// children (atomic inline boxes, floats) from *inside* the layout recursion,
// with Taffy's block formatting context (`BlockContext`: floats, clearance,
// margin struts) handed in, instead of being approximated as a flex row and
// repaired after the fact.
//
// Node ids handed to Python pack a slot index (low 32 bits) with a per-slot
// generation (high 32 bits), so an id that outlives its node fails
// validation instead of silently aliasing onto whatever reused the slot --
// the same guarantee `TaffyTree`'s slotmap keys gave.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum NodeKind {
    /// Dispatch on `Style.display`, exactly as `TaffyTree` does.
    Auto,
    /// An inline formatting context: `compute_inline_layout`.
    Inline,
    /// A table grid: `compute_table_layout`.
    Table,
}

struct Node {
    style: Style,
    kind: NodeKind,
    children: Vec<u64>,
    parent: Option<usize>,
    measure: Option<Py<PyAny>>,
    cache: Cache,
    layout: Layout,
    generation: u32,
    alive: bool,
    /// For an absolutely positioned box laid out as a child of its
    /// containing block: the zero-size placeholder left where the box would
    /// sit in normal flow, and whether that flow is right-to-left. Its
    /// position is the box's static position (CSS 2.1 10.3.7/10.6.4).
    anchor: Option<(u64, bool)>,
}

impl Node {
    fn dead(generation: u32) -> Self {
        Node {
            style: Style::default(),
            kind: NodeKind::Auto,
            children: Vec::new(),
            parent: None,
            measure: None,
            cache: Cache::new(),
            layout: Layout::with_order(0),
            generation,
            alive: false,
            anchor: None,
        }
    }
}

fn pack(index: usize, generation: u32) -> u64 {
    ((generation as u64) << 32) | (index as u64 & 0xFFFF_FFFF)
}

fn index_of(id: NodeId) -> usize {
    (u64::from(id) & 0xFFFF_FFFF) as usize
}

// `taffy::Style` carries a raw pointer for calc()-expression handles (a
// feature we don't use), which makes it not automatically Send/Sync.
// `unsendable` is PyO3's standard escape hatch for a Rust type that a Python
// extension only ever touches from the thread that created it -- true here.
#[pyclass(unsendable)]
struct Tree {
    nodes: Vec<Node>,
    free: Vec<usize>,
}

impl Tree {
    fn resolve(&self, id: u64) -> PyResult<usize> {
        let index = (id & 0xFFFF_FFFF) as usize;
        let generation = (id >> 32) as u32;
        match self.nodes.get(index) {
            Some(node) if node.alive && node.generation == generation => Ok(index),
            _ => Err(PyRuntimeError::new_err(format!("unknown or stale layout node id {id}"))),
        }
    }

    fn alloc(&mut self, style: Style, kind: NodeKind, children: Vec<u64>, measure: Option<Py<PyAny>>) -> u64 {
        let index = match self.free.pop() {
            Some(index) => index,
            None => {
                self.nodes.push(Node::dead(0));
                self.nodes.len() - 1
            }
        };
        for child in &children {
            self.nodes[index_of(NodeId::from(*child))].parent = Some(index);
        }
        let node = &mut self.nodes[index];
        node.style = style;
        node.kind = kind;
        node.children = children;
        node.parent = None;
        node.measure = measure;
        node.cache = Cache::new();
        node.layout = Layout::with_order(0);
        node.anchor = None;
        node.alive = true;
        pack(index, node.generation)
    }

    fn validate_children(&self, children: &[u64]) -> PyResult<()> {
        for child in children {
            self.resolve(*child)?;
        }
        Ok(())
    }

    /// Move every anchored absolutely positioned box whose insets are auto
    /// on an axis to its static position on that axis: where its anchor
    /// (a zero-size placeholder laid out in normal flow) ended up. A box
    /// can sit before its anchor in tree order (a `fixed` box inside an
    /// absolutely positioned parent: both belong to the viewport root, the
    /// fixed one first), and moving a box moves any anchors inside it, so
    /// this settles by repetition: place every box whose anchor is known,
    /// recompute, until nothing moves.
    fn resolve_static_anchors(&mut self, root: usize) {
        let anchored: Vec<usize> = (0..self.nodes.len())
            .filter(|&i| self.nodes[i].alive && self.nodes[i].anchor.is_some())
            .collect();
        if anchored.is_empty() {
            return;
        }
        for _ in 0..8 {
            let absolute = self.absolute_positions(root);
            let mut moved = false;
            for &index in &anchored {
                let Some((anchor, rtl)) = self.nodes[index].anchor else { continue };
                let Some(anchor_pos) = self.resolve(anchor).ok().and_then(|a| absolute[a]) else { continue };
                let (Some((own_x, own_y)), Some(parent)) = (absolute[index], self.nodes[index].parent) else { continue };
                let _ = parent;
                let node = &mut self.nodes[index];
                let inset = node.style.inset;
                let margin = node.layout.margin;
                let parent_x = own_x - node.layout.location.x;
                let parent_y = own_y - node.layout.location.y;
                if inset.left.is_auto() && inset.right.is_auto() {
                    let x = if rtl {
                        anchor_pos.0 - node.layout.size.width - margin.right
                    } else {
                        anchor_pos.0 + margin.left
                    };
                    if (x - own_x).abs() > 0.001 {
                        node.layout.location.x = x - parent_x;
                        moved = true;
                    }
                }
                if inset.top.is_auto() && inset.bottom.is_auto() {
                    let y = anchor_pos.1 + margin.top;
                    if (y - own_y).abs() > 0.001 {
                        node.layout.location.y = y - parent_y;
                        moved = true;
                    }
                }
            }
            if !moved {
                break;
            }
        }
    }

    /// Every live node's absolute (x, y), by index.
    fn absolute_positions(&self, root: usize) -> Vec<Option<(f32, f32)>> {
        let mut absolute: Vec<Option<(f32, f32)>> = vec![None; self.nodes.len()];
        let mut stack: Vec<(usize, f32, f32)> = vec![(root, 0.0, 0.0)];
        while let Some((index, parent_x, parent_y)) = stack.pop() {
            let node = &self.nodes[index];
            let x = parent_x + node.layout.location.x;
            let y = parent_y + node.layout.location.y;
            absolute[index] = Some((x, y));
            for child in &node.children {
                stack.push((index_of(NodeId::from(*child)), x, y));
            }
        }
        absolute
    }

    fn collect_absolute(&self, index: usize, parent_x: f32, parent_y: f32, out: &Bound<PyDict>) -> PyResult<()> {
        let node = &self.nodes[index];
        let layout = &node.layout;
        let x = parent_x + layout.location.x;
        let y = parent_y + layout.location.y;
        let tuple = (
            (x, y, layout.size.width, layout.size.height),
            (layout.border.top, layout.border.right, layout.border.bottom, layout.border.left),
            (layout.padding.top, layout.padding.right, layout.padding.bottom, layout.padding.left),
            (layout.margin.top, layout.margin.right, layout.margin.bottom, layout.margin.left),
        );
        out.set_item(pack(index, node.generation), tuple)?;
        for child in &node.children {
            self.collect_absolute(index_of(NodeId::from(*child)), x, y, out)?;
        }
        Ok(())
    }
}

#[pymethods]
impl Tree {
    #[new]
    fn new() -> Self {
        Tree { nodes: Vec::new(), free: Vec::new() }
    }

    /// A leaf node with no measure callback (a plain box: nothing to
    /// intrinsically size, e.g. an empty `<div>`).
    fn new_leaf(&mut self, style: &Bound<PyDict>) -> PyResult<u64> {
        let style = parse_style(style)?;
        Ok(self.alloc(style, NodeKind::Auto, Vec::new(), None))
    }

    /// A text leaf: `measure` is called during layout as
    /// `measure(available_width, available_height, known_width, known_height)`
    /// returning `(width, height)` or `(width, height, baseline)` -- the
    /// baseline (distance from the content box's top to the first
    /// baseline, or None) lets flex/grid `align-items: baseline` and an
    /// enclosing inline formatting context see the real one.
    fn new_text_leaf(&mut self, style: &Bound<PyDict>, measure: Py<PyAny>) -> PyResult<u64> {
        let style = parse_style(style)?;
        Ok(self.alloc(style, NodeKind::Auto, Vec::new(), Some(measure)))
    }

    fn new_with_children(&mut self, style: &Bound<PyDict>, children: Vec<u64>) -> PyResult<u64> {
        let style = parse_style(style)?;
        self.validate_children(&children)?;
        Ok(self.alloc(style, NodeKind::Auto, children, None))
    }

    /// An inline formatting context node: `measure` lays out this node's
    /// lines (see `compute_inline_layout`); `children` are the atomic
    /// inline-level boxes (inline-block, replaced, inline-table, floats)
    /// that flow inside those lines, in `measure`'s item order.
    fn new_inline(&mut self, style: &Bound<PyDict>, measure: Py<PyAny>, children: Vec<u64>) -> PyResult<u64> {
        let style = parse_style(style)?;
        self.validate_children(&children)?;
        Ok(self.alloc(style, NodeKind::Inline, children, Some(measure)))
    }

    /// A table box: `measure` runs the table algorithm (see
    /// `compute_table_layout`); `children` are its captions and row groups
    /// or rows, whose own children are the cells the algorithm places.
    fn new_table(&mut self, style: &Bound<PyDict>, measure: Py<PyAny>, children: Vec<u64>) -> PyResult<u64> {
        let style = parse_style(style)?;
        self.validate_children(&children)?;
        Ok(self.alloc(style, NodeKind::Table, children, Some(measure)))
    }

    fn set_style(&mut self, node: u64, style: &Bound<PyDict>) -> PyResult<()> {
        let index = self.resolve(node)?;
        self.nodes[index].style = parse_style(style)?;
        Ok(())
    }

    /// Update retained-node insets in one Python crossing. Animation paths
    /// use this when every other style field and the tree topology are stable.
    fn set_insets(&mut self, updates: Vec<(u64, f32, f32, f32, f32)>) -> PyResult<()> {
        for (node, top, right, bottom, left) in updates {
            let index = self.resolve(node)?;
            self.nodes[index].style.inset = Rect {
                top: length(top),
                right: length(right),
                bottom: length(bottom),
                left: length(left),
            };
        }
        Ok(())
    }

    fn set_children(&mut self, node: u64, children: Vec<u64>) -> PyResult<()> {
        let index = self.resolve(node)?;
        self.validate_children(&children)?;
        let old = std::mem::take(&mut self.nodes[index].children);
        for child in old {
            let child_index = index_of(NodeId::from(child));
            if self.nodes[child_index].parent == Some(index) {
                self.nodes[child_index].parent = None;
            }
        }
        for child in &children {
            self.nodes[index_of(NodeId::from(*child))].parent = Some(index);
        }
        self.nodes[index].children = children;
        Ok(())
    }

    /// Link an absolutely positioned box (built as a child of its containing
    /// block) to the placeholder marking its static position; None unlinks.
    #[pyo3(signature = (node, anchor, rtl=false))]
    fn set_static_anchor(&mut self, node: u64, anchor: Option<u64>, rtl: bool) -> PyResult<()> {
        let index = self.resolve(node)?;
        if let Some(anchor) = anchor {
            self.resolve(anchor)?;
        }
        self.nodes[index].anchor = anchor.map(|a| (a, rtl));
        Ok(())
    }

    fn set_measure(&mut self, node: u64, measure: Option<Py<PyAny>>) -> PyResult<()> {
        let index = self.resolve(node)?;
        self.nodes[index].measure = measure;
        Ok(())
    }

    /// Drop a node. An id that is already gone is a no-op: the caller's
    /// intent is only ever "make sure this node is no longer in the tree".
    fn remove(&mut self, node: u64) -> PyResult<()> {
        let Ok(index) = self.resolve(node) else { return Ok(()) };
        if let Some(parent) = self.nodes[index].parent {
            self.nodes[parent].children.retain(|child| index_of(NodeId::from(*child)) != index);
        }
        for child in std::mem::take(&mut self.nodes[index].children) {
            let child_index = index_of(NodeId::from(child));
            if self.nodes[child_index].parent == Some(index) {
                self.nodes[child_index].parent = None;
            }
        }
        let generation = self.nodes[index].generation.wrapping_add(1);
        self.nodes[index] = Node::dead(generation);
        self.free.push(index);
        Ok(())
    }

    /// Run layout, then return `{node_id: ((x, y, width, height), (border
    /// top, right, bottom, left), (padding ...), (used margin ...))}` for
    /// every node, in **absolute**
    /// page coordinates (Taffy itself only stores parent-relative
    /// `location`; this walks the tree accumulating the offset once so
    /// Python never has to).
    fn compute(
        &mut self,
        py: Python<'_>,
        root: u64,
        available_width: Option<f32>,
        available_height: Option<f32>,
    ) -> PyResult<Py<PyDict>> {
        let root_index = self.resolve(root)?;
        // Every compute is a full relayout. A cached PerformLayout result
        // for a block subtree would skip re-placing that subtree's floats
        // into the (fresh) block formatting context its siblings lay out
        // against, so a retained tree must not carry results across
        // computes. Style resolution, not Taffy, dominates relayout cost.
        for node in self.nodes.iter_mut() {
            if node.alive {
                node.cache.clear();
            }
        }
        let available = Size {
            width: available_width.map(AvailableSpace::Definite).unwrap_or(AvailableSpace::MaxContent),
            height: available_height.map(AvailableSpace::Definite).unwrap_or(AvailableSpace::MaxContent),
        };
        {
            let mut view = TreeView { tree: self, py };
            compute_root_layout(&mut view, NodeId::from(root), available);
        }
        self.resolve_static_anchors(root_index);
        let out = PyDict::new(py);
        self.collect_absolute(root_index, 0.0, 0.0, &out)?;
        Ok(out.into())
    }
}

/// The `Tree` plus the GIL token layout runs under -- what Taffy's traits
/// are implemented on (mirrors `TaffyView` inside `TaffyTree`).
struct TreeView<'a> {
    tree: &'a mut Tree,
    py: Python<'a>,
}

struct ChildIter<'a>(std::slice::Iter<'a, u64>);

impl Iterator for ChildIter<'_> {
    type Item = NodeId;
    fn next(&mut self) -> Option<Self::Item> {
        self.0.next().copied().map(NodeId::from)
    }
}

impl TraversePartialTree for TreeView<'_> {
    type ChildIter<'a>
        = ChildIter<'a>
    where
        Self: 'a;

    fn child_ids(&self, node_id: NodeId) -> Self::ChildIter<'_> {
        ChildIter(self.tree.nodes[index_of(node_id)].children.iter())
    }

    fn child_count(&self, node_id: NodeId) -> usize {
        self.tree.nodes[index_of(node_id)].children.len()
    }

    fn get_child_id(&self, node_id: NodeId, index: usize) -> NodeId {
        NodeId::from(self.tree.nodes[index_of(node_id)].children[index])
    }
}

impl TraverseTree for TreeView<'_> {}

impl LayoutPartialTree for TreeView<'_> {
    type CustomIdent = String;
    type CoreContainerStyle<'a>
        = &'a Style
    where
        Self: 'a;

    fn get_core_container_style(&self, node_id: NodeId) -> Self::CoreContainerStyle<'_> {
        &self.tree.nodes[index_of(node_id)].style
    }

    fn set_unrounded_layout(&mut self, node_id: NodeId, layout: &Layout) {
        self.tree.nodes[index_of(node_id)].layout = *layout;
    }

    fn resolve_calc_value(&self, _val: *const (), _basis: f32) -> f32 {
        0.0
    }

    fn compute_child_layout(&mut self, node_id: NodeId, inputs: LayoutInput) -> LayoutOutput {
        self.dispatch(node_id, inputs, None)
    }
}

impl CacheTree for TreeView<'_> {
    fn cache_get(&mut self, node_id: NodeId, inputs: &LayoutInput) -> Option<LayoutOutput> {
        self.tree.nodes[index_of(node_id)].cache.get(inputs)
    }

    fn cache_store(&mut self, node_id: NodeId, inputs: &LayoutInput, layout_output: LayoutOutput) {
        self.tree.nodes[index_of(node_id)].cache.store(inputs, layout_output)
    }

    fn cache_clear(&mut self, node_id: NodeId) {
        self.tree.nodes[index_of(node_id)].cache.clear();
    }
}

impl LayoutBlockContainer for TreeView<'_> {
    type BlockContainerStyle<'a>
        = &'a Style
    where
        Self: 'a;
    type BlockItemStyle<'a>
        = &'a Style
    where
        Self: 'a;

    fn get_block_container_style(&self, node_id: NodeId) -> Self::BlockContainerStyle<'_> {
        &self.tree.nodes[index_of(node_id)].style
    }

    fn get_block_child_style(&self, child_node_id: NodeId) -> Self::BlockItemStyle<'_> {
        &self.tree.nodes[index_of(child_node_id)].style
    }

    fn compute_block_child_layout(
        &mut self,
        node_id: NodeId,
        inputs: LayoutInput,
        block_ctx: Option<&mut BlockContext<'_>>,
    ) -> LayoutOutput {
        self.dispatch(node_id, inputs, block_ctx)
    }
}

impl LayoutFlexboxContainer for TreeView<'_> {
    type FlexboxContainerStyle<'a>
        = &'a Style
    where
        Self: 'a;
    type FlexboxItemStyle<'a>
        = &'a Style
    where
        Self: 'a;

    fn get_flexbox_container_style(&self, node_id: NodeId) -> Self::FlexboxContainerStyle<'_> {
        &self.tree.nodes[index_of(node_id)].style
    }

    fn get_flexbox_child_style(&self, child_node_id: NodeId) -> Self::FlexboxItemStyle<'_> {
        &self.tree.nodes[index_of(child_node_id)].style
    }
}

impl LayoutGridContainer for TreeView<'_> {
    type GridContainerStyle<'a>
        = &'a Style
    where
        Self: 'a;
    type GridItemStyle<'a>
        = &'a Style
    where
        Self: 'a;

    fn get_grid_container_style(&self, node_id: NodeId) -> Self::GridContainerStyle<'_> {
        &self.tree.nodes[index_of(node_id)].style
    }

    fn get_grid_child_style(&self, child_node_id: NodeId) -> Self::GridItemStyle<'_> {
        &self.tree.nodes[index_of(child_node_id)].style
    }
}

impl TreeView<'_> {
    /// Same dispatch `TaffyTree` performs, plus the `Inline` node kind. A
    /// same-BFC block child arrives here with its parent's `BlockContext`
    /// (via `compute_block_child_layout`); any other route passes `None`.
    fn dispatch(
        &mut self,
        node_id: NodeId,
        inputs: LayoutInput,
        block_ctx: Option<&mut BlockContext<'_>>,
    ) -> LayoutOutput {
        if inputs.run_mode == RunMode::PerformHiddenLayout {
            return compute_hidden_layout(self, node_id);
        }
        compute_cached_layout(self, node_id, inputs, |tree, node_id, inputs| {
            let node = &tree.tree.nodes[index_of(node_id)];
            let (display, kind, has_children) = (node.style.display, node.kind, !node.children.is_empty());
            match (kind, display, has_children) {
                (_, Display::None, _) => compute_hidden_layout(tree, node_id),
                (NodeKind::Inline, _, _) => tree.compute_inline_layout(node_id, inputs, block_ctx),
                (NodeKind::Table, _, _) => tree.compute_table_layout(node_id, inputs),
                (_, Display::Block, true) => compute_block_layout(tree, node_id, inputs, block_ctx),
                (_, Display::Flex, true) => compute_flexbox_layout(tree, node_id, inputs),
                (_, Display::Grid, true) => compute_grid_layout(tree, node_id, inputs),
                (_, _, true) => compute_block_layout(tree, node_id, inputs, None),
                (_, _, false) => tree.compute_leaf(node_id, inputs),
            }
        })
    }

    /// A table (CSS 2.1 17.5). The Python table algorithm (`measure`) runs
    /// as a short conversation, each call handed every answer so far in
    /// `responses` (a dict keyed by the keys it chose):
    ///
    ///   measure(available_width, available_height, known_width, known_height, responses)
    ///     -> ("intrinsic", [(key, node), ...])
    ///          min-content and max-content border-box widths of each node:
    ///          responses[key] = (min, max)
    ///     -> ("layout", [(key, node, width, height_or_None), ...])
    ///          each node laid out at that border-box size:
    ///          responses[key] = (width, height, first_baseline, last_baseline)
    ///     -> ("done", width, height, first_baseline, placements)
    ///          content size and baseline, and every captions/row-group/
    ///          row/cell box as (node, x, y, width, height, content_offset)
    ///          relative to this node's content box. A node whose parent is
    ///          also placed is converted to parent-relative coordinates.
    ///          (+ layout_width, layout_height). content_offset >= 0 lays the
    ///          node out at the layout size (a cell spanning a collapsed row or
    ///          column keeps its uncollapsed size) and moves its content down
    ///          by the offset (a cell's vertical-align);
    ///          a negative offset only positions and sizes the box (a row
    ///          or row group, whose children are placed separately).
    fn compute_table_layout(&mut self, node_id: NodeId, inputs: LayoutInput) -> LayoutOutput {
        let py = self.py;
        let index = index_of(node_id);
        let (style, measure) = {
            let node = &self.tree.nodes[index];
            (node.style.clone(), node.measure.as_ref().map(|m| m.clone_ref(py)))
        };
        let own = OwnBox::resolve(&style, &inputs);
        if let Some(output) = own.short_circuit(inputs.run_mode) {
            return output;
        }
        let Some(measure) = measure else {
            return own.finish(&style, Size::ZERO, None, None);
        };
        let perform = inputs.run_mode == RunMode::PerformLayout;
        let content = own.content_available;
        let cb_width = content.width.into_option();
        let width_arg: Option<f32> = match content.width {
            AvailableSpace::Definite(v) => Some(v),
            AvailableSpace::MinContent => Some(-1.0),
            AvailableSpace::MaxContent => None,
        };
        let height_arg = content.height.into_option();
        let known = if perform { Size::NONE } else { inputs.known_dimensions };
        // The parent (a stretching flex/grid container) or the style fixes
        // the width: the grid fills it rather than shrinking to fit.
        let fills_width = own.known_dimensions.width.is_some() || own.node_size.width.is_some();
        let child_inputs = |run_mode: RunMode, known: Size<Option<f32>>, width: AvailableSpace| LayoutInput {
            run_mode,
            sizing_mode: SizingMode::InherentSize,
            axis: RequestedAxis::Both,
            known_dimensions: known,
            known_dimensions_are_definite: Size { width: true, height: true },
            parent_size: Size { width: cb_width, height: content.height.into_option() },
            available_space: Size { width, height: AvailableSpace::MaxContent },
            vertical_margins_are_collapsible: Line::FALSE,
        };
        let responses = PyDict::new(py);
        let mut result: Option<(f32, f32, Option<f32>, Vec<(u64, f32, f32, f32, f32, f32, f32, f32)>)> = None;
        for _ in 0..16 {
            let args = (width_arg, height_arg, known.width, known.height, responses.clone(), fills_width);
            let Ok(reply) = measure.call1(py, args) else { break };
            let reply = reply.bind(py);
            if let Ok((tag, requests)) = reply.extract::<(String, Vec<(Py<PyAny>, u64)>)>() {
                if tag != "intrinsic" {
                    break;
                }
                for (key, node) in requests {
                    let Ok(child) = self.tree.resolve(node) else { continue };
                    let child = NodeId::from(pack(child, self.tree.nodes[child].generation));
                    let min = self
                        .compute_child_layout(child, child_inputs(RunMode::ComputeSize, Size::NONE, AvailableSpace::MinContent))
                        .size
                        .width;
                    let max = self
                        .compute_child_layout(child, child_inputs(RunMode::ComputeSize, Size::NONE, AvailableSpace::MaxContent))
                        .size
                        .width;
                    let _ = responses.set_item(key, (min, max));
                }
                continue;
            }
            if let Ok((tag, requests)) = reply.extract::<(String, Vec<(Py<PyAny>, u64, f32, Option<f32>)>)>() {
                if tag != "layout" {
                    break;
                }
                for (key, node, width, height) in requests {
                    let Ok(child) = self.tree.resolve(node) else { continue };
                    let child = NodeId::from(pack(child, self.tree.nodes[child].generation));
                    let output = self.compute_child_layout(
                        child,
                        child_inputs(RunMode::PerformLayout, Size { width: Some(width), height }, AvailableSpace::Definite(width)),
                    );
                    let _ = responses.set_item(
                        key,
                        (output.size.width, output.size.height, output.baselines.first, output.baselines.last),
                    );
                }
                continue;
            }
            if let Ok((tag, w, h, baseline, placements)) =
                reply.extract::<(String, f32, f32, Option<f32>, Vec<(u64, f32, f32, f32, f32, f32, f32, f32)>)>()
            {
                if tag == "done" {
                    result = Some((w, h, baseline, placements));
                }
            }
            break;
        }
        let Some((width, height, baseline, placements)) = result else {
            return own.finish(&style, Size::ZERO, None, None);
        };
        if perform {
            let inset = Point { x: own.padding_border.left, y: own.padding_border.top };
            // table-content-relative position of every placed node
            let mut placed: std::collections::HashMap<usize, (f32, f32)> = std::collections::HashMap::new();
            for (order, (node, x, y, w, h, offset, layout_w, layout_h)) in placements.iter().enumerate() {
                let Ok(child) = self.tree.resolve(*node) else { continue };
                let child_id = NodeId::from(pack(child, self.tree.nodes[child].generation));
                let (px, py_) = match self.tree.nodes[child].parent.and_then(|p| placed.get(&p)) {
                    Some(&(px, py_)) => (px, py_),
                    None => (-inset.x, -inset.y),
                };
                placed.insert(child, (*x, *y));
                if *offset >= 0.0 {
                    self.compute_child_layout(
                        child_id,
                        child_inputs(
                            RunMode::PerformLayout,
                            Size { width: Some(*layout_w), height: Some(*layout_h) },
                            AvailableSpace::Definite(*layout_w),
                        ),
                    );
                    if *offset > 0.0 {
                        let kids = self.tree.nodes[child].children.clone();
                        for kid in kids {
                            let k = index_of(NodeId::from(kid));
                            self.tree.nodes[k].layout.location.y += *offset;
                        }
                    }
                }
                // A node's own layout record is its parent's to write (Taffy
                // never sets it while laying the node out), so its padding
                // and border come from its style here.
                let mut layout = Layout::with_order(order as u32);
                {
                    let child_style = &self.tree.nodes[child].style;
                    layout.padding = child_style.padding.resolve_or_zero(cb_width, |_, _| 0.0);
                    layout.border = child_style.border.resolve_or_zero(cb_width, |_, _| 0.0);
                }
                layout.order = order as u32;
                layout.location = Point { x: x - px, y: y - py_ };
                layout.size = Size { width: *w, height: *h };
                self.set_unrounded_layout(child_id, &layout);
            }
        }
        own.finish(&style, Size { width, height }, baseline, baseline)
    }

    /// A leaf: Taffy's own leaf sizing around the Python measure callback,
    /// plus the first baseline the callback may report.
    fn compute_leaf(&mut self, node_id: NodeId, inputs: LayoutInput) -> LayoutOutput {
        let py = self.py;
        let node = &self.tree.nodes[index_of(node_id)];
        let mut baselines = Baselines::NONE;
        let mut output = compute_leaf_layout(inputs, &node.style, |_, _| 0.0, |known_dimensions, available_space| {
            let (size, measured_baselines) = measure_via_python(py, known_dimensions, available_space, node.measure.as_ref());
            baselines = measured_baselines;
            size
        });
        if baselines.first.is_some() || baselines.last.is_some() {
            let inset_top = node.style.padding.top.resolve_or_zero(inputs.parent_size.width, |_, _| 0.0)
                + node.style.border.top.resolve_or_zero(inputs.parent_size.width, |_, _| 0.0);
            output.baselines.first = baselines.first.map(|b| b + inset_top);
            output.baselines.last = baselines.last.map(|b| b + inset_top);
        }
        output
    }

    /// An inline formatting context (CSS 2.1 9.4.2): the block container's
    /// lines, laid by the Python plan (`measure`), with this node's
    /// children -- the atomic inline-level boxes (inline-block, replaced,
    /// inline-table) and floats mixed into those lines -- sized here by
    /// their own formatting context first and positioned afterwards.
    ///
    /// Protocol with the Python side, per measure:
    ///   measure(available_width, available_height, known_width, known_height,
    ///           atomics, bands, placed_floats, owns_bfc)
    /// where `atomics[k] = (k, width, height, baseline, mt, mr, mb, ml,
    /// float)` is child k's border-box size after shrink-to-fit sizing
    /// (CSS 2.1 10.3.9) plus its margins and float side; `bands` is the
    /// float-free horizontal space per vertical range, content-box
    /// relative `(top, bottom, left, width)` (None when this node's width
    /// is indefinite, i.e. an intrinsic-size probe); `placed_floats` is
    /// every float already placed this measure as `(k, x, y)` margin-box
    /// positions. The plan returns either `("float", k, y)` -- place
    /// child k's float no higher than content-box `y`, then call again
    /// (the lines restart against the updated bands) -- or the final
    /// `(width, height, baseline, [(k, x, y), ...])`: content size, first
    /// baseline, and each non-float atomic's border-box position.
    ///
    /// Floats go through Taffy's own float context: the parent block's
    /// `BlockContext` when this node is a same-BFC block child (so later
    /// siblings flow around them and a BFC root's auto height includes
    /// them), or a private one when this node is itself the root of its
    /// block formatting context.
    fn compute_inline_layout(
        &mut self,
        node_id: NodeId,
        inputs: LayoutInput,
        mut block_ctx: Option<&mut BlockContext<'_>>,
    ) -> LayoutOutput {
        let py = self.py;
        let index = index_of(node_id);
        let (style, measure, children) = {
            let node = &self.tree.nodes[index];
            (node.style.clone(), node.measure.as_ref().map(|m| m.clone_ref(py)), node.children.clone())
        };
        let Some(measure) = measure else {
            return compute_leaf_layout(inputs, &style, |_, _| 0.0, |_, _| Size::ZERO);
        };
        let own = OwnBox::resolve(&style, &inputs);
        if let Some(output) = own.short_circuit(inputs.run_mode) {
            return output;
        }
        let LayoutInput { known_dimensions, run_mode, .. } = inputs;
        let OwnBox { padding_border, content_available, .. } = own;
        let calc = |_: *const (), _: f32| 0.0;
        let inset_left = padding_border.left;
        let inset_top = padding_border.top;
        let cb_width = content_available.width.into_option();
        let perform = run_mode == RunMode::PerformLayout;

        // -- size every atomic child in its own formatting context --
        let child_inputs = |run_mode: RunMode, known: Size<Option<f32>>, available: Size<AvailableSpace>| LayoutInput {
            run_mode,
            sizing_mode: SizingMode::InherentSize,
            axis: RequestedAxis::Both,
            known_dimensions: known,
            known_dimensions_are_definite: Size { width: true, height: true },
            // CSS 2.1 10.5: a percentage height resolves against the
            // containing block's height when that is definite
            // (block-formatting-contexts-008.xht: a `height: 50%` float).
            parent_size: Size { width: cb_width, height: content_available.height.into_option() },
            available_space: available,
            vertical_margins_are_collapsible: Line::FALSE,
        };
        let mut atomics: Vec<AtomicChild> = Vec::with_capacity(children.len());
        for child in &children {
            let child_id = NodeId::from(*child);
            let (child_margin, child_padding, child_border, float, clear) = {
                let child_style = &self.tree.nodes[index_of(child_id)].style;
                (
                    child_style.margin.resolve_or_zero(cb_width, calc),
                    child_style.padding.resolve_or_zero(cb_width, calc),
                    child_style.border.resolve_or_zero(cb_width, calc),
                    child_style.float,
                    child_style.clear,
                )
            };
            let max_content = self
                .compute_child_layout(
                    child_id,
                    child_inputs(
                        RunMode::ComputeSize,
                        Size::NONE,
                        Size { width: AvailableSpace::MaxContent, height: AvailableSpace::MaxContent },
                    ),
                )
                .size
                .width;
            let min_content = self
                .compute_child_layout(
                    child_id,
                    child_inputs(
                        RunMode::ComputeSize,
                        Size::NONE,
                        Size { width: AvailableSpace::MinContent, height: AvailableSpace::MaxContent },
                    ),
                )
                .size
                .width;
            // CSS 2.1 10.3.9/10.3.5 shrink-to-fit: min(max(min-content,
            // available), max-content), the available width being the
            // containing block's, not the remaining line space.
            let available_width = match content_available.width {
                AvailableSpace::Definite(width) => (width - child_margin.horizontal_axis_sum()).max(0.0),
                AvailableSpace::MinContent => 0.0,
                AvailableSpace::MaxContent => f32::INFINITY,
            };
            let used_width = max_content.min(available_width.max(min_content));
            let output = self.compute_child_layout(
                child_id,
                child_inputs(
                    if perform { RunMode::PerformLayout } else { RunMode::ComputeSize },
                    Size { width: Some(used_width), height: None },
                    Size { width: AvailableSpace::Definite(used_width), height: AvailableSpace::MaxContent },
                ),
            );
            atomics.push(AtomicChild {
                id: child_id,
                size: output.size,
                // CSS 2.1 10.8.1: an inline-block sits on its *last* line
                // box's baseline (first when that's all that's known).
                baseline: output.baselines.last.or(output.baselines.first),
                margin: child_margin,
                padding: child_padding,
                border: child_border,
                float,
                clear,
            });
        }

        // -- lay the lines against a float context: the parent block's when
        // this node is a same-BFC block child, else a private one --
        let definite_width = matches!(content_available.width, AvailableSpace::Definite(_));
        let owns_bfc = block_ctx.is_none();
        let probe = InlineProbe {
            py,
            measure: &measure,
            atomics: &atomics,
            inset_left,
            inset_top,
            content_width: cb_width.unwrap_or(0.0),
            width_arg: match content_available.width {
                AvailableSpace::Definite(v) => Some(v),
                AvailableSpace::MinContent => Some(-1.0),
                AvailableSpace::MaxContent => None,
            },
            height_arg: match content_available.height {
                AvailableSpace::Definite(v) => Some(v),
                _ => None,
            },
            known: if perform { Size::NONE } else { known_dimensions },
            owns_bfc,
            definite_width,
        };
        let lines = match block_ctx.as_deref_mut() {
            Some(ctx) => {
                // Floats' containing block is this node's content box, not
                // its border box (all Taffy's own nested-block handling
                // tracks -- see block.rs's "TODO: handle nested blocks
                // with different widths").
                ctx.apply_content_box_inset([padding_border.left, padding_border.right]);
                probe.lay_lines(Some(ctx))
            }
            None if definite_width => {
                let mut local_bfc = BlockFormattingContext::new();
                let mut ctx = local_bfc.root_block_context();
                ctx.set_width(cb_width.unwrap_or(0.0) + padding_border.horizontal_axis_sum());
                ctx.apply_content_box_inset([padding_border.left, padding_border.right]);
                probe.lay_lines(Some(&mut ctx))
            }
            None => probe.lay_lines(None),
        };
        let InlineLines { measured, baseline, last_baseline, placements, placed_floats } = lines;

        // -- position the children --
        if perform {
            for (k, x, y) in &placements {
                if let Some(atomic) = atomics.get(*k) {
                    let layout = atomic.layout(*k as u32, inset_left + x, inset_top + y);
                    self.set_unrounded_layout(atomic.id, &layout);
                }
            }
            for (k, x, y) in &placed_floats {
                if let Some(atomic) = atomics.get(*k) {
                    let layout = atomic.layout(
                        *k as u32,
                        inset_left + x + atomic.margin.left,
                        inset_top + y + atomic.margin.top,
                    );
                    self.set_unrounded_layout(atomic.id, &layout);
                }
            }
        }

        // -- this node's size --
        own.finish(&style, measured, baseline, last_baseline)
    }
}

/// A custom node's own box, resolved from its style and layout inputs
/// exactly as Taffy's leaf algorithm does: its size when the style or the
/// parent fixes it, its min/max clamps, its padding and border, and the
/// space its content is laid out in.
struct OwnBox {
    known_dimensions: Size<Option<f32>>,
    node_size: Size<Option<f32>>,
    node_min_size: Size<Option<f32>>,
    node_max_size: Size<Option<f32>>,
    padding: Rect<f32>,
    padding_border: Rect<f32>,
    prevents_collapse_through: bool,
    content_available: Size<AvailableSpace>,
}

impl OwnBox {
    fn resolve(style: &Style, inputs: &LayoutInput) -> OwnBox {
        let LayoutInput { known_dimensions, parent_size, available_space, sizing_mode, .. } = *inputs;
        let calc = |_: *const (), _: f32| 0.0;
        let margin = style.margin.resolve_or_zero(parent_size.width, calc);
        let padding = style.padding.resolve_or_zero(parent_size.width, calc);
        let border = style.border.resolve_or_zero(parent_size.width, calc);
        let padding_border = padding + border;
        let pb_sum = padding_border.sum_axes();
        let box_sizing_adjustment = if style.box_sizing == BoxSizing::ContentBox { pb_sum } else { Size::ZERO };
        let (node_size, node_min_size, node_max_size) = match sizing_mode {
            SizingMode::ContentSize => (known_dimensions, Size::NONE, Size::NONE),
            SizingMode::InherentSize => {
                let aspect_ratio = style.aspect_ratio;
                let style_size = style
                    .size
                    .maybe_resolve(parent_size, calc)
                    .maybe_apply_aspect_ratio(aspect_ratio)
                    .maybe_add(box_sizing_adjustment);
                let style_min = style
                    .min_size
                    .maybe_resolve(parent_size, calc)
                    .maybe_apply_aspect_ratio(aspect_ratio)
                    .maybe_add(box_sizing_adjustment);
                let style_max = style.max_size.maybe_resolve(parent_size, calc).maybe_add(box_sizing_adjustment);
                (known_dimensions.or(style_size), style_min, style_max)
            }
        };
        let prevents_collapse_through = style.overflow.x.is_scroll_container()
            || style.overflow.y.is_scroll_container()
            || style.position == Position::Absolute
            || style.contain.establishes_independent_formatting_context()
            || padding.top > 0.0
            || padding.bottom > 0.0
            || border.top > 0.0
            || border.bottom > 0.0
            || matches!(node_size.height, Some(h) if h > 0.0)
            || matches!(node_min_size.height, Some(h) if h > 0.0);
        let content_available = Size {
            width: known_dimensions
                .width
                .map(AvailableSpace::from)
                .unwrap_or(available_space.width)
                .maybe_sub(margin.horizontal_axis_sum())
                .maybe_set(known_dimensions.width)
                .maybe_set(node_size.width)
                .map_definite_value(|size| {
                    size.maybe_clamp(node_min_size.width, node_max_size.width) - padding_border.horizontal_axis_sum()
                }),
            height: known_dimensions
                .height
                .map(AvailableSpace::from)
                .unwrap_or(available_space.height)
                .maybe_sub(margin.vertical_axis_sum())
                .maybe_set(known_dimensions.height)
                .maybe_set(node_size.height)
                .map_definite_value(|size| {
                    size.maybe_clamp(node_min_size.height, node_max_size.height) - padding_border.vertical_axis_sum()
                }),
        };
        OwnBox {
            known_dimensions, node_size, node_min_size, node_max_size, padding, padding_border,
            prevents_collapse_through, content_available,
        }
    }

    /// Taffy's early answer for a size-only request whose size is fixed.
    fn short_circuit(&self, run_mode: RunMode) -> Option<LayoutOutput> {
        if run_mode == RunMode::ComputeSize && self.prevents_collapse_through {
            if let Size { width: Some(width), height: Some(height) } = self.node_size {
                let size = Size { width, height }
                    .maybe_clamp(self.node_min_size, self.node_max_size)
                    .maybe_max(self.padding_border.sum_axes().map(Some));
                return Some(LayoutOutput::from_outer_size(size));
            }
        }
        None
    }

    /// The node's output for content of `measured` size with the given
    /// content-box-relative baselines.
    fn finish(&self, style: &Style, measured: Size<f32>, first: Option<f32>, last: Option<f32>) -> LayoutOutput {
        let clamped = self
            .known_dimensions
            .or(self.node_size)
            .unwrap_or(measured + self.padding_border.sum_axes())
            .maybe_clamp(self.node_min_size, self.node_max_size);
        let size = Size {
            width: clamped.width,
            height: clamped.height.max(style.aspect_ratio.map(|ratio| clamped.width / ratio).unwrap_or(0.0)),
        }
        .maybe_max(self.padding_border.sum_axes().map(Some));
        let top = self.padding_border.top;
        let mut output = LayoutOutput::from_sizes_and_baselines(
            size,
            Rect {
                left: 0.0,
                right: self.padding.left + measured.width,
                top: 0.0,
                bottom: self.padding.top + measured.height,
            },
            Baselines { first: first.map(|b| b + top), last: last.map(|b| b + top) },
        );
        output.margins_can_collapse_through =
            !self.prevents_collapse_through && size.height == 0.0 && measured.height == 0.0;
        output
    }
}

/// The inputs one inline-formatting-context measure hands the Python plan.
struct InlineProbe<'a> {
    py: Python<'a>,
    measure: &'a Py<PyAny>,
    atomics: &'a [AtomicChild],
    inset_left: f32,
    inset_top: f32,
    content_width: f32,
    width_arg: Option<f32>,
    height_arg: Option<f32>,
    known: Size<Option<f32>>,
    owns_bfc: bool,
    definite_width: bool,
}

/// What the plan decided: content size, first baseline, each non-float
/// atomic's border-box position and each float's margin-box position, all
/// content-box relative.
struct InlineLines {
    measured: Size<f32>,
    baseline: Option<f32>,
    last_baseline: Option<f32>,
    placements: Vec<(usize, f32, f32)>,
    placed_floats: Vec<(usize, f32, f32)>,
}

impl InlineProbe<'_> {
    fn lay_lines(&self, mut ctx: Option<&mut BlockContext<'_>>) -> InlineLines {
        let py = self.py;
        let atomics_py: Vec<(usize, f32, f32, Option<f32>, f32, f32, f32, f32, &str)> = self
            .atomics
            .iter()
            .enumerate()
            .map(|(k, a)| {
                (
                    k, a.size.width, a.size.height, a.baseline,
                    a.margin.top, a.margin.right, a.margin.bottom, a.margin.left,
                    match a.float { Float::Left => "left", Float::Right => "right", Float::None => "" },
                )
            })
            .collect();
        let mut lines = InlineLines {
            measured: Size::ZERO,
            baseline: None,
            last_baseline: None,
            placements: Vec::new(),
            placed_floats: Vec::new(),
        };
        for _ in 0..(self.atomics.len() + 2) {
            let bands: Option<Vec<(f32, f32, f32, f32)>> = match (ctx.as_deref(), self.definite_width) {
                (Some(ctx), true) => Some(content_bands(ctx, self.inset_left, self.inset_top, self.content_width)),
                _ => None,
            };
            let args = (
                self.width_arg, self.height_arg, self.known.width, self.known.height,
                atomics_py.clone(), bands, lines.placed_floats.clone(), self.owns_bfc,
            );
            let Ok(result) = self.measure.call1(py, args) else { break };
            let result = result.bind(py);
            if let Ok((tag, k, y)) = result.extract::<(String, usize, f32)>() {
                if tag == "float" && k < self.atomics.len() {
                    let atomic = &self.atomics[k];
                    let placed = match (ctx.as_deref_mut(), atomic.float.float_direction()) {
                        (Some(ctx), Some(direction)) => {
                            let margin_box = atomic.size + atomic.margin.sum_axes();
                            let pos = ctx.place_floated_box(
                                margin_box, y + self.inset_top, direction, atomic.clear, false,
                            );
                            (k, pos.x - self.inset_left, pos.y - self.inset_top)
                        }
                        _ => (k, 0.0, y),
                    };
                    lines.placed_floats.push(placed);
                    continue;
                }
                break;
            }
            if let Ok((w, h, first, last, p)) =
                result.extract::<(f32, f32, Option<f32>, Option<f32>, Vec<(usize, f32, f32)>)>()
            {
                lines.measured = Size { width: w, height: h };
                lines.baseline = first;
                lines.last_baseline = last;
                lines.placements = p;
            }
            break;
        }
        lines
    }
}

/// One atomic inline-level child of an inline formatting context, sized by
/// its own formatting context before the lines are laid.
struct AtomicChild {
    id: NodeId,
    size: Size<f32>,
    baseline: Option<f32>,
    margin: Rect<f32>,
    padding: Rect<f32>,
    border: Rect<f32>,
    float: Float,
    clear: Clear,
}

impl AtomicChild {
    fn layout(&self, order: u32, x: f32, y: f32) -> Layout {
        let mut layout = Layout::with_order(order);
        layout.location = Point { x, y };
        layout.size = self.size;
        layout.border = self.border;
        layout.padding = self.padding;
        layout.margin = self.margin;
        layout
    }
}

/// The float-free horizontal space of an inline formatting context, band
/// by band down the page: `(top, bottom, left, width)` in the node's
/// content-box coordinates, the last band open-ended. A content slot only
/// says where it starts, so each band's bottom is the next float segment's
/// top (walked with the slot's `after` cursor), the last one ending where
/// every float does (`cleared_threshold(Both)`).
fn content_bands(ctx: &BlockContext<'_>, inset_left: f32, inset_top: f32, content_width: f32) -> Vec<(f32, f32, f32, f32)> {
    let mut bands = Vec::new();
    let floats_bottom = ctx.cleared_threshold(Clear::Both).map(|bottom| bottom - inset_top);
    let mut y = inset_top;
    let mut after: Option<usize> = None;
    for _ in 0..512 {
        let slot = ctx.find_content_slot(y, Clear::None, after);
        let top = (slot.y - inset_top).max(bands.last().map(|band: &(f32, f32, f32, f32)| band.1).unwrap_or(0.0));
        let left = (slot.x - inset_left).max(0.0);
        let right = ((slot.x + slot.width) - inset_left).min(content_width);
        let width = (right - left).max(0.0);
        let Some(segment_id) = slot.segment_id else {
            bands.push((top, f32::INFINITY, left, width));
            break;
        };
        let next = ctx.find_content_slot(slot.y, Clear::None, Some(segment_id));
        let bottom = match next.segment_id {
            Some(_) => next.y - inset_top,
            None => floats_bottom.unwrap_or(f32::INFINITY),
        }
        .max(top);
        bands.push((top, bottom, left, width));
        if !bottom.is_finite() {
            break;
        }
        y = bottom + inset_top;
        after = Some(segment_id);
    }
    bands
}

/// Call the Python measure callback. Returns the measured content size and
/// the first baseline (content-box relative) when the callback reports one.
fn measure_via_python(
    py: Python<'_>,
    known_dimensions: taffy::geometry::Size<Option<f32>>,
    available_space: taffy::geometry::Size<AvailableSpace>,
    callback: Option<&Py<PyAny>>,
) -> (taffy::geometry::Size<f32>, Baselines) {
    if let (Some(w), Some(h)) = (known_dimensions.width, known_dimensions.height) {
        return (taffy::geometry::Size { width: w, height: h }, Baselines::NONE);
    }
    let Some(callback) = callback else {
        return (taffy::geometry::Size::ZERO, Baselines::NONE);
    };
    // Width: a `MinContent` request (Taffy sizing a flex item's automatic
    // minimum, `min-width: auto`, or a `min-content` track) is passed as
    // the sentinel `-1.0` so the Python measure can wrap at every break
    // opportunity and report its widest unbreakable piece; `MaxContent`
    // stays `None` (lay out unconstrained). Both used to collapse to
    // `None`, so every text item's minimum was its max-content width and
    // never shrank (`css-flexbox/flex-minimum-width-flex-items-001.xht`:
    // a 50px-Ahem "IT E" item in a 10px container measured 200px, never
    // Chrome's 100px two-line minimum).
    let width_arg = known_dimensions.width.or(match available_space.width {
        AvailableSpace::Definite(v) => Some(v),
        AvailableSpace::MinContent => Some(-1.0),
        AvailableSpace::MaxContent => None,
    });
    let height_arg = known_dimensions.height.or(match available_space.height {
        AvailableSpace::Definite(v) => Some(v),
        _ => None,
    });
    // The known (already-resolved) dimensions are passed separately as a
    // third and fourth argument: a text leaf wraps to whichever width it
    // gets, but a replaced leaf (an `<img>` flex item) must only adopt a
    // *known* size, never the available space it is merely offered.
    let result = callback.call1(py, (width_arg, height_arg, known_dimensions.width, known_dimensions.height));
    let Ok(result) = result else {
        return (taffy::geometry::Size::ZERO, Baselines::NONE);
    };
    let (w, h, baselines) = if let Ok((w, h, first, last, _placements)) =
        result.extract::<(f32, f32, Option<f32>, Option<f32>, Vec<(usize, f32, f32)>)>(py)
    {
        (w, h, Baselines { first, last })
    } else if let Ok((w, h, first, last)) = result.extract::<(f32, f32, Option<f32>, Option<f32>)>(py) {
        (w, h, Baselines { first, last })
    } else if let Ok((w, h, first)) = result.extract::<(f32, f32, Option<f32>)>(py) {
        (w, h, Baselines::from_first(first))
    } else if let Ok((w, h)) = result.extract::<(f32, f32)>(py) {
        (w, h, Baselines::NONE)
    } else {
        return (taffy::geometry::Size::ZERO, Baselines::NONE);
    };
    (
        taffy::geometry::Size {
            width: known_dimensions.width.unwrap_or(w),
            height: known_dimensions.height.unwrap_or(h),
        },
        baselines,
    )
}

// -- Parley: real text layout ------------------------------------------------

/// Register an in-memory web font in this thread's layout context. The private
/// family alias also identifies the identical bytes in the Skia registry.
#[pyfunction]
#[pyo3(signature = (data, family, weight=400.0, italic=false))]
fn register_font(data: Vec<u8>, family: &str, weight: f32, italic: bool) -> PyResult<()> {
    TEXT_FONT_CX.with(|cell| {
        let mut cx = cell.borrow_mut();
        let fonts = cx.collection.register_fonts(
            parley::fontique::Blob::new(std::sync::Arc::new(data)),
            Some(parley::fontique::FontInfoOverride {
                family_name: Some(family),
                weight: Some(ParleyFontWeight::new(weight)),
                style: Some(if italic { ParleyFontStyle::Italic } else { ParleyFontStyle::Normal }),
                ..Default::default()
            }),
        );
        if fonts.is_empty() {
            return Err(PyValueError::new_err("invalid web font"));
        }
        Ok(())
    })
}
//
// `FontContext` (a font database) and `LayoutContext` (scratch space) are
// meant to be constructed rarely and reused -- Parley's own docs say
// "perhaps even once per app". `thread_local!` matches how `Tree` is already
// `#[pyclass(unsendable)]`: nothing here ever crosses a Python thread.
thread_local! {
    static TEXT_FONT_CX: RefCell<FontContext> = RefCell::new(FontContext::new());
    static TEXT_LAYOUT_CX: RefCell<LayoutContext<()>> = RefCell::new(LayoutContext::new());
}

/// Real text layout via Parley -- font matching (`fontique`), shaping, and
/// Unicode line-breaking -- replacing `domonic._fontmetrics`'s one
/// hardcoded Helvetica-shaped advance-width table. `font_family` is a raw
/// CSS `font-family` value (`"Georgia, 'Times New Roman', serif"`); Parley
/// parses and resolves it directly, generic keywords included, needing no
/// translation on the Python side. `max_width=None` means unconstrained
/// (a single, unwrapped line, same "don't wrap" case `tree.py`'s own
/// pre-Parley `_wrap_lines` had for an unconstrained Taffy measure call).
///
/// Returns `(total_width, total_height, [(line_text, line_width,
/// line_height), ...])` -- deliberately *not* glyph-level output: painting
/// still goes through `skia-python`, which does its own font resolution
/// and shaping (see `chromonic.fonts`) -- there is no shared glyph-ID space
/// between Parley's font backend (`fontique`) and Skia's to hand positioned
/// glyphs across that boundary yet. Parley here supplies the *line-breaking
/// and metrics decisions* (real Unicode line-breaking, real per-font
/// metrics from whatever font actually matched); each returned line's text
/// is still handed to Skia to shape and draw on its own, same as before.
#[pyfunction]
#[pyo3(signature = (
    text, font_family, font_size, font_weight=400.0, italic=false, max_width=None,
    letter_spacing=0.0, word_spacing=0.0, line_height=None,
    word_break="normal", overflow_wrap="normal"
))]
#[allow(clippy::too_many_arguments)]
fn layout_text(
    text: &str,
    font_family: &str,
    font_size: f32,
    font_weight: f32,
    italic: bool,
    max_width: Option<f32>,
    letter_spacing: f32,
    word_spacing: f32,
    line_height: Option<f32>,
    word_break: &str,
    overflow_wrap: &str,
) -> PyResult<(f32, f32, Vec<(String, f32, f32)>)> {
    // CSS Text 3 `word-break`/`overflow-wrap` (the latter's legacy alias
    // `word-wrap` resolves to the same values before reaching here --
    // `style_bridge.py`'s job, not this binding's) map directly onto
    // Parley's own `WordBreak`/`OverflowWrap` style properties, which
    // already implement the real semantics (line_break.rs's own
    // Unicode-aware break-opportunity walk, not reimplemented here).
    // An unrecognized value (a typo, or a keyword this CSS level doesn't
    // define) falls back to `Normal`, matching how an invalid CSS
    // declaration is simply never applied at all.
    let word_break = match word_break {
        "break-all" => ParleyWordBreak::BreakAll,
        "keep-all" => ParleyWordBreak::KeepAll,
        _ => ParleyWordBreak::Normal,
    };
    let overflow_wrap = match overflow_wrap {
        "anywhere" => ParleyOverflowWrap::Anywhere,
        "break-word" => ParleyOverflowWrap::BreakWord,
        _ => ParleyOverflowWrap::Normal,
    };
    TEXT_FONT_CX.with(|font_cx_cell| {
        TEXT_LAYOUT_CX.with(|layout_cx_cell| {
            let mut font_cx = font_cx_cell.borrow_mut();
            let mut layout_cx = layout_cx_cell.borrow_mut();
            let mut builder = layout_cx.ranged_builder(&mut font_cx, text, 1.0, true);
            builder.push_default(StyleProperty::FontFamily(font_family.into()));
            builder.push_default(StyleProperty::FontSize(font_size));
            builder.push_default(StyleProperty::FontWeight(ParleyFontWeight::new(font_weight)));
            if italic {
                builder.push_default(StyleProperty::FontStyle(ParleyFontStyle::Italic));
            }
            if letter_spacing != 0.0 {
                builder.push_default(StyleProperty::LetterSpacing(letter_spacing));
            }
            if word_spacing != 0.0 {
                builder.push_default(StyleProperty::WordSpacing(word_spacing));
            }
            if let Some(lh) = line_height {
                builder.push_default(StyleProperty::LineHeight(ParleyLineHeight::Absolute(lh)));
            }
            builder.push_default(StyleProperty::WordBreak(word_break));
            builder.push_default(StyleProperty::OverflowWrap(overflow_wrap));
            let mut layout: parley::Layout<()> = builder.build(text);
            layout.break_all_lines(max_width);
            layout.align(Alignment::Start, AlignmentOptions::default());

            let height = layout.height();
            let mut width: f32 = 0.0;
            let mut lines = Vec::new();
            for line in layout.lines() {
                let range = line.text_range();
                let line_text = text.get(range).unwrap_or("").to_string();
                let metrics = line.metrics();
                let mut line_width = metrics.advance;
                // Parley trims a line's *measured* width down to its last
                // non-whitespace glyph (any Unicode whitespace, matching how
                // most text-layout engines treat trailing whitespace for
                // alignment purposes) -- but CSS 2.1 16.6.1 only ever
                // collapses/trims plain ASCII space/tab/newline/CR/FF;
                // U+00A0 (`&nbsp;`) always renders as a real glyph and must
                // never be trimmed from the box's width. `line_text` itself
                // already includes the trailing NBSPs (`text_range()` spans
                // the whole line's source text regardless), so only the
                // numeric width needs correcting: re-measure the trailing
                // NBSP run on its own and add it back. Found on
                // `wpt/css/CSS2/positioning/abspos-011.xht`:
                // `<p>FAIL&nbsp;&nbsp;&nbsp;&nbsp;</p>` measured 4
                // characters wide instead of 9.
                let trailing_nbsp: String =
                    line_text.chars().rev().take_while(|&c| c == '\u{00A0}').collect();
                if !trailing_nbsp.is_empty() {
                    let mut run_builder =
                        layout_cx.ranged_builder(&mut font_cx, &trailing_nbsp, 1.0, true);
                    run_builder.push_default(StyleProperty::FontFamily(font_family.into()));
                    run_builder.push_default(StyleProperty::FontSize(font_size));
                    run_builder
                        .push_default(StyleProperty::FontWeight(ParleyFontWeight::new(font_weight)));
                    if italic {
                        run_builder.push_default(StyleProperty::FontStyle(ParleyFontStyle::Italic));
                    }
                    if letter_spacing != 0.0 {
                        run_builder.push_default(StyleProperty::LetterSpacing(letter_spacing));
                    }
                    if word_spacing != 0.0 {
                        run_builder.push_default(StyleProperty::WordSpacing(word_spacing));
                    }
                    let mut run_layout: parley::Layout<()> = run_builder.build(&trailing_nbsp);
                    run_layout.break_all_lines(None);
                    line_width += run_layout.width();
                }
                width = width.max(line_width);
                lines.push((line_text, line_width, metrics.line_height));
            }
            Ok((width, height, lines))
        })
    })
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Tree>()?;
    m.add_function(wrap_pyfunction!(layout_text, m)?)?;
    m.add_function(wrap_pyfunction!(register_font, m)?)?;
    Ok(())
}
