"""
The plane: one place that answers "where is every pane".

`docs/layout-engine-plan.md` holds the design and Lillecarl/pymux#217
holds the argument for it. The short form: four places decide geometry
today, and the clearest symptom is that "which pane is beside this
one" has two answers, one read off the last frame and one walked out
of the tree. A plane answers it once.

**The plane is unbounded.** `x` and `y` may be negative, and a
rectangle needs no permission from a screen size. A view is what is
bounded, and a view comes later (slice 5).

This module is the bottom of that work and it is pure: no toolkit, no
pty, no widget. It holds three things and no behaviour.

    Rect     a rectangle of cells on the plane
    Slot     one rectangle's worth of it, and the panes in it
    Plan     where every slot is, for one frame, and the services

**A pane is any object with an identity.** Nothing here asks a pane
for anything, so nothing here imports one. `pymux.arrangement` passes
its own `Pane` and a test passes a stand-in.

**A slot is the thing that has a rectangle, not a pane.** More than
one pane in a slot means tabbed: one shown, the rest behind it. Carl:
"tabstacking on rectangles is handled by an invisible wrapper element
that owns 1 or more terminals". So "no two slots overlap" needs no
word about which pane is visible, and moving a stack is one operation
on one object.

**Nothing here can stop a popup.** "No two slots overlap" is a rule
about the tiling, not about the screen. A popup draws in the layer
above the panes and always has: the chrome floats sit at `Z_INDEX` 5
to 9, and `display-popup -E` opens `Pymux.overlay_pane`, which is not
in the window tree at all. A floating *window* -- a pane a person
parks somewhere and leaves there -- is the other thing, and it belongs
to every layout rather than to one. `Plan` grows a second, ordered
list for those when float mode is built, and the rule above keeps its
words because it is about `rects`.

**What is not here.** No `Plane` class yet, and no layout: a plan
arrives already laid out, and the classes that lay one out
(`Strip`, `Divided`, `Masonry`) come in the slices after this one. So
nothing in this module validates geometry. A plan holding two
overlapping slots is a fault of whatever laid it out, and
`tests/test_the_plane.py` is where that promise is kept.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Iterable, Iterator, NamedTuple

from prompt_toolkit.data_structures import Point

__all__ = [
    "Pane",
    "Rect",
    "Side",
    "Slot",
    "Plan",
]


#: What a slot holds. Any object with an identity, because nothing
#: here asks a pane for anything.
Pane = object


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
        way drifts by about one cell in ten thousand million. That is
        the whole reason the four cardinal directions have a path of
        their own.
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
        if side.sideways:
            return (self.y, self.bottom)
        return (self.x, self.right)

    def gap_to(self, other: Rect, side: Side) -> int:
        """
        How many cells lie between this rectangle and that one, that
        way.

        Zero means they touch. A negative answer means `other` is not
        beyond this edge at all: it overlaps this rectangle, or it is
        behind it.
        """
        return side.growth * (other.edge(side.opposite) - self.edge(side))

    def holds(self, point: Point) -> bool:
        "True when that cell is one of this rectangle's."
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def overlaps(self, other: Rect) -> bool:
        "True when the two share a cell."
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )

    def cells(self) -> Iterator[Point]:
        "Every cell of this rectangle, a row at a time."
        for y in range(self.y, self.bottom):
            for x in range(self.x, self.right):
                yield Point(x=x, y=y)


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


def _place_of(panes: list[Pane], pane: Pane) -> int:
    """
    Where this pane sits in the list, by identity.

    `list.index` asks whether two panes are equal, and a stand-in for
    a pane in a test may well answer yes to another one. Identity is
    what a pane has and what this needs. `arrangement._place_of` is
    the same function, for the same reason.
    """
    for place, held in enumerate(panes):
        if held is pane:
            return place
    raise ValueError("%r is not in %r" % (pane, panes))


class Slot:
    """
    One rectangle's worth of the plane, and the panes in it.

    A slot owns one or more panes and shows exactly one of them. More
    than one means tabbed, and nothing else: a slot never holds a
    layout of its own, so there is no recursion here.

    **Every pane of a slot has the slot's rectangle**, shown or not. A
    pty needs a size, and giving a hidden pane the size it will have
    means revealing it costs no `SIGWINCH` and no redraw.

    A slot is identified by being itself. Two slots holding the same
    panes are two slots, so `Plan.rects` can key on one.
    """

    def __init__(self, *panes: Pane) -> None:
        if not panes:
            raise ValueError("a slot owns at least one pane")

        self.panes = list(panes)

        #: Which pane a person sees, as a place in `panes`.
        self.showing = 0

    @property
    def shown(self) -> Pane:
        "The one pane a person sees."
        return self.panes[self.showing]

    @property
    def stacked(self) -> bool:
        "True when this slot holds more than one pane."
        return len(self.panes) > 1

    def add(self, pane: Pane, after: Pane | None = None) -> None:
        """
        Put another pane in this slot, so the slot is tabbed.

        `after` names the pane it goes behind, and the default is the
        end of the stack. What a person sees does not change: opening
        a tab and showing it are two things, and `show` is the other
        one.
        """
        if after is None:
            self.panes.append(pane)
            return

        self.panes.insert(_place_of(self.panes, after) + 1, pane)
        if _place_of(self.panes, pane) <= self.showing:
            self.showing += 1

    def remove(self, pane: Pane) -> None:
        """
        Take a pane out of this slot.

        **A slot refuses to empty itself.** A slot with no pane has a
        rectangle and nothing to draw in it, which no invariant
        allows, so the last pane leaving means the *slot* goes. That
        is the caller's move, and it is the one place that knows what
        holds the slot.

        Taking the shown pane out shows the one behind it, the way a
        tab bar does when a person closes the tab they are on.
        """
        if len(self.panes) == 1:
            raise ValueError("the last pane of a slot leaves with the slot")

        place = _place_of(self.panes, pane)
        del self.panes[place]

        if place < self.showing or self.showing >= len(self.panes):
            self.showing -= 1

    def show(self, pane: Pane) -> None:
        "Bring one of this slot's panes to the front."
        self.showing = _place_of(self.panes, pane)

    def __contains__(self, pane: Pane) -> bool:
        return any(held is pane for held in self.panes)

    def __len__(self) -> int:
        return len(self.panes)

    def __repr__(self) -> str:
        if not self.stacked:
            return "Slot(%r)" % (self.shown,)
        return "Slot(%r, showing %r)" % (self.panes, self.shown)


class Plan:
    """
    Where every slot is, and what that lets a person ask.

    A plan is a value: whatever laid it out is finished with it, and
    reading it changes nothing. A layout hands one over on every
    frame, and everything that draws, moves the focus or names a
    neighbour reads this and nothing else.

    `order` is the numbering, and it is **over panes, not slots**,
    because a pane number is what `select-pane -t 1` takes and what a
    title bar draws. A slot contributes its panes in stack order, so a
    hidden pane has a number too and choosing it shows it. The default
    is insertion order, which is what the bare plane promises;
    `Strip` and `Divided` will pass `reading_order()` instead
    (Lillecarl/pymux#210).
    """

    def __init__(
        self,
        rects: dict[Slot, Rect] | Iterable[tuple[Slot, Rect]],
        order: Iterable[Pane] | None = None,
    ) -> None:
        self.rects: dict[Slot, Rect] = dict(rects)

        #: Which slot each pane is in. Built once, because everything
        #: that asks where a pane is comes through here.
        self._slots: dict[int, Slot] = {}
        for slot in self.rects:
            for pane in slot.panes:
                if id(pane) in self._slots:
                    raise ValueError("%r is in two slots of one plan" % (pane,))
                self._slots[id(pane)] = slot

        #: The bounding box of every rectangle. The plane is
        #: unbounded, so this is where the *panes* are and not where
        #: the plane ends.
        self.plane: Rect = bounding_box(self.rects.values())

        self.order: list[Pane] = list(order) if order is not None else list(self.panes)

    # ------------------------------------------------------------------
    # What is on it.

    @property
    def slots(self) -> list[Slot]:
        "Every slot, in the order it was put on the plane."
        return list(self.rects)

    @property
    def panes(self) -> list[Pane]:
        "Every pane, insertion order, and stack order inside a slot."
        return [pane for slot in self.rects for pane in slot.panes]

    def at(self, point: Point) -> Slot | None:
        """
        The slot that holds that cell, if a slot does.

        A hole is allowed on the plane, so `None` is a real answer.
        """
        for slot, rect in self.rects.items():
            if rect.holds(point):
                return slot
        return None

    def slot_of(self, pane: Pane) -> Slot:
        """
        The slot this pane is in.

        A pane that is not on the plan is a fault in whatever asked,
        so this raises rather than answering `None`. Everything that
        wants a pane's rectangle comes through here, because a pane
        does not have one: its slot does.
        """
        try:
            return self._slots[id(pane)]
        except KeyError:
            raise KeyError("%r is not on this plan" % (pane,)) from None

    def rect_of(self, pane: Pane) -> Rect:
        "Where this pane is drawn, which is where its slot is."
        return self.rects[self.slot_of(pane)]

    # ------------------------------------------------------------------
    # What is beside it.

    def neighbour(self, slot: Slot, side: Side) -> Slot | None:
        """
        The slot a person means by "the one to my left", and so on.

        **Strict.** Only a slot that is beyond this edge and whose
        band overlaps this one can answer. Of those, the smallest gap
        wins; a tie goes to the largest overlap, and then to whichever
        went on the plane first. If nothing overlaps this band the
        answer is nothing: no diagonal jump. tmux is strict here, and
        it is the less surprising of the two.

        A gap of zero is the ordinary case, because two rectangles of
        a tiling touch.
        """
        mine = self.rects[slot]
        best: tuple[tuple[int, int], Slot] | None = None

        for other, rect in self.rects.items():
            if other is slot:
                continue

            gap = mine.gap_to(rect, side)
            if gap < 0:
                continue

            overlap = overlap_of(mine.span(side), rect.span(side))
            if overlap == 0:
                continue

            key = (gap, -overlap)
            if best is None or key < best[0]:
                best = (key, other)

        return None if best is None else best[1]

    def trace(self, origin: Point, angle: float) -> Slot | None:
        """
        The first slot a ray from that cell runs into.

        This is the general answer, and `neighbour` is the cheap exact
        one for the four directions a key is bound to. Carl asked for
        both: "tracing diagonally (even on floating point angles)
        shouldn't be blocked, just not the default (integral tracing
        is cheaper)."

        The ray leaves the middle of `origin`, so it never runs along
        an edge. `angle` is in radians, zero points right, and it
        grows clockwise on the screen because `y` grows downwards. A
        slot that holds `origin` is not an answer; a tie goes to
        whichever slot went on the plane first.
        """
        start = (origin.x + 0.5, origin.y + 0.5)
        step = (math.cos(angle), math.sin(angle))
        best: tuple[float, Slot] | None = None

        for slot, rect in self.rects.items():
            if rect.holds(origin):
                continue

            when = _enters(rect, start, step)
            if when is None:
                continue

            if best is None or when < best[0]:
                best = (when, slot)

        return None if best is None else best[1]

    # ------------------------------------------------------------------
    # What order it reads in.

    def reading_order(self) -> list[Pane]:
        """
        Every pane in the order a person reads the screen.

        Columns first, left to right, and top to bottom inside a
        column. That is the order `Window.panes` already walks out of
        the tree (Lillecarl/pymux#210), recovered from the rectangles
        alone: a straight line that divides them all is a split, so
        looking for the leftmost such line and then the topmost one
        takes the tree apart again.

        **Two trees can lay out the same rectangles.** An even grid of
        four is columns of stacks or stacks of columns, and the
        rectangles cannot say which. This answers columns first, which
        is what a strip is and what the common splits are. A set that
        no straight line divides -- a pinwheel, which no split makes
        and only a bare plane can hold -- reads top left first.
        """
        return [pane for slot in _read(list(self.rects.items())) for pane in slot.panes]

    def __repr__(self) -> str:
        return "Plan(%r)" % (self.rects,)


def _enters(
    rect: Rect, start: tuple[float, float], step: tuple[float, float]
) -> float | None:
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


def _cut(items: list[tuple[Slot, Rect]], side: Side) -> int | None:
    """
    Where a straight line divides these rectangles, or `None`.

    The nearest one, which for `RIGHT` is the leftmost vertical line
    and for `BELOW` the topmost horizontal one. A line divides them
    when no rectangle straddles it and both sides hold something.
    """
    edges = sorted({rect.edge(side.opposite) for _, rect in items})

    for at in edges[1:]:
        if all(
            rect.edge(side) <= at or rect.edge(side.opposite) >= at for _, rect in items
        ):
            return at

    return None


def _read(items: list[tuple[Slot, Rect]]) -> list[Slot]:
    "The slots of these rectangles, in reading order."
    if len(items) <= 1:
        return [slot for slot, _ in items]

    for side in (Side.RIGHT, Side.BELOW):
        at = _cut(items, side)
        if at is None:
            continue

        before = [it for it in items if it[1].edge(side) <= at]
        after = [it for it in items if it[1].edge(side.opposite) >= at]
        return _read(before) + _read(after)

    return [slot for slot, rect in sorted(items, key=lambda it: (it[1].y, it[1].x))]
