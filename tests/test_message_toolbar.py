"""
A message that does not fit on one row. Lillecarl/pymux#205.

`report_startup_errors` joins every failed line of a configuration file
into one message, and each names the file and the line, so two of them
pass the width of a normal terminal easily.

The toolbar was a `FormattedTextToolbar`, which does not wrap, and the
message carried a `[SetCursorPosition]` marker after the text. A window
scrolls to keep its cursor visible, so the message was drawn from its
**end** and the front of it could not be read at all. A person fixed
the error they could see, ran pymux again, and met the first one.

These tests read the cells of the real layout, because that is the only
place the difference exists: the whole message reaches
`client_state.message` either way.
"""

from __future__ import annotations

from prompt_toolkit.layout import walk
from test_strip_draws import ROWS, create_client

from pymux.layout.widgets import MessageToolbar

#: Two complaints of the shape a configuration file makes, each long
#: enough that the pair cannot fit on one row of the test's screen.
FIRST = "/home/someone/.config/pymux/pymux.conf line 1: first thing wrong"
SECOND = "/home/someone/.config/pymux/pymux.conf line 2: second thing wrong"


#: A status line with nothing in it that time moves. The default
#: `status-right` draws a clock. `test_auto_refresh.py` empties it for
#: the same reason.
NO_CLOCK = ("set-option status-right ''",)


async def drawn_with_message(message, columns=40):
    "Every row of the screen while `message` is up."
    async with create_client(commands=NO_CLOCK, columns=columns) as (pymux, draw):
        pymux.get_client_state().message = message
        return draw()


async def drawn_with_and_without(message, columns=40):
    """
    The screen with `message` up, and the same screen without it.

    One server, and no turn of the loop between the two draws. The pane
    runs a shell, and its prompt and title arrive when they arrive: two
    servers, or one with a wait between, differ by rows that have
    nothing to do with a message.
    """
    async with create_client(commands=NO_CLOCK, columns=columns) as (pymux, draw):
        state = pymux.get_client_state()
        state.message = ""
        without = draw()
        state.message = message
        return draw(), without


def on_screen(rows):
    """
    Everything the screen holds, with every space taken out.

    A wrap can fall in the middle of a word, and where it falls is not
    the question here. So the text is squashed and so is what a test
    looks for, which asks that the characters are on the screen in that
    order and says nothing about the rows they landed on.
    """
    joined = "".join(rows[number] for number in sorted(rows) if number >= 0)
    return "".join(joined.split())


def squashed(text):
    return "".join(text.split())


async def test_start_of_long_message_is_on_screen():
    "The fault. The front of the message used to be scrolled away."
    rows = await drawn_with_message("; ".join([FIRST, SECOND]))

    assert squashed("line 1: first thing wrong") in on_screen(rows)


async def test_rest_of_it_is_there_too():
    "It wraps, so the second complaint did not take the first's place."
    rows = await drawn_with_message("; ".join([FIRST, SECOND]))

    assert squashed("line 2: second thing wrong") in on_screen(rows)


async def test_short_message_still_takes_one_row():
    """
    A message that fits may not push the panes up. `dont_extend_height`
    holds the toolbar to what it needs.

    **The toolbar's own height, not the cells.** Its rows past the
    message are blank, and the second one fell on a row no pane covers:
    a toolbar two rows high changed the same characters as one.
    Lillecarl/pymux#586.
    """
    async with create_client(commands=NO_CLOCK, columns=40) as (pymux, draw):
        state = pymux.get_client_state()
        state.message = "a short message"
        draw()
        (toolbar,) = [item for item in walk(state.app.layout.container) if isinstance(item, MessageToolbar)]
        assert toolbar.render_info is not None
        assert toolbar.render_info.window_height == 1


async def test_message_may_not_take_whole_screen():
    """
    It is a pop-up over the panes, so a runaway message is cut rather
    than allowed to become a page. The end is the end a reader reaches
    last.
    """
    rows, none = await drawn_with_and_without("word " * 400)

    used = [number for number in sorted(rows) if rows[number] != none[number]]
    assert len(used) <= 5, used
    assert len(used) < ROWS, used
