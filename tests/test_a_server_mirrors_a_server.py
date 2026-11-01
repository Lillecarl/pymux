"""
One pymux server shows the sessions of another, over the other's
socket. Lillecarl/pymux#759.

Two servers in one loop: the owner listens on a socket in the test's
directory, and the peer runs `connect` on it. A client on the peer
switches to the mirrored session; the panes it shows stream, keys typed
there reach the owner's programs, and the owner answers for the
terminal at the peer.
"""

from __future__ import annotations

import base64
import io
import socket
import sys
from urllib.parse import urlparse

import anyio
import anyio.abc
import apsw
import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from pyte.keys.events import DEFAULT_KEY_PROFILE
from pyte.mouse import MouseAction
from pyte.osc import ColorBase
from session import Connection
from test_snapshot import _Copied
from test_ssh_client import create_ssh_server

from pymux.answer import Answer, current_answer
from pymux.commands import handle_command
from pymux.core.forwarding import with_port
from pymux.core.main import Pymux
from pymux.core.snapshot.read import load, start
from pymux.core.snapshot.write import save
from pymux.enums import Clipboard
from pymux.layout.geometry import plan_of
from pymux.mirror import program as mirror_program
from pymux.mirror.program import RemoteProgram
from pymux.query import ask
from pymux.style.colors import theme_color_base
from pymux.terminals import PeerTerminal

STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


def rows_of(control) -> list[str]:
    screen = control.screen
    return ["".join(screen.page.data_buffer[y][x].char for x in range(screen.columns)) for y in range(screen.lines)]


async def until(condition, what: str, seen=None) -> None:
    "Wait for `condition`, which the other server's packets make true. `seen` says what was there instead."
    try:
        with anyio.fail_after(5):
            while not condition():
                await anyio.sleep(0.01)
    except TimeoutError:
        raise AssertionError("never: %s%s" % (what, "" if seen is None else "; saw %r" % (seen(),))) from None


@pytest.fixture
async def two_servers(tmp_path):
    "The owner, and a peer with one client attached to its own session."
    owner = Pymux()
    owner.options.test_mode = True
    owner.serving.listen_on_socket(str(tmp_path / "owner.sock"))
    peer = Pymux()
    peer.options.test_mode = True
    # So the owner can link back, and the peer to itself.
    peer.serving.listen_on_socket(str(tmp_path / "peer.sock"))
    async with owner.running(), peer.running():
        # Started, so the first command from the peer opens no window.
        owner.startup_command = STAYS
        await owner.startup()
        await peer.panes.create_window(STAYS)
        output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=24, columns=80))
        with create_pipe_input() as pipe:
            client = peer.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=Connection("here", None, peer, "/dev/pts/8"),
            )
            try:
                yield owner, peer, client
            finally:
                for mux in (owner, peer):
                    for pane in list(mux.panes_by_id.values()):
                        process = getattr(pane, "process", None)
                        if process is not None and not process.is_terminated:
                            process.kill()
                peer.stop()
                owner.stop()


async def shown_here(owner, peer, client, link=None):
    """
    Connect the peer to the owner and put the client on the mirrored
    session. Answers the owner's active pane and its mirror here.
    `link` is a link made already, which `connect` would not make.
    """
    there = owner.arrangement.get_active_window().active_pane
    if link is None:
        peer.handle_command("connect %s" % (owner.socket_name,))
        (link,) = peer.peers.by_address.values()
    else:
        link.watch()
    await until(lambda: there.pane_id in link.tree.panes, "the owner's sessions arrive")
    (session,) = [session for session in peer.sessions if session.remote is not None]
    peer.attach_client_to(client, session)
    peer.invalidate()
    return there, link.tree.panes[there.pane_id]


