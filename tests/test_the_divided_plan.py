"""
The plan a divided layout makes: where every pane of the tree is.

`Divided` is what pymux does unless a person asks for something else,
so this is the layout the other tests in this suite have been drawing
all along. It answers with a `Plan` now, the way `Strip` does, and
`PlanContainer` draws it. Lillecarl/pymux#217.

This file judges the plan on its own, in cells. `test_the_plane.py`
holds it to the promises every layout makes, and the cell tests
elsewhere in this suite hold what is drawn against what a person sees.
"""

from hypothesis import given
from hypothesis import strategies as st
from prompt_toolkit.data_structures import Point, Size
from test_the_plane import every_promise_holds, everything_is_reachable

from pymux.arrangement import Pane, Window
from pymux.divided import Divided
from pymux.plane import View
from pymux.tiling import BORDER_HORIZONTAL, BORDER_VERTICAL, BORDER_WIDTH, Gaps

#: A window big enough that a pane is wide and a stack is deep.
SIZE = Size(rows=24, columns=80)


class _Fake:
    "Enough of a terminal for the arrangement to hold a pane."


def create_pane(name: str) -> Pane:
    pane = Pane(terminal=_Fake())
    pane.chosen_name = name
    return pane


def create_window(splits=(), focus=None):
    """
    A divided window, and its panes.

    One entry per split, after the first pane: `True` opens a pane
    beside the one that has the focus and `False` opens one under it,
    which is what `split-window -h` and `split-window -v` do. `focus`
    says which pane each split happens on; without it every split
    happens on the pane the one before it made.
    """
    window = Window()
    panes = [create_pane("pane 1")]
    window.add_pane(panes[0])

    for step, sideways in enumerate(splits):
        if focus is not None:
            window.active_pane = panes[focus[step] % len(panes)]

        panes.append(create_pane("pane %d" % (len(panes) + 1)))
        window.add_pane(panes[-1], vsplit=sideways)

    return window, panes


def create_plan(window, size=SIZE, gaps=Gaps()):
    return Divided(window, gaps).measure(size)


def where(plan, pane):
    return plan.rect_of(pane)


# ----------------------------------------------------------------------
# Dividing the view.


def test_two_panes_side_by_side_share_the_width():
    "And the cell between them is the border, which is nobody's."
    window, panes = create_window([True])
    left, right = (where(create_plan(window), pane) for pane in panes)

    assert (left.x, left.width) == (0, 40)
    assert (right.x, right.width) == (41, 39)
    assert right.x == left.right + BORDER_WIDTH
    assert left.height == right.height == SIZE.rows


def test_two_panes_stacked_share_the_height():
    window, panes = create_window([False])
    top, bottom = (where(create_plan(window), pane) for pane in panes)

    assert (top.y, top.height) == (0, 12)
    assert (bottom.y, bottom.height) == (13, 11)
    assert top.width == bottom.width == SIZE.columns


def test_the_tiling_fills_the_view_exactly():
    """
    Which is the whole difference from a strip: every cell of the view
    is a pane or a border, and nothing runs past the edge.
    """
    window, _ = create_window([True, True, False])
    plan = create_plan(window)

    assert plan.plane == plan.plane._replace(x=0, y=0)
    assert plan.plane.width == SIZE.columns
    assert plan.plane.height == SIZE.rows


def test_the_weights_say_who_gets_the_room():
    "A weight is a share of the whole, and `resize-pane` writes them."
    window, panes = create_window([True])
    row = window.root[0]
    row.weights[panes[0]] = 3
    row.weights[panes[1]] = 1

    left, right = (where(create_plan(window), pane) for pane in panes)

    # Three quarters of the 79 cells the border leaves, and the cell
    # that does not divide goes to the pane the division shortchanged
    # most, which is the narrow one.
    assert (left.width, right.width) == (59, 20)


def test_a_bigger_gap_comes_out_of_the_panes():
    """
    The view is the same size, so the room for a bar is paid by them.

    The panes give up the row, and they do not move: the tiling still
    ends where the view ends, so the pane above is the one that loses
    a row and the pane below starts where it started.
    """
    window, panes = create_window([False])

    tight = create_plan(window, gaps=Gaps(between_panes=1))
    roomy = create_plan(window, gaps=Gaps(between_panes=2))

    assert where(roomy, panes[0]).height == where(tight, panes[0]).height - 1
    assert where(roomy, panes[1]).y == where(tight, panes[1]).y
    assert roomy.plane.height == tight.plane.height == SIZE.rows


def test_the_same_window_measures_the_same_twice():
    "Nothing here remembers a frame, so nothing drifts."
    window, panes = create_window([True, False, True])

    assert len(create_plan(window).rects) == len(panes)
    for pane in panes:
        assert where(create_plan(window), pane) == where(create_plan(window), pane)


# ----------------------------------------------------------------------
# The lines it draws in the gaps.


def lines_of(window, size=SIZE, gaps=Gaps()):
    layout = Divided(window, gaps)
    return layout.chrome(layout.measure(size))


def test_a_border_fills_the_gap_between_two_panes():
    window, panes = create_window([True])
    plan = create_plan(window)
    (line,) = lines_of(window)

    assert line.char == BORDER_VERTICAL
    assert line.rect.x == where(plan, panes[0]).right
    assert line.rect.width == BORDER_WIDTH
    assert (line.rect.y, line.rect.height) == (0, SIZE.rows)


def test_a_border_between_stacked_panes_runs_across_the_pane():
    window, panes = create_window([False])
    plan = create_plan(window)
    (line,) = lines_of(window)

    assert line.char == BORDER_HORIZONTAL
    assert line.rect.y == where(plan, panes[0]).bottom
    assert line.rect.height == Gaps().between_panes
    assert (line.rect.x, line.rect.width) == (0, SIZE.columns)


