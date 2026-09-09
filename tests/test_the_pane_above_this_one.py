"""
Which pane is over this one, and which is under it.

The same question as `test_the_pane_beside_this_one.py`, turned ninety
degrees, and the same plan answers it. A stack is what a niri column
holds, and a person in one cannot see what is out there any more than
they could sideways. Lillecarl/pymux#211.

The rule is the sideways rule, turned: the pane above is the one
beyond our top edge that shares most of our width, and nearest wins
first. A pane inside a row therefore takes the row's neighbour, and a
row below us gives its leftmost pane.
"""

from pymux.arrangement import HSplit, VSplit
from pymux.plane import Side
from test_the_pane_beside_this_one import a_pane, a_window, beside


def above(window, pane):
    return beside(window, pane, Side.ABOVE)


def below(window, pane):
    return beside(window, pane, Side.BELOW)


def a_stack(how_many):
    "A stack of panes one above another, and the panes in order."
    panes = [a_pane() for _ in range(how_many)]
    return a_window(HSplit(panes)), panes


# ----------------------------------------------------------------------
# A stack of panes.


def test_the_middle_of_a_stack_has_a_pane_on_each_side():
    window, panes = a_stack(3)

    assert above(window, panes[1]) is panes[0]
    assert below(window, panes[1]) is panes[2]


def test_the_top_of_a_stack_has_nothing_above_it():
    window, panes = a_stack(2)

    assert above(window, panes[0]) is None
    assert below(window, panes[0]) is panes[1]


def test_the_bottom_of_a_stack_has_nothing_below_it():
    window, panes = a_stack(2)

    assert below(window, panes[1]) is None


def test_one_pane_has_nothing_on_either_side():
    window, panes = a_stack(1)

    assert above(window, panes[0]) is None
    assert below(window, panes[0]) is None


# ----------------------------------------------------------------------
# A pane inside a row takes the row's neighbours.


def test_a_pane_in_a_row_has_no_pane_of_its_own_above_it():
    """
    The pane over the row runs the whole width of it, so it is across
    from every pane in the row.
    """
    over = a_pane()
    left, right = a_pane(), a_pane()
    window = a_window(HSplit([over, VSplit([left, right])]))

    assert above(window, left) is over
    assert above(window, right) is over


def test_a_row_on_its_own_has_nothing_above_or_below():
    left, right = a_pane(), a_pane()
    window = a_window(VSplit([left, right]))

    assert above(window, left) is None
    assert below(window, right) is None


# ----------------------------------------------------------------------
# Which pane of a neighbour is named.


def test_a_stack_above_gives_the_pane_that_touches_us():
    "Its last pane, which is the one at the bottom of it."
    top, bottom = a_pane(), a_pane()
    under = a_pane()
    window = a_window(HSplit([HSplit([top, bottom]), under]))

    assert above(window, under) is bottom


def test_a_stack_below_gives_the_pane_that_touches_us():
    over = a_pane()
    top, bottom = a_pane(), a_pane()
    window = a_window(HSplit([over, HSplit([top, bottom])]))

    assert below(window, over) is top


def test_a_row_below_gives_the_pane_that_shares_most_of_our_width():
    """
    Which for an even row of two is the left one, by the odd cell the
    division gives it. The answer does not move when the focus inside
    the row moves, which is what a title bar wants.
    """
    over = a_pane()
    left, right = a_pane(), a_pane()
    window = a_window(HSplit([over, VSplit([left, right])]))

    assert below(window, over) is left


# ----------------------------------------------------------------------
# Whether the window has a stack at all.


def test_one_pane_is_not_a_stack():
    window = a_window(HSplit([a_pane()]))

    assert not window.has_a_stack()


def test_a_row_is_not_a_stack():
    window = a_window(VSplit([a_pane(), a_pane()]))

    assert not window.has_a_stack()


def test_two_panes_one_above_another_are_a_stack():
    window, _ = a_stack(2)

    assert window.has_a_stack()


def test_a_stack_inside_a_column_of_a_strip_counts():
    "Which is the case the bar under a pane was asked for."
    window = a_window(VSplit([HSplit([a_pane()]), HSplit([a_pane(), a_pane()])]))

    assert window.has_a_stack()


def test_a_strip_of_one_pane_columns_is_not_a_stack():
    window = a_window(VSplit([HSplit([a_pane()]), HSplit([a_pane()])]))

    assert not window.has_a_stack()
