"""
When a pane's escape for the outer terminal reaches that terminal.

With the frame, and not the moment it arrives. A pane's "OSC 52" is a
clipboard write, and anything that reads the clipboard as a marker for
what is on the screen needs the two in that order: a flush of its own
put the clipboard ahead of the frame that showed the copy, and
`checks.pymux-pictures` photographed a pane that was still catching up
because of it.

tmux orders it the same way. A pane's clipboard goes through
`screen_write_setselection` (`input.c:3414-3440`), so it is dispatched
as `tty_cmd_setselection` among the drawing commands (`tty.c:2140`);
only its popup case, which has no pane, writes straight to the
terminal. Lillecarl/pymux#478.
"""

import sys

import pytest
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux
from pymux.options import Clipboard
from pymux.protocol import Packet
from pymux.server import ServerConnection, _SocketStdout

#: A clipboard write, as a pane writes one.
COPIED = "\x1b]52;c;aGVsbG8=\x07"


class _ClientState:
    "As much of one as `forward_osc` reads."

    temporary = False

    def __init__(self, output) -> None:
        self.output = output


@pytest.fixture
def wire():
    """
    A connection whose output is the real one, and the packets it sends.

    `_SocketStdout` is where a frame is counted, and `Vt100_Output` is
    what the renderer writes through, so the buffering under test is
    the buffering that ships.
    """
    sent = []
    stdout = _SocketStdout(sent.append)
    output = Vt100_Output(stdout, lambda: None, term="xterm")

    connection = ServerConnection.__new__(ServerConnection)
    connection.client_state = _ClientState(output)
    return connection, output, sent


def written(sent) -> str:
    "Everything the client has been sent, in order."
    return "".join(
        packet["data"] for packet in sent if packet["cmd"] == Packet.OUT
    )


def test_nothing_goes_out_before_the_frame(wire):
    "A flush of its own is what put the clipboard ahead of the screen."
    connection, _output, sent = wire

    connection.forward_osc(COPIED)

    assert sent == []


def test_the_frame_carries_it(wire):
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    output.write_raw("the frame")
    output.flush()

    assert COPIED in written(sent)


def test_it_comes_before_what_the_frame_draws(wire):
    """
    The pane drew the text before it copied it, so the screen already
    holds what the copy is about: the escape belongs at the head of the
    frame that carries it, never after a later one.
    """
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    output.write_raw("the frame")
    output.flush()

    whole = written(sent)
    assert whole.index(COPIED) < whole.index("the frame")


def test_a_client_with_no_terminal_is_written_nothing(wire):
    "The temporary client of a socket command reads the answer, not this."
    connection, _output, sent = wire
    connection.client_state.temporary = True

    connection.forward_osc(COPIED)
    connection.client_state.output.flush()

    assert written(sent) == ""


# ----------------------------------------------------------------------
# And a frame has to come.


@pytest.fixture
def pymux():
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


def test_forwarding_asks_for_a_frame(pymux):
    """
    A pane in a window nobody looks at draws no frame of its own, and a
    notification from one is exactly what forwarding is for. Without a
    frame its escape would sit in a client's buffer until something
    else happened to draw.
    """
    woke = []
    pymux.invalidate = lambda reason=None: woke.append(reason)
    pymux.clipboard_mode = Clipboard.ON
    pane = pymux.arrangement.get_active_window().active_pane

    pymux.forward_osc(pane, "52", "c;aGVsbG8=")

    assert [one for one in woke if one and "escape" in one]
