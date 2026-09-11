"""
A window laid out as a strip.

`set-window-option strip on` lays this window's panes out as a row that
may run past the edge of the screen, the way niri's scrollable tiling
works, instead of dividing the window between them.

**It is one window's option, and it is off.** Every other layout is
what it was, the rendering path it uses is untouched, and
`select-layout` turns the strip off again, so there is always a way
back. These tests say all three of those things, because "not the only
mode" is the requirement and not a side effect.

Lillecarl/pymux#198.
"""

from pymux.arrangement import (
    DEFAULT_COLUMN_WIDTH,
    HSplit,
    LayoutTypes,
    Pane,
    VSplit,
    Window,
)


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def create_pane():
    return Pane(terminal=_Fake())


def create_window(panes=1):
    "A window with this many panes, laid out the way pymux starts."
    window = Window()
    for _ in range(panes):
        window.add_pane(create_pane())
    return window


# ----------------------------------------------------------------------
# It is off, and it stays off.


def test_a_window_is_not_a_strip():
    assert create_window().strip is False


def test_the_root_of_a_window_is_what_it_always_was():
    "Nothing about the shape of a window changes until it is asked for."
    window = create_window(2)

    assert isinstance(window.root, HSplit)


def test_a_strip_can_be_turned_off_again():
    window = create_window(2)
    window.strip = True

    window.strip = False

    assert window.strip is False


def test_choosing_a_layout_leaves_the_strip():
    """
    All five of them divide the window between the panes, which is the
    one thing a strip does not do, so asking for one is asking to
    leave.
    """
    window = create_window(3)
    window.strip = True

    window.select_layout(LayoutTypes.TILED)

    assert window.strip is False


# ----------------------------------------------------------------------
# The shape a strip needs.


def test_turning_it_on_makes_the_root_the_row():
    "The columns of a strip are the children of the root."
    window = create_window(2)

    window.strip = True

    assert isinstance(window.root, VSplit)


def test_what_was_in_the_window_becomes_one_column():
    "So nothing on screen moves except the way it is laid out."
    window = create_window(2)
    was = window.root

    window.strip = True

    assert list(window.root) == [was]


def test_a_root_that_is_already_a_row_is_left_alone():
    "Two panes, because one of them is always laid out the other way."
    window = create_window(2)
    window.select_layout(LayoutTypes.EVEN_VERTICAL)
    assert isinstance(window.root, VSplit)
    was = list(window.root)

    window.strip = True

    assert list(window.root) == was


def test_a_window_with_no_pane_becomes_an_empty_row():
    "Wrapping an empty split would give a column with nothing in it."
    window = Window()

    window.strip = True

    assert isinstance(window.root, VSplit)
    assert list(window.root) == []


def test_every_pane_is_still_there():
    window = create_window(3)
    panes = window.panes

    window.strip = True

    assert window.panes == panes


# ----------------------------------------------------------------------
# The widths.


def test_a_column_takes_half_the_window():
    """
    niri's own default, and the reason two columns exactly fill the
    screen while a third one pushes past the edge.
    """
    window = create_window(2)
    window.strip = True

    assert window.column_width(window.root[0]) == DEFAULT_COLUMN_WIDTH
    assert DEFAULT_COLUMN_WIDTH == 1 / 2


def test_a_column_can_be_given_a_width_of_its_own():
    window = create_window(2)
    window.strip = True
    column = window.root[0]

    window.column_widths[column] = 1 / 3

    assert window.column_width(column) == 1 / 3


# ----------------------------------------------------------------------
# Rebuilding the layout.


def test_turning_it_on_changes_the_hash_that_rebuilds_the_layout():
    """
    Turning the mode on changes how the same panes are laid out and
    nothing else. Without it in the hash the layout would not be
    rebuilt, and the mode would take hold at the next unrelated change.
    """
    window = create_window(2)
    before = window.invalidation_hash()

    window.strip = True

    assert window.invalidation_hash() != before
