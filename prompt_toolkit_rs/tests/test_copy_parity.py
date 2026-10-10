"""
`prompt_toolkit_rs.copy_single_width` copies what the Python copies.

Both take the same text, the same known characters and a row each,
and they have to agree on the index, the column, every cell of the
row by identity, and what they collected. Lillecarl/pymux#566.
"""

from __future__ import annotations

from collections import defaultdict

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit.layout import containers
from prompt_toolkit.layout.screen import _CHAR_CACHE

import prompt_toolkit_rs

#: The reference, taken before any test could have installed the kernel.
PURE = containers._copy_single_width

#: What the known characters may hold: one cell, two cells, none, and
#: a character the style has not drawn yet.
KNOWN = {c: _CHAR_CACHE[c, "class:x", False] for c in "abc ~é"}
KNOWN["漢"] = _CHAR_CACHE["漢", "class:x", False]
KNOWN["́"] = _CHAR_CACHE["́", "class:x", False]
ALPHABET = "abc ~é漢́z\udcff"


def copied(copy, text, index, x, offset, width, collect):
    row = defaultdict(lambda: None)
    collected = [] if collect else None
    answer = copy(text, index, KNOWN, row, x, offset, width, collected)
    return answer, {column: id(char) for column, char in row.items()}, [id(char) for char in collected or []]


@given(
    st.text(alphabet=ALPHABET, max_size=30),
    st.integers(0, 10),
    st.integers(0, 20),
    st.integers(0, 5),
    st.integers(0, 25),
    st.booleans(),
)
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_run_is_copied(text, index, x, offset, width, collect):
    index = min(index, len(text))
    assert copied(PURE, text, index, x, offset, width, collect) == copied(
        prompt_toolkit_rs.copy_single_width, text, index, x, offset, width, collect
    )


def a_fresh_install(monkeypatch):
    "Undo whatever `install()` does once the test is over."
    from prompt_toolkit import renderer

    monkeypatch.setattr(containers, "_copy_single_width", PURE)
    monkeypatch.setattr(renderer, "_changed_spans", renderer._changed_spans)
    monkeypatch.setattr(prompt_toolkit_rs, "_replaced", {})
    return renderer


def test_install_puts_the_kernels_in_place(monkeypatch):
    renderer = a_fresh_install(monkeypatch)
    monkeypatch.delenv(prompt_toolkit_rs.PURE, raising=False)
    assert prompt_toolkit_rs.install() is True
    assert containers._copy_single_width is prompt_toolkit_rs.copy_single_width
    assert renderer._changed_spans is prompt_toolkit_rs.changed_spans


def test_pure_says_not_to(monkeypatch):
    renderer = a_fresh_install(monkeypatch)
    monkeypatch.setenv(prompt_toolkit_rs.PURE, "1")
    assert prompt_toolkit_rs.install() is False
    assert containers._copy_single_width is PURE
    assert renderer._changed_spans is not prompt_toolkit_rs.changed_spans
