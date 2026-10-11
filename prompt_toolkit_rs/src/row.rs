//! `prompt_toolkit_rs.Row`: one row of a composed screen, stored by column
//! in one contiguous vector. It behaves as `prompt_toolkit.layout.screen.
//! _new_row` does, a defaultdict of `Char`: a column nobody wrote reads the
//! row's default and keeps it. Lillecarl/pymux#570.

use std::collections::BTreeMap;

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyDict, PyIterator, PyList, PyTuple};

#[pyclass(module = "prompt_toolkit_rs", mapping)]
pub struct Row {
    /// The cells of columns 0 and up; `None` is a column that holds
    /// nothing. It never ends in `None`, so its length is one past the
    /// last column held.
    cells: Vec<Option<Py<PyAny>>>,
    /// Columns left of 0: a float may be drawn partly off the screen.
    outside: BTreeMap<i64, Py<PyAny>>,
    /// How many columns hold a cell.
    count: usize,
    /// What a column nobody wrote reads, and then holds.
    default: Py<PyAny>,
}

impl Row {
    pub(crate) fn cell(&self, column: i64) -> Option<&Py<PyAny>> {
        match usize::try_from(column) {
            Ok(index) => self.cells.get(index).and_then(Option::as_ref),
            Err(_) => self.outside.get(&column),
        }
    }

    pub(crate) fn store(&mut self, column: i64, cell: Py<PyAny>) {
        let replaced = match usize::try_from(column) {
            Ok(index) => {
                if index >= self.cells.len() {
                    self.cells.resize_with(index + 1, || None);
                }
                self.cells[index].replace(cell)
            }
            Err(_) => self.outside.insert(column, cell),
        };
        if replaced.is_none() {
            self.count += 1;
        }
    }

    /// The last column held, or -1. `cells` never ends in `None`.
    pub(crate) fn last_column(&self) -> i64 {
        match self.cells.len() {
            0 => self
                .outside
                .last_key_value()
                .map_or(-1, |(&column, _)| column),
            length => length as i64 - 1,
        }
    }

    /// `row[column]`: a column nobody wrote takes the default first.
    pub(crate) fn read(&mut self, py: Python<'_>, column: i64) -> Py<PyAny> {
        if let Some(cell) = self.cell(column) {
            return cell.clone_ref(py);
        }
        let cell = self.default.clone_ref(py);
        self.store(column, cell.clone_ref(py));
        cell
    }

    /// The cells of `chars`, one to a column from `start`.
    pub(crate) fn store_run(&mut self, start: i64, chars: &Bound<'_, PyList>) {
        let length = chars.len();
        if length == 0 {
            return;
        }
        if start >= 0 {
            let start = start as usize;
            if start + length > self.cells.len() {
                self.cells.resize_with(start + length, || None);
            }
            for (slot, cell) in self.cells[start..start + length]
                .iter_mut()
                .zip(chars.iter())
            {
                if slot.replace(cell.unbind()).is_none() {
                    self.count += 1;
                }
            }
        } else {
            for (offset, cell) in chars.iter().enumerate() {
                self.store(start + offset as i64, cell.unbind());
            }
        }
    }

    /// Every cell of `source`, in its column: a copy of pointers, with
    /// no column to read out of a Python int.
    fn copy_from(&mut self, py: Python<'_>, source: &Row) {
        if source.cells.len() > self.cells.len() {
            self.cells.resize_with(source.cells.len(), || None);
        }
        let mut added = 0;
        for (slot, cell) in self.cells.iter_mut().zip(&source.cells) {
            if let Some(cell) = cell
                && slot.replace(cell.clone_ref(py)).is_none()
            {
                added += 1;
            }
        }
        self.count += added;
        for (&column, cell) in &source.outside {
            self.store(column, cell.clone_ref(py));
        }
    }

    fn remove(&mut self, column: i64) -> Option<Py<PyAny>> {
        let removed = match usize::try_from(column) {
            Ok(index) => {
                let removed = self.cells.get_mut(index).and_then(Option::take);
                while matches!(self.cells.last(), Some(None)) {
                    self.cells.pop();
                }
                removed
            }
            Err(_) => self.outside.remove(&column),
        };
        if removed.is_some() {
            self.count -= 1;
        }
        removed
    }

    /// Every cell with its column, left to right.
    fn columns(&self) -> impl Iterator<Item = (i64, &Py<PyAny>)> {
        self.outside
            .iter()
            .map(|(&column, cell)| (column, cell))
            .chain(
                self.cells
                    .iter()
                    .enumerate()
                    .filter_map(|(index, cell)| cell.as_ref().map(|cell| (index as i64, cell))),
            )
    }

