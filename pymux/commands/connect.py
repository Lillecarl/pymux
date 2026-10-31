from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.commands import CommandParser, add_command

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
    of two servers. Lillecarl/pymux#759.
    """
    pymux.peers.of(args.address).watch()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, connect)
    parser.add_argument("address", metavar="<address>", help="The other server's socket path, or ssh://.")
