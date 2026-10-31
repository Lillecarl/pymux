"""
The tree of splits inside a window. A split holds panes and splits of
the other way, and each child's share of it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from pyte.keep import Keep

from .pane import Pane

__all__ = ["HSplit", "Node", "NodeKey", "Split", "VSplit", "node_key", "panes_of", "place_of", "shape_of"]


#: What names one node of a window's layout tree: its kind and its id.
#: A pane and a split count their ids apart, so the kind keeps them
#: apart. Keys are values, not objects, so a snapshot can write them
#: and a new process can read them back. Lillecarl/pymux#399.
NodeKey = tuple[str, int]


def node_key(item: Node) -> NodeKey:
    "The key of a pane or a split."
    if isinstance(item, Pane):
        return ("pane", item.pane_id)
    return ("split", item.split_id)


class Split(list["Node"]):
    """
    Base class for horizontal and vertical splits. (This is a higher level
    split than prompt_toolkit.layout.HSplit.)
    """

    _split_counter = 0

    KEEP: ClassVar[dict[str, Keep]] = {
        "split_id": Keep.SAVED,
        "weights": Keep.SAVED,
    }

    def __init__(self, children: Iterable[Node] = ()) -> None:
        list.__init__(self, children)

        Split._split_counter += 1
        self.split_id = Split._split_counter

        #: Each child's share of this split, by `node_key`. A child with
        #: no entry weighs 1. An entry outlives a child that leaves, so
        #: a pane that comes back finds the weight it had.
        self.weights: dict[NodeKey, int] = {}

    def weight_of(self, child: Node) -> int:
        "This child's share of the split."
        return self.weights.get(node_key(child), 1)

    def set_weight(self, child: Node, weight: int) -> None:
        self.weights[node_key(child)] = weight

    def __repr__(self) -> str:
        return "%s(%s)" % (self.__class__.__name__, list.__repr__(self))


class HSplit(Split):
    """Panes stacked one above another."""


class VSplit(Split):
    """Panes side by side, so this is the split that has a left and a right."""


#: What a split holds: a pane, or a split of the other way.
type Node = Pane | Split


def shape_of(split: Split) -> tuple[bool, tuple[object, ...]]:
    "The kind of `split` and what it holds: pane ids, and the shape of each split inside."
    return (
        type(split) is HSplit,
        tuple(shape_of(item) if isinstance(item, Split) else item.pane_id for item in split),
    )


def place_of(split: Split, item: object) -> int:
    """
    Where this child sits in the split, by identity.

    `list.index` asks whether two children are equal, and a split *is*
    a list: two stacks holding the same panes would compare equal and
    the first of them would answer for both.
    """
    for place, child in enumerate(split):
        if child is item:
            return place
    raise ValueError("%r is not in %r" % (item, split))


def panes_of(item: Node) -> list[Pane]:
    "Every pane under this item, in the order they are drawn."
    if isinstance(item, Pane):
        return [item]

    result: list[Pane] = []
    for child in item:
        result.extend(panes_of(child))
    return result
