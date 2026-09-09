"""
The plan a strip makes: where every pane of the row is.

`Strip` is the first layout to answer with a `Plan`, and it is the
hardest one pymux has, which is why it goes first: the row may be
wider than the screen, so the screen cannot answer "which pane is
beside this one". Lillecarl/pymux#217.

This file judges the plan on its own, in cells. Two others hold it to
something outside itself: `test_the_plane.py` holds every plan to the
promises every layout makes, and
`test_the_plan_matches_the_frame.py` holds this plan against the frame
prompt_toolkit actually draws.
"""

from hypothesis import given
from hypothesis import strategies as st
from prompt_toolkit.data_structures import Size
from test_the_plane import every_promise_holds, everything_is_reachable

from pymux.arrangement import Pane, Window
from pymux.plane import Side
from pymux.strip import BORDER_WIDTH, Gaps, Strip

#: A window big enough that a column is wide and a stack is deep.
SIZE = Size(rows=24, columns=80)


class _Fake:
    "Enough of a terminal for the arrangement to hold a pane."


def a_pane(name: str) -> Pane:
    pane = Pane(terminal=_Fake())
    pane.chosen_name = name
    return pane


def a_strip(shape=(1, 1)):
    """
    A strip window, and its panes.

    One number per column, saying how many panes are stacked in it.
    The panes are opened the way a person opens them: a horizontal
    split makes a column, and a vertical one stacks inside the column.
    """
    window = Window()
    window.strip = True
    panes = []

    for count in shape:
        panes.append(a_pane("pane %d" % (len(panes) + 1)))
        window.add_pane(panes[-1], vsplit=True)

        for _ in range(count - 1):
            panes.append(a_pane("pane %d" % (len(panes) + 1)))
            window.add_pane(panes[-1])

    return window, panes


def a_plan(window, size=SIZE, gaps=Gaps()):
    return Strip(window, gaps).measure(size)


def where(plan, pane):
    return plan.rect_of(pane)


# ----------------------------------------------------------------------
# The row.


def test_a_column_starts_where_the_one_before_it_ends():
    "And the border the column owns is the cell between them."
    window, panes = a_strip((1, 1, 1))
    plan = a_plan(window)

    first, second, third = (where(plan, pane) for pane in panes)

    assert first.x == 0
    assert second.x == first.right + BORDER_WIDTH
    assert third.x == second.right + BORDER_WIDTH


def test_a_column_takes_its_share_of_the_window_less_its_border():
    """
    Half a window by default, which is niri's default and the reason
    two columns fit exactly and a third runs past the edge.
    `layout._create_strip` measures a column the same way.
    """
    window, panes = a_strip((1,))

    assert where(a_plan(window), panes[0]).width == 40 - BORDER_WIDTH


def test_a_strip_of_three_runs_past_the_window():
    "Which is the whole point of the mode. The plane is unbounded."
    window, _ = a_strip((1, 1, 1))
    plan = a_plan(window)

    assert plan.plane.width > SIZE.columns


def test_every_pane_of_a_column_is_as_wide_as_the_column():
    window, panes = a_strip((3,))
    plan = a_plan(window)

    widths = {where(plan, pane).width for pane in panes}
    assert len(widths) == 1


def test_a_stack_divides_the_column_from_the_top_down():
    window, panes = a_strip((3,))
    plan = a_plan(window)

    top, middle, bottom = (where(plan, pane) for pane in panes)

    assert top.y == 0
    assert middle.y == top.bottom + Gaps().between_panes
    assert bottom.y == middle.bottom + Gaps().between_panes


def test_the_bar_below_a_pane_costs_the_stack_a_row_each_time():
    """
    Two rows between stacked panes when that bar is drawn: the lower
    pane hangs its title bar in one and the upper one hangs the bar
    below in the other. `layout._create_split` leaves the same gap.
    Lillecarl/pymux#211.
    """
    window, panes = a_strip((2,))

    tight = a_plan(window, gaps=Gaps(between_panes=1))
    roomy = a_plan(window, gaps=Gaps(between_panes=2))

    assert where(tight, panes[1]).y - where(tight, panes[0]).bottom == 1
    assert where(roomy, panes[1]).y - where(roomy, panes[0]).bottom == 2

    # And the row comes out of the panes, not out of the window: the
    # column ends where it ended.
    assert where(roomy, panes[1]).bottom == where(tight, panes[1]).bottom


