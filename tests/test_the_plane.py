"""
The promises a plan makes, and the two it does not.

`pymux/plane.py` holds no layout, so what is worth judging is not what
it computes but what it guarantees. This file is that list. Every
promise here is written against *any* plan, so the classes that lay
one out (`Strip`, `Divided`, `Masonry`) import
`every_promise_holds` and hold their own `measure` to it, and a
layout that breaks one fails here rather than on somebody's screen.
Lillecarl/pymux#217, and `docs/layout-engine-plan.md`.

**Two promises are false and are written down as false**, because a
plausible one that nobody checked is how a suite starts lying:

- *"If B is to the right of A, then A is to the left of B."* False for
  any layout with panes of different sizes.
  `test_the_neighbour_of_my_neighbour_is_not_always_me` is the
  counterexample.
- *"Every slot is reachable from every other."* True of a tiling and
  false of the bare plane, where two rectangles set diagonally are
  neighbours of nothing.
  `test_a_diagonal_pair_are_not_neighbours` is that one.

A third was expected and is missing on purpose. "A slot to the left of
another comes earlier in reading order" is false, and
`test_reading_order_follows_the_splits_and_not_the_rows` shows the
shape that breaks it.
"""

import itertools
import math

from hypothesis import find, given, settings
from hypothesis import strategies as st
from prompt_toolkit.data_structures import Point

from pymux.plane import Plan, Rect, Side, Slot, bounding_box, overlap_of