def test_a_border_stops_where_the_split_that_left_it_stops():
    """
    A pane across the top, and two beside each other under it. The
    line between those two belongs to their own split, so it starts
    where they start and not at the top of the window.

    That is what the padding of a `VSplit` did when prompt_toolkit
    divided the window, and it is what a person sees: a border that
    ran the whole height would cut the pane above in half.
    """
    window, panes = create_window([False, True], focus=[0, 1])
    plan = create_plan(window)

    across = [line for line in lines_of(window) if line.char == BORDER_HORIZONTAL]
    down = [line for line in lines_of(window) if line.char == BORDER_VERTICAL]

    assert len(across) == 1 and len(down) == 1
    assert down[0].rect.y == where(plan, panes[1]).y
    assert down[0].rect.height == where(plan, panes[1]).height
    assert down[0].rect.y > 0


def test_a_gap_holds_no_pane():
    "Every line runs where no rectangle is."
    window, _ = create_window([True, False, True])
    plan = create_plan(window)

    for line in lines_of(window):
        for rect in plan.rects.values():
            assert not line.rect.overlaps(rect), (line, rect)


# ----------------------------------------------------------------------
# What it says about the view.


def test_a_view_as_big_as_the_plane_never_moves():
    """
    A tiling is measured to fit, so there is nothing to scroll to, and
    a view that has wandered comes back to the origin.
    """
    window, panes = create_window([True, True])
    plan = create_plan(window)
    layout = Divided(window)
    view = View(Point(x=5, y=5), SIZE)

    for pane in panes + [None]:
        assert layout.look_at(plan, view, pane) == Point(x=0, y=0)


def test_a_view_smaller_than_the_plane_follows_the_focus():
    """
    `window-size largest` measures the plane for the biggest client,
    so a smaller one moves its view over it rather than being stuck at
    the top left.
    """
    window, panes = create_window([True, True])
    plan = create_plan(window)
    layout = Divided(window)
    view = View(Point(x=0, y=0), Size(rows=SIZE.rows, columns=SIZE.columns // 4))

    on_the_right = layout.look_at(plan, view, panes[1])
    assert on_the_right.x == plan.rect_of(panes[1]).x
    # And it stays on the plane.
    assert on_the_right.x <= plan.plane.right - view.size.columns


# ----------------------------------------------------------------------
# Numbering.


def test_the_panes_are_numbered_the_way_a_person_reads_them():
    """
    The walk takes each split's children in order, so the plan numbers
    the panes exactly as `Window.panes` lists them, which is what
    `select-pane -t` and every title bar use. Lillecarl/pymux#210.
    """
    window, _ = create_window([True, False, True, False], focus=[0, 0, 1, 2])
    plan = create_plan(window)

    assert [pane.name for pane in plan.order] == [pane.name for pane in window.panes]


# ----------------------------------------------------------------------
# A window too small for what is in it.


def test_every_pane_keeps_a_cell_in_a_window_that_is_too_small():
    """
    Six panes stacked in four rows. Each one keeps a row, so the
    tiling runs past the bottom of the view rather than giving a pane
    no cells to draw in.

    tmux does the same: `layout_resize`'s own comment says a window
    can be smaller than its layout, and `resize_window` grows the
    window rather than shrinking the layout.
    """
    window, panes = create_window([False] * 5)
    plan = create_plan(window, Size(rows=4, columns=20))

    assert all(where(plan, pane).height >= 1 for pane in panes)
    assert plan.plane.height > 4


def test_a_deep_tree_in_a_tiny_window_lays_nothing_on_anything():
    """
    The case that made `lay_out` answer with the room it took. A split
    that runs past its own rectangle used to start its next sibling
    where the arithmetic said, which is inside the pane that had just
    overflowed. Two slots then shared cells, and that is the one
    promise everything else rests on.
    """
    window, _ = create_window([False, True, False, True, False], focus=[0, 1, 1, 3, 3])

    every_promise_holds(create_plan(window, Size(rows=3, columns=6)))


# ----------------------------------------------------------------------
# Any window a person can build.

ANY_TREE = st.lists(st.booleans(), min_size=0, max_size=6)
ANY_FOCUS = st.lists(st.integers(min_value=0, max_value=9), min_size=6, max_size=6)
ANY_SIZE = st.builds(
    Size,
    rows=st.integers(min_value=2, max_value=60),
    columns=st.integers(min_value=2, max_value=200),
)


@given(ANY_TREE, ANY_FOCUS, ANY_SIZE, st.integers(min_value=1, max_value=2))
def test_any_window_makes_a_plan_that_keeps_every_promise(tree, focus, size, between):
    "The promises of slice 1, cashed by the second layout that makes a plan."
    window, _ = create_window(tree, focus)

    every_promise_holds(create_plan(window, size, Gaps(between_panes=between)))


@given(ANY_TREE, ANY_FOCUS)
def test_every_pane_of_a_tiling_is_reachable(tree, focus):
    """
    A person reaches every pane with the four direction keys.

    At a size that fits, because a window too small for its panes is
    not a tiling any more: the panes that run past the edge keep their
    cells and the rows they were meant to have are not there.
    """
    window, _ = create_window(tree, focus)

    assert everything_is_reachable(create_plan(window))


@given(ANY_TREE, ANY_FOCUS)
def test_a_tiling_that_fits_covers_the_view_and_no_more(tree, focus):
    "The property that separates this layout from a strip."
    plan = create_plan(create_window(tree, focus)[0])

    assert (plan.plane.x, plan.plane.y) == (0, 0)
    assert (plan.plane.width, plan.plane.height) == (SIZE.columns, SIZE.rows)
