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

from pymux.commands.mirror_pane import _open
from pymux.core.main import Pymux
from pymux.mirror.program import RemoteProgram

STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


def rows_of(control) -> list[str]:
    screen = control.screen
    return ["".join(screen.page.data_buffer[y][x].char for x in range(screen.columns)) for y in range(screen.lines)]


async def until(condition, what: str) -> None:
    "Wait for `condition`, which the other server's packets make true."
    try:
        with anyio.fail_after(5):
            while not condition():
                await anyio.sleep(0.01)
    except TimeoutError:
        raise AssertionError("never: %s" % what) from None


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
