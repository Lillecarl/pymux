"""
`capture-pane` gives back the rows of a pane, or the lines it holds.

A pane cuts a line to fit its width. So a capture of the rows gives a
path or a compiler message in pieces, and `-J` joins the pieces back
into the line a program wrote. tmux spells the flag the same way.

The two modes number their lines apart, and they have to: line -1 of
the rows is the row above the screen, and line -1 of the lines is the
line above the screen, which can be several rows. Lillecarl/pymux#135.
"""

import sys

import pytest

from pymux.commands import call_command_handler
from pymux.main import Pymux

COLUMNS = 20
LINES = 5


@pytest.fixture
def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


def create_pane(mux, data: str):
    "The active pane, sized, with `data` written on it."
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    pane.terminal.terminal_control.stream.feed(data)
    return pane


def capture(mux, *arguments) -> str:
    "What `capture-pane -p` prints."
    printed = []
    mux.print_command_line = printed.append
    call_command_handler("capture-pane", mux, ["-p", *arguments])
    return printed[0]


#: Twenty-five characters on a twenty column pane, so the line takes
#: two rows and the second row is a wrap of the first.
LONG = "a line longer than that pane"


def test_capture_of_rows_holds_cut(pymux):
    create_pane(pymux, LONG)
    assert capture(pymux).splitlines() == ["a line longer than t", "hat pane"]


def test_capture_of_lines_joins_cut(pymux):
    create_pane(pymux, LONG)
    assert capture(pymux, "-J").splitlines() == [LONG]


def test_rows_and_lines_agree_on_line_that_fits(pymux):
    "Nothing wrapped, so joining changes nothing."
    create_pane(pymux, "one\r\ntwo")
    assert capture(pymux) == capture(pymux, "-J") == "one\ntwo"


def test_line_zero_of_rows_is_first_row_of_screen(pymux):
    create_pane(pymux, "".join("line %d\r\n" % number for number in range(9)))
    assert capture(pymux, "-S", "0", "-E", "0") == "line 5"


def test_negative_line_reaches_into_history(pymux):
    create_pane(pymux, "".join("line %d\r\n" % number for number in range(9)))
    assert capture(pymux, "-S", "-2", "-E", "-1") == "line 3\nline 4"


def test_line_that_wrap_carried_onto_screen_is_whole(pymux):
    """
    The long line starts above the first visible row and ends below
    it. Asking for the screen gives the whole line, because the line
    is what `-J` counts.
    """
    create_pane(
        pymux,
        "".join("line %d\r\n" % number for number in range(3)) + LONG + "\r\nlast",
    )
    assert capture(pymux, "-J", "-S", "0").splitlines() == [
        "line 1",
        "line 2",
        LONG,
        "last",
    ]


# ----------------------------------------------------------------------
# What a capture with no range covers.
#
# The visible pane, which is what a caller that asks for a pane means.
# It used to be the whole buffer, so a poller drawing a pane a few
# times a second read up to `history-limit` rows a frame and nothing
# said so. `-S -` is the spelling that still reaches the history, and
# tmux reads the two apart the same way
# (`cmd-capture-pane.c:294-306`). Lillecarl/pymux#457.

#: More lines than the pane is tall, so some of them scroll off.
SCROLLED = "".join("line %d\r\n" % number for number in range(9))


def test_no_range_is_the_visible_pane(pymux):
    create_pane(pymux, SCROLLED)
    assert capture(pymux).splitlines() == ["line 5", "line 6", "line 7", "line 8"]


def test_a_start_of_dash_reaches_the_history(pymux):
    create_pane(pymux, SCROLLED)
    lines = capture(pymux, "-S", "-").splitlines()
    assert lines[0] == "line 0"
    assert lines[-1] == "line 8"


def test_no_range_is_the_visible_pane_with_joined_lines(pymux):
    "`-J` numbers lines and not rows, and the default is the same idea."
    create_pane(pymux, SCROLLED)
    assert capture(pymux, "-J").splitlines() == [
        "line 5",
        "line 6",
        "line 7",
        "line 8",
    ]


def test_a_start_of_dash_reaches_the_history_with_joined_lines(pymux):
    create_pane(pymux, SCROLLED)
    assert capture(pymux, "-J", "-S", "-").splitlines()[0] == "line 0"


def test_a_wrapped_line_of_the_history_is_not_in_the_default(pymux):
    """
    The pane is what a caller asked for, so a row above it stays out
    however long the line on it was.
    """
    create_pane(pymux, LONG + "\r\n" + SCROLLED)
    assert "a line longer" not in capture(pymux)


def test_line_number_that_is_not_number_is_error(pymux):
    create_pane(pymux, "one")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    call_command_handler("capture-pane", pymux, ["-p", "-S", "nope"])
    assert errors == ["pymux: Invalid start line: nope"]
