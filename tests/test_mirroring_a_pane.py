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
from argparse import Namespace

import anyio
import pytest
from libpymux.protocol import OLDEST_PEER_VERSION, PEER_VERSION, Field, Message, Packet, peer_upgrade
from libpymux.wire import MSGPACK, encode_json
from ptterm.control import TerminalControl
from pyte.mouse import MouseAction

from pymux.answer import Answer, current_answer
from pymux.commands.send_keys import send_keys
from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.mirror.peer import Peer
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
    # As `Serving` lists each connection it accepts.
    mux.connections.append(connection)
    client_end.write_nowait(encode_json({**HELLO, Field.WIRE: [MSGPACK]}))
    client_end.write_nowait(encode_json({Field.CMD: Packet.MIRROR, Field.PANE_ID: int(pane.pane_id)}))
    return connection, client_end


#: The peer's first packet, from a build of this version.
HELLO = {Field.CMD: Packet.PEER, Field.VERSION: PEER_VERSION, Field.OLDEST: OLDEST_PEER_VERSION}


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


async def test_an_image_the_owner_shows_reaches_the_peer(pymux):
    pane, owner = the_control(pymux)

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        owner.feed_output("\x1b_Ga=T,f=24,s=1,v=1,i=7,c=2,r=1;/wAA\x1b\\")
        graphics = sink.control.screen.graphics
        with anyio.fail_after(5):
            while not graphics.placements:
                sink.take(await client_end.read())
        assert graphics.images_by_id[7].data == b"\xff\x00\x00"
        assert [(p.image_id, p.columns, p.rows) for p in graphics.placements] == [(7, 2, 1)]

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


async def test_a_reply_from_the_peer_reaches_the_program_as_it_is(pymux):
    "Under the kitty keyboard protocol a key would be encoded again; a reply is not a key."
    pane, owner = the_control(pymux)
    typed: list[str] = []
    owner.process.write_input = typed.append
    owner.feed_output("\x1b[>31u")
    reply = "\x1b]99;i=1;\x1b\\"

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        ask(client_end, Packet.MIRROR_WRITE, pane, data=reply)
        with anyio.fail_after(5):
            while not typed:
                await anyio.sleep(0.01)
        assert typed == [reply]

        pymux.stop()


def shows(sink: MirrorSink, text: str) -> bool:
    return any(text in row for row in picture(sink.control)[0])


async def until(client_end, sink: MirrorSink, seen) -> list[Message]:
    "Apply what the link sends until `seen()` holds, and give what was not a frame."
    others = []
    with anyio.fail_after(5):
        while not seen():
            packet = await client_end.read()
            if packet.get(Field.CMD) == Packet.MIRROR_FRAME:
                sink.take(packet)
            else:
                others.append(packet)
    return others


#: Page up, in either table of copy mode's keys.
PAGE_UP = "\x1b[5~"


async def test_copy_mode_of_a_mirror_runs_on_the_owner_and_reads_its_history(pymux):
    pane, owner = the_control(pymux)
    owner.feed_output("".join("line %d\r\n" % number for number in range(40)))
    pymux.handle_command("set-option -g mode-keys emacs")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        assert not shows(sink, "line 0 ")

        ask(client_end, Packet.MIRROR_COPY, pane)
        # A page up moves by the window as last drawn, so a person sees
        # each page before the next.
        with anyio.fail_after(5):
            while not shows(sink, "line 0 "):
                ask(client_end, Packet.MIRROR_KEY, pane, data=PAGE_UP)
                packet = await client_end.read()
                while packet.get(Field.CMD) != Packet.MIRROR_FRAME:
                    packet = await client_end.read()
                sink.take(packet)

        ask(client_end, Packet.MIRROR_KEY, pane, data="q")
        with anyio.fail_after(5):
            while source_of(connection).copying is not None:
                await anyio.sleep(0.01)
        await converge(client_end, sink, owner)

        pymux.stop()


async def test_a_forwarded_copy_mode_command_acts_in_the_peers_copy_mode(pymux):
    "`send-keys -X` typed at the peer runs here (`mirror.forward`), in the copy mode that peer reads."
    pane, owner = the_control(pymux)
    owner.feed_output("\x1b]133;A\x07$ one\r\nout\r\n\x1b]133;A\x07$ two\r\nout\r\n")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        ask(client_end, Packet.MIRROR_COPY, pane)
        source = source_of(connection)
        await until(client_end, sink, lambda: source.copying is not None and source.copying.place is not None)
        opened_at = source.copying.place

        token = current_answer.set(Answer(forwarded=True))
        try:
            send_keys(pymux, Namespace(X=True, keys=["previous-prompt"], target_pane="%%%d" % pane.pane_id))
        finally:
            current_answer.reset(token)
        assert source.copying.place != opened_at

        pymux.stop()


