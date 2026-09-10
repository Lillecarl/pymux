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

from test_strip_draws import ROWS, a_client

#: Two complaints of the shape a configuration file makes, each long
#: enough that the pair cannot fit on one row of the test's screen.
FIRST = "/home/someone/.config/pymux/pymux.conf line 1: first thing wrong"
SECOND = "/home/someone/.config/pymux/pymux.conf line 2: second thing wrong"


#: A status line with nothing in it that time moves.
#:
#: Three of these tests compare two screens, and each screen comes from
#: a server of its own. The default `status-right` draws a clock, so two
#: servers built either side of a second differ by a row that has
#: nothing to do with a message. `test_auto_refresh.py` empties it for
#: the same reason.
NO_CLOCK = ("set-option status-right ''",)


def drawn_with_a_message(message, columns=40):
    "Every row of the screen while `message` is up."
    with a_client(commands=NO_CLOCK, columns=columns) as (pymux, draw):
        pymux.get_client_state().message = message
        return draw()


def on_the_screen(rows):
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


def test_the_start_of_a_long_message_is_on_the_screen():
    "The fault. The front of the message used to be scrolled away."
    rows = drawn_with_a_message("; ".join([FIRST, SECOND]))

    assert squashed("line 1: first thing wrong") in on_the_screen(rows)


def test_the_rest_of_it_is_there_too():
    "It wraps, so the second complaint did not take the first's place."
    rows = drawn_with_a_message("; ".join([FIRST, SECOND]))

    assert squashed("line 2: second thing wrong") in on_the_screen(rows)


def test_a_short_message_still_takes_one_row():
    """
    A message that fits may not push the panes up. `dont_extend_height`
    holds the toolbar to what it needs.
    """
    short = drawn_with_a_message("a short message")
    none = drawn_with_a_message("")

    used = [number for number in sorted(short) if short[number] != none[number]]
    assert len(used) == 1, (used, short, none)


def test_a_message_may_not_take_the_whole_screen():
    """
    It is a pop-up over the panes, so a runaway message is cut rather
    than allowed to become a page. The end is the end a reader reaches
    last.
    """
    rows = drawn_with_a_message("word " * 400)
    none = drawn_with_a_message("")

    used = [number for number in sorted(rows) if rows[number] != none[number]]
    assert len(used) <= 5, used
    assert len(used) < ROWS, used