async def test_a_session_there_shows_here(two_servers):
    owner, peer, client = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_control = there.terminal.terminal_control
    owner_control.resize_screen(6, 30)
    owner_control.feed_output("hello from there\r\n")
    typed: list[str] = []
    owner_control.process.write_input = typed.append

    there, here = await shown_here(owner, peer, client)
    assert here_session(peer).name == "%s/%s" % (socket.gethostname().partition(".")[0], owner.sessions[0].name)
    assert isinstance(here.process, RemoteProgram)
    control = here.terminal.terminal_control

    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror shows the owner's screen")
    owner_control.feed_output("and more")
    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror follows the owner")

    control.process.write_input("ls\r")
    await until(lambda: typed == ["ls\r"], "a key typed here reaches the program there")


async def test_a_copy_in_a_pane_there_reaches_the_clipboard_here(two_servers):
    "The owner passes the OSC on; the peer serves it under its own set-clipboard, though nobody looks at the pane."
    owner, peer, client = two_servers
    owner.options.set_clipboard = Clipboard.OFF
    peer.options.set_clipboard = Clipboard.ON
    there, here = await shown_here(owner, peer, client)
    own = next(session for session in peer.sessions if session.remote is None)
    peer.attach_client_to(client, own)
    peer.invalidate()

    payload = base64.b64encode(b"copied there").decode("ascii")
    there.terminal.terminal_control.feed_output("\x1b]52;c;%s\x07" % payload)
    await until(
        lambda: peer.clipboard.get_data().text == "copied there",
        "the copy reaches the paste buffer here",
        lambda: peer.clipboard.get_data().text,
    )


async def test_a_pane_streams_only_while_seen(two_servers):
    "The client leaves for its own session: the mirror pauses, and catches up on its return."
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    owner_control = there.terminal.terminal_control
    control = here.terminal.terminal_control
    (link,) = [connection.peer for connection in owner.connections if connection.peer is not None]
    await until(lambda: there.pane_id in link.sources, "the seen pane streams")

    own = next(session for session in peer.sessions if session.remote is None)
    peer.attach_client_to(client, own)
    peer.invalidate()
    await until(lambda: link.sources[there.pane_id].paused, "a pane nobody sees pauses")
    owner_control.feed_output("written meanwhile")
    assert "written meanwhile" not in "".join(rows_of(control))

    peer.attach_client_to(client, here_session(peer))
    peer.invalidate()
    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror catches up when seen again")


async def test_the_owner_lays_out_at_the_size_of_the_peer(two_servers):
    """
    Nobody is attached to the owner, so the client at the peer decides
    the window's size. Both servers lay the window out from the same
    splits at that size, so the mirror's screen fills the box the peer
    draws it in.
    """
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    owner_window = owner.arrangement.get_active_window()
    (link,) = peer.peers.by_address.values()
    (mirror_window,) = link.tree.windows.values()

    await until(
        lambda: owner.planes.size(owner_window) == peer.planes.size(mirror_window),
        "the owner's window takes the room the peer has",
        lambda: (owner.planes.size(owner_window), peer.planes.size(mirror_window)),
    )
    (rect,) = [rect for slot, rect in plan_of(peer, mirror_window).rects.items() if here in slot.panes]
    screen = here.terminal.terminal_control.screen
    await until(
        lambda: (screen.columns, screen.lines) == (rect.width, rect.height),
        "the mirror fills its box here",
        lambda: ((screen.columns, screen.lines), (rect.width, rect.height)),
    )


def here_session(peer):
    (session,) = [session for session in peer.sessions if session.remote is not None]
    return session


def run_as(peer, client, line: str):
    "Run a line as the client at the peer does, and answer what is left to await."
    with set_app(client.app):
        return handle_command(peer, line)


async def test_a_split_here_splits_the_window_there(two_servers):
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    owner_window = owner.arrangement.get_active_window()
    (link,) = peer.peers.by_address.values()

    answer = run_as(peer, client, "split-window")
    assert answer is not None, "the command goes to the owner"
    await answer
    assert len(owner_window.panes) == 2, "the owner split the window the person is in"
    new = owner_window.active_pane
    await until(lambda: new.pane_id in link.tree.panes, "the tree brings the new pane")
    (mirror_window,) = link.tree.windows.values()
    await until(lambda: mirror_window.active_pane is link.tree.panes[new.pane_id], "the focus follows the owner")