async def test_what_a_peer_copies_in_copy_mode_goes_to_the_peer(pymux):
    pane, owner = the_control(pymux)
    owner.feed_output("the words to copy\r\n")
    # The keys are tmux's emacs table, whatever $EDITOR says.
    pymux.handle_command("set-option -g mode-keys emacs")

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)

        ask(client_end, Packet.MIRROR_COPY, pane)
        await until(client_end, sink, lambda: source_of(connection).copying is not None)
        # To the top, a selection, to the end of the line, and copy.
        for key in (PAGE_UP, "\x00", "\x05", "\x17"):
            ask(client_end, Packet.MIRROR_KEY, pane, data=key)
        copied = []
        with anyio.fail_after(5):
            while not copied:
                packet = await client_end.read()
                if packet.get(Field.CMD) == Packet.MIRROR_COPIED:
                    copied.append(packet[Field.DATA])
        assert copied == ["the words to copy"]

        pymux.stop()


async def test_a_copy_from_the_owner_fills_the_paste_buffer_at_the_peer(pymux):
    link = Peer(pymux, "owner")
    link._take({Field.CMD: Packet.MIRROR_COPIED, Field.PANE_ID: 1, Field.DATA: "copied there"})
    assert pymux.clipboard.get_data().text == "copied there"


async def test_the_wheel_over_a_program_that_wants_no_mouse_opens_copy_mode_on_the_owner(pymux):
    pane, owner = the_control(pymux)
    owner.feed_output("".join("line %d\r\n" % number for number in range(40)))

    async with pymux.running():
        connection, client_end = open_link(pymux, pane)
        sink = a_peer()
        await converge(client_end, sink, owner)
        ask(client_end, Packet.MIRROR_MOUSE, pane, mouse={"action": int(MouseAction.WHEEL_UP), "column": 1, "row": 1})
        await until(client_end, sink, lambda: not shows(sink, "line 39"))
        assert source_of(connection).copying is not None

        pymux.stop()


async def test_a_pane_nobody_has_is_refused(pymux):
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json(HELLO))
        client_end.write_nowait(encode_json({Field.CMD: Packet.MIRROR, Field.PANE_ID: 99999}))
        with anyio.fail_after(5):
            packet = await client_end.read()
            while packet[Field.CMD] == Packet.PEER_ACCEPTED:
                packet = await client_end.read()
        assert packet[Field.CMD] == Packet.MIRROR_REFUSED
        assert packet[Field.PANE_ID] == 99999

        pymux.stop()


@pytest.mark.parametrize(
    ("hello", "side"),
    [
        ({Field.CMD: Packet.PEER}, "the server that connects"),
        ({**HELLO, Field.VERSION: PEER_VERSION + 5, Field.OLDEST: PEER_VERSION + 1}, "the server on"),
    ],
    ids=["a peer older than any version", "a peer that left this version behind"],
)
async def test_a_peer_of_no_common_version_is_told_which_side_to_upgrade(pymux, hello, side):
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json(hello))
        with anyio.fail_after(5):
            packet = await client_end.read()
        assert packet[Field.CMD] == Packet.PEER_REFUSED
        assert packet[Field.ERROR].startswith("upgrade pymux on " + side)

        pymux.stop()


async def test_a_peer_names_the_side_to_upgrade_from_the_owners_answer(pymux):
    link = Peer(pymux, "owner")
    assert link._refusal({Field.CMD: Packet.TREE}).startswith("upgrade pymux on owner"), "an owner too old to say"
    assert link._refusal({Field.CMD: Packet.PEER_REFUSED, Field.ERROR: "upgrade pymux on here"}) == (
        "upgrade pymux on here"
    )
    left_behind = {Field.CMD: Packet.PEER_ACCEPTED, Field.VERSION: PEER_VERSION + 2, Field.OLDEST: PEER_VERSION + 1}
    assert link._refusal(left_behind).startswith("upgrade pymux on this server")
    assert not link.accepted
    assert link._refusal({Field.CMD: Packet.WIRE}) is None
    assert link._refusal({Field.CMD: Packet.PEER_ACCEPTED, Field.VERSION: PEER_VERSION}) is None
    assert link.accepted


@pytest.mark.parametrize(
    ("version", "oldest", "upgrade"),
    [
        (PEER_VERSION, OLDEST_PEER_VERSION, None),
        (None, None, "there"),
        (OLDEST_PEER_VERSION - 1, OLDEST_PEER_VERSION - 1, "there"),
        (PEER_VERSION + 3, PEER_VERSION + 1, "here"),
        (PEER_VERSION + 3, OLDEST_PEER_VERSION, None),
    ],
)
def test_two_versions_meet_or_name_the_side_to_upgrade(version, oldest, upgrade):
    assert peer_upgrade(version, oldest) == upgrade
