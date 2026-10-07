"""
A pane, streamed over the socket pymux already has.

`protocol.py` decides what a frame holds and `tests/test_the_web_protocol.py`
judges that. This is the transport: that a client may ask for a pane, that
frames arrive as they change, that input goes the other way, and that a
read-only stream refuses it.

**The frames are the protocol and the packet is not.** A relay passes
`data` on without reading it, so `stream-out` and `stream-in` stay ours to
change while the frames inside them do not. Lillecarl/pymux#461.

The in-memory pipe is what makes this a test rather than a timing
experiment: it is the server's own dispatch, with no socket and no sleep.
`tests/test_libpymux_streams.py` is the other half, over a real socket,
which is the only thing that says the two agree.
"""

from __future__ import annotations

import json
import sys

import anyio
import pytest

from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection
from pymux.web.stream import LOOK_AGAIN, PaneStream

#: A program that stays alive until something kills it.
#:
#: **`python -c pass` is no good here.** It ends before the first frame,
#: so the pane is already dead, the last pane of the last session stops
#: the server, and a test about a pane dying cannot say when it died. A
#: probe measured `is_terminated` as true at the moment of a kill that had
#: not happened yet, which is what that looks like from the outside.
STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


@pytest.fixture
async def pymux():
    mux = Pymux()
    mux.test_mode = True
    async with mux.running():
        await mux.create_window(STAYS)
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def the_pane(mux, lines: int = 5, columns: int = 20):
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(lines, columns)
    return pane


def arrives(pane, data: str) -> None:
    "Output, the way the pty delivers it: fed, then announced."
    pane.terminal.terminal_control.stream.feed(data)
    pane.terminal.terminal_control.on_content_changed.fire()


async def frames_of(client_end, how_many: int, seconds: float = 2.0) -> list:
    "The next `how_many` stream messages, or as many as arrive in time."
    found = []
    with anyio.move_on_after(seconds):
        while len(found) < how_many:
            packet = json.loads(await client_end.read())
            if packet["cmd"] == "stream-out":
                found.append(json.loads(packet["data"]))
            elif packet["cmd"] in ("err", "exit"):
                found.append(packet)
    return found


def ask(client_end, pane, writable: bool = False) -> None:
    client_end.write_nowait(
        json.dumps(
            {
                "cmd": "stream-pane",
                "pane": "%%%i" % pane.pane_id,
                "writable": writable,
            }
        )
    )


class _Ended:
    """
    A real pane whose program has ended.

    `Pane.process` is a property over the terminal's, so it cannot be
    assigned; and the terminal is the thing the stream needs to be real,
    because it holds the screen and the event. So the pane is wrapped and
    one answer replaced.
    """

    def __init__(self, pane) -> None:
        self._pane = pane

    def __getattr__(self, name):
        return getattr(self._pane, name)

    @property
    def process(self):
        class Gone:
            is_terminated = True

        return Gone()


def ended(pane):
    return _Ended(pane)


def text_of(frame) -> dict:
    return {number: "".join(text for _style, text in runs) for number, runs in frame["rows"].items()}


# ----------------------------------------------------------------------
# Opening one.


async def test_a_stream_begins_with_a_welcome_and_a_frame(pymux):
    pane = the_pane(pymux)
    arrives(pane, "hello")

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane)

        welcome, first = await frames_of(client_end, 2)

        assert welcome["type"] == "welcome"
        assert ".pyte-screen {" in welcome["css"]
        assert welcome["writable"] is False
        assert first["type"] == "frame"
        assert text_of(first)["0"] == "hello"

        pymux.stop()


async def test_a_pane_nobody_can_find_is_refused(pymux):
    the_pane(pymux)

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(json.dumps({"cmd": "stream-pane", "pane": "%9999"}))

        said = await frames_of(client_end, 2)

        assert said[0]["cmd"] == "err"
        assert "can't find pane" in said[0]["data"]
        assert said[1] == {"cmd": "exit", "code": 1}

        pymux.stop()


# ----------------------------------------------------------------------
# What arrives after.


async def test_output_arrives_as_the_row_that_changed(pymux):
    pane = the_pane(pymux)
    arrives(pane, "one\r\ntwo")

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane)
        await frames_of(client_end, 2)

        arrives(pane, "\x1b[1;1Hnew")

        (frame,) = await frames_of(client_end, 1)
        assert list(frame["rows"]) == ["0"]
        assert text_of(frame)["0"] == "new"

        pymux.stop()


