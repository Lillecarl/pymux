//! Rust kernels for prompt_toolkit's per-cell loops.
//! `python/prompt_toolkit_rs/__init__.py` says how they are put in place;
//! each one gives the answer its Python original gives, decision for
//! decision. Lillecarl/pymux#566.

mod row;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyStringData};

use row::Row;

/// A row of a composed screen as the kernels reach it: a Rust `Row`, or
/// the defaultdict `prompt_toolkit.layout.screen._new_row` makes.
enum Cells<'a, 'py> {
    Rust(&'a Bound<'py, Row>),
    Dict(&'a Bound<'py, PyDict>),
}

impl<'a, 'py> Cells<'a, 'py> {
    fn of(row: &'a Bound<'py, PyAny>) -> PyResult<Self> {
        match row.cast::<Row>() {
            Ok(row) => Ok(Self::Rust(row)),
            Err(_) => Ok(Self::Dict(row.cast::<PyDict>()?)),
        }
    }

    /// `row[column]` the way Python reads it: a column nobody wrote
    /// takes the row's default, and keeps it.
    fn read(&self, column: i64) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Rust(row) => Ok(row.borrow_mut().read(row.py(), column).into_bound(row.py())),
            Self::Dict(row) => match row.get_item(column)? {
                Some(cell) => Ok(cell),
                None => row.as_any().get_item(column),
            },
        }
    }

    fn write(&self, column: i64, cell: &Bound<'py, PyAny>) -> PyResult<()> {
        match self {
            Self::Rust(row) => {
                row.borrow_mut().store(column, cell.clone().unbind());
                Ok(())
            }
            Self::Dict(row) => row.set_item(column, cell),
        }
    }
}

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
    row: &Bound<'py, PyAny>,
    mut x: i64,
    offset: i64,
    width: i64,
    collected: Option<&Bound<'py, PyList>>,
) -> PyResult<(usize, i64)> {
    // SAFETY: `text` is borrowed for the whole call, and a str is
    // immutable once made, so its storage stays where it is.
    let data = unsafe { text.data()? };
    let width_name = intern!(py, "width");
    let row = Cells::of(row)?;
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
        row.write(x + offset, &char)?;
        if let Some(collected) = collected {
            collected.append(&char)?;
        }
        x += 1;
        index += 1;
    }
    Ok((index, x))
}

/// `prompt_toolkit.layout.containers._store_line`: put `chars` into `row`,
/// one to a column, from column `start`.
#[pyfunction]
fn store_line(row: &Bound<'_, PyAny>, start: i64, chars: &Bound<'_, PyList>) -> PyResult<()> {
    match Cells::of(row)? {
        Cells::Rust(row) => row.borrow_mut().store_run(start, chars),
        Cells::Dict(row) => {
            for (column, cell) in (start..).zip(chars.iter()) {
                row.set_item(column, cell)?;
            }
        }
    }
    Ok(())
}

/// `prompt_toolkit.renderer._changed_spans`: the maximal runs of changed
/// columns of a row, up to `last`.
#[pyfunction]
fn changed_spans<'py>(
    py: Python<'py>,
    new_row: &Bound<'py, PyAny>,
    previous_row: &Bound<'py, PyAny>,
    last: i64,
) -> PyResult<Bound<'py, PyList>> {
    let width_name = intern!(py, "width");
    let char_name = intern!(py, "char");
    let style_name = intern!(py, "style");
    let new_row = Cells::of(new_row)?;
    let previous_row = Cells::of(previous_row)?;
    let spans = PyList::empty(py);
    let mut span_start: Option<i64> = None;
    let mut column = 0;
    while column <= last {
        let new_char = new_row.read(column)?;
        let old_char = previous_row.read(column)?;
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

/// `prompt_toolkit.renderer._moves_one_column_everywhere`: one code point
/// that every terminal draws in one cell.
fn moves_one_column_everywhere(text: &Bound<'_, PyString>) -> PyResult<bool> {
    // SAFETY: `text` is borrowed for the whole call, and a str is
    // immutable once made, so its storage stays where it is.
    let data = unsafe { text.data()? };
    let (Some(point), None) = (point_at(&data, 0), point_at(&data, 1)) else {
        return Ok(false);
    };
    Ok((0x20..=0x7e).contains(&point)
        || (0xa0..0x2300).contains(&point)
        || (0x2500..0x25a0).contains(&point))
}

/// `prompt_toolkit.renderer._queue_same_style`: queue the common run of a
/// changed span onto `pending`, and say where it stopped.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn queue_same_style<'py>(
    py: Python<'py>,
    row: &Bound<'py, PyAny>,
    mut c: i64,
    end: i64,
    last_column: i64,
    style: &Bound<'py, PyAny>,
    escapes_row: &Bound<'py, PyDict>,
    pending: &Bound<'py, PyList>,
) -> PyResult<i64> {
    let width_name = intern!(py, "width");
    let char_name = intern!(py, "char");
    let style_name = intern!(py, "style");
    let row = Cells::of(row)?;
    while c < end && c < last_column {
        let cell = row.read(c)?;
        if cell.getattr(width_name)?.extract::<i64>()? != 1
            || cell.getattr(style_name)?.ne(style)?
            || escapes_row.contains(c)?
        {
            break;
        }
        let character = cell.getattr(char_name)?;
        if !moves_one_column_everywhere(character.cast::<PyString>()?)? {
            break;
        }
        pending.append(character)?;
        c += 1;
    }
    Ok(c)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(queue_same_style, module)?)?;
    module.add_function(wrap_pyfunction!(copy_single_width, module)?)?;
    module.add_function(wrap_pyfunction!(changed_spans, module)?)?;
    module.add_function(wrap_pyfunction!(store_line, module)?)?;
    module.add_class::<Row>()?;
    Ok(())
}
