"""
Which pane is to the left of this one, and which is to the right.

A title bar names its neighbours, so that a person can see what is out
there without it being drawn. Lillecarl/pymux#207.

**The tree answers, not the last render.** `_move_focus` asks the same
question geometrically: it steps one cell past a pane's edge and looks
up which pane was drawn there. That is right for a key press, which
happens between two frames. A title bar is drawn *during* a frame, so
the positions it could read are the frame before -- and on the first
frame there are none at all.

The rules the tests below hold to:

- A `VSplit` is the only split with a left and a right.
- A pane inside a stack has no neighbour of its own, and takes the
  stack's.
- A neighbour that is a column of panes gives the pane that touches
  us, and a stack gives its top.
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


def a_row(how_many):
    "A row of panes side by side, and the panes in order."
    panes = [a_pane() for _ in range(how_many)]
    return a_window(VSplit(panes)), panes


# ----------------------------------------------------------------------
# A row of panes.


def test_the_middle_of_a_row_has_a_pane_on_each_side():
    window, panes = a_row(3)

    assert window.pane_to_the_left(panes[1]) is panes[0]
    assert window.pane_to_the_right(panes[1]) is panes[2]


def test_the_ends_of_a_row_have_one_side_each():
    window, panes = a_row(3)

    assert window.pane_to_the_left(panes[0]) is None
    assert window.pane_to_the_right(panes[0]) is panes[1]

    assert window.pane_to_the_left(panes[2]) is panes[1]
    assert window.pane_to_the_right(panes[2]) is None


def test_a_lone_pane_has_neither():
    window, panes = a_row(1)

    assert window.pane_to_the_left(panes[0]) is None
    assert window.pane_to_the_right(panes[0]) is None


def test_a_pane_that_is_the_whole_window_has_neither():
    "A window with no split at all, which is what a new one is."
    window = Window()
    window.add_pane(a_pane())

    assert window.pane_to_the_left(window.active_pane) is None
    assert window.pane_to_the_right(window.active_pane) is None


# ----------------------------------------------------------------------
# A column that is a stack of panes, which is what a niri column is.


def test_a_pane_in_a_stack_takes_the_stack_s_neighbours():
    """
    The panes of a stack sit above one another, so none of them is to
    the left of another. The question walks up to the column.
    """
    left, top, bottom, right = a_pane(), a_pane(), a_pane(), a_pane()
    window = a_window(VSplit([left, HSplit([top, bottom]), right]))

    for pane in (top, bottom):
        assert window.pane_to_the_left(pane) is left
        assert window.pane_to_the_right(pane) is right


def test_a_stack_beside_us_is_named_by_its_top_pane():
    """
    A stack has no pane that is nearer to us than the others, so it
    gives the one a person's eye starts on. It also does not move when
    the focus inside that stack moves, which a title bar wants.
    """
    top, bottom, alone = a_pane(), a_pane(), a_pane()
    window = a_window(VSplit([HSplit([top, bottom]), alone]))

    assert window.pane_to_the_left(alone) is top


# ----------------------------------------------------------------------
# A neighbour that is itself a row.


def test_a_row_beside_us_gives_the_pane_that_touches_us():
    """
    The pane to name is the one against our own edge: the rightmost of
    a column on our left, and the leftmost of a column on our right.
    """
    first, second, middle, third, fourth = (a_pane() for _ in range(5))
    window = a_window(
        VSplit([VSplit([first, second]), middle, VSplit([third, fourth])])
    )

    assert window.pane_to_the_left(middle) is second
    assert window.pane_to_the_right(middle) is third


# ----------------------------------------------------------------------
# The way a person reaches these shapes.


def test_splitting_a_strip_gives_each_column_its_neighbours():
    "What `split-window -h` builds, three times over."
    window = Window()
    opened = [a_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(2):
        opened.append(a_pane())
        window.add_pane(opened[-1], vsplit=True)

    assert window.pane_to_the_left(opened[1]) is opened[0]
    assert window.pane_to_the_right(opened[1]) is opened[2]
    assert window.pane_to_the_right(opened[2]) is None
