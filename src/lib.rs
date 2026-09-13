//! chromonic_native -- a thin PyO3 binding onto the Taffy layout engine, plus
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
use taffy::{compute_leaf_layout, AlignContent, AlignItems, TaffyError};

use parley::{
    Alignment, AlignmentOptions, FontContext, FontStyle as ParleyFontStyle,
    FontWeight as ParleyFontWeight, LayoutContext, LineHeight as ParleyLineHeight, StyleProperty,
};
use std::cell::RefCell;

// -- Python value -> Taffy value ---------------------------------------------

/// A length-like Python value: `float` (px), `("pct", frac)`, `("fr", n)`,
/// or the string `"auto"`.
enum RawLen {
    Px(f32),
    Pct(f32),
    Fr(f32),
    Auto,
}

fn read_raw_len(value: &Bound<PyAny>) -> PyResult<RawLen> {
    if let Ok(text) = value.extract::<String>() {
        if text == "auto" {
            return Ok(RawLen::Auto);
        }
        return Err(PyValueError::new_err(format!("unrecognised length keyword: {text:?}")));
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
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is only valid for grid track sizes")),
    })
}

fn length_percentage(value: &Bound<PyAny>) -> PyResult<LengthPercentage> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Auto => LengthPercentage::ZERO, // padding/border/gap have no "auto"
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is not valid here")),
    })
}

fn length_percentage_auto(value: &Bound<PyAny>) -> PyResult<LengthPercentageAuto> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Auto => auto(),
        RawLen::Fr(_) => return Err(PyValueError::new_err("fr is not valid here")),
    })
}

fn track_sizing_function(value: &Bound<PyAny>) -> PyResult<TrackSizingFunction> {
    Ok(match read_raw_len(value)? {
        RawLen::Px(px) => length(px),
        RawLen::Pct(frac) => percent(frac),
        RawLen::Fr(n) => fr(n),
        RawLen::Auto => auto(),
    })
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

fn get_f32(dict: &Bound<PyDict>, key: &str, default: f32) -> PyResult<f32> {
    match get(dict, key) {
        Some(v) => v.extract::<f32>(),
        None => Ok(default),
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
                let n: i16 = item.extract()?;
                Ok(line(n))
            };
            Ok(Line { start: one(tuple.get_item(0)?)?, end: one(tuple.get_item(1)?)? })
        }
    }
}

