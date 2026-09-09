"""
What a view promises: where it goes, and where it refuses to go.

`pymux/plane.py` holds `View`, the part of the plane one client sees.
A plan is shared between the clients and a view is not, so this is the
one place a client of its own size is answered. Decision 10 of
`docs/layout-engine-plan.md`, and slice 5 of Lillecarl/pymux#217.

Three promises, and every layout leans on all three:

- **A rectangle already wholly in the view moves nothing.** Walking to
  a pane and back is a round trip. Lillecarl/pymux#207.
- **A rectangle too big to show whole shows its start**, left and top.
  Lillecarl/pymux#218.
- **The view stays on the plane**, whatever the focus asks for.

The strip owned all three before, for one axis. They are here so that
a divided window bigger than a client's screen scrolls by the same
rule, rather than by a second one that drifts from it.
"""

from hypothesis import given
from hypothesis import strategies as st
from prompt_toolkit.data_structures import Point, Size

from pymux.plane import Rect, View

#: A plane big enough that a view of twenty by five can move on it.
PLANE = Rect(x=0, y=0, width=60, height=15)


def a_view(x=0, y=0, columns=20, rows=5) -> View:
    return View(Point(x=x, y=y), Size(rows=rows, columns=columns))


# ----------------------------------------------------------------------
# What it can see.


def test_a_view_is_a_rectangle_of_the_plane():
    assert a_view(x=7, y=3).rect == Rect(x=7, y=3, width=20, height=5)


def test_a_view_shows_what_it_overlaps():
    view = a_view(x=10, y=0)
    assert view.shows(Rect(x=29, y=4, width=1, height=1))
    assert view.shows(Rect(x=5, y=0, width=10, height=1))


def test_a_view_shows_nothing_it_only_touches():
    "The right edge is one past the last cell, so touching is not seeing."
    view = a_view(x=10, y=0)
    assert not view.shows(Rect(x=30, y=0, width=4, height=1))
    assert not view.shows(Rect(x=6, y=0, width=4, height=1))
    assert not view.shows(Rect(x=10, y=5, width=4, height=1))


# ----------------------------------------------------------------------
# Where it goes.


def test_a_rectangle_already_in_the_view_moves_nothing():
    """
    The property the strip was built around. A pane the person can
    already see is not a reason to scroll. Lillecarl/pymux#207.
    """
    view = a_view(x=10)
    assert view.moved_onto(Rect(x=12, y=1, width=4, height=2), PLANE) == Point(
        x=10, y=0
    )


def test_a_rectangle_off_the_right_brings_its_left_edge_in():
    view = a_view(x=0)
    assert view.moved_onto(Rect(x=25, y=0, width=8, height=5), PLANE) == Point(
        x=25, y=0
    )


def test_a_rectangle_off_the_left_brings_its_left_edge_in():
    view = a_view(x=30)
    assert view.moved_onto(Rect(x=4, y=0, width=8, height=5), PLANE) == Point(x=4, y=0)


def test_a_rectangle_wider_than_the_view_shows_its_left_edge():
    """
    It cannot be shown whole, so one end is cut, and it is the right
    one. Carl: applications begin writing text at the left, so a
    missing right column is likely to miss nothing. Lillecarl/pymux#218.
    """
    view = a_view(x=0)
    assert view.moved_onto(Rect(x=25, y=0, width=30, height=5), PLANE).x == 25


def test_the_page_follows_the_same_rule_as_the_row():
    "Down is not a second policy. A tall pane shows its top."
    view = a_view(y=0)
    assert view.moved_onto(Rect(x=0, y=9, width=4, height=9), PLANE).y == 9


def test_nothing_to_follow_leaves_the_view_where_it_is():
    "A dialog holds the keyboard, so there is no pane to move onto."
    view = a_view(x=13, y=2)
    assert view.moved_onto(None, PLANE) == Point(x=13, y=2)


# ----------------------------------------------------------------------
# Where it refuses to go.


def test_the_view_never_passes_the_end_of_the_plane():
    "A pane that closed can leave the view out past the last cell."
    view = a_view(x=55)
    assert view.moved_onto(None, PLANE) == Point(x=40, y=0)


def test_a_plane_smaller_than_the_view_puts_it_at_the_start():
    """
    There is nowhere to scroll to, so the answer is the plane's own
    origin. This is the ordinary case: a tiling is measured to fit.
    """
    view = a_view(x=9, y=4)
    assert view.moved_onto(None, Rect(x=0, y=0, width=20, height=5)) == Point(x=0, y=0)
    assert view.moved_onto(None, Rect(x=0, y=0, width=8, height=2)) == Point(x=0, y=0)


def test_a_plane_that_starts_behind_the_origin_is_still_the_bound():
    """
    The plane is unbounded, so a plan may sit at a negative coordinate.

    Six rows of plane under five rows of view leave one row to move
    in, and the last of them is where the view is asked to be.
    """
    plane = Rect(x=-30, y=-4, width=40, height=6)
    assert a_view(x=0, y=0).moved_onto(None, plane) == Point(x=-10, y=-3)


# ----------------------------------------------------------------------
# The same three, said over any view and any plane.

VIEWS = st.builds(
    a_view,
    x=st.integers(min_value=-40, max_value=80),
    y=st.integers(min_value=-10, max_value=30),
    columns=st.integers(min_value=1, max_value=40),
    rows=st.integers(min_value=1, max_value=12),
)

RECTS = st.builds(
    Rect,
    x=st.integers(min_value=0, max_value=59),
    y=st.integers(min_value=0, max_value=14),
    width=st.integers(min_value=1, max_value=60),
    height=st.integers(min_value=1, max_value=15),
)


@given(VIEWS, RECTS)
def test_the_view_always_lands_on_the_plane(view, rect):
    "However far the focus is, the view is a part of the plane."
    view.offset = view.moved_onto(rect, PLANE)

    assert PLANE.x <= view.offset.x
    assert PLANE.y <= view.offset.y
    assert view.offset.x <= max(PLANE.x, PLANE.right - view.size.columns)
    assert view.offset.y <= max(PLANE.y, PLANE.bottom - view.size.rows)


@given(VIEWS, RECTS)
def test_moving_onto_the_same_rectangle_twice_moves_it_once(view, rect):
    """
    The frames a person does not touch anything are most of them, and
    each of them asks this question again.
    """
    view.offset = view.moved_onto(rect, PLANE)
    assert view.moved_onto(rect, PLANE) == view.offset


@given(VIEWS, RECTS)
def test_the_start_of_the_rectangle_is_shown_where_the_plane_allows(view, rect):
    """
    The point of moving at all. The top left cell of what a person is
    looking at is on the screen, unless the plane's own edge is in the
    way -- a rectangle that runs past the plane is a fault of whatever
    laid it out, and the view still refuses to leave the plane.
    """
    view.offset = view.moved_onto(rect, PLANE)

    if rect.x <= PLANE.right - view.size.columns and rect.y <= (
        PLANE.bottom - view.size.rows
    ):
        assert view.shows(Rect(x=rect.x, y=rect.y, width=1, height=1))
