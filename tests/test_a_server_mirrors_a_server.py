"""
One pymux server shows a pane of another, over the other's socket.
Lillecarl/pymux#759.

Two servers in one loop: the owner listens on a socket in the test's
directory, and the peer opens `mirror-pane` on it. The peer's new
window has to show the owner's pane, send keys to its program, and let
it go when the window is killed.
"""

from __future__ import annotations

import sys

import anyio
import pytest
from pyte.osc import ColorBase

from pymux.commands.mirror_pane import _open
from pymux.core.main import Pymux
from pymux.mirror.program import RemoteProgram
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
    owner = Pymux()
    owner.options.test_mode = True
    owner.serving.listen_on_socket(str(tmp_path / "owner.sock"))
    peer = Pymux()
    peer.options.test_mode = True
    async with owner.running(), peer.running():
        await owner.panes.create_window(STAYS)
        await peer.panes.create_window(STAYS)
        try:
            yield owner, peer
        finally:
            for mux in (owner, peer):
                for window in list(mux.arrangement.windows):
                    for pane in list(window.panes):
                        process = getattr(pane, "process", None)
                        if process is not None and not process.is_terminated:
                            process.kill()
            peer.stop()
            owner.stop()


async def test_a_window_here_shows_a_pane_there(two_servers):
    owner, peer = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_control = there.terminal.terminal_control
    owner_control.resize_screen(6, 30)
    owner_control.feed_output("hello from there\r\n")
    typed: list[str] = []
    owner_control.process.write_input = typed.append

    here = await _open(peer, owner.socket_name, int(there.pane_id), detached=False)
    assert isinstance(here.process, RemoteProgram)
    control = here.terminal.terminal_control

    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror shows the owner's screen")
    owner_control.feed_output("and more")
    await until(lambda: rows_of(control) == rows_of(owner_control), "the mirror follows the owner")

    control.process.write_input("ls\r")
    await until(lambda: typed == ["ls\r"], "a key typed here reaches the program there")

    here.process.kill()
    (link,) = [connection.peer for connection in owner.connections if connection.peer is not None]
    await until(lambda: not link.sources, "killing the window stops the mirror there")
    assert not owner_control.followers
    assert not there.process.is_terminated, "the pane runs on, on its own server"


async def test_the_owner_reports_the_focus_of_a_mirror(two_servers):
    "The program hears the focus from its owner once, never as keys from the peer."
    owner, peer = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_control = there.terminal.terminal_control
    owner_control.feed_output("\x1b[?1004h")
    typed: list[str] = []
    owner_control.process.write_input = typed.append

    here = await _open(peer, owner.socket_name, int(there.pane_id), detached=False)
    await until(lambda: here.terminal.terminal_control.screen.reports_focus, "the mirror shows the mode")
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
    keyboard, and the mirror takes them back from the owner. The peer's
    own idea of its panes never reaches the mirror.
    """
    owner, peer = two_servers
    there = owner.arrangement.get_active_window().active_pane
    owner_screen = there.terminal.terminal_control.screen
    # A painted screen answers with the theme's colours, whoever looks.
    owner.options_of_pane(there).paint_screen = False
    base = owner_screen.color_base
    theirs = PeerTerminal((7, 15), ColorBase(list(reversed(base.palette)), base.defaults), 1, "pygments:dracula")
    peer.terminals.peer_terminal = lambda: theirs

    here = await _open(peer, owner.socket_name, int(there.pane_id), detached=False)
    peer.terminals.tell_peers()
    mirror_screen = here.terminal.terminal_control.screen

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
    await until(
        lambda: told(mirror_screen), "the mirror takes what the owner settled on", lambda: surroundings(mirror_screen)
    )

    peer.terminals._cell_size_sent = None
    peer.terminals._keyboard_state_sent = None
    peer.terminals.sync_cell_size()
    peer.terminals.sync_keyboard_source_flags()
    peer.terminals.sync_color_bases()
    assert told(mirror_screen), "only the owner tells a mirrored pane about its surroundings"

    # A painted screen answers with the theme the person at the peer sees.
    owner.options_of_pane(there).paint_screen = True
    owner.terminals.sync_color_bases()
    assert owner_screen.color_base == theme_color_base("pygments:dracula")
    await until(lambda: mirror_screen.color_base == owner_screen.color_base, "the mirror takes the theme")

    here.process.kill()
    (link,) = [connection.peer for connection in owner.connections if connection.peer is not None]
    await until(lambda: not link.sources, "the mirror stops")
    assert owner_screen.keyboard_source_flags == 0, "a peer that watches nothing stops counting"