def pane_sizes(mux, window) -> dict:
    "Each pane's screen, and the box the window's plan gives it."
    return {
        pane.pane_id: (
            (pane.terminal.terminal_control.screen.columns, pane.terminal.terminal_control.screen.lines),
            (rect.width, rect.height),
        )
        for slot, rect in plan_of(mux, window).rects.items()
        for pane in slot.panes
    }


async def test_a_split_here_lays_the_window_out_there_at_the_peers_size(two_servers):
    "A client bigger than the default: the owner's new pane and the old one both fill the peer's room."
    owner, peer, client = two_servers
    client.app.output.get_size = lambda: Size(rows=50, columns=200)
    peer.invalidate()
    there, here = await shown_here(owner, peer, client)
    owner_window = owner.arrangement.get_active_window()
    (link,) = peer.peers.by_address.values()
    (mirror_window,) = link.tree.windows.values()
    await until(
        lambda: owner.planes.size(owner_window) == peer.planes.size(mirror_window),
        "the owner's window takes the room the peer has",
        lambda: (owner.planes.size(owner_window), peer.planes.size(mirror_window)),
    )
    assert owner.planes.size(owner_window).columns == 200

    await run_as(peer, client, "split-window -h")
    await until(lambda: len(link.tree.panes) == 2, "the tree brings the new pane")
    await until(
        lambda: (
            all(screen == box for screen, box in pane_sizes(owner, owner_window).values())
            and owner.planes.size(owner_window).columns == 200
        ),
        "both panes there fill their boxes at the peer's size",
        lambda: (owner.planes.size(owner_window), pane_sizes(owner, owner_window)),
    )


async def test_a_new_window_here_is_laid_out_there_at_the_peers_size(two_servers):
    owner, peer, client = two_servers
    client.app.output.get_size = lambda: Size(rows=50, columns=200)
    peer.invalidate()
    await shown_here(owner, peer, client)
    (link,) = peer.peers.by_address.values()

    await run_as(peer, client, "new-window")
    await until(lambda: len(owner.arrangement.windows) == 2, "the owner opens the window")
    window = owner.arrangement.windows[-1]
    await until(
        lambda: (
            owner.planes.size(window).columns == 200
            and all(screen == box for screen, box in pane_sizes(owner, window).values())
        ),
        "the new pane there fills the peer's room",
        lambda: (owner.planes.size(window), pane_sizes(owner, window)),
    )


async def test_a_window_the_peer_stops_watching_keeps_its_size_there(two_servers):
    "Looking away resizes nothing: the program keeps the size it had while somebody watched."
    owner, peer, client = two_servers
    client.app.output.get_size = lambda: Size(rows=50, columns=200)
    peer.invalidate()
    there, here = await shown_here(owner, peer, client)
    owner_window = owner.arrangement.get_active_window()
    (link,) = peer.peers.by_address.values()
    (mirror_window,) = link.tree.windows.values()
    await until(
        lambda: owner.planes.size(owner_window) == peer.planes.size(mirror_window),
        "the owner's window takes the room the peer has",
    )
    watched = owner.planes.size(owner_window)

    own = next(session for session in peer.sessions if session.remote is None)
    peer.attach_client_to(client, own)
    peer.invalidate()
    await until(lambda: not owner.planes.peer_rooms, "the peer stops watching")
    assert owner.planes.size(owner_window) == watched
    screen = there.terminal.terminal_control.screen
    assert screen.columns == watched.columns


