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

from pymux.commands.commands import call_command_handler
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


def a_pane(mux, data: str):
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


def test_a_capture_of_the_rows_holds_the_cut(pymux):
    a_pane(pymux, LONG)
    assert capture(pymux).splitlines() == ["a line longer than t", "hat pane"]


def test_a_capture_of_the_lines_joins_the_cut(pymux):
    a_pane(pymux, LONG)
    assert capture(pymux, "-J").splitlines() == [LONG]


def test_the_rows_and_the_lines_agree_on_a_line_that_fits(pymux):
    "Nothing wrapped, so joining changes nothing."
    a_pane(pymux, "one\r\ntwo")
    assert capture(pymux) == capture(pymux, "-J") == "one\ntwo"


def test_line_zero_of_the_rows_is_the_first_row_of_the_screen(pymux):
    a_pane(pymux, "".join("line %d\r\n" % number for number in range(9)))
    assert capture(pymux, "-S", "0", "-E", "0") == "line 5"


def test_a_negative_line_reaches_into_the_history(pymux):
    a_pane(pymux, "".join("line %d\r\n" % number for number in range(9)))
    assert capture(pymux, "-S", "-2", "-E", "-1") == "line 3\nline 4"


def test_a_line_that_a_wrap_carried_onto_the_screen_is_whole(pymux):
    """
    The long line starts above the first visible row and ends below
    it. Asking for the screen gives the whole line, because the line
    is what `-J` counts.
    """
    a_pane(
        pymux,
        "".join("line %d\r\n" % number for number in range(3)) + LONG + "\r\nlast",
    )
    assert capture(pymux, "-J", "-S", "0").splitlines() == [
        "line 1",
        "line 2",
        LONG,
        "last",
    ]


def test_a_line_number_that_is_not_a_number_is_an_error(pymux):
    a_pane(pymux, "one")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    call_command_handler("capture-pane", pymux, ["-p", "-S", "nope"])
    assert errors == ["pymux: Invalid start line: nope"]
