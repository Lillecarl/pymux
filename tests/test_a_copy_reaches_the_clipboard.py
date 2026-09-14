"""
What a person copies in copy mode reaches their clipboard.

The clipboard belongs to the terminal of the user, and a pane asks for
it with OSC 52. A copy asks for it the same way, through the door that
ptterm already gives the embedder for a program in a pane. tmux does
this too: `window_copy_copy_buffer` writes the selection to the screen
of the pane with `screen_write_setselection`, and `set-clipboard` says
whether it goes out.

`ptterm/tests/test_copy_mode.py` judges the keys. This judges the wire:
that a pane of pymux hands the ask over, that the payload survives the
check, and that every client hears it. Lillecarl/pymux#376.
"""

import base64
import sys

import pytest

from pymux.main import Pymux

COLUMNS = 20
LINES = 5

#: What is on the screen of the pane, and the part of it that is
#: selected.
WRITTEN = "hello world"
SELECTED = "hello"


class FakeConnection:
    "A client connection that collects what pymux writes to it."

    def __init__(self):
        self.written = []

    def forward_osc(self, data):
        self.written.append(data)

    def set_pointer_shape(self, shape):
        pass


class FakeClientState:
    app = None


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


def a_selection(mux):
    "The active pane, in copy mode, with `SELECTED` selected."
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    pane.terminal.terminal_control.stream.feed(WRITTEN)
    pane.terminal.read_the_screen_into_the_copy_buffer()

    buffer = pane.terminal.copy_buffer
    buffer.cursor_position = 0
    buffer.start_selection()
    buffer.cursor_position = len(SELECTED)
    return pane, buffer


def listening(mux):
    "One client, and what pymux writes to its terminal."
    connection = FakeConnection()
    mux._client_states = {connection: FakeClientState()}
    return connection


def asked_for(text):
    "The sequence that asks a terminal to hold this text."
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\x1b]52;c;%s\x1b\\" % (payload,)


def test_a_copy_reaches_the_terminal_of_the_client(pymux):
    connection = listening(pymux)
    pane, buffer = a_selection(pymux)

    pane.terminal.copy_selection(buffer)

    assert connection.written == [asked_for(SELECTED)]


def test_a_copy_reaches_the_paste_buffer_of_the_session(pymux):
    "Which is what `paste-buffer` reads, and stays where it was."
    listening(pymux)
    pane, buffer = a_selection(pymux)

    pane.terminal.copy_selection(buffer)

    assert pymux.clipboard.get_data().text == SELECTED


def test_set_clipboard_off_keeps_the_copy_inside(pymux):
    "tmux asks the same question of a copy as of a pane: `set-clipboard`."
    connection = listening(pymux)
    pymux.enable_clipboard = False
    pane, buffer = a_selection(pymux)

    pane.terminal.copy_selection(buffer)

    assert connection.written == []
