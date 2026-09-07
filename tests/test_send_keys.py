"""
`send-keys` types into a pane, and `-R` puts the pane back.

`-R` is the command a person runs when a program has left the pane in
a state they cannot type out of, so it is the one command that has to
work when nothing else does. It raised `AttributeError` and reset
nothing: it called `reset` on prompt_toolkit's `Screen`, which has no
such method, so the call after it never ran. Nothing tested it.
Lillecarl/pymux#118.
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


def a_pane(mux, data: str = ""):
    "The active pane, sized, with `data` drawn on it."
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    if data:
        pane.terminal.terminal_control.stream.feed(data)
    return pane


def send(mux, *arguments):
    "Run `send-keys`, and give back what reached the program."
    pane = mux.arrangement.get_active_window().active_pane
    written = []
    pane.process.write_input = written.append
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None

    call_command_handler("send-keys", mux, list(arguments))
    return "".join(written), errors


def test_a_reset_clears_what_the_program_drew(pymux):
    pane = a_pane(pymux, "hello\r\nworld")
    assert pane.screen.page.data_buffer

    send(pymux, "-R")

    assert not pane.screen.page.data_buffer


def test_a_reset_puts_the_cursor_home(pymux):
    pane = a_pane(pymux, "hello\r\nworld")

    send(pymux, "-R")

    assert (pane.screen.pt_cursor_position.y, pane.screen.pt_cursor_position.x) == (0, 0)


def test_a_reset_takes_back_a_mode_a_program_set(pymux):
    "DECSCNM turns the whole pane over, and a person cannot type it off."
    pane = a_pane(pymux, "\x1b[?5h")
    assert pane.screen.has_reverse_video

    send(pymux, "-R")

    assert not pane.screen.has_reverse_video


def test_a_reset_leaves_the_alternate_screen(pymux):
    "A program that died inside vim leaves the pane on the other screen."
    pane = a_pane(pymux, "\x1b[?1049h")
    assert pane.screen.in_alternate_screen

    send(pymux, "-R")

    assert not pane.screen.in_alternate_screen


def test_a_reset_says_nothing_to_the_program(pymux):
    "It puts the terminal back. The program hears nothing of it."
    a_pane(pymux, "hello")
    assert send(pymux, "-R") == ("", [])


def test_the_keys_reach_the_program(pymux):
    a_pane(pymux)
    written, errors = send(pymux, "Enter")
    assert errors == []
    assert written == "\r"


def test_a_key_name_nobody_knows_goes_as_text(pymux):
    "tmux sends an argument it cannot name as the text it is."
    a_pane(pymux)
    assert send(pymux, "notakey")[0] == "notakey"


def test_dash_l_sends_the_names_as_text(pymux):
    "`-l` says to send what is written, so `Enter` is five letters."
    a_pane(pymux)
    assert send(pymux, "-l", "Enter")[0] == "Enter"


def test_keys_are_refused_while_a_person_reads_the_history(pymux):
    """
    The program is suspended in copy mode, so it cannot answer.
    Lillecarl/pymux#133.
    """
    pane = a_pane(pymux, "hello")
    pane.terminal.is_copying = True

    written, errors = send(pymux, "Enter")

    assert written == ""
    assert errors == ["pymux: Cannot send keys. Pane is in copy mode."]
