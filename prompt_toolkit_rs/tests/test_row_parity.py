"""
`prompt_toolkit_rs.Row` answers what `screen._new_row`'s defaultdict
answers, and `store_line` stores what `_store_line` stores.

Both kinds of row take the same operations and have to give the same
answers on the way and hold the same cells at the end, by identity. A
read of a column nobody wrote stores the default in both: the diff reads
two rows up to their last column, and what it stored is what the next
frame compares. Two rows have to be equal in both kinds or in neither.

One difference is allowed: a Rust row iterates by column and a dict in
the order it was written, so contents are compared sorted.
`checks.prompt-toolkit-rs-unit` runs prompt-toolkit's whole suite with
the Rust row, which is what says nothing reads that order.
Lillecarl/pymux#570.
"""

from __future__ import annotations

import prompt_toolkit_rs
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit.layout import containers, screen
from prompt_toolkit.layout.screen import _CHAR_CACHE, Char

#: The references, taken before any test could have installed a kernel.
PURE_ROW = screen._new_row
PURE_STORE = containers._store_line

DEFAULT = _CHAR_CACHE[" ", "[transparent]"]

#: Cells to put in a row: shared ones, equal ones that are other
#: objects, and the default itself.
CELLS = [
    _CHAR_CACHE["a", "", False],
    _CHAR_CACHE["b", "class:x", False],
    Char("a", ""),
    Char("a", ""),
    DEFAULT,
]
COLUMNS = st.integers(-3, 12)
WHICH = st.integers(0, len(CELLS) - 1)

operations = st.lists(
    st.one_of(
        st.tuples(st.just("set"), COLUMNS, WHICH),
        st.tuples(st.just("del"), COLUMNS),
        st.tuples(st.just("pop"), COLUMNS),
        st.tuples(st.just("get"), COLUMNS),
        st.tuples(st.just("read"), COLUMNS),
        st.tuples(st.just("in"), COLUMNS),
        st.tuples(st.just("update"), st.dictionaries(COLUMNS, WHICH, max_size=4)),
        st.tuples(st.just("pairs"), st.lists(st.tuples(COLUMNS, WHICH), max_size=4)),
        st.tuples(st.just("row"), st.dictionaries(COLUMNS, WHICH, max_size=6)),
        st.tuples(st.just("itself")),
        st.tuples(st.just("store"), COLUMNS, st.lists(WHICH, max_size=6)),
        st.tuples(st.just("clear")),
    ),
    max_size=30,
)


def apply(row, steps, store):
    answers = []
    for step in steps:
        kind = step[0]
        if kind == "set":
            row[step[1]] = CELLS[step[2]]
        elif kind == "del":
            try:
                del row[step[1]]
                answers.append("deleted")
            except KeyError:
                answers.append("KeyError")
        elif kind == "pop":
            answers.append(id(row.pop(step[1], None)))
        elif kind == "get":
            answers.append(id(row.get(step[1])))
        elif kind == "read":
            answers.append(id(row[step[1]]))
        elif kind == "in":
            answers.append(step[1] in row)
        elif kind == "update":
            row.update({column: CELLS[which] for column, which in step[1].items()})
        elif kind == "pairs":
            row.update((column, CELLS[which]) for column, which in step[1])
        elif kind == "row":
            # A row of the same kind, which is what a caller holding
            # `data_buffer.default_factory` copies from.
            source = type(row)(DEFAULT) if isinstance(row, prompt_toolkit_rs.Row) else PURE_ROW(DEFAULT)
            source.update({column: CELLS[which] for column, which in step[1].items()})
            row.update(source)
        elif kind == "itself":
            row.update(row)
        elif kind == "store":
            store(row, step[1], [CELLS[which] for which in step[2]])
        else:
            row.clear()
        answers.append(len(row))
    return answers


def held(row):
    return sorted((column, id(cell)) for column, cell in row.items())


@given(operations)
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_operations_give_the_same_answers(steps):
    pure, fast = PURE_ROW(DEFAULT), prompt_toolkit_rs.Row(DEFAULT)
    assert apply(pure, steps, PURE_STORE) == apply(fast, steps, prompt_toolkit_rs.store_line)
    assert held(pure) == held(fast)
    assert sorted(pure) == sorted(fast) == sorted(fast.keys())
    assert [id(cell) for _, cell in sorted(pure.items())] == [id(cell) for _, cell in sorted(fast.items())]
    assert (pure == fast) and (fast == pure) and (fast == dict(pure))
    assert not (fast != pure)
    assert bool(pure) == bool(fast)


@given(operations)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_store_kernel_fills_a_dict_as_the_python_does(steps):
    pure, fast = PURE_ROW(DEFAULT), PURE_ROW(DEFAULT)
    assert apply(pure, steps, PURE_STORE) == apply(fast, steps, prompt_toolkit_rs.store_line)
    assert held(pure) == held(fast)


@given(operations, operations)
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_two_rows_are_equal_in_both_kinds_or_in_neither(one, other):
    pure = [PURE_ROW(DEFAULT), PURE_ROW(DEFAULT)]
    fast = [prompt_toolkit_rs.Row(DEFAULT), prompt_toolkit_rs.Row(DEFAULT)]
    for steps, pure_row, fast_row in zip((one, other), pure, fast):
        apply(pure_row, steps, PURE_STORE)
        apply(fast_row, steps, prompt_toolkit_rs.store_line)
    equal = pure[0] == pure[1]
    assert (fast[0] == fast[1]) is equal
    assert (fast[0] != fast[1]) is not equal
    assert (fast[0] == pure[1]) is equal


def test_a_column_nobody_wrote_reads_the_default_and_keeps_it():
    row = prompt_toolkit_rs.Row(DEFAULT)
    assert row[5] is DEFAULT
    assert 5 in row
    assert len(row) == 1
    assert row.get(6) is None
    assert 6 not in row
