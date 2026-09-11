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
from prompt_toolkit.data_structures import Point, Size
from test_the_plane import every_promise_holds, everything_is_reachable

from pymux.arrangement import Pane, Window
from pymux.plane import Side, View
from pymux.strip import Strip
from pymux.tiling import BORDER_HORIZONTAL, BORDER_VERTICAL, BORDER_WIDTH, Gaps

#: A window big enough that a column is wide and a stack is deep.
SIZE = Size(rows=24, columns=80)


class _Fake:
    "Enough of a terminal for the arrangement to hold a pane."


def create_pane(name: str) -> Pane:
    pane = Pane(terminal=_Fake())
    pane.chosen_name = name
    return pane


def create_strip(shape=(1, 1)):
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
        panes.append(create_pane("pane %d" % (len(panes) + 1)))
        window.add_pane(panes[-1], vsplit=True)

        for _ in range(count - 1):
            panes.append(create_pane("pane %d" % (len(panes) + 1)))
            window.add_pane(panes[-1])

    return window, panes


def create_plan(window, size=SIZE, gaps=Gaps()):
    return Strip(window, gaps).measure(size)


def where(plan, pane):
    return plan.rect_of(pane)


# ----------------------------------------------------------------------
# The row.


def test_a_column_starts_where_the_one_before_it_ends():
    "And the border the column owns is the cell between them."
    window, panes = create_strip((1, 1, 1))
    plan = create_plan(window)

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
    window, panes = create_strip((1,))

    assert where(create_plan(window), panes[0]).width == 40 - BORDER_WIDTH


def test_a_strip_of_three_runs_past_the_window():
    "Which is the whole point of the mode. The plane is unbounded."
    window, _ = create_strip((1, 1, 1))
    plan = create_plan(window)

    assert plan.plane.width > SIZE.columns


def test_every_pane_of_a_column_is_as_wide_as_the_column():
    window, panes = create_strip((3,))
    plan = create_plan(window)

    widths = {where(plan, pane).width for pane in panes}
    assert len(widths) == 1


def test_a_stack_divides_the_column_from_the_top_down():
    window, panes = create_strip((3,))
    plan = create_plan(window)

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
    window, panes = create_strip((2,))

    tight = create_plan(window, gaps=Gaps(between_panes=1))
    roomy = create_plan(window, gaps=Gaps(between_panes=2))

    assert where(tight, panes[1]).y - where(tight, panes[0]).bottom == 1
    assert where(roomy, panes[1]).y - where(roomy, panes[0]).bottom == 2

    # And the row comes out of the panes, not out of the window: the
    # column ends where it ended.
    assert where(roomy, panes[1]).bottom == where(tight, panes[1]).bottom


def test_a_plan_of_a_strip_keeps_every_promise():
    window, _ = create_strip((1, 2, 3))

    every_promise_holds(create_plan(window))


# ----------------------------------------------------------------------
# What is beside what.


def test_the_column_on_the_left_is_the_pane_on_the_left():
    window, panes = create_strip((1, 1, 1))
    plan = create_plan(window)

    beside = plan.neighbour(plan.slot_of(panes[1]), Side.LEFT)

    assert beside.shown is panes[0]


def test_nothing_is_beside_the_column_at_the_end_of_the_row():
    window, panes = create_strip((1, 1))
    plan = create_plan(window)

    assert plan.neighbour(plan.slot_of(panes[1]), Side.RIGHT) is None


def test_a_pane_of_a_stack_has_the_pane_over_it_above_it():
    window, panes = create_strip((3,))
    plan = create_plan(window)

    above = plan.neighbour(plan.slot_of(panes[1]), Side.ABOVE)
    below = plan.neighbour(plan.slot_of(panes[1]), Side.BELOW)

    assert (above.shown, below.shown) == (panes[0], panes[2])


def test_a_stack_does_not_reach_into_the_column_beside_it():
    "A column is its own stack: `above` and `below` stay inside it."
    window, panes = create_strip((1, 1))
    plan = create_plan(window)

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
    window, panes = create_strip((1, 2))
    alone, top, bottom = panes

    # `resize-pane` writes real sizes into the weights, so this is what
    # a stack looks like after a person has dragged the border up.
    column = window.root[1]
    column.weights[top] = 3
    column.weights[bottom] = 20

    plan = create_plan(window)
    beside = plan.neighbour(plan.slot_of(alone), Side.RIGHT)

    assert beside.shown is bottom


# ----------------------------------------------------------------------
# The numbering.


def test_a_strip_numbers_its_panes_the_way_a_person_reads_them():
    "Columns from the left, and a column's panes from the top."
    window, panes = create_strip((2, 2))
    plan = create_plan(window)

    assert plan.order == panes
    assert plan.reading_order() == panes


# ----------------------------------------------------------------------
# The lines the strip draws.


def lines_of(window, size=SIZE, gaps=Gaps()):
    strip = Strip(window, gaps)
    return strip.chrome(strip.measure(size))


def test_every_column_has_a_line_down_its_right():
    "The border it owns, and paid for out of its own share."
    window, _ = create_strip((1, 1))
    down = [line for line in lines_of(window) if line.char == BORDER_VERTICAL]

    assert [line.rect.x for line in down] == [39, 79]
    assert {line.rect.width for line in down} == {BORDER_WIDTH}


def test_a_line_down_a_column_runs_the_whole_height():
    """
    A column of stacked panes has one line beside it, not one for each
    pane: the panes stop at the gap between them and the line does
    not.
    """
    window, _ = create_strip((3,))
    down = [line for line in lines_of(window) if line.char == BORDER_VERTICAL]

    assert len(down) == 1
    assert (down[0].rect.y, down[0].rect.height) == (0, SIZE.rows)


