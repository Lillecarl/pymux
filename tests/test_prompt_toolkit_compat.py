"""
The faster `take_using_weights` must give the same sequence as the one
of prompt_toolkit.
"""
import itertools

import pytest
from prompt_toolkit.utils import take_using_weights as original

from pymux.prompt_toolkit_compat import take_using_weights as faster

CASES = [
    (["a"], [1]),
    (["a"], [7]),
    (["a", "b"], [1, 1]),
    (["a", "b"], [1, 0]),
    (["a", "b"], [0, 1]),
    (["a", "b"], [5, 10]),
    (["a", "b", "c"], [5, 10, 20]),
    (["a", "b", "c"], [1, 1, 1]),
    (["a", "b", "c"], [3, 1, 2]),
    (["a", "b", "c", "d"], [1, 2, 3, 4]),
    (["a", "b", "c", "d"], [0, 2, 0, 4]),
]


@pytest.mark.parametrize("items,weights", CASES)
def test_the_sequence_is_the_same(items, weights):
    expected = list(itertools.islice(original(items, weights), 200))
    got = list(itertools.islice(faster(items, weights), 200))
    assert got == expected


def test_a_weight_of_zero_everywhere_raises():
    with pytest.raises(ValueError):
        next(faster(["a", "b"], [0, 0]))


def test_the_documented_proportion_holds():
    # The example of the docstring of prompt_toolkit: the first 70 items
    # are 10 times A, 20 times B and 40 times C.
    got = list(itertools.islice(faster(["A", "B", "C"], [5, 10, 20]), 70))
    assert got.count("A") == 10
    assert got.count("B") == 20
    assert got.count("C") == 40
