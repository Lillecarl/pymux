"""
A row of panes wider than the screen, and the window onto it.

Every layout pymux has divides a fixed budget, so adding a pane makes
every other pane smaller. `ScrollableStrip` is the other thing: the
panes keep their widths, the row runs past the edge of the screen, and
the view scrolls to the one a person is on. Lillecarl/pymux#198.

**The tests read the cells.** A strip is a copy of one screen onto
another with an offset, and every way it can be wrong -- an off-by-one
in the slice, a pane drawn at the wrong place, a scroll that does not
follow the focus -- shows as the wrong letters in a row. Asking the
container what it thinks its scroll is would miss all of them.

Each column is filled with a letter of its own, so a row of the screen
says which part of the strip is on it.
"""

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.containers import ScrollOffsets, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput

from pymux.strip import ScrollableStrip

HEIGHT = 2

#: One letter for each column, in order.
LETTERS = "abcdefgh"


def a_column(letter, width, height=HEIGHT):
    "A window that fills itself with one letter."
    return Window(
        content=FormattedTextControl([("", "\n".join([letter * width] * height))]),
        width=D.exact(width),
        height=D.exact(height),
    )


def a_strip(widths, **kwargs):
    "A strip of columns of these widths, and the columns themselves."
    columns = [a_column(LETTERS[i], width) for i, width in enumerate(widths)]
    return ScrollableStrip(VSplit(columns, padding=0), **kwargs), columns


def rendered(strip, columns, visible, focus=0, scroll=None):
    """
    The top row of the screen, as a string.

    A real application has to be current: the strip asks which window
    has the focus, and that is a fact about an application.
    """
    if scroll is not None:
        strip.horizontal_scroll = scroll

    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(strip, focused_element=columns[focus]),
            input=pipe,
            output=DummyOutput(),
        )
        with set_app(app):
            screen = Screen()
            strip.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=visible, height=HEIGHT),
                "",
                False,
                None,
            )

    row = screen.data_buffer[0]
    return "".join(row[x].char for x in range(visible))


# ----------------------------------------------------------------------
# The window onto the strip.


def test_a_strip_that_fits_is_drawn_whole():
    strip, columns = a_strip([4, 4])

    assert rendered(strip, columns, visible=8) == "aaaabbbb"


def test_a_strip_wider_than_the_screen_shows_its_left_edge():
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8) == "aaaabbbb"


def test_the_scroll_says_which_part_is_on_screen():
    strip, columns = a_strip([4, 4, 4])

    # The focus stays on the first column, which is why the scroll is
    # named rather than reached by moving the focus.
    assert rendered(strip, columns, visible=8, scroll=4, focus=1) == "bbbbcccc"


def test_a_scroll_between_two_columns_shows_both():
    "Nothing says a strip may only stop on a column edge."
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=2, focus=1) == "aabbbbcc"


def test_a_strip_never_scrolls_past_its_end():
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=99, focus=2) == "bbbbcccc"


def test_a_strip_never_scrolls_before_its_start():
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=-5) == "aaaabbbb"


def test_a_strip_narrower_than_the_screen_fills_it():
    """
    A strip that stopped at its content would leave a band of nothing
    on the right, and there is nothing to scroll to there.
    """
    strip, columns = a_strip([4])

    assert rendered(strip, columns, visible=8) == "aaaa    "


# ----------------------------------------------------------------------
# Following the focus.


def test_the_view_follows_the_focus_to_the_right():
    "The pane a person moves to is brought on screen."
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=0, focus=2) == "bbbbcccc"


def test_the_view_follows_the_focus_back_to_the_left():
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=4, focus=0) == "aaaabbbb"


def test_a_focus_that_is_already_on_screen_moves_nothing():
    strip, columns = a_strip([4, 4, 4])

    assert rendered(strip, columns, visible=8, scroll=2, focus=1) == "aabbbbcc"


def test_an_offset_keeps_the_columns_beside_it_peeking():
    """
    This is what a niri screen looks like: the focused column sits in
    the middle and the ones on either side are cut off at the edges, so
    a person can see that there is more of the strip both ways.

    Without an offset the focused column is already on screen at scroll
    zero, and nothing moves.
    """
    strip, columns = a_strip(
        [4, 4, 4, 4], scroll_offsets=ScrollOffsets(left=2, right=2)
    )

    assert rendered(strip, columns, visible=8, scroll=0, focus=1) == "aabbbbcc"


def test_an_offset_cannot_push_the_strip_past_its_end():
    "There is nothing to peek at beyond the last column."
    strip, columns = a_strip([4, 4, 4], scroll_offsets=ScrollOffsets(left=2, right=2))

    assert rendered(strip, columns, visible=8, scroll=0, focus=2) == "bbbbcccc"


def test_a_column_wider_than_the_screen_shows_its_left_edge():
    """
    It cannot be shown whole. The left edge is where a prompt is and
    where a person reading a pane starts.
    """
    strip, columns = a_strip([4, 12])

    assert rendered(strip, columns, visible=8, scroll=0, focus=1) == "bbbbbbbb"


# ----------------------------------------------------------------------
# What the rest of the layout is told.


def test_a_pane_is_told_where_it_ended_up():
    "A click has to reach the pane that was drawn under it."
    strip, columns = a_strip([4, 4, 4])

    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(strip, focused_element=columns[0]),
            input=pipe,
            output=DummyOutput(),
        )
        with set_app(app):
            screen = Screen()
            strip.horizontal_scroll = 4
            strip.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=8, height=HEIGHT),
                "",
                False,
                None,
            )

    where = screen.visible_windows_to_write_positions
    # The focus is on the first column, so the strip scrolled back to
    # it and every column sits where it started.
    assert where[columns[0]].xpos == 0
    assert where[columns[1]].xpos == 4


def test_the_strip_asks_for_no_width_of_its_own():
    """
    It scrolls, so any width will do. Asking for the width its content
    wants is what every other layout does and what this exists to
    avoid.
    """
    strip, _ = a_strip([4, 4, 4])

    assert strip.preferred_width(80).preferred <= 80


@pytest.mark.parametrize("visible", [1, 3, 7, 12, 40])
def test_a_row_is_as_wide_as_the_screen_whatever_the_strip_is(visible):
    strip, columns = a_strip([4, 4, 4])

    assert len(rendered(strip, columns, visible=visible)) == visible