async def test_a_window_the_peer_shows_is_seen_there(two_servers):
    "A mark the owner raised goes when a person at the peer looks, and none goes up while they do."
    owner, peer, client = two_servers
    owner_window = owner.arrangement.get_active_window()
    owner_window.bell = True
    there, here = await shown_here(owner, peer, client)
    (link,) = peer.peers.by_address.values()
    (mirror_window,) = link.tree.windows.values()
    await until(lambda: not owner_window.bell, "the owner's mark goes", seen=lambda: owner_window.bell)
    await until(lambda: not mirror_window.bell, "the mark here goes with it")

    assert not owner.arrangement.notify(owner_window, owner.apps)
    assert not owner_window.notified


async def test_the_focus_here_follows_a_pane_selected_there(two_servers):
    "select-pane runs on the owner; the tree that answers it moves the client's focus, with no key after."
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    (link,) = peer.peers.by_address.values()
    await run_as(peer, client, "split-window")
    await until(lambda: len(link.tree.panes) == 2, "the tree brings the new pane")
    new = link.tree.panes[owner.arrangement.get_active_window().active_pane.pane_id]
    await until(lambda: client.app.layout.has_focus(new.terminal), "the client focuses the new pane")

    await run_as(peer, client, "select-pane -t %%%d" % (here.pane_id,))
    await until(
        lambda: client.app.layout.has_focus(here.terminal),
        "the client focuses the pane selected there",
        seen=lambda: client.app.layout.current_control,
    )


async def test_a_command_goes_over_ssh_beside_the_link(two_servers, tmp_path):
    "Over SSH a forwarded command is a second channel on the connection the link holds."
    owner, peer, client = two_servers
    server, port, client_key = await create_ssh_server(tmp_path, owner.socket_name)
    try:
        link = peer.peers.of(
            "ssh://127.0.0.1:%d%s" % (port, owner.socket_name),
            known_hosts=None,
            client_keys=[client_key],
            username="anybody",
        )
        there, here = await shown_here(owner, peer, client, link)
        owner_window = owner.arrangement.get_active_window()

        await run_as(peer, client, "split-window")
        assert len(owner_window.panes) == 2
        await run_as(peer, client, "split-window")
        assert len(owner_window.panes) == 3, "the link still holds after a channel beside it closed"
    finally:
        server.close()


@pytest.mark.parametrize("line", ["new-window", "new-window -a -t {end}"])
async def test_a_new_window_there_takes_the_client_along(two_servers, line):
    "With a target named too: the owner runs it as from the person's pane, and the client follows and focuses."
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    session = here_session(peer)
    (link,) = peer.peers.by_address.values()

    await run_as(peer, client, line)
    assert len(owner.sessions[0].arrangement.windows) == 2
    shown = session.arrangement.get_active_window_for(client.app)
    assert shown is not None and shown.active_pane is not here, "the client moved to the new window"
    assert shown is link.tree.windows[link.tree.id_of_window(shown)]
    await until(
        lambda: client.app.layout.has_focus(shown.active_pane.terminal),
        "the client focuses the new pane",
        seen=lambda: client.app.layout.current_control,
    )


async def test_a_window_shown_again_draws_once_its_mirror_caught_up(two_servers, monkeypatch):
    "Nothing changed there either: the owner answers the resume, and the client draws then, not at the hold's end."
    monkeypatch.setattr(mirror_program, "HOLD_FOR_FRESH", 30.0)
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    session = here_session(peer)
    first = session.arrangement.get_active_window_for(client.app)
    await run_as(peer, client, "new-window")
    await until(lambda: not client._mirror_catching_up(), "the new window's first frame comes")
    await until(lambda: not here.process._streaming, "the window left behind pauses")

    with set_app(client.app):
        session.arrangement.set_active_window(first)
    peer.invalidate()
    assert here.process.stale
    assert client.should_skip_render(), "the old page is not drawn"
    await until(lambda: not client._mirror_catching_up(), "the owner answers the resume")


