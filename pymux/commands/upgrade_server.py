from __future__ import annotations

import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command


def upgrade_server(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None]:
    """
    Run a new build of pymux in this server's place, and keep every pane.

    COMMAND starts the new build, `pymux` on the server's PATH by
    default. It loads a snapshot of this server first, and a build that
    cannot is refused with nothing lost. Attached clients wait for the
    new server and attach again. Lillecarl/pymux#399.
    """
    from pymux.upgrade import upgrade

    command = shlex.split(args.command)
    if not command:
        raise CommandException("no command to start the new build")
    return upgrade(pymux, command)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, upgrade_server)
    parser.add_argument("command", nargs="?", default="pymux", metavar="COMMAND")
