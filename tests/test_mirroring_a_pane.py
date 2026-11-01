"""
A pane, lent to a peer over a peer link. Lillecarl/pymux#759.

The peer here is a bare mirrored control fed by `MirrorSink`, which is
what a peer server puts behind its pane. It has to end up with the
owner's page through output, a resize, a pause and a resume, and a
link too slow for every frame. The link turns to msgpack the way a
real one does, so the frames cross the wire a peer reads.
"""

from __future__ import annotations

import sys

import anyio
import pytest
from libpymux.protocol import Field, Packet
from libpymux.wire import MSGPACK, encode_json
from ptterm.control import TerminalControl

from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.mirror.sink import MirrorSink
from pymux.pipes.memory import connect_in_memory

STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


class Quiet:
    "A backend that starts nothing and takes whatever it is given."

    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False
        self.ready_f = None

    def write_text(self, text: str) -> None:
        self.written.append(text)

    def add_input_ready_callback(self, callback) -> None:
        pass

    def set_size(self, width: int, height: int) -> None:
        pass

    async def start(self, task_group) -> None:
        pass

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture
async def pymux():
    mux = Pymux()
    mux.options.test_mode = True
    async with mux.running():
        await mux.panes.create_window(STAYS)
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def the_control(mux):
    pane = mux.arrangement.get_active_window().active_pane
    control = pane.terminal.terminal_control
    control.resize_screen(6, 30)
    return pane, control


def a_peer():
    told: list[None] = []
    sink = MirrorSink(TerminalControl(backend=Quiet()), lambda: told.append(None))
    sink.told = told  # type: ignore[attr-defined]
    return sink


def picture(control: TerminalControl):
    screen = control.screen
    top = screen.line_offset
    rows = [
        "".join(screen.page.data_buffer[top + y][x].char for x in range(screen.columns)) for y in range(screen.lines)
    ]
    cursor = screen.pt_cursor_position
    return rows, (cursor.x, cursor.y - top)


async def converge(client_end, sink: MirrorSink, owner: TerminalControl) -> None:
    "Apply what the link sends until the peer shows the owner's screen."
    with anyio.fail_after(5):
        while picture(sink.control) != picture(owner):
            packet = await client_end.read()
            if packet.get(Field.CMD) == Packet.MIRROR_REFUSED:
                raise AssertionError(packet)
            sink.take(packet)


def open_link(mux, pane):
    "The owner's end of a new peer link that mirrors `pane`, and the peer's end."
    server_end, client_end = connect_in_memory()
    connection = ServerConnection(mux, server_end)
    client_end.write_nowait(encode_json({Field.CMD: Packet.PEER, Field.WIRE: [MSGPACK]}))
    client_end.write_nowait(encode_json({Field.CMD: Packet.MIRROR, Field.PANE_ID: int(pane.pane_id)}))
    return connection, client_end


def source_of(connection):
    "The one mirror of a peer link."
    (source,) = connection.peer.sources.values()
    return source


def ask(client_end, kind: Packet, pane, **fields) -> None:
    client_end.write_nowait(encode_json({Field.CMD: kind, Field.PANE_ID: int(pane.pane_id), **fields}))


async def test_a_peer_follows_output_and_a_resize(pymux):
    pane, owner = the_control(pymux)
    owner.feed_output("before the mirror\r\n")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        assert sink.control.screen.lines == 6

        owner.feed_output("\x1b[31mred\x1b[0m and plain\r\n")
        owner.resize_screen(4, 20)
        owner.feed_output("after")
        await converge(client_end, sink, owner)
        assert sink.control.screen.lines == 4
        row = sink.control.screen.page.data_buffer
        assert any(cell.appearance.rendition.color for line in row.values() for cell in line.values())

        pymux.stop()


async def test_a_peer_takes_the_title_and_the_pointer(pymux):
    pane, owner = the_control(pymux)

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        owner.feed_output("\x1b]2;the title\x1b\\\x1b]22;>wait\x1b\\")
        screen = sink.control.screen
        with anyio.fail_after(5):
            while screen.pointer_shapes.shape != "wait":
                sink.take(await client_end.read())
        assert screen.titles.window == "the title"
        assert sink.told, "the pointer over the pane is drawn again"

        pymux.stop()


async def test_a_paused_mirror_gets_what_changed_on_resume(pymux):
    pane, owner = the_control(pymux)

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        owner.feed_output("one\r\ntwo\r\n")
        await converge(client_end, sink, owner)

        ask(client_end, Packet.MIRROR_PAUSE, pane)
        with anyio.fail_after(5):
            while not source_of(connection).paused:
                await anyio.sleep(0.01)
        owner.feed_output("\x1b[2J\x1b[Hwhile nobody watched")
        owner.resize_screen(5, 25)

        # Only the frame on resume can bring the peer to this page.
        ask(client_end, Packet.MIRROR_RESUME, pane)
        await converge(client_end, sink, owner)

        pymux.stop()


async def test_a_slow_link_holds_one_frame_and_sends_the_newest_page(pymux):
    pane, owner = the_control(pymux)

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)

        # Nothing reads the link while these go in.
        for number in range(50):
            owner.feed_output("line %d of a program that writes fast\r\n" % number)
        source = source_of(connection)
        assert sum(1 for entry, _compress in connection.peer._queue if entry is source) <= 1

        await converge(client_end, sink, owner)
        owner.feed_output("live again")
        await converge(client_end, sink, owner)

        pymux.stop()


async def test_a_peer_drives_the_pane_in_the_modes_the_program_set(pymux):
    "The owner encodes: application cursor keys, a bracketed paste and an SGR mouse report."
    pane, owner = the_control(pymux)
    typed: list[str] = []
    owner.process.write_input = typed.append
    owner.feed_output("\x1b[?1h\x1b[?2004h\x1b[?1000;1006h")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        ask(client_end, Packet.MIRROR_KEY, pane, data="\x1b[A", **{Field.KEY_PROFILE: {"menu": True}})
        ask(client_end, Packet.MIRROR_PASTE, pane, data="pasted")
        ask(client_end, Packet.MIRROR_MOUSE, pane, mouse={"action": 1, "column": 3, "row": 1})
        with anyio.fail_after(5):
            while len(typed) < 3:
                await anyio.sleep(0.01)
        assert typed == ["\x1bOA", "\x1b[200~pasted\x1b[201~", "\x1b[<0;4;2M"]

        pymux.stop()


async def test_a_pane_nobody_has_is_refused(pymux):
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json({Field.CMD: Packet.PEER}))
        client_end.write_nowait(encode_json({Field.CMD: Packet.MIRROR, Field.PANE_ID: 99999}))
        with anyio.fail_after(5):
            packet = await client_end.read()
        assert packet[Field.CMD] == Packet.MIRROR_REFUSED
        assert packet[Field.PANE_ID] == 99999

        pymux.stop()