def test_a_plan_of_a_strip_keeps_every_promise():
    window, _ = a_strip((1, 2, 3))

    every_promise_holds(a_plan(window))


# ----------------------------------------------------------------------
# What is beside what.


def test_the_column_on_the_left_is_the_pane_on_the_left():
    window, panes = a_strip((1, 1, 1))
    plan = a_plan(window)

    beside = plan.neighbour(plan.slot_of(panes[1]), Side.LEFT)

    assert beside.shown is panes[0]


def test_nothing_is_beside_the_column_at_the_end_of_the_row():
    window, panes = a_strip((1, 1))
    plan = a_plan(window)

    assert plan.neighbour(plan.slot_of(panes[1]), Side.RIGHT) is None


def test_a_pane_of_a_stack_has_the_pane_over_it_above_it():
    window, panes = a_strip((3,))
    plan = a_plan(window)

    above = plan.neighbour(plan.slot_of(panes[1]), Side.ABOVE)
    below = plan.neighbour(plan.slot_of(panes[1]), Side.BELOW)

    assert (above.shown, below.shown) == (panes[0], panes[2])


def test_a_stack_does_not_reach_into_the_column_beside_it():
    "A column is its own stack: `above` and `below` stay inside it."
    window, panes = a_strip((1, 1))
    plan = a_plan(window)

    for pane in panes:
        assert plan.neighbour(plan.slot_of(pane), Side.ABOVE) is None
        assert plan.neighbour(plan.slot_of(pane), Side.BELOW) is None


def test_the_pane_beside_a_stack_is_the_one_sharing_most_of_its_edge():
    """
    **This is the answer the two older mechanisms disagreed about.**
    The tree named the top pane of the stack beside us, whatever its
    size, and the frame named whichever pane our own middle row ran
    into. The plan names the one that shares most of our edge, which
    is decision 7 of `docs/layout-engine-plan.md`.

    Here the second pane of the stack is much the taller, so it is
    what a person means by "the pane on my right".
    """
    window, panes = a_strip((1, 2))
    alone, top, bottom = panes

    # `resize-pane` writes real sizes into the weights, so this is what
    # a stack looks like after a person has dragged the border up.
    column = window.root[1]
    column.weights[top] = 3
    column.weights[bottom] = 20

    plan = a_plan(window)
    beside = plan.neighbour(plan.slot_of(alone), Side.RIGHT)

    assert beside.shown is bottom


# ----------------------------------------------------------------------
# The numbering.


def test_a_strip_numbers_its_panes_the_way_a_person_reads_them():
    "Columns from the left, and a column's panes from the top."
    window, panes = a_strip((2, 2))
    plan = a_plan(window)

    assert plan.order == panes
    assert plan.reading_order() == panes


# ----------------------------------------------------------------------
# Any strip a person can build.

A_SHAPE = st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=4)
A_SIZE = st.builds(
    Size,
    rows=st.integers(min_value=4, max_value=60),
    columns=st.integers(min_value=10, max_value=200),
)


@given(A_SHAPE, A_SIZE, st.integers(min_value=1, max_value=2))
def test_any_strip_makes_a_plan_that_keeps_every_promise(shape, size, between):
    "The promises of slice 1, cashed by the first layout that makes a plan."
    window, _ = a_strip(shape)

    every_promise_holds(a_plan(window, size, Gaps(between_panes=between)))


@given(A_SHAPE, A_SIZE)
def test_the_four_keys_reach_every_pane_of_a_strip(shape, size):
    """
    A strip is not a tiling -- the borders between its columns are
    holes -- but it is joined up, so a person can always walk to every
    pane.
    """
    window, _ = a_strip(shape)

    assert everything_is_reachable(a_plan(window, size))
