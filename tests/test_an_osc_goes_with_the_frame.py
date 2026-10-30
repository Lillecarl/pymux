"""
When a pane's escape for the outer terminal reaches that terminal.

Behind the next frame, and not the moment it arrives. A pane's "OSC
52" is a clipboard write, and anything that reads the clipboard as a
marker for what is on the screen needs the two in that order: a flush
of its own put the clipboard ahead of the frame that showed the copy,
and `checks.pymux-pictures` photographed a pane that was still catching
up because of it. The middleman's fence is the same marker, an OSC 99,
so behind the frame its arrival is all a harness waits for.
Lillecarl/pymux#579.

tmux orders it the same way. A pane's clipboard goes through
`screen_write_setselection` (`input.c:3414-3440`), so it is dispatched
as `tty_cmd_setselection` among the drawing commands (`tty.c:2140`);
only its popup case, which has no pane, writes straight to the
terminal. Lillecarl/pymux#478.
"""

from __future__ import annotations

import sys

import pytest
from libpymux.protocol import Packet
from session import once
from test_a_session_on_a_pyte_screen import attached, shows

from pymux.connection.client_io import FrameOutput, SocketStdout
from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.enums import Clipboard, NotificationPopups

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

    `SocketStdout` is where a frame is counted, and `FrameOutput` is
    what the renderer writes through, so the buffering under test is
    the buffering that ships.
    """
    sent = []
    stdout = SocketStdout(sent.append)
    output = FrameOutput(stdout, lambda: None, term="xterm")

    connection = ServerConnection.__new__(ServerConnection)
    connection.client_state = _ClientState(output)
    return connection, output, sent


def written(sent) -> str:
    "Everything the client has been sent, in order."
    return "".join(packet["data"] for packet in sent if packet["cmd"] == Packet.OUT)


def a_frame(output, drawn: str = "") -> None:
    "What the renderer does: the frame begins, it is written and flushed, and it ends."
    output.frame_begins()
    output.write_raw(drawn)
    output.flush()
    output.frame_ends()


def test_nothing_goes_out_before_the_frame(wire):
    "A flush of its own is what put the clipboard ahead of the screen."
    connection, _output, sent = wire

    connection.forward_osc(COPIED)

    assert sent == []


def test_a_flush_that_is_not_a_frame_carries_nothing(wire):
    """
    Graphics and terminal modes flush on their own, between frames. One
    of those flushes sent the vterm middleman's fence ahead of the frame
    that drew the payload, and libvterm read an empty screen.
    Lillecarl/pymux#611.
    """
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    output.write_raw("a terminal mode")
    output.flush()
    assert COPIED not in written(sent)

    a_frame(output, "the frame")
    whole = written(sent)
    assert whole.index("the frame") < whole.index(COPIED)


def test_a_skipped_frame_leaves_it_for_the_next(wire):
    """
    The stale-frame check returns before the renderer writes or
    flushes, between `before_render` and `after_render`. A flush after
    it is not a frame either.
    """
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    output.frame_begins()
    output.frame_ends()
    output.flush()
    assert COPIED not in written(sent)

    a_frame(output, "the frame")
    assert COPIED in written(sent)


def test_the_frame_carries_it(wire):
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    a_frame(output, "the frame")

    assert COPIED in written(sent)


def test_it_comes_behind_what_the_frame_draws(wire):
    """
    The pane drew the text before it copied it, and the frame that
    carries the escape draws that text. Behind the frame, the terminal
    shows the text before it hears of the copy.
    """
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    a_frame(output, "the frame")

    whole = written(sent)
    assert whole.index("the frame") < whole.index(COPIED)


def test_a_frame_that_draws_nothing_still_carries_it(wire):
    "A pane out of sight draws nothing, and its notification still goes."
    connection, output, sent = wire

    connection.forward_osc(COPIED)
    a_frame(output)

    assert written(sent) == COPIED


def test_a_client_with_no_terminal_is_written_nothing(wire):
    "The temporary client of a socket command reads the answer, not this."
    connection, _output, sent = wire
    connection.client_state.temporary = True

    connection.forward_osc(COPIED)
    a_frame(connection.client_state.output)

    assert written(sent) == ""


async def test_a_real_client_holds_it_through_a_flush_between_frames():
    """
    The same, end to end: a pane draws and sends its fence, and before
    the frame comes the client's terminal is flushed for something
    else, as graphics do. The fence still comes behind the drawing.
    Lillecarl/pymux#611.
    """
    mux = Pymux()
    mux.options.test_mode = True
    try:
        async with mux.running(), attached(mux) as session:
            await shows(session, "$")
            connection = mux.connections[-1]
            (pane,) = mux.panes_by_id.values()
            # A notification of a window in sight stays inside.
            # Lillecarl/pymux#799.
            connection.client_state.terminal_focused = False
            # A kitty, so the fence goes as it came. Lillecarl/pymux#497.
            connection.replies.answered_notification_query = True

            # The fence is a notification, and its box would cover the line.
            connection.client_state.options.notification_popups = NotificationPopups.OFF
            fence = "\x1b]99;;fence-611\x1b\\"
            pane.terminal.terminal_control.feed_output("drawn-before-the-fence" + fence)
            connection.flush_output()
            await once(lambda: "fence-611" in written(session.packets), 10.0, "no fence")

            wire = written(session.packets)
            assert wire.index("drawn-before-the-fence") < wire.index("fence-611")
    finally:
        for pane in list(mux.panes_by_id.values()):
            if not pane.process.is_terminated:
                pane.process.kill()


# ----------------------------------------------------------------------
# And a frame has to come.


@pytest.fixture
async def pymux():
    mux = Pymux()
    async with mux.running():
        await mux.panes.create_window("%s -c pass" % (sys.executable,))
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


async def test_forwarding_asks_for_a_frame(pymux):
    """
    A pane in a window nobody looks at draws no frame of its own, and a
    notification from one is exactly what forwarding is for. Without a
    frame its escape would sit in a client's buffer until something
    else happened to draw.
    """
    woke = []
    pymux.invalidate = lambda reason=None: woke.append(reason)
    pymux.options.set_clipboard = Clipboard.ON
    pane = pymux.arrangement.get_active_window().active_pane

    pymux.outward.forward_osc(pane, "52", "c;aGVsbG8=")

    assert [one for one in woke if one and "escape" in one]