def test_a_stack_has_a_line_across_every_gap_in_it():
    window, panes = create_strip((2,))
    plan = create_plan(window)
    across = [line for line in lines_of(window) if line.char == BORDER_HORIZONTAL]

    assert len(across) == 1
    assert across[0].rect.y == where(plan, panes[0]).bottom
    assert across[0].rect.height == Gaps().between_panes


def test_the_line_across_a_stack_grows_with_the_gap():
    "Two rows when a pane draws a bar below it and the next one above."
    window, _ = create_strip((2,))
    across = [
        line
        for line in lines_of(window, gaps=Gaps(between_panes=2))
        if line.char == BORDER_HORIZONTAL
    ]

    assert across[0].rect.height == 2


def test_a_row_of_lone_panes_has_no_line_across_it():
    window, _ = create_strip((1, 1))

    assert not [line for line in lines_of(window) if line.char == BORDER_HORIZONTAL]


# ----------------------------------------------------------------------
# Where the view goes.
#
# `test_strip.py` asks the same questions of `ScrollableStrip` by
# reading cells. These ask the layout on its own, which is where the
# rule lives now: the container draws what it is told.


def looking_at(window, focus, offset=0, columns=SIZE.columns, size=SIZE):
    "Where the view lands, given where it was and what has the focus."
    strip = Strip(window)
    plan = strip.measure(size)
    view = View(Point(x=offset, y=0), Size(rows=size.rows, columns=columns))

    return strip.look_at(plan, view, focus).x


def test_a_column_already_on_screen_moves_nothing():
    """
    What makes moving the focus a round trip: walk right and back, and
    the strip is where it started. Lillecarl/pymux#207.
    """
    window, panes = create_strip((1, 1, 1))

    assert looking_at(window, panes[1], offset=40) == 40


def test_the_view_follows_the_focus_to_the_right():
    window, panes = create_strip((1, 1, 1))

    # Three columns of forty cells, and a window eighty wide. The
    # third ends at 120, so the view stops at 40.
    assert looking_at(window, panes[2], offset=0) == 40


def test_the_view_follows_the_focus_back_to_the_left():
    window, panes = create_strip((1, 1, 1))

    assert looking_at(window, panes[0], offset=40) == 0


def test_a_column_owns_the_border_the_view_has_to_show():
    """
    A column is its panes and the border on its right, so the view
    goes one cell further than the pane needs. Without that the border
    of the focused column sits just off the screen.
    """
    window, panes = create_strip((1, 1))

    # Two columns fit exactly at eighty. At seventy nine the second
    # column's border is the cell that does not, so the view moves by
    # one and not by none.
    assert looking_at(window, panes[1], offset=0, columns=79) == 1


def test_the_view_never_passes_the_end_of_the_row():
    window, panes = create_strip((1, 1))

    assert looking_at(window, panes[0], offset=99) == 0


def test_the_view_never_starts_before_the_row():
    window, panes = create_strip((1, 1, 1))

    assert looking_at(window, panes[0], offset=-5) == 0


def test_a_column_wider_than_the_view_shows_its_left_edge():
    """
    It cannot be shown whole, so one end is cut, and the left end is
    the one to keep.

    Carl: "left should generally be preferred for terminals since
    that's where ~100% of applications begin writing text, it's even
    likely that a missing right column doesn't miss anything."
    Lillecarl/pymux#218.

    The rule this was moved from said the same in a comment and did
    the opposite, and the test that judged it could not tell: every
    cell it read held the same letter either way. This reads the
    offset, so it can.
    """
    window, panes = create_strip((1, 1))
    window.column_widths[window.root[1]] = 1.0

    # The second column is the whole window wide, at 40 to 119, and
    # the view is forty cells. Its left edge is at 40, so that is
    # where the view goes, and cells 80 to 119 of it are cut.
    assert looking_at(window, panes[1], offset=0, columns=40) == 40


def test_a_column_that_fits_is_still_shown_whole():
    "The rule only changes for a column that cannot be shown whole."
    window, panes = create_strip((1, 1))

    # Two columns of half an eighty cell window, so the second runs
    # from 40 to 79 and needs the view at nothing but the origin.
    assert looking_at(window, panes[1], offset=0, columns=80) == 0

    # And in a view of sixty cells it does not fit at the origin, so
    # the view moves far enough to show its end and no further.
    assert looking_at(window, panes[1], offset=0, columns=60) == 20


def test_nothing_focused_leaves_the_view_where_it_is():
    "A command line or a dialog has the keyboard, and the view holds."
    window, _ = create_strip((1, 1, 1))

    assert looking_at(window, None, offset=20) == 20


# ----------------------------------------------------------------------
# Any strip a person can build.

ANY_SHAPE = st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=4)
ANY_SIZE = st.builds(
    Size,
    rows=st.integers(min_value=4, max_value=60),
    columns=st.integers(min_value=10, max_value=200),
)


@given(ANY_SHAPE, ANY_SIZE, st.integers(min_value=1, max_value=2))
def test_any_strip_makes_a_plan_that_keeps_every_promise(shape, size, between):
    "The promises of slice 1, cashed by the first layout that makes a plan."
    window, _ = create_strip(shape)

    every_promise_holds(create_plan(window, size, Gaps(between_panes=between)))


@given(ANY_SHAPE, ANY_SIZE)
def test_the_four_keys_reach_every_pane_of_create_strip(shape, size):
    """
    A strip is not a tiling -- the borders between its columns are
    holes -- but it is joined up, so a person can always walk to every
    pane.
    """
    window, _ = create_strip(shape)

    assert everything_is_reachable(create_plan(window, size))
