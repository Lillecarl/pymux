"""
Rectangles on the plane, in cells: the sides of one, how two overlap,
and how one is cut by or covered by others. Arithmetic only: no
pane, no window and no plan.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from enum import Enum
from typing import NamedTuple

from prompt_toolkit.data_structures import Point

__all__ = ["Rect", "Side", "bounding_box", "covered", "enters", "inside", "overlap_of", "towards"]


class Side(Enum):
    """
    A direction to look in, and one step that way.

    The names are the ones the arrangement already uses, so
    `pane_to_the_left` and `pane_above` keep their words. tmux spells
    the same four `-L`, `-R`, `-U` and `-D`.

    `y` grows downwards, the way a terminal counts rows. So `ABOVE` is
    the negative step and `BELOW` is the positive one.
    """

    LEFT = (-1, 0)
    RIGHT = (1, 0)
    ABOVE = (0, -1)
    BELOW = (0, 1)

    @property
    def step(self) -> Point:
        "One cell that way."
        return Point(*self.value)

    @property
    def sideways(self) -> bool:
        "True for the two that look along a row."
        return self.value[1] == 0

    @property
    def growth(self) -> int:
        "Whether the numbers grow this way, or shrink."
        return self.value[0] if self.sideways else self.value[1]

    @property
    def opposite(self) -> Side:
        return _OPPOSITE[self]

    @property
    def angle(self) -> float:
        """
        The angle `Plan.trace` takes to look this way.

        **`Plan.neighbour` is the exact answer and this is not.**
        `math.cos` of a right angle is not zero, so a ray cast this
        way drifts sideways. It drifts far less than a cell over any
        screen, and it is still the reason the four directions a key
        is bound to have a path of their own.
        """
        return math.atan2(self.value[1], self.value[0])


_OPPOSITE = {
    Side.LEFT: Side.RIGHT,
    Side.RIGHT: Side.LEFT,
    Side.ABOVE: Side.BELOW,
    Side.BELOW: Side.ABOVE,
}


class Rect(NamedTuple):
    """
    A rectangle of cells on the plane.

    `x` and `y` are its top left cell and may be negative. `right` and
    `bottom` are one past the last cell, the way a range ends, so two
    rectangles that touch share a number and no cell.
    """

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        "One past the rightmost cell."
        return self.x + self.width

    @property
    def bottom(self) -> int:
        "One past the bottom cell."
        return self.y + self.height

    def edge(self, side: Side) -> int:
        "Where this rectangle ends, looking that way."
        if side is Side.LEFT:
            return self.x
        if side is Side.RIGHT:
            return self.right
        if side is Side.ABOVE:
            return self.y
        return self.bottom

    def span(self, side: Side) -> tuple[int, int]:
        """
        The band this rectangle covers across that direction.

        Looking along a row, the band is the rows it covers. Two
        rectangles are neighbours only if their bands overlap.
        """
        if side is Side.LEFT or side is Side.RIGHT:
            return (self.y, self.y + self.height)
        return (self.x, self.x + self.width)

    def gap_to(self, other: Rect, side: Side) -> int:
        """
        How many cells lie between this rectangle and that one, that
        way.

        Zero means they touch. A negative answer means `other` is not
        beyond this edge at all: it overlaps this rectangle, or it is
        behind it.

        It is `side.growth * (other.edge(side.opposite) - self.edge(side))`
        written out for each side: every title bar of every frame asks
        it of every other pane, and the enum properties cost more than
        the arithmetic.
        """
        if side is Side.LEFT:
            return self.x - (other.x + other.width)
        if side is Side.RIGHT:
            return other.x - (self.x + self.width)
        if side is Side.ABOVE:
            return self.y - (other.y + other.height)
        return other.y - (self.y + self.height)

    def holds(self, point: Point) -> bool:
        "True when that cell is one of this rectangle's."
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def overlaps(self, other: Rect) -> bool:
        "True when the two share a cell."
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom

    def encloses(self, other: Rect) -> bool:
        """
        True when every cell of that rectangle is one of this one's.

        A rectangle of no cells is enclosed by anything, which is what
        `range` says about an empty range and the answer a caller
        wants: there is nothing of it outside.
        """
        if other.width <= 0 or other.height <= 0:
            return True
        return self.x <= other.x and other.right <= self.right and self.y <= other.y and other.bottom <= self.bottom

    def cells(self) -> Iterator[Point]:
        "Every cell of this rectangle, a row at a time."
        for y in range(self.y, self.bottom):
            for x in range(self.x, self.right):
                yield Point(x=x, y=y)

    def minus(self, others: Iterable[Rect]) -> list[Rect]:
        """
        The cells of this rectangle that none of `others` covers, as
        rectangles that do not overlap: itself when nothing covers it,
        nothing when they cover it whole. `_without` takes each one out.

        Rectangles, not cells, so it costs the number of rectangles and
        not the area. Lillecarl/pymux#355.
        """
        left: list[Rect] = [self]
        for one in others:
            if not left:
                break
            left = [piece for held in left for piece in _without(held, one)]
        return left


def towards(at: int, size: int, start: int, end: int) -> int:
    """
    Where one edge of a view goes, to show that band of the plane.

    As little as it takes: a band past the far edge lines its end up
    with the view's, so what the view showed beside it stays. Snapping
    its start to the near edge threw away a whole neighbour to bring in
    one cell of border. Lillecarl/pymux#614.
    """
    if start < at or end - start > size:
        return start
    if end > at + size:
        return end - size
    return at


def inside(at: int, size: int, low: int, high: int) -> int:
    """
    That edge, brought back onto the plane.

    A plane smaller than the view leaves nowhere to go, and the answer
    is its own start. `max` of the two bounds says so, and it is why
    this is not one `min` and one `max`.
    """
    return max(low, min(at, max(low, high - size)))


def overlap_of(one: tuple[int, int], other: tuple[int, int]) -> int:
    "How much two half open bands share. Zero when they only touch."
    return max(0, min(one[1], other[1]) - max(one[0], other[0]))


def bounding_box(rects: Iterable[Rect]) -> Rect:
    """
    The smallest rectangle that holds all of them.

    An empty plane has an empty box, at the origin. Nothing draws it,
    and a box of no cells is the honest answer for no rectangles.
    """
    rects = list(rects)
    if not rects:
        return Rect(x=0, y=0, width=0, height=0)

    x = min(rect.x for rect in rects)
    y = min(rect.y for rect in rects)
    return Rect(
        x=x,
        y=y,
        width=max(rect.right for rect in rects) - x,
        height=max(rect.bottom for rect in rects) - y,
    )


def _without(rect: Rect, cut: Rect) -> list[Rect]:
    """
    What is left of `rect` when `cut` is taken out of it.

    Up to four pieces: the band above the cut, the band below it, and
    the left and right of what the cut's own rows cover. Every piece
    has cells in it -- each is guarded by the edge that makes it -- so
    an empty answer means nothing is left.
    """
    if not rect.overlaps(cut):
        return [rect]

    pieces = []
    if cut.y > rect.y:
        pieces.append(Rect(rect.x, rect.y, rect.width, cut.y - rect.y))
    if cut.bottom < rect.bottom:
        pieces.append(Rect(rect.x, cut.bottom, rect.width, rect.bottom - cut.bottom))

    # The rows the cut covers, which is where it can leave a side.
    top = max(rect.y, cut.y)
    bottom = min(rect.bottom, cut.bottom)
    if cut.x > rect.x:
        pieces.append(Rect(rect.x, top, cut.x - rect.x, bottom - top))
    if cut.right < rect.right:
        pieces.append(Rect(cut.right, top, rect.right - cut.right, bottom - top))

    return pieces


def covered(rect: Rect, over: Iterable[Rect]) -> bool:
    """
    Whether those rectangles hold every cell of this one, together.

    **Together**, which is what `Rect.encloses` cannot say: two
    rectangles that cover this one between them each answer no on
    their own.

    **Rectangles, not cells**: `Rect.minus`. `PlanContainer` calls
    `Plan.drawn` on every frame, and a full pane is thousands of cells.

    `tests/test_plane.py` holds the cell-at-a-time version as the
    oracle this is checked against.
    """
    return not rect.minus(over)


def enters(rect: Rect, start: tuple[float, float], step: tuple[float, float]) -> float | None:
    """
    How far along the ray it first reaches inside that rectangle.

    `None` when it never does. This is the slab test: each axis gives
    the stretch of the ray inside that axis's band, and the ray is
    inside the rectangle where the two stretches meet.
    """
    near, far = 0.0, math.inf

    for low, high, at, along in (
        (rect.x, rect.right, start[0], step[0]),
        (rect.y, rect.bottom, start[1], step[1]),
    ):
        if along == 0.0:
            # Parallel to this band: either always in it, or never.
            if not low <= at < high:
                return None
            continue

        first, last = (low - at) / along, (high - at) / along
        if first > last:
            first, last = last, first

        near, far = max(near, first), min(far, last)

    if near <= 0.0 or near >= far:
        return None
    return near