async def test_a_target_here_names_the_pane_there(two_servers):
    owner, peer, client = two_servers
    await owner.panes.create_window(STAYS)
    gone = owner.arrangement.get_active_window().active_pane
    there, here = await shown_here(owner, peer, client)
    assert there is gone
    (link,) = peer.peers.by_address.values()

    # The id the pane has here, which the owner knows by another.
    await run_as(peer, client, "kill-pane -t %%%d" % (here.pane_id,))
    assert owner.session_holding(gone) is None
    await until(lambda: gone.pane_id not in link.tree.panes, "the tree loses the pane")


async def test_record_pane_records_there_and_says_so_here(two_servers, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)

    await run_as(peer, client, "record-pane")
    assert there.recording is not None and here.recording is None
    assert client.message.startswith("Recording "), "the owner's message reaches the person here"

    await run_as(peer, client, "record-pane")
    assert there.recording is None
    assert client.message.startswith("Recorded ")
    assert list((tmp_path / "pymux" / "recordings").glob("*.bin"))


async def test_capture_pane_reads_the_pane_there_while_nobody_sees_it(two_servers):
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    own = next(session for session in peer.sessions if session.remote is None)
    peer.attach_client_to(client, own)
    peer.invalidate()
    there.terminal.terminal_control.feed_output("written while paused")

    with set_app(client.app):
        reply = Answer()
        current_answer.set(reply)
        try:
            answer = handle_command(peer, "capture-pane -p -t %%%d" % (here.pane_id,))
        finally:
            current_answer.set(None)
    await answer
    assert "written while paused" in "\n".join(reply.output)


async def test_a_pane_cannot_move_between_servers(two_servers):
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    own = next(session for session in peer.sessions if session.remote is None)
    own_window = own.arrangement.windows[0]

    assert run_as(peer, client, "move-pane -s %%%d -t @%d" % (here.pane_id, own_window.window_id)) is None
    assert any("different servers" in line for line in peer.message_log)
    assert len(owner.arrangement.get_active_window().panes) == 1


async def test_sql_says_which_server_a_session_is_on(two_servers):
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    answer = await ask(peer, "SELECT server, server_ids FROM live.sessions ORDER BY position", {}, 0, 0)
    assert answer.rows == [("", ""), (socket.gethostname().partition(".")[0], owner.server_id)]


async def test_a_link_back_brings_no_session_home(two_servers):
    "Each server mirrors the other's own session, and not its mirror of its own."
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    owner.handle_command("connect %s" % (peer.socket_name,))

    def mirrored():
        return [session for session in owner.sessions if session.remote is not None]

    # One tree carries both of the peer's sessions, so one seen is both read.
    await until(lambda: mirrored(), "the peer's session arrives", seen=lambda: owner.sessions)
    (session,) = mirrored()
    assert session.remote.server_ids == (peer.server_id,)
    assert [session.remote.server_ids for session in peer.sessions if session.remote is not None] == [
        (owner.server_id,)
    ]


def links_of(owner) -> list:
    "The owner's connections that are peer links."
    return [connection for connection in owner.connections if connection.peer is not None]


async def test_a_link_that_goes_is_dialled_again_with_copy_mode_where_it_was(two_servers):
    "As when the owner upgrades: the peer links again, and copy mode opens where the person left it."
    owner, peer, client = two_servers
    owner.handle_command("set-option -g mode-keys emacs")
    owner_control = owner.arrangement.get_active_window().active_pane.terminal.terminal_control
    owner_control.feed_output("".join("line %d\r\n" % number for number in range(10)))
    there, here = await shown_here(owner, peer, client)
    control = here.terminal.terminal_control
    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror shows the owner's screen")

    sink = here.process.sink
    here.terminal.enter_copy_mode()
    await until(lambda: sink.copying is not None, "copy mode opens on the owner")
    opened_at = sink.copying[0]
    for _ in range(3):
        here.process.key("\x1b[A", DEFAULT_KEY_PROFILE)
    await until(lambda: sink.copying is not None and sink.copying[0] < opened_at, "the caret moves up")
    left_at = sink.copying

    (link,) = links_of(owner)
    link.detach_and_close()

    def mirrors_again():
        return [source for one in links_of(owner) if one is not link for source in one.peer.sources.values()]

    await until(mirrors_again, "the peer dials again and asks for the pane")
    (source,) = mirrors_again()
    await until(
        lambda: source.copying is not None and source.copying.place == left_at,
        "copy mode opens where it was",
        seen=lambda: source.copying and source.copying.place,
    )

    source.copying.end()
    owner_control.feed_output("after the link came back")
    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror follows the new link")


