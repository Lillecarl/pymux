"""
A pane, lent to a peer over a peer link. Lillecarl/pymux#759.

The peer here is a bare mirrored control fed by `MirrorSink`, which is
what a peer server puts behind its pane. It has to end up with the
owner's screen through output, a resize, new surroundings, a pause and
a resume, and a link too slow for the reads. The link turns to msgpack
the way a real one does, so the frozen screens cross the wire a peer
reads.
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
from pymux.mirror import source as source_module
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
    return MirrorSink(TerminalControl(backend=Quiet()))


def picture(control: TerminalControl):
    screen = control.screen
    rows = ["".join(screen.page.data_buffer[y][x].char for x in range(screen.columns)) for y in range(screen.lines)]
    history = len(screen.page.data_buffer)
    cursor = screen.pt_cursor_position
    return rows, history, (cursor.x, cursor.y), screen.surroundings


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


async def test_a_peer_follows_output_sizes_and_surroundings(pymux):
    pane, owner = the_control(pymux)
    owner.feed_output("before the mirror\r\n")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        assert sink.control.screen.lines == 6

        owner.feed_output("\x1b[31mred\x1b[0m and \x1b[1;")
        owner._ground_timer.since -= 10
        owner.feed_output("plain\r\n")
        owner.resize_screen(4, 20)
        owner.screen.set_cell_size(7, 15)
        owner.feed_output("after")
        await converge(client_end, sink, owner)
        assert sink.control.screen.lines == 4

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

        # Only the freeze on resume can bring the peer to this screen.
        ask(client_end, Packet.MIRROR_RESUME, pane)
        await converge(client_end, sink, owner)

        pymux.stop()


async def test_a_slow_link_stops_the_reads_and_sends_what_changed(pymux, monkeypatch):
    monkeypatch.setattr(source_module, "LAG_BYTES", 200)
    pane, owner = the_control(pymux)

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)

        # Nothing reads the link while these go in, so the queue grows.
        for number in range(50):
            owner.feed_output("line %d of a program that writes fast\r\n" % number)
        assert source_of(connection).lagging

        await converge(client_end, sink, owner)
        owner.feed_output("live again")
        await converge(client_end, sink, owner)
        assert not source_of(connection).lagging

        pymux.stop()


async def test_a_peer_types_into_the_pane(pymux):
    pane, owner = the_control(pymux)
    typed: list[str] = []
    owner.process.write_input = typed.append

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        ask(client_end, Packet.MIRROR_INPUT, pane, data="ls\r")
        with anyio.fail_after(5):
            while not typed:
                await anyio.sleep(0.01)
        assert typed == ["ls\r"]

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
