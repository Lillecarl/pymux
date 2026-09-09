"""
Which pane is over this one, and which is under it.

The same question as `test_the_pane_beside_this_one.py`, turned ninety
degrees. A stack is what a niri column holds, and a person in one
cannot see what is out there any more than they could sideways.
Lillecarl/pymux#211.

The rules are the mirror of the sideways ones:

- An `HSplit` is the only split with an above and a below.
- A pane inside a row has no pane of its own above it, and takes the
  row's.
- A neighbour that is a stack gives the pane that touches us, and a row
  gives its leftmost.
"""

from pymux.arrangement import HSplit, Pane, VSplit, Window


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def a_pane():
    return Pane(terminal=_Fake())


def a_window(root):
    "A window laid out exactly this way, however that was reached."
    window = Window()
    window.root = root
    return window


def a_stack(how_many):
    "A stack of panes one above another, and the panes in order."
    panes = [a_pane() for _ in range(how_many)]
    return a_window(HSplit(panes)), panes


# ----------------------------------------------------------------------
# A stack of panes.


def test_the_middle_of_a_stack_has_a_pane_on_each_side():
    window, panes = a_stack(3)

    assert window.pane_above(panes[1]) is panes[0]
    assert window.pane_below(panes[1]) is panes[2]


def test_the_top_of_a_stack_has_nothing_above_it():
    window, panes = a_stack(2)

    assert window.pane_above(panes[0]) is None
    assert window.pane_below(panes[0]) is panes[1]


def test_the_bottom_of_a_stack_has_nothing_below_it():
    window, panes = a_stack(2)

    assert window.pane_below(panes[1]) is None


def test_one_pane_has_nothing_on_either_side():
    window, panes = a_stack(1)

    assert window.pane_above(panes[0]) is None
    assert window.pane_below(panes[0]) is None


# ----------------------------------------------------------------------
# A pane inside a row takes the row's neighbours.


def test_a_pane_in_a_row_has_no_pane_of_its_own_above_it():
    """
    A row is one thing to the stack that holds it, so every pane in it
    has the same pane above.
    """
    above = a_pane()
    left, right = a_pane(), a_pane()
    window = a_window(HSplit([above, VSplit([left, right])]))

    assert window.pane_above(left) is above
    assert window.pane_above(right) is above


def test_a_row_on_its_own_has_nothing_above_or_below():
    left, right = a_pane(), a_pane()
    window = a_window(VSplit([left, right]))

    assert window.pane_above(left) is None
    assert window.pane_below(right) is None


# ----------------------------------------------------------------------
# Which pane of a neighbour is named.


def test_a_stack_above_gives_the_pane_that_touches_us():
    "Its last pane, which is the one at the bottom of it."
    top, bottom = a_pane(), a_pane()
    under = a_pane()
    window = a_window(HSplit([HSplit([top, bottom]), under]))

    assert window.pane_above(under) is bottom


def test_a_stack_below_gives_the_pane_that_touches_us():
    over = a_pane()
    top, bottom = a_pane(), a_pane()
    window = a_window(HSplit([over, HSplit([top, bottom])]))

    assert window.pane_below(over) is top


def test_a_row_below_gives_its_leftmost_pane():
    """
    A row has no end that is nearer, so it gives the one a person's eye
    starts at, and that answer does not move when the focus inside the
    row moves.
    """
    over = a_pane()
    left, right = a_pane(), a_pane()
    window = a_window(HSplit([over, VSplit([left, right])]))

    assert window.pane_below(over) is left


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