async def test_a_stream_ends_when_the_pane_has_ended(pymux):
    """
    **The close is the whole contract, and there is no frame for it.**

    A frame saying "the pane ended" would be a path nothing can test: the
    last pane of a server takes the server down with it, so whatever was
    going to say so is cancelled first. A client has to handle a closed
    connection in any case, so a second answer would only be the one
    nobody exercises.

    **The rule is tested here and the pty is not.** How long a real
    program takes to be reaped is `ptyhost`'s business and is tested
    there; three attempts at watching a real death through a socket
    measured the pane's own timing and not this loop. So `run` is asked
    directly, with the one field it reads.
    """
    pane = the_pane(pymux)
    sent = []

    async def send(frame):
        sent.append(frame)

    stream = PaneStream(pymux, ended(pane), send)

    with anyio.fail_after(LOOK_AGAIN + 2):
        await stream.run()

    # The welcome and one frame, and then it stopped rather than sending
    # something to say why.
    assert [frame.get("type") for frame in sent] == ["welcome", "frame"]


async def test_a_stream_that_ended_leaves_no_handler_behind(pymux):
    "The same leak `wait-pane-change` has a test for, in the same shape."
    pane = the_pane(pymux)
    changed = pane.terminal.terminal_control.on_content_changed
    before = len(changed._handlers)

    async def send(_frame):
        pass

    stream = PaneStream(pymux, ended(pane), send)
    with anyio.fail_after(LOOK_AGAIN + 2):
        await stream.run()

    assert len(changed._handlers) == before


# ----------------------------------------------------------------------
# What the viewer sends.


async def test_a_writable_stream_sends_keys_to_the_program(pymux):
    """
    A key arrives as a **name** and the pane spells it, because the pane
    is what knows which keyboard protocol the program asked for.
    """
    pane = the_pane(pymux)
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane, writable=True)
        await frames_of(client_end, 2)

        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "stream-in",
                    "data": json.dumps({"type": "input", "keys": "C-c"}),
                }
            )
        )
        await anyio.sleep(0.05)

        assert written == ["\x03"], written

        pymux.stop()


async def test_composed_text_reaches_the_program_as_it_stands(pymux):
    """
    What a viewer finished composing: a dead key, an emoji picker, an IME,
    dictation. The intermediate state never reaches here -- a browser draws
    a composition itself, and so does a real terminal.
    """
    pane = the_pane(pymux)
    written = []
    pane.process.write_input = written.append
    arrives(pane, "\x1b[?2004h")  # asking about pastes changes nothing here

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane, writable=True)
        await frames_of(client_end, 2)

        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "stream-in",
                    "data": json.dumps({"type": "text", "text": "é"}),
                }
            )
        )
        await anyio.sleep(0.05)

        # Not bracketed: a program that asked about pastes would draw the
        # markers around a character somebody typed.
        assert written == ["é"], written

        pymux.stop()


async def test_a_paste_goes_as_it_stands(pymux):
    pane = the_pane(pymux)
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane, writable=True)
        await frames_of(client_end, 2)

        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "stream-in",
                    "data": json.dumps({"type": "paste", "text": "C-c is text"}),
                }
            )
        )
        await anyio.sleep(0.05)

        assert written == ["C-c is text"], written

        pymux.stop()


async def test_a_paste_is_bracketed_when_the_program_asked(pymux):
    """
    A program that set mode 2004 must not read a paste as keys, which is
    how a paste into a shell runs the lines in it.
    """
    pane = the_pane(pymux)
    written = []
    pane.process.write_input = written.append
    arrives(pane, "\x1b[?2004h")

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane, writable=True)
        await frames_of(client_end, 2)

        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "stream-in",
                    "data": json.dumps({"type": "paste", "text": "ls\n"}),
                }
            )
        )
        await anyio.sleep(0.05)

        assert written == ["\x1b[200~ls\n\x1b[201~"], written

        pymux.stop()


async def test_a_read_only_stream_refuses_input(pymux):
    """
    It protects nothing against whoever holds the socket, which can
    `send-keys` regardless. It stops a bug in a relay's loop turning a
    read-only viewer into a writing one.
    """
    pane = the_pane(pymux)
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        ask(client_end, pane, writable=False)
        await frames_of(client_end, 2)

        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "stream-in",
                    "data": json.dumps({"type": "input", "keys": "C-c"}),
                }
            )
        )
        await anyio.sleep(0.05)

        assert written == []

        pymux.stop()


async def test_input_for_no_stream_is_ignored_and_not_a_crash(pymux):
    "A packet that arrives with no stream running has nowhere to go."
    the_pane(pymux)

    async with pymux.running():
        server_end, client_end = connect_in_memory()
        connection = ServerConnection(pymux, server_end)

        client_end.write_nowait(json.dumps({"cmd": "stream-in", "data": "{}"}))
        await anyio.sleep(0.05)

        assert not connection._closed

        pymux.stop()
