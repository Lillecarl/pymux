"""
Compatibility fixes for prompt_toolkit.

These make the layout of a render cheaper. (TODO: upstream these to
prompt_toolkit.)
"""
from typing import Generator, List, Sequence, TypeVar

from prompt_toolkit import utils as pt_utils
from prompt_toolkit.layout import containers as pt_containers

__all__ = [
    "apply_prompt_toolkit_compat_fixes",
    "take_using_weights",
]

_T = TypeVar("_T")


def take_using_weights(
    items: Sequence[_T], weights: Sequence[int]
) -> Generator[_T, None, None]:
    """
    The same generator as `prompt_toolkit.utils.take_using_weights`, and
    the same sequence, but faster.

    One render asks for a few thousand items of it, which made it about
    a fifth of the work of a key press. Two things are different. One
    item takes a short path, because a split of one child is the common
    case in pymux. And the loop reads a list of indexes, instead of
    building a `zip` object for every pass.
    """
    assert len(items) == len(weights)
    assert len(items) > 0

    # Remove items with zero-weight.
    kept: List[_T] = []
    kept_weights: List[int] = []
    for item, weight in zip(items, weights):
        if weight > 0:
            kept.append(item)
            kept_weights.append(weight)

    # Make sure that we have some items left.
    if not kept:
        raise ValueError("Did't got any items with a positive weight.")

    count = len(kept)

    if count == 1:
        # One item takes every turn.
        item = kept[0]
        while True:
            yield item

    already_taken = [0] * count
    max_weight = max(kept_weights)
    indexes = list(range(count))

    i = 0
    while True:
        # Each iteration of this loop, we fill up until by
        # (total_weight/max_weight).
        adding = True
        while adding:
            adding = False

            for index in indexes:
                if already_taken[index] < i * kept_weights[index] / max_weight:
                    yield kept[index]
                    already_taken[index] += 1
                    adding = True

        i += 1


def apply_prompt_toolkit_compat_fixes() -> None:
    "Install the fixes. (Safe to call more than once.)"
    pt_utils.take_using_weights = take_using_weights

    # `containers` took its own reference at import time.
    pt_containers.take_using_weights = take_using_weights
