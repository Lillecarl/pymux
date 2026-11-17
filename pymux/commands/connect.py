from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

from pymux.client.defaults import is_ssh_url
from pymux.commands import CommandException, CommandParser, add_command

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def connect(pymux: Pymux, args: argparse.Namespace):
    """
    Show the sessions of another pymux server beside this server's own.

    Each of its sessions appears here as `host/name`, in the chooser and
    for `switch-client`. The other server runs the programs and lays out
    the windows; this one shows them and sends the keys typed here. The
    address is a socket path on this machine, or
    `ssh://[user@]host[:port][/path]`. A session never mixes the windows
    of two servers. Lillecarl/pyterm#761.

    Run again on a link that is down, it dials at once. Lillecarl/pyterm#876.
    """
    _refuse_a_machine_named_as_a_path(args.address)
    peer = pymux.peers.of(args.address)
    peer.watch()
    if peer.connected:
        pymux.show_message("%s is linked already." % (args.address,))
        return
    peer.dial_now()
    pymux.show_message("Linking to %s." % (args.address,))


def _refuse_a_machine_named_as_a_path(address: str) -> None:
    """
    `user@host` reads as a socket path and fails as "No such file or
    directory". A path with a `/` stays, socket or not: its socket may
    come later (Lillecarl/pyterm#876). Lillecarl/pyterm#1025.
    """
    if is_ssh_url(address) or "/" in address or os.path.exists(address):
        return
    raise CommandException("no socket at %s; a machine is ssh://[user@]host" % (address,))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, connect)
    parser.add_argument("address", metavar="<address>", help="The other server's socket path, or ssh://.")