async def test_a_chain_of_three_encodes_input_once_on_the_owner(two_servers):
    "C mirrors B's mirror of A's pane: B's screen holds none of the program's modes, so B passes all on."
    owner, peer, client = two_servers
    owner_control = owner.arrangement.get_active_window().active_pane.terminal.terminal_control
    owner_control.feed_output("\x1b[?2004h\x1b[?1000;1006h")
    typed: list[str] = []
    owner_control.process.write_input = typed.append
    there, middle = await shown_here(owner, peer, client)

    third = Pymux()
    third.options.test_mode = True
    async with third.running():
        try:
            third.handle_command("connect %s" % (peer.socket_name,))
            (link,) = third.peers.by_address.values()
            await until(lambda: middle.pane_id in link.tree.panes, "B's mirror of A's pane reaches C")
            last = link.tree.panes[middle.pane_id].process
            last.seen(True)
            await until(lambda: links_of(peer) and links_of(peer)[0].peer.sources, "C mirrors B's pane")

            last.paste("pasted")
            last.mouse(MouseAction.PRESS, 3, 1)
            await until(lambda: len(typed) >= 2, "C's input reaches A", seen=lambda: typed)
            assert typed == ["\x1b[200~pasted\x1b[201~", "\x1b[<0;4;2M"]

            last.copy_mode()
            (a_to_b,) = links_of(owner)
            (source,) = a_to_b.peer.sources.values()
            await until(lambda: source.copying is not None and source.copying.place is not None, "copy mode opens on A")
            await until(lambda: last.copying is not None, "C learns where copy mode on A stands")
            assert a_to_b.peer.copied(source.copying.app, "copied at C")
            await until(
                lambda: third.clipboard.get_data().text == "copied at C",
                "A's copy reaches C, whose person made it",
                seen=lambda: (third.clipboard.get_data().text, peer.clipboard.get_data().text),
            )
            source.copying.end()
        finally:
            third.stop()


async def test_an_upgrade_saves_no_mirror_and_links_again(two_servers, tmp_path):
    "The snapshot holds the owner's address and none of its sessions; the next server links again."
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    path = tmp_path / "peer.sqlite"
    save(peer, path)
    db = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    assert list(db.execute("SELECT session_name FROM sessions")) == [(peer.sessions[0].name,)]
    db.close()

    fresh = Pymux()
    fresh.options.test_mode = True
    async with fresh.running():
        try:
            load(fresh, path, _Copied())
            await start(fresh)
            await until(
                lambda: [session.remote is not None for session in fresh.sessions] == [False, True],
                "the owner's session comes back",
                seen=lambda: [session.name for session in fresh.sessions],
            )
            assert list(fresh.peers.by_address) == [owner.socket_name]
        finally:
            fresh.stop()