class _Pane:
    """
    Enough of a pane for a plan to hold it.

    A plan asks a pane for nothing, so this needs nothing. It carries
    a name so that a failing example reads.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


# ----------------------------------------------------------------------
# Building one by hand, for the tests that name a shape.


def create_plan(**rects: Rect) -> Plan:
    "A plan of one pane per rectangle, each named by its keyword."
    return Plan({Slot(_Pane(name)): rect for name, rect in rects.items()})


def named(plan: Plan, name: str) -> Slot:
    "The slot of the pane with that name."
    for slot in plan.slots:
        if slot.shown.name == name:
            return slot
    raise KeyError(name)


def names(panes) -> list[str]:
    return [pane.name for pane in panes]


# ----------------------------------------------------------------------
# Building one by drawing, for the tests that name a promise.


def _split(draw, box: Rect, room: int) -> list[Rect]:
    """
    Cut a box into rectangles that fill it exactly.

    This is what a split does, so what comes out is the family of
    layouts pymux can already make: every cut runs the whole way
    across, and no cell is in two rectangles or in none.
    """
    ways = []
    if box.width >= 2:
        ways.append(Side.RIGHT)
    if box.height >= 2:
        ways.append(Side.BELOW)

    if not ways or room == 0 or not draw(st.booleans()):
        return [box]

    side = draw(st.sampled_from(ways))

    if side is Side.RIGHT:
        at = draw(st.integers(min_value=1, max_value=box.width - 1))
        first = Rect(box.x, box.y, at, box.height)
        second = Rect(box.x + at, box.y, box.width - at, box.height)
    else:
        at = draw(st.integers(min_value=1, max_value=box.height - 1))
        first = Rect(box.x, box.y, box.width, at)
        second = Rect(box.x, box.y + at, box.width, box.height - at)

    return _split(draw, first, room - 1) + _split(draw, second, room - 1)


@st.composite
def any_box(draw) -> Rect:
    "Somewhere on the plane, which is unbounded and may be negative."
    return Rect(
        x=draw(st.integers(min_value=-30, max_value=30)),
        y=draw(st.integers(min_value=-30, max_value=30)),
        width=draw(st.integers(min_value=1, max_value=24)),
        height=draw(st.integers(min_value=1, max_value=12)),
    )


@st.composite
def any_tiling(draw) -> list[Rect]:
    "Rectangles that fill a box exactly, the way a divided layout does."
    return _split(draw, draw(any_box()), room=3)


@st.composite
def any_scatter(draw) -> list[Rect]:
    """
    Rectangles that overlap nothing and fill nothing.

    This is the bare plane: a person put things where they wanted
    them, so there are holes, and an edge lines up with another one
    only by accident. It is made by taking a tiling apart, because
    dropping rectangles and shrinking each one where it stands keeps
    the one rule the plane has.
    """
    rects = draw(any_tiling())
    kept = []

    for rect in rects:
        if not draw(st.booleans()):
            continue
        kept.append(
            Rect(
                x=rect.x,
                y=rect.y,
                width=draw(st.integers(min_value=1, max_value=rect.width)),
                height=draw(st.integers(min_value=1, max_value=rect.height)),
            )
        )

    return kept or rects[:1]


@st.composite
def any_drawn_plan(draw, rects) -> Plan:
    "A plan of those rectangles, some of the slots tabbed."
    slots = []
    number = 0

    for rect in draw(rects):
        panes = []
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            number += 1
            panes.append(_Pane("pane %d" % number))

        slot = Slot(*panes)
        slot.showing = draw(st.integers(min_value=0, max_value=len(panes) - 1))
        slots.append((slot, rect))

    return Plan(slots)


TILINGS = any_drawn_plan(any_tiling())
SCATTERS = any_drawn_plan(any_scatter())
PLANS = st.one_of(TILINGS, SCATTERS)


# ----------------------------------------------------------------------
# The promises, in one place, for every slice after this one.


def walk(plan: Plan, slot: Slot, side: Side) -> list[Slot]:
    """
    Every slot a person reaches by pressing one direction again and
    again. It raises if the walk comes back to somewhere it has been,
    because that walk would never end.
    """
    seen = [slot]

    while True:
        slot = plan.neighbour(slot, side)
        if slot is None:
            return seen

        assert not any(slot is before for before in seen), (
            "walking %s came back to %r" % (side.name, slot)
        )
        seen.append(slot)


def _the_same_panes(one, other) -> bool:
    "True when the two hold the same panes, in any order."
    return sorted(id(pane) for pane in one) == sorted(id(pane) for pane in other)


def every_promise_holds(plan: Plan) -> None:
    """
    What a plan promises, whatever laid it out.

    Slice 2 onwards import this: a layout hands over a plan and this
    says whether it is one. It holds for the bare plane too, so it
    says nothing about holes and nothing about reaching everything.
    """
    slots = plan.slots
    rects = plan.rects

    for one, other in itertools.combinations(slots, 2):
        assert not rects[one].overlaps(rects[other]), "%r and %r share a cell" % (
            one,
            other,
        )

    for slot in slots:
        rect = rects[slot]
        assert rect.width >= 1 and rect.height >= 1, "%r has no cells" % (slot,)

        assert slot.panes, "%r owns no pane" % (slot,)
        assert 0 <= slot.showing < len(slot.panes), "%r shows nothing" % (slot,)

    box = plan.plane
    for rect in rects.values():
        assert box.x <= rect.x and rect.right <= box.right, "%r is outside %r" % (
            rect,
            box,
        )
        assert box.y <= rect.y and rect.bottom <= box.bottom, "%r is outside %r" % (
            rect,
            box,
        )

    if rects:
        # And no larger than it needs to be: something touches each side.
        assert any(rect.x == box.x for rect in rects.values())
        assert any(rect.y == box.y for rect in rects.values())
        assert any(rect.right == box.right for rect in rects.values())
        assert any(rect.bottom == box.bottom for rect in rects.values())

    for slot in slots:
        for side in Side:
            other = plan.neighbour(slot, side)
            if other is None:
                continue

            assert other is not slot, "%r is its own %s" % (slot, side.name)

            mine, theirs = rects[slot], rects[other]
            assert mine.gap_to(theirs, side) >= 0, "%r is not %s of %r" % (
                other,
                side.name,
                slot,
            )
            assert overlap_of(mine.span(side), theirs.span(side)) > 0, (
                "%r is not across from %r" % (other, slot)
            )

            walk(plan, slot, side)

    assert _the_same_panes(plan.order, plan.shown), "the numbering lost a pane"
    assert _the_same_panes(plan.reading_order(), plan.shown), "reading lost a pane"


@given(PLANS)
def test_a_plan_keeps_every_promise(plan):
    every_promise_holds(plan)


# ----------------------------------------------------------------------
# What a tiling promises on top of that.


@given(TILINGS)
def test_a_tiling_leaves_no_hole(plan):
    "Which is what makes it a tiling, and the bare plane not one."
    box = plan.plane
    covered = sum(rect.width * rect.height for rect in plan.rects.values())

    assert covered == box.width * box.height


def everything_is_reachable(plan: Plan) -> bool:
    """
    Whether the four direction keys reach every slot of the plan.

    **This is not a promise the plane makes.** On a bare plane two
    rectangles set diagonally have no neighbour at all, so nothing
    reaches the second one. It holds wherever a cut runs the whole way
    across, because then something on this side of it is across from
    something on that side, so a tiling and a strip are both held to
    it.
    """
    if not plan.slots:
        return True

    first = plan.slots[0]
    reached = {id(first)}
    edge = [first]

    while edge:
        for side in Side:
            other = plan.neighbour(edge[-1], side)
            if other is not None and id(other) not in reached:
                reached.add(id(other))
                edge.append(other)
                break
        else:
            edge.pop()

    return len(reached) == len(plan.slots)


@given(TILINGS)
def test_every_slot_of_a_tiling_is_reachable(plan):
    "A person can reach every pane with the four direction keys."
    assert everything_is_reachable(plan)


# ----------------------------------------------------------------------
# Finding what is at a cell.


@given(PLANS)
def test_at_finds_whatever_holds_a_cell(plan):
    "And says nothing for a hole, which is a real answer on a plane."
    for point in plan.plane.cells():
        holding = [slot for slot in plan.slots if plan.rects[slot].holds(point)]
        found = plan.at(point)

        assert holding == ([] if found is None else [found])


def test_a_cell_outside_everything_holds_nothing():
    plan = create_plan(A=Rect(0, 0, 4, 4))

    assert plan.at(Point(x=-1, y=0)) is None
    assert plan.at(Point(x=4, y=0)) is None
    assert plan.at(Point(x=3, y=3)) is named(plan, "A")


def test_a_pane_is_found_by_its_slot_and_a_hidden_one_too():
    """
    A pane has no rectangle of its own: its slot has one, and every
    pane of a slot gets it, shown or not. A pty needs a size, so a
    hidden pane already has the size it will have.
    """
    behind, front = _Pane("behind"), _Pane("front")
    slot = Slot(behind, front)
    slot.show(front)
    plan = Plan({slot: Rect(2, 3, 10, 5)})

    assert plan.slot_of(behind) is slot
    assert plan.rect_of(behind) == plan.rect_of(front) == Rect(2, 3, 10, 5)


def test_a_pane_that_is_not_on_the_plan_is_a_fault():
    plan = create_plan(A=Rect(0, 0, 4, 4))

    try:
        plan.slot_of(_Pane("elsewhere"))
    except KeyError:
        return
    raise AssertionError("a pane that is nowhere answered anyway")


def test_a_pane_in_two_slots_is_a_fault():
    "Because the numbering is a list of panes, and it would hold it twice."
    pane = _Pane("shared")

    try:
        Plan({Slot(pane): Rect(0, 0, 2, 2), Slot(pane): Rect(2, 0, 2, 2)})
    except ValueError:
        return
    raise AssertionError("one pane went into two slots")


# ----------------------------------------------------------------------
# Neighbours: the four keys a person presses all day.


def two_columns() -> Plan:
    return create_plan(A=Rect(0, 0, 10, 6), B=Rect(10, 0, 10, 6))


def test_the_slot_beside_this_one_is_the_one_that_touches_it():
    plan = two_columns()

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "B")
    assert plan.neighbour(named(plan, "B"), Side.LEFT) is named(plan, "A")


def test_nothing_is_beyond_the_edge():
    plan = two_columns()

    assert plan.neighbour(named(plan, "A"), Side.LEFT) is None
    assert plan.neighbour(named(plan, "A"), Side.ABOVE) is None


def test_the_nearest_one_wins():
    "Two slots across from me, and the near one answers."
    plan = create_plan(
        A=Rect(0, 0, 4, 4),
        far=Rect(20, 0, 4, 4),
        near=Rect(8, 0, 4, 4),
    )

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "near")


def test_the_widest_one_wins_a_tie():
    "Both touch my edge, so the one that shares more of it answers."
    plan = create_plan(
        A=Rect(0, 0, 4, 6),
        thin=Rect(4, 0, 4, 2),
        wide=Rect(4, 2, 4, 4),
    )

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "wide")


def test_the_first_one_on_the_plane_wins_an_even_tie():
    "Same gap and the same share of my edge, so insertion order says."
    plan = create_plan(
        A=Rect(0, 0, 4, 4),
        first=Rect(4, 0, 4, 2),
        second=Rect(4, 2, 4, 2),
    )

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "first")


def test_a_neighbour_has_to_be_across_from_me():
    """
    Strict, the way tmux is. The slot below and to the right is not
    "to the right", because none of its rows are mine.
    """
    plan = create_plan(A=Rect(0, 0, 4, 4), corner=Rect(4, 4, 4, 4))

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is None
    assert plan.neighbour(named(plan, "A"), Side.BELOW) is None


def test_touching_at_a_corner_only_is_not_across_from_me():
    "The bands share a number and no cell, which is not an overlap."
    plan = create_plan(A=Rect(0, 0, 4, 4), corner=Rect(4, -4, 4, 4))

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is None


def test_a_diagonal_pair_are_not_neighbours():
    """
    The counterexample to a promise this suite must not make.

    "Every slot is reachable from every other" holds for a tiling and
    not for the bare plane. Nothing reaches `B` here, and that is the
    strictness working: a person pressing right does not want the
    focus to jump down a row.
    """
    plan = create_plan(A=Rect(0, 0, 4, 4), B=Rect(10, 10, 4, 4))

    for side in Side:
        assert plan.neighbour(named(plan, "A"), side) is None
        assert plan.neighbour(named(plan, "B"), side) is None


def test_the_neighbour_of_my_neighbour_is_not_always_me():
    """
    The other counterexample, and the reason a symmetry promise is not
    in `every_promise_holds`.

        +-----+-----------+
        |  A  |           |
        +-----+     B     |
        |     |           |
        |  D  |           |
        +-----+-----------+

    B is to the right of both A and D, and only one of them can be to
    the left of B. This is not a fault to fix: it is what a layout
    with panes of different sizes is.
    """
    plan = create_plan(A=Rect(0, 0, 6, 2), D=Rect(0, 2, 6, 4), B=Rect(6, 0, 14, 6))

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "B")
    assert plan.neighbour(named(plan, "D"), Side.RIGHT) is named(plan, "B")
    assert plan.neighbour(named(plan, "B"), Side.LEFT) is named(plan, "D")


def test_the_plane_reaches_below_the_origin():
    "`x` and `y` may be negative, because the plane is unbounded."
    plan = create_plan(A=Rect(-20, -8, 4, 4), B=Rect(-16, -8, 4, 4))

    assert plan.neighbour(named(plan, "A"), Side.RIGHT) is named(plan, "B")
    assert plan.plane == Rect(-20, -8, 8, 4)


# ----------------------------------------------------------------------
# Tracing: the general answer, at any angle.


def test_a_ray_finds_what_it_runs_into():
    plan = two_columns()

    assert plan.trace(Point(x=0, y=3), Side.RIGHT.angle) is named(plan, "B")
    assert plan.trace(Point(x=19, y=3), Side.LEFT.angle) is named(plan, "A")


def test_a_ray_leaves_the_slot_it_starts_in():
    "Otherwise every answer is the slot that was asking."
    plan = two_columns()

    assert plan.trace(Point(x=9, y=3), Side.RIGHT.angle) is named(plan, "B")
    assert plan.trace(Point(x=0, y=3), Side.LEFT.angle) is None


def test_a_ray_goes_where_no_key_does():
    """
    What Carl asked the angle for. `B` is not a neighbour of `A` in
    any direction, and a ray at forty five degrees still finds it.
    """
    plan = create_plan(A=Rect(0, 0, 4, 4), B=Rect(10, 10, 4, 4))

    assert plan.neighbour(named(plan, "A"), Side.BELOW) is None
    assert plan.trace(Point(x=3, y=3), math.pi / 4) is named(plan, "B")


@given(PLANS)
def test_a_ray_never_beats_the_neighbour(plan):
    """
    A ray cast along a row runs into something that could have
    answered `neighbour`, so `neighbour` never picks anything further
    away. That is what makes the cheap answer and the general one one
    idea rather than two.
    """
    for slot in plan.slots:
        mine = plan.rects[slot]
        for side in Side:
            middle = Point(
                x=mine.x + mine.width // 2,
                y=mine.y + mine.height // 2,
            )
            hit = plan.trace(middle, side.angle)
            if hit is None:
                continue

            beside = plan.neighbour(slot, side)
            assert beside is not None, "a ray found %r and no key does" % (hit,)
            assert mine.gap_to(plan.rects[beside], side) <= mine.gap_to(
                plan.rects[hit], side
            )


# ----------------------------------------------------------------------
# Reading order, which is the numbering a person sees.


def test_a_row_reads_from_the_left():
    plan = create_plan(A=Rect(0, 0, 4, 6), B=Rect(4, 0, 4, 6), C=Rect(8, 0, 4, 6))

    assert names(plan.reading_order()) == ["A", "B", "C"]


def test_a_stack_reads_from_the_top():
    plan = create_plan(A=Rect(0, 0, 12, 2), B=Rect(0, 2, 12, 2), C=Rect(0, 4, 12, 2))

    assert names(plan.reading_order()) == ["A", "B", "C"]


def test_a_column_is_read_out_before_the_next_column():
    """
    The shape of `test_a_deep_tree_reads_left_to_right_and_top_to_bottom`
    in `test_the_order_of_the_panes.py`, as rectangles.
    `VSplit([HSplit([A, B]), VSplit([C, D])])` draws this, and the
    numbering it gives is the one to keep. Lillecarl/pymux#210.
    """
    plan = create_plan(
        A=Rect(0, 0, 40, 12),
        B=Rect(0, 12, 40, 12),
        C=Rect(40, 0, 20, 24),
        D=Rect(60, 0, 20, 24),
    )

    assert names(plan.reading_order()) == ["A", "B", "C", "D"]


def test_reading_order_is_not_the_order_the_slots_went_on():
    "Or it would say nothing that insertion order does not."
    plan = create_plan(B=Rect(4, 0, 4, 6), A=Rect(0, 0, 4, 6))

    assert names(plan.panes) == ["B", "A"]
    assert names(plan.reading_order()) == ["A", "B"]


def test_a_tabbed_slot_reads_out_the_pane_a_person_sees():
    """
    A stack is one thing on the screen, so it is one thing in the
    numbering. Carl: "in a stack the visible pane is the only thing to
    be concerned with (at least for now)."

    The hidden pane is still on the plan, and still has the slot's
    rectangle, so its pty has the size it will be shown at. It has no
    number until it is the one being shown.
    """
    left, behind, front = _Pane("left"), _Pane("behind"), _Pane("front")
    stack = Slot(behind, front)
    stack.show(front)
    plan = Plan({Slot(left): Rect(0, 0, 4, 6), stack: Rect(4, 0, 4, 6)})

    assert names(plan.reading_order()) == ["left", "front"]
    assert names(plan.order) == ["left", "front"]

    # And the one behind is still there, with a rectangle of its own.
    assert plan.rect_of(behind) == Rect(4, 0, 4, 6)


def test_reading_order_follows_the_splits_and_not_the_rows():
    """
    Why "a slot to the left of another comes first" is not a promise.

        +-----+-----+
        |  A  |  Y  |
        +-----+-----+
        |     S     |
        +-----+-----+
        |  X  |  B  |
        +-----+-----+

    `S` runs the whole way across, so the first cut is horizontal and
    the top row is read before the bottom one. `X` is to the left of
    `Y` and comes after it, which is exactly what the tree does today:
    `HSplit([VSplit([A, Y]), S, VSplit([X, B])])`.
    """
    plan = create_plan(
        A=Rect(0, 0, 10, 5),
        Y=Rect(10, 0, 10, 5),
        S=Rect(0, 5, 20, 5),
        X=Rect(0, 10, 10, 5),
        B=Rect(10, 10, 10, 5),
    )

    assert names(plan.reading_order()) == ["A", "Y", "S", "X", "B"]


def test_a_shape_no_split_makes_reads_from_the_top_left():
    """
    A pinwheel: no straight line divides it, so there is no tree to
    recover and the answer is the plain one. Only a bare plane can
    hold this shape.
    """
    plan = create_plan(
        A=Rect(0, 0, 2, 1),
        B=Rect(2, 0, 1, 2),
        C=Rect(1, 2, 2, 1),
        D=Rect(0, 1, 1, 2),
        E=Rect(1, 1, 1, 1),
    )

    assert names(plan.reading_order()) == ["A", "B", "D", "E", "C"]


@given(PLANS)
def test_reading_a_plan_twice_reads_it_the_same_way(plan):
    "A number that moves while nothing moves is a number nobody trusts."
    assert plan.reading_order() == plan.reading_order()


# ----------------------------------------------------------------------
# The slot: the invisible thing that owns the panes.


def test_a_slot_shows_the_first_pane_it_was_given():
    first, second = _Pane("first"), _Pane("second")

    assert Slot(first, second).shown is first


def test_a_slot_owns_at_least_one_pane():
    try:
        Slot()
    except ValueError:
        return
    raise AssertionError("a slot with no pane")


def test_showing_a_pane_brings_it_to_the_front():
    first, second = _Pane("first"), _Pane("second")
    slot = Slot(first, second)

    slot.show(second)

    assert slot.shown is second


def test_a_pane_added_to_a_slot_stacks_behind_the_one_shown():
    "Opening a tab and going to it are two things."
    first, second = _Pane("first"), _Pane("second")
    slot = Slot(first)

    slot.add(second)

    assert slot.stacked
    assert slot.shown is first


def test_a_pane_added_behind_the_one_shown_leaves_it_shown():
    """
    The path that can break the promise above: `after` puts a pane
    earlier in the stack, so the place of the one being shown moves
    and the slot has to move with it.
    """
    first, second, third = _Pane("first"), _Pane("second"), _Pane("third")
    slot = Slot(first, second)
    slot.show(second)

    slot.add(third, after=first)

    assert slot.panes == [first, third, second]
    assert slot.shown is second


def test_closing_the_pane_a_person_is_on_shows_the_one_behind_it():
    first, second = _Pane("first"), _Pane("second")
    slot = Slot(first, second)
    slot.show(second)

    slot.remove(second)

    assert slot.shown is first


def test_closing_a_pane_in_front_leaves_the_one_shown_alone():
    first, second, third = _Pane("first"), _Pane("second"), _Pane("third")
    slot = Slot(first, second, third)
    slot.show(third)

    slot.remove(first)

    assert slot.shown is third


def test_the_last_pane_of_a_slot_leaves_with_the_slot():
    """
    A slot with no pane has a rectangle and nothing to draw in it,
    which no promise here allows. So it refuses, and whatever holds
    the slot takes the slot out instead.
    """
    slot = Slot(_Pane("only"))

    try:
        slot.remove(slot.shown)
    except ValueError:
        return
    raise AssertionError("a slot emptied itself")


# ----------------------------------------------------------------------
# The examples this suite draws.


def test_the_plans_drawn_here_are_worth_drawing():
    """
    A strategy that only ever made one rectangle would leave every
    promise above true and empty, and nothing would say so. So this
    asks for the shapes the promises are about, and fails if the
    strategy cannot make one.
    """
    crowded = find(
        TILINGS,
        lambda plan: len(plan.slots) >= 4 and any(slot.stacked for slot in plan.slots),
    )
    assert crowded

    def has_a_hole(plan) -> bool:
        covered = sum(rect.width * rect.height for rect in plan.rects.values())
        return len(plan.slots) >= 2 and covered < plan.plane.width * plan.plane.height

    assert find(SCATTERS, has_a_hole)


def test_a_property_test_of_this_suite_inherits_the_profile():
    """
    The one that breaks in silence.

    A `@settings(...)` decorator reads the profile that is loaded when
    the decorator runs, which is when pytest imports the test module.
    Loading the profile any later than `tests/conftest.py` leaves
    every property test on hypothesis's own default, and nothing says
    so: the suite still passes, and it goes back to drawing a
    different test every run. `pyte` has the same guard, for the same
    reason. Lillecarl/pymux#180.
    """
    drawn = test_a_plan_keeps_every_promise._hypothesis_internal_use_settings

    assert drawn.derandomize is settings.default.derandomize


# ----------------------------------------------------------------------
# The small arithmetic, which everything above leans on.


def test_a_rectangle_ends_one_past_its_last_cell():
    rect = Rect(2, 3, 4, 5)

    assert (rect.right, rect.bottom) == (6, 8)
    assert rect.holds(Point(x=5, y=7))
    assert not rect.holds(Point(x=6, y=7))


def test_two_rectangles_that_touch_share_no_cell():
    assert not Rect(0, 0, 4, 4).overlaps(Rect(4, 0, 4, 4))
    assert Rect(0, 0, 4, 4).overlaps(Rect(3, 0, 4, 4))


def test_bands_that_only_touch_do_not_overlap():
    assert overlap_of((0, 4), (4, 8)) == 0
    assert overlap_of((0, 4), (2, 8)) == 2


def test_the_box_of_nothing_is_empty():
    assert bounding_box([]) == Rect(0, 0, 0, 0)


def test_every_direction_has_an_opposite_and_a_step():
    for side in Side:
        assert side.opposite.opposite is side
        assert side.step == Point(*side.value)
        assert side.growth in (-1, 1)
