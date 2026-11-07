from __future__ import annotations

import argparse
from collections.abc import Awaitable
from typing import TYPE_CHECKING

import anyio

from pymux import log
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import show_listing, the_client

if TYPE_CHECKING:
    from pymux.connection.log_asks import LogAsks
    from pymux.core.main import Pymux

#: How many lines `show-log` shows when `-n` does not say.
LINES = 100


def show_log(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None]:
    """
    Show the end of the pymux log here, or on a client's or a peer's machine.

    Every pymux process logs on its own machine, so a fault at the far
    end of an `ssh://` attach or a peer link is in neither this
    server's log nor its reach. `-t` asks a client and `-p` a peer for
    theirs, over the link they already hold. A peer is named by its
    address or by its host, from either end of the link.
    Lillecarl/pyterm#936.
    """
    if args.target_client is not None and args.peer is not None:
        raise CommandException("show-log: -t and -p name two machines")
    if args.lines < 0:
        raise CommandException("show-log: -n takes a count of lines")
    if args.target_client is not None:
        client = the_client(pymux, args.target_client, "show-log").client
        return _show_asked(pymux, "client %s" % (client.connection.name,), client.log_asks, args.lines)
    if args.peer is not None:
        named, asks = _peer(pymux, args.peer)
        return _show_asked(pymux, "peer %s" % (named,), asks, args.lines)
    return _show_here(pymux, args.lines)


def _peer(pymux: Pymux, name: str) -> tuple[str, LogAsks]:
    "The peer link `name` names: one this server dialled, or one that dialled it."
    found: list[tuple[str, LogAsks]] = [
        (peer.address, peer.log_asks)
        for peer in pymux.peers.by_address.values()
        if peer.connected and name in (peer.address, peer.server)
    ]
    found += [(link.host, link.log_asks) for link in pymux.peer_links() if link.host and link.host == name]
    if not found:
        linked = sorted(
            {peer.address for peer in pymux.peers.by_address.values() if peer.connected}
            | {link.host for link in pymux.peer_links() if link.host}
        )
        raise CommandException(
            "show-log: no peer is linked as %s%s" % (name, "; linked: %s" % (", ".join(linked),) if linked else "")
        )
    if len(found) > 1:
        raise CommandException("show-log: %d peer links answer to %s; name one by its address" % (len(found), name))
    return found[0]


async def _show_asked(pymux: Pymux, who: str, asks: LogAsks, lines: int) -> None:
    try:
        text = await asks.ask(lines)
    except LookupError as why:
        raise CommandException("show-log: %s: %s" % (who, why)) from None
    show_listing(pymux, "show-log", text.rstrip("\n") or "The log of %s is empty." % (who,))


async def _show_here(pymux: Pymux, lines: int) -> None:
    try:
        text = await anyio.to_thread.run_sync(log.tail, lines)
    except OSError as why:
        raise CommandException("show-log: cannot read the log: %s" % (why,)) from None
    show_listing(pymux, "show-log", text.rstrip("\n") or "The log is empty.")


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_log)
    parser.add_argument(
        "-n", dest="lines", type=int, default=LINES, metavar="<lines>", help="How many lines, from the end."
    )
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client whose machine's log.")
    parser.add_argument("-p", dest="peer", metavar="<peer>", help="The peer link whose machine's log.")
