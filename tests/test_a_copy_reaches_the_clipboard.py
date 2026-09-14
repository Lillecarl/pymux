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
from prompt_toolkit.application.current import set_app
from prompt_toolkit.application.dummy import DummyApplication

from pymux.main import Pymux
from pymux.options import Clipboard

from session import create_session

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
    """
    Which is what `paste-buffer` reads.

    A copy writes the clipboard of the application, and the clipboard
    of the application is that buffer: `_create_app` hands it over, so
    copy mode in one client and `paste-buffer` in another are the same
    text. The test below says that is really where it comes from.
    """
    app = DummyApplication()
    app.clipboard = pymux.clipboard
    pane, buffer = a_selection(pymux)

    with set_app(app):
        pane.terminal.copy_selection(buffer)

    assert pymux.clipboard.get_data().text == SELECTED


async def test_the_clipboard_of_a_client_is_the_buffer_of_the_session():
    "The wiring the test above stands on."
    async with create_session() as (mux, state):
        assert state.app.clipboard is mux.clipboard


@pytest.mark.parametrize("mode", [Clipboard.EXTERNAL, Clipboard.ON])
def test_a_copy_goes_out_on_both_of_the_values_that_allow_it(pymux, mode):
    """
    `set-clipboard` answers two questions, and tmux gives them
    different answers. A copy the person makes goes out on "external"
    and on "on"; a program in a pane is allowed on "on" alone.
    Lillecarl/pymux#378.
    """
    connection = listening(pymux)
    pymux.clipboard_mode = mode
    pane, buffer = a_selection(pymux)

    pane.terminal.copy_selection(buffer)

    assert connection.written == [asked_for(SELECTED)]


def test_set_clipboard_off_keeps_the_copy_inside(pymux):
    "The one value that says no to the person as well."
    connection = listening(pymux)
    pymux.clipboard_mode = Clipboard.OFF
    pane, buffer = a_selection(pymux)

    pane.terminal.copy_selection(buffer)

    assert connection.written == []