fn parse_style(dict: &Bound<PyDict>) -> PyResult<Style> {
    let display = match get_str(dict, "display", "block")?.as_str() {
        "block" => Display::Block,
        "flex" => Display::Flex,
        "grid" => Display::Grid,
        "none" => Display::None,
        other => return Err(PyValueError::new_err(format!("unrecognised display: {other:?}"))),
    };
    let position = match get_str(dict, "position", "relative")?.as_str() {
        "relative" => Position::Relative,
        "absolute" => Position::Absolute,
        other => return Err(PyValueError::new_err(format!("unrecognised position: {other:?}"))),
    };
    let box_sizing = match get_str(dict, "box_sizing", "border-box")?.as_str() {
        "border-box" => BoxSizing::BorderBox,
        "content-box" => BoxSizing::ContentBox,
        other => return Err(PyValueError::new_err(format!("unrecognised box-sizing: {other:?}"))),
    };
    let flex_direction = match get_str(dict, "flex_direction", "row")?.as_str() {
        "row" => FlexDirection::Row,
        "column" => FlexDirection::Column,
        "row-reverse" => FlexDirection::RowReverse,
        "column-reverse" => FlexDirection::ColumnReverse,
        other => return Err(PyValueError::new_err(format!("unrecognised flex-direction: {other:?}"))),
    };
    let flex_wrap = match get_str(dict, "flex_wrap", "nowrap")?.as_str() {
        "nowrap" => FlexWrap::NoWrap,
        "wrap" => FlexWrap::Wrap,
        "wrap-reverse" => FlexWrap::WrapReverse,
        other => return Err(PyValueError::new_err(format!("unrecognised flex-wrap: {other:?}"))),
    };
    let grid_auto_flow = match get_str(dict, "grid_auto_flow", "row")?.as_str() {
        "row" => GridAutoFlow::Row,
        "column" => GridAutoFlow::Column,
        "row-dense" => GridAutoFlow::RowDense,
        "column-dense" => GridAutoFlow::ColumnDense,
        other => return Err(PyValueError::new_err(format!("unrecognised grid-auto-flow: {other:?}"))),
    };

    let grid_template_columns = match get(dict, "grid_template_columns") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| track_sizing_function(&item).map(Into::into)).collect::<PyResult<Vec<_>>>()?,
    };
    let grid_template_rows = match get(dict, "grid_template_rows") {
        None => vec![],
        Some(v) => v.cast::<PyList>()?.iter().map(|item| track_sizing_function(&item).map(Into::into)).collect::<PyResult<Vec<_>>>()?,
    };

    Ok(Style {
        display,
        position,
        box_sizing,
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
        grid_auto_flow,
        grid_template_columns,
        grid_template_rows,
        grid_column: parse_grid_placement(get(dict, "grid_column"))?,
        grid_row: parse_grid_placement(get(dict, "grid_row"))?,
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

type MeasureCallback = Option<Py<PyAny>>;

// `taffy::Style` carries a raw pointer for calc()-expression handles (a
// feature we don't use), which makes `TaffyTree` not automatically Send/Sync.
// `unsendable` is PyO3's standard escape hatch for a Rust type that a Python
// extension only ever touches from the thread that created it -- true here,
// there's no threading in this POC.
#[pyclass(unsendable)]
struct Tree {
    inner: TaffyTree<MeasureCallback>,
}

#[pymethods]
impl Tree {
    #[new]
    fn new() -> Self {
        let mut inner = TaffyTree::new();
        // Browsers retain CSS subpixel geometry and only rasterize at paint.
        // Taffy's default rounding loses half-pixel collapsed borders and
        // fractional grid tracks before geometry reaches the DOM APIs.
        inner.disable_rounding();
        Tree { inner }
    }

    /// A leaf node with no measure callback (a plain box: nothing to
    /// intrinsically size, e.g. an empty `<div>`).
    fn new_leaf(&mut self, style: &Bound<PyDict>) -> PyResult<u64> {
        let style = parse_style(style)?;
        let id = self.inner.new_leaf_with_context(style, None).map_err(to_py_err)?;
        Ok(id.into())
    }

    /// A text leaf: `measure` is called during layout as
    /// `measure(available_width, available_height) -> (width, height)`,
    /// both `float | None` in, both `float` out.
    fn new_text_leaf(&mut self, style: &Bound<PyDict>, measure: Py<PyAny>) -> PyResult<u64> {
        let style = parse_style(style)?;
        let id = self.inner.new_leaf_with_context(style, Some(measure)).map_err(to_py_err)?;
        Ok(id.into())
    }

    fn new_with_children(&mut self, style: &Bound<PyDict>, children: Vec<u64>) -> PyResult<u64> {
        let style = parse_style(style)?;
        let child_ids: Vec<NodeId> = children.into_iter().map(NodeId::from).collect();
        let id = self.inner.new_with_children(style, &child_ids).map_err(to_py_err)?;
        Ok(id.into())
    }

    fn set_style(&mut self, node: u64, style: &Bound<PyDict>) -> PyResult<()> {
        let style = parse_style(style)?;
        self.inner.set_style(NodeId::from(node), style).map_err(to_py_err)
    }

    /// Update retained-node insets in one Python crossing. Animation paths
    /// use this when every other style field and the tree topology are stable.
    fn set_insets(&mut self, updates: Vec<(u64, f32, f32, f32, f32)>) -> PyResult<()> {
        for (node, top, right, bottom, left) in updates {
            let node = NodeId::from(node);
            let mut style = self.inner.style(node).map_err(to_py_err)?.clone();
            style.inset = Rect {
                top: length(top),
                right: length(right),
                bottom: length(bottom),
                left: length(left),
            };
            self.inner.set_style(node, style).map_err(to_py_err)?;
        }
        Ok(())
    }

    fn set_children(&mut self, node: u64, children: Vec<u64>) -> PyResult<()> {
        let child_ids: Vec<NodeId> = children.into_iter().map(NodeId::from).collect();
        self.inner.set_children(NodeId::from(node), &child_ids).map_err(to_py_err)
    }

    fn set_measure(&mut self, node: u64, measure: Option<Py<PyAny>>) -> PyResult<()> {
        self.inner.set_node_context(NodeId::from(node), Some(measure)).map_err(to_py_err)
    }

    fn remove(&mut self, node: u64) -> PyResult<()> {
        self.inner.remove(NodeId::from(node)).map(|_| ()).map_err(to_py_err)
    }

    /// Run layout, then return `{node_id: (x, y, width, height, border_top,
    /// border_right, border_bottom, border_left, padding_top, padding_right,
    /// padding_bottom, padding_left)}` for every node, in **absolute**
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
        let root_id = NodeId::from(root);
        let available = Size {
            width: available_width.map(AvailableSpace::Definite).unwrap_or(AvailableSpace::MaxContent),
            height: available_height.map(AvailableSpace::Definite).unwrap_or(AvailableSpace::MaxContent),
        };

        self.inner
            .compute_layout_with_measure(root_id, available, |inputs, _node_id, node_context, style| {
                compute_leaf_layout(inputs, style, |_, _| 0.0, |known_dimensions, available_space| {
                    measure_via_python(py, known_dimensions, available_space, node_context.and_then(|c| c.as_ref()))
                })
            })
            .map_err(to_py_err)?;

        let out = PyDict::new(py);
        self.collect_absolute(py, root_id, 0.0, 0.0, &out)?;
        Ok(out.into())
    }
}

impl Tree {
    fn collect_absolute(&self, py: Python<'_>, node: NodeId, parent_x: f32, parent_y: f32, out: &Bound<PyDict>) -> PyResult<()> {
        let layout = self.inner.layout(node).map_err(to_py_err)?;
        let x = parent_x + layout.location.x;
        let y = parent_y + layout.location.y;
        let tuple = (
            x, y, layout.size.width, layout.size.height,
            layout.border.top, layout.border.right, layout.border.bottom, layout.border.left,
            layout.padding.top, layout.padding.right, layout.padding.bottom, layout.padding.left,
        );
        out.set_item(u64::from(node), tuple)?;
        let children: Vec<NodeId> = self.inner.child_ids(node).collect();
        for child in children {
            self.collect_absolute(py, child, x, y, out)?;
        }
        let _ = py;
        Ok(())
    }
}

fn measure_via_python(
    py: Python<'_>,
    known_dimensions: taffy::geometry::Size<Option<f32>>,
    available_space: taffy::geometry::Size<AvailableSpace>,
    callback: Option<&Py<PyAny>>,
) -> taffy::geometry::Size<f32> {
    if let (Some(w), Some(h)) = (known_dimensions.width, known_dimensions.height) {
        return taffy::geometry::Size { width: w, height: h };
    }
    let Some(callback) = callback else {
        return taffy::geometry::Size::ZERO;
    };
    let as_opt = |s: AvailableSpace| -> Option<f32> {
        match s {
            AvailableSpace::Definite(v) => Some(v),
            _ => None,
        }
    };
    let result = callback.call1(py, (known_dimensions.width.or(as_opt(available_space.width)), known_dimensions.height.or(as_opt(available_space.height))));
    match result.and_then(|r| r.extract::<(f32, f32)>(py)) {
        Ok((w, h)) => taffy::geometry::Size {
            width: known_dimensions.width.unwrap_or(w),
            height: known_dimensions.height.unwrap_or(h),
        },
        Err(_) => taffy::geometry::Size::ZERO,
    }
}

fn to_py_err(err: TaffyError) -> PyErr {
    PyRuntimeError::new_err(err.to_string())
}

// -- Parley: real text layout ------------------------------------------------
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
    letter_spacing=0.0, word_spacing=0.0, line_height=None
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
) -> PyResult<(f32, f32, Vec<(String, f32, f32)>)> {
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
            let mut layout: parley::Layout<()> = builder.build(text);
            layout.break_all_lines(max_width);
            layout.align(Alignment::Start, AlignmentOptions::default());

            let width = layout.width();
            let height = layout.height();
            let mut lines = Vec::new();
            for line in layout.lines() {
                let range = line.text_range();
                let line_text = text.get(range).unwrap_or("").to_string();
                let metrics = line.metrics();
                lines.push((line_text, metrics.advance, metrics.line_height));
            }
            Ok((width, height, lines))
        })
    })
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Tree>()?;
    m.add_function(wrap_pyfunction!(layout_text, m)?)?;
    Ok(())
}
