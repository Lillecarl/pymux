//! Rust kernels for prompt_toolkit's per-cell loops.
//! `python/prompt_toolkit_rs/__init__.py` says how they are put in place;
//! each one gives the answer its Python original gives, decision for
//! decision. Lillecarl/pymux#566.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyStringData};

/// The code point at `index` of a string's own storage, which is what
/// Python's `text[index]` reads: a lone surrogate is a code point too.
fn point_at(data: &PyStringData<'_>, index: usize) -> Option<u32> {
    match data {
        PyStringData::Ucs1(units) => units.get(index).map(|&unit| u32::from(unit)),
        PyStringData::Ucs2(units) => units.get(index).map(|&unit| u32::from(unit)),
        PyStringData::Ucs4(units) => units.get(index).copied(),
    }
}

/// `text[index]` as a key, without a call into Python unless the code
/// point is a surrogate, which no Rust `char` can hold.
fn key_at<'py>(
    py: Python<'py>,
    text: &Bound<'py, PyString>,
    index: usize,
    point: u32,
    buffer: &mut [u8; 4],
) -> PyResult<Bound<'py, PyString>> {
    match char::from_u32(point) {
        Some(character) => Ok(PyString::new(py, character.encode_utf8(buffer))),
        None => Ok(text.get_item(index)?.cast_into::<PyString>()?),
    }
}

/// `prompt_toolkit.layout.containers._copy_single_width`: copy the common
/// run of `text` from `index` into `row` from column `x`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn copy_single_width<'py>(
    py: Python<'py>,
    text: &Bound<'py, PyString>,
    mut index: usize,
    known: &Bound<'py, PyDict>,
    row: &Bound<'py, PyDict>,
    mut x: i64,
    offset: i64,
    width: i64,
    collected: Option<&Bound<'py, PyList>>,
) -> PyResult<(usize, i64)> {
    // SAFETY: `text` is borrowed for the whole call, and a str is
    // immutable once made, so its storage stays where it is.
    let data = unsafe { text.data()? };
    let width_name = intern!(py, "width");
    let mut buffer = [0u8; 4];
    while x < width {
        let Some(point) = point_at(&data, index) else {
            break;
        };
        let key = key_at(py, text, index, point, &mut buffer)?;
        let Some(char) = known.get_item(&key)? else {
            break;
        };
        let char_width: i64 = char.getattr(width_name)?.extract()?;
        if char_width != 1 {
            break;
        }
        row.set_item(x + offset, &char)?;
        if let Some(collected) = collected {
            collected.append(&char)?;
        }
        x += 1;
        index += 1;
    }
    Ok((index, x))
}

/// `row[column]` the way Python reads it: a defaultdict stores its
/// default for a column it does not hold.
fn read<'py>(row: &Bound<'py, PyDict>, column: i64) -> PyResult<Bound<'py, PyAny>> {
    match row.get_item(column)? {
        Some(cell) => Ok(cell),
        None => row.as_any().get_item(column),
    }
}

/// `prompt_toolkit.renderer._changed_spans`: the maximal runs of changed
/// columns of a row, up to `last`.
#[pyfunction]
fn changed_spans<'py>(
    py: Python<'py>,
    new_row: &Bound<'py, PyDict>,
    previous_row: &Bound<'py, PyDict>,
    last: i64,
) -> PyResult<Bound<'py, PyList>> {
    let width_name = intern!(py, "width");
    let char_name = intern!(py, "char");
    let style_name = intern!(py, "style");
    let spans = PyList::empty(py);
    let mut span_start: Option<i64> = None;
    let mut column = 0;
    while column <= last {
        let new_char = read(new_row, column)?;
        let old_char = read(previous_row, column)?;
        let char_width = match new_char.getattr(width_name)?.extract::<i64>()? {
            0 => 1,
            width => width,
        };
        let changed = !new_char.is(&old_char)
            && (new_char
                .getattr(char_name)?
                .ne(old_char.getattr(char_name)?)?
                || new_char
                    .getattr(style_name)?
                    .ne(old_char.getattr(style_name)?)?);
        if changed {
            span_start.get_or_insert(column);
        } else if let Some(start) = span_start.take() {
            spans.append((start, column))?;
        }
        column += char_width;
    }
    if let Some(start) = span_start {
        spans.append((start, column))?;
    }
    Ok(spans)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(copy_single_width, module)?)?;
    module.add_function(wrap_pyfunction!(changed_spans, module)?)?;
    Ok(())
}