async def test_an_upgrade_of_the_peer_opens_copy_mode_where_it_was(two_servers, tmp_path):
    owner, peer, client = two_servers
    owner.handle_command("set-option -g mode-keys emacs")
    owner_control = owner.arrangement.get_active_window().active_pane.terminal.terminal_control
    owner_control.feed_output("".join("line %d\r\n" % number for number in range(10)))
    there, here = await shown_here(owner, peer, client)
    sink = here.process.sink
    here.terminal.enter_copy_mode()
    await until(lambda: sink.copying is not None, "copy mode opens on the owner")
    here.process.key("\x1b[A", DEFAULT_KEY_PROFILE)
    opened_at = sink.copying
    await until(lambda: sink.copying != opened_at, "the caret moves up")
    left_at = sink.copying
    path = tmp_path / "peer.sqlite"
    save(peer, path)
    (old_link,) = links_of(owner)

    fresh = Pymux()
    fresh.options.test_mode = True
    async with fresh.running():
        try:
            load(fresh, path, _Copied())
            await start(fresh)
            (link,) = fresh.peers.by_address.values()
            await until(lambda: there.pane_id in link.tree.panes, "the owner's sessions come back")
            link.tree.panes[there.pane_id].process.seen(True)

            def sources_again():
                return [s for one in links_of(owner) if one is not old_link for s in one.peer.sources.values()]

            await until(sources_again, "the fresh peer mirrors the pane")
            (source,) = sources_again()
            await until(
                lambda: source.copying is not None and source.copying.place == left_at,
                "copy mode opens where it was",
                seen=lambda: source.copying and source.copying.place,
            )
            source.copying.end()
        finally:
            fresh.stop()


async def test_a_loopback_url_there_opens_through_a_port_here(two_servers, monkeypatch):
    "The program's port is taken here, by the program itself: the URL moves, and its bytes still reach it."
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)

    async def shout(stream):
        async with stream:
            async for data in stream:
                await stream.send(data.upper())

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_port)
    packets: list[dict] = []
    monkeypatch.setattr(client.connection, "send_packet", packets.append)

    def opened():
        return [packet[Field.DATA] for packet in packets if packet.get(Field.CMD) == Packet.OPEN]

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, shout)
        url = "http://127.0.0.1:%d/callback" % (port,)
        peer.outward.forward_osc(here, "1337", "OpenURL=:" + base64.b64encode(url.encode()).decode())
        await until(lambda: opened(), "the URL opens here", seen=lambda: packets)
        (moved,) = opened()
        assert moved == with_port(url, urlparse(moved).port) and urlparse(moved).port != port
        async with await anyio.connect_tcp("127.0.0.1", urlparse(moved).port) as stream:
            await stream.send(b"hello")
            with anyio.fail_after(5):
                assert await stream.receive() == b"HELLO"
        tasks.cancel_scope.cancel()


async def test_a_tunnel_passes_a_server_that_only_mirrors_the_pane(two_servers):
    "A third server mirrors the peer's mirror: its tunnel goes through the peer to the owner's machine."
    owner, peer, client = two_servers
    there, _ = await shown_here(owner, peer, client)

    async def shout(stream):
        async with stream:
            async for data in stream:
                await stream.send(data.upper())

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_port)
    leaf = Pymux()
    leaf.options.test_mode = True
    async with leaf.running(), anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, shout)
        try:
            await leaf.panes.create_window(STAYS)
            link = leaf.peers.of(peer.socket_name)
            link.watch()
            # The peer's mirror of `there`, as the leaf mirrors it in turn.
            middle = peer.peers.by_address[owner.socket_name].tree.panes[there.pane_id]
            await until(lambda: middle.pane_id in link.tree.panes, "the peer's mirror arrives at the leaf")
            far = link.tree.panes[middle.pane_id]
            moved = await leaf.peers.tunnels.opened(far.process, "http://127.0.0.1:%d/" % (port,), 60)
            async with await anyio.connect_tcp("127.0.0.1", urlparse(moved).port) as stream:
                await stream.send(b"hello")
                with anyio.fail_after(5):
                    assert await stream.receive() == b"HELLO"
        finally:
            tasks.cancel_scope.cancel()
            for pane in list(leaf.panes_by_id.values()):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()
            leaf.stop()


async def test_a_loopback_url_keeps_its_port_when_it_is_free_here(two_servers):
    owner, peer, client = two_servers
    there, here = await shown_here(owner, peer, client)
    probe = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = probe.extra(anyio.abc.SocketAttribute.local_port)
    await probe.aclose()
    url = "http://127.0.0.1:%d/callback" % (port,)

    assert await peer.peers.tunnels.opened(here.process, url, 60) == url
    assert await peer.peers.tunnels.opened(here.process, url, 60) == url
    assert list(peer.peers.tunnels.listening) == [port]


