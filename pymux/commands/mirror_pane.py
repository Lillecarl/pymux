from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.commands import CommandParser, add_command
from pymux.core.planes import remember_the_size_made_at
from pymux.enums import Woke
from pymux.mirror.program import RemoteProgram

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.core.pane import Pane


def mirror_pane(pymux: Pymux, args: argparse.Namespace):
    """
    Show a pane of another pymux server in a new window here.

    The other server runs the program and owns the pane's screen and
    size; this window shows that screen and sends it the keys typed
    here. The address is a socket path on this machine, or
    `ssh://[user@]host[:port][/path]`. One link carries every pane of
    one server. Killing the window stops the mirror and leaves the pane
    running on its own server. Lillecarl/pymux#759.
    """
    return _open(pymux, args.address, args.pane_id, args.d)


async def _open(pymux: Pymux, address: str, pane_id: int, detached: bool) -> Pane:
    session = pymux.current_session
    peer = pymux.peers.of(address)
    pane = await pymux.panes.create_pane(session=session, program=RemoteProgram.of(peer, pane_id))
    remember_the_size_made_at(session.arrangement.create_window(pane, name="%s %%%d" % (address, pane_id)), pane)
    if not detached:
        pane.focus()
    pymux.invalidate(Woke.WINDOW_OPENED)
    return pane


def _pane_id(text: str) -> int:
    return int(text.removeprefix("%"))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, mirror_pane, name="mirror-pane")
    parser.add_argument("-d", dest="d", action="store_true", help="Do not make the new window the current one.")
    parser.add_argument("address", metavar="<address>", help="The other server's socket path, or ssh://.")
    parser.add_argument("pane_id", metavar="<pane-id>", type=_pane_id, help="The pane there, as %%<id> or <id>.")