    fn keys_list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.columns().map(|(column, _)| column))
    }

    /// Whether `other` holds the same columns as this row, with equal
    /// cells. Cells that are the same object are equal without asking
    /// them, which is most cells: `_CHAR_CACHE` hands out one `Char` for
    /// each character and style.
    fn equals_row(py: Python<'_>, this: &Bound<'_, Row>, other: &Bound<'_, Row>) -> PyResult<bool> {
        if this.is(other) {
            return Ok(true);
        }
        // Cells that are other objects, compared after both borrows end:
        // `Char.__eq__` is Python, and Python may reach either row.
        let mut ask: Vec<(Py<PyAny>, Py<PyAny>)> = Vec::new();
        {
            let mine = this.borrow();
            let theirs = other.borrow();
            if mine.count != theirs.count || mine.cells.len() != theirs.cells.len() {
                return Ok(false);
            }
            for (a, b) in mine.cells.iter().zip(&theirs.cells) {
                match (a, b) {
                    (None, None) => {}
                    (Some(a), Some(b)) => {
                        if a.as_ptr() != b.as_ptr() {
                            ask.push((a.clone_ref(py), b.clone_ref(py)));
                        }
                    }
                    _ => return Ok(false),
                }
            }
            if mine.outside.len() != theirs.outside.len() {
                return Ok(false);
            }
            for ((column, a), (other_column, b)) in mine.outside.iter().zip(&theirs.outside) {
                if column != other_column {
                    return Ok(false);
                }
                if a.as_ptr() != b.as_ptr() {
                    ask.push((a.clone_ref(py), b.clone_ref(py)));
                }
            }
        }
        for (a, b) in ask {
            if !a.bind(py).eq(b.bind(py))? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Whether a mapping that is not a `Row` holds the same columns,
    /// with equal cells, as a dict compares.
    fn equals_mapping(
        py: Python<'_>,
        this: &Bound<'_, Row>,
        other: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        if !other.hasattr("items")? {
            return Ok(false);
        }
        let mine: Vec<(i64, Py<PyAny>)> = {
            let row = this.borrow();
            if other.len()? != row.count {
                return Ok(false);
            }
            row.columns()
                .map(|(column, cell)| (column, cell.clone_ref(py)))
                .collect()
        };
        for (column, cell) in mine {
            let theirs = other.call_method1("get", (column,))?;
            if theirs.is_none() || !cell.bind(py).eq(&theirs)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

#[pymethods]
impl Row {
    #[new]
    fn new(default: Py<PyAny>) -> Self {
        Self {
            cells: Vec::new(),
            outside: BTreeMap::new(),
            count: 0,
            default,
        }
    }

    /// What a column nobody wrote reads.
    #[getter]
    fn default(&self, py: Python<'_>) -> Py<PyAny> {
        self.default.clone_ref(py)
    }

    fn __getitem__(&mut self, py: Python<'_>, column: i64) -> Py<PyAny> {
        self.read(py, column)
    }

    fn __setitem__(&mut self, column: i64, cell: Py<PyAny>) {
        self.store(column, cell);
    }

    fn __delitem__(&mut self, column: i64) -> PyResult<()> {
        match self.remove(column) {
            Some(_) => Ok(()),
            None => Err(PyKeyError::new_err(column)),
        }
    }

    fn __contains__(&self, column: &Bound<'_, PyAny>) -> bool {
        column
            .extract::<i64>()
            .is_ok_and(|column| self.cell(column).is_some())
    }

    fn __len__(&self) -> usize {
        self.count
    }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        self.keys_list(py)?.try_iter()
    }

    fn keys<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.keys_list(py)
    }

    fn values<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.columns().map(|(_, cell)| cell.clone_ref(py)))
    }

    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(
            py,
            self.columns()
                .map(|(column, cell)| (column, cell.clone_ref(py))),
        )
    }

    #[pyo3(signature = (column, default = None))]
    fn get(
        &self,
        py: Python<'_>,
        column: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
    ) -> Py<PyAny> {
        let found = column
            .extract::<i64>()
            .ok()
            .and_then(|column| self.cell(column));
        match (found, default) {
            (Some(cell), _) => cell.clone_ref(py),
            (None, Some(default)) => default,
            (None, None) => py.None(),
        }
    }

    #[pyo3(signature = (column, *default))]
    fn pop(&mut self, column: i64, default: &Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        if let Some(cell) = self.remove(column) {
            return Ok(cell);
        }
        match default.len() {
            0 => Err(PyKeyError::new_err(column)),
            1 => Ok(default.get_item(0)?.unbind()),
            given => Err(PyTypeError::new_err(format!(
                "pop expected at most 2 arguments, got {}",
                given + 1
            ))),
        }
    }

    /// Take the cells of a mapping, or of an iterable of pairs.
    fn update(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        if let Ok(source) = other.cast::<Row>() {
            if source.is(slf) {
                return Ok(());
            }
            slf.borrow_mut().copy_from(py, &source.borrow());
            return Ok(());
        }
        if let Ok(dict) = other.cast::<PyDict>() {
            let mut row = slf.borrow_mut();
            for (column, cell) in dict.iter() {
                row.store(column.extract()?, cell.unbind());
            }
            return Ok(());
        }
        let pairs = if other.hasattr("items")? {
            other.call_method0("items")?
        } else {
            other.clone()
        };
        // Collected before the row is borrowed: the pairs may be a
        // generator that reads this very row.
        let mut taken: Vec<(i64, Py<PyAny>)> = Vec::new();
        for pair in pairs.try_iter()? {
            taken.push(pair?.extract()?);
        }
        let mut row = slf.borrow_mut();
        for (column, cell) in taken {
            row.store(column, cell);
        }
        Ok(())
    }

    fn clear(&mut self) {
        self.cells.clear();
        self.outside.clear();
        self.count = 0;
    }

    /// Equal to any mapping that holds the same columns and equal cells,
    /// as a dict is. The default takes no part, as a defaultdict's
    /// factory takes none.
    fn __richcmp__(
        slf: &Bound<'_, Self>,
        other: &Bound<'_, PyAny>,
        op: CompareOp,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let equal = match op {
            CompareOp::Eq | CompareOp::Ne => match other.cast::<Row>() {
                Ok(other) => Row::equals_row(py, slf, other)?,
                Err(_) => Row::equals_mapping(py, slf, other)?,
            },
            _ => return Ok(py.NotImplemented()),
        };
        let answer = if matches!(op, CompareOp::Eq) {
            equal
        } else {
            !equal
        };
        Ok(answer.into_pyobject(py)?.to_owned().into_any().unbind())
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let mut parts = Vec::with_capacity(self.count);
        for (column, cell) in self.columns() {
            parts.push(format!("{column}: {}", cell.bind(py).repr()?));
        }
        Ok(format!("Row({{{}}})", parts.join(", ")))
    }
}
