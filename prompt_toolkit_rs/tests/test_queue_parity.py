"""
`prompt_toolkit_rs.queue_same_style` queues what the Python queues.

Both take the same row, the same escapes and the same running style,
and they have to agree on where they stopped, on what they queued by
identity, and on what the walk left in the row: a defaultdict stores
its default for a column it is asked about. Lillecarl/pymux#566.
"""

from __future__ import annotations

from collections import defaultdict

import prompt_toolkit_rs
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit import renderer
from prompt_toolkit.layout.screen import _CHAR_CACHE, Char

#: The reference, taken before any test could have installed the kernel.
PURE = renderer._queue_same_style

DEFAULT = Char(" ", "")
STYLES = ["", "class:x", "class:y"]

#: Characters every terminal moves one column for, and ones it may not:
#: an emoji, a symbol with an emoji form, a combining mark, two code
#: points, a wide one, nothing, and the edges of the ranges.
CHARACTERS = [
    "a",
    " ",
    "~",
    "\x7f",
    "\xa0",
    "\u22ff",
    "\u2300",
    "\u2500",
    "\u259f",
    "\u25a0",
    "\u2733",
    "😀",
    "\u0301",
    "e\u0301",
    "漢",
    "",
    "\udcff",
]

a_row = st.dictionaries(
    st.integers(0, 15),
    st.tuples(st.sampled_from(CHARACTERS), st.sampled_from(STYLES)),
    max_size=16,
)


def queued(queue, row, c, end, last_column, style, escapes):
    built = defaultdict(lambda: DEFAULT)
    for column, (character, cell_style) in row.items():
        built[column] = _CHAR_CACHE[character, cell_style, False]
    escapes_row = defaultdict(str, {column: "\x1b]8;;\x1b\\" for column in escapes})
    pending: list[str] = []
    stopped = queue(built, c, end, last_column, style, escapes_row, pending)
    return stopped, [id(text) for text in pending], {column: id(cell) for column, cell in built.items()}


@pytest.mark.parametrize("character", CHARACTERS)
def test_each_character_is_judged_the_same(character):
    "The edges of the ranges, which random rows reach too rarely."
    row = {0: (character, "class:x"), 1: ("a", "class:x")}
    assert queued(PURE, row, 0, 2, 10, "class:x", set()) == queued(
        prompt_toolkit_rs.queue_same_style, row, 0, 2, 10, "class:x", set()
    )


@given(
    a_row,
    st.integers(0, 16),
    st.integers(0, 18),
    st.integers(0, 18),
    st.sampled_from([None, *STYLES]),
    st.sets(st.integers(0, 15), max_size=3),
)
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_run_is_queued(row, c, end, last_column, style, escapes):
    assert queued(PURE, row, c, end, last_column, style, escapes) == queued(
        prompt_toolkit_rs.queue_same_style, row, c, end, last_column, style, escapes
    )
