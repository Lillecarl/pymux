"""
`prompt_toolkit_rs.changed_spans` finds the spans the Python finds.

Both walk the same two rows and have to agree on the spans, and on
what the walk left in each row: a defaultdict stores its default for
a column it is asked about, and the rest of the diff reads the cells
the walk stored. Lillecarl/pymux#566.
"""

from __future__ import annotations

import prompt_toolkit_rs
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit import renderer
from prompt_toolkit.layout import screen
from prompt_toolkit.layout.screen import _CHAR_CACHE, Char

#: The references, taken before any test could have installed a kernel.
PURE = renderer._changed_spans
PURE_ROW = screen._new_row

DEFAULT = Char(" ", "")

#: Cells to put in a row: the same objects, equal ones that are other
#: objects, wide ones and ones of no width.
CELLS = [
    _CHAR_CACHE["a", "", False],
    _CHAR_CACHE["a", "class:x", False],
    _CHAR_CACHE["b", "", False],
    _CHAR_CACHE[" ", "", False],
    Char("a", ""),
    Char(" ", ""),
    _CHAR_CACHE["漢", "", False],
    _CHAR_CACHE["\u0301", "", False],
]

a_row = st.dictionaries(st.integers(0, 20), st.sampled_from(range(len(CELLS))), max_size=20)


def made(row, new_row):
    built = new_row(DEFAULT)
    for column, which in row.items():
        built[column] = CELLS[which]
    return built


def walked(spans, new, old, last, new_row=PURE_ROW):
    new_row, previous_row = made(new, new_row), made(old, new_row)
    answer = spans(new_row, previous_row, last)
    return (
        answer,
        {column: id(cell) for column, cell in new_row.items()},
        {column: id(cell) for column, cell in previous_row.items()},
    )


@pytest.mark.parametrize("new_row", [PURE_ROW, prompt_toolkit_rs.Row], ids=["dict", "rust"])
@given(a_row, a_row, st.integers(-1, 22))
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_spans_are_found(new_row, new, old, last):
    assert walked(PURE, new, old, last) == walked(prompt_toolkit_rs.changed_spans, new, old, last, new_row)