async def test_a_link_to_itself_mirrors_nothing(two_servers):
    owner, peer, client = two_servers
    link = peer.peers.of(peer.socket_name)
    updated = link.tree._updated
    link.watch()
    with anyio.fail_after(5):
        await updated.wait()
    assert [session.remote for session in peer.sessions] == [None]


async def test_a_pane_that_ends_there_ends_here(two_servers):
    owner, peer, client = two_servers
    await owner.panes.create_window(STAYS)
    gone = owner.arrangement.get_active_window().active_pane
    there, here = await shown_here(owner, peer, client)
    assert there is gone
    (link,) = peer.peers.by_address.values()
    window_count = len(here_session(peer).arrangement.windows)

    owner.panes.kill_pane(gone)
    await until(lambda: gone.pane_id not in link.tree.panes, "the tree loses the pane")
    assert here.process.is_terminated
    assert len(here_session(peer).arrangement.windows) == window_count - 1


async def test_the_owner_reports_the_focus_of_a_mirror(two_servers):
    "The program hears the focus from its owner once, never as keys from the peer."
    owner, peer, client = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_control = there.terminal.terminal_control
    owner_control.feed_output("\x1b[?1004h")
    typed: list[str] = []
    owner_control.process.write_input = typed.append

    there, here = await shown_here(owner, peer, client)
    (link,) = [connection.peer for connection in owner.connections if connection.peer is not None]
    await until(lambda: there.pane_id in link.sources, "the seen pane streams")
    here.process.focus(True)
    await until(lambda: typed == ["\x1b[I"], "the program hears the focus come", lambda: typed)
    here.process.focus(False)
    await until(lambda: typed == ["\x1b[I", "\x1b[O"], "the program hears the focus go", lambda: typed)

    here.process.focus(True)
    await until(lambda: len(typed) == 3, "the focus comes again", lambda: typed)
    here.process.kill()
    await until(lambda: typed[3:] == ["\x1b[O"], "a mirror that stops takes its focus along", lambda: typed)


async def test_the_owner_answers_for_the_terminal_at_the_peer(two_servers):
    """
    Nobody is attached to the owner, so the terminal at the peer is the
    only one looking: the owner's pane takes its cell, colours and
    keyboard. The owner answers the program, so the mirror needs none
    of them.
    """
    owner, peer, client = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_screen = there.terminal.terminal_control.screen
    # A painted screen answers with the theme's colours, whoever looks.
    owner.options_of_pane(there).paint_screen = False
    base = owner_screen.color_base
    theirs = PeerTerminal((7, 15), ColorBase(list(reversed(base.palette)), base.defaults), 1, "pygments:dracula")
    peer.terminals.peer_terminal = lambda: theirs

    there, here = await shown_here(owner, peer, client)

    def surroundings(screen) -> tuple[int, int, bool, int]:
        return (
            screen.cell_width,
            screen.cell_height,
            screen.color_base == theirs.color_base,
            screen.keyboard_source_flags,
        )

    def told(screen) -> bool:
        return surroundings(screen) == (7, 15, True, 1)

    await until(
        lambda: told(owner_screen), "the owner's pane takes the peer's terminal", lambda: surroundings(owner_screen)
    )

    # A painted screen answers with the theme the person at the peer sees.
    owner.options_of_pane(there).paint_screen = True
    owner.terminals.sync_color_bases()
    assert owner_screen.color_base == theme_color_base("pygments:dracula")

    here.process.kill()
    (link,) = [connection.peer for connection in owner.connections if connection.peer is not None]
    await until(lambda: not link.sources, "the mirror stops")
    assert owner_screen.keyboard_source_flags == 0, "a peer that watches nothing stops counting"
