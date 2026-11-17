from __future__ import annotations

import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command


def upgrade_server(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None]:
    """
    Run a new build of pymux in this server's place, and keep every pane.

    COMMAND starts the new build, `pymux` on the server's PATH by
    default. It loads a snapshot of this server first, and a build that
    cannot is refused with nothing lost. Attached clients wait for the
    new server and attach again. Lillecarl/pyterm#401.

    Wayland windows go on too: each compositor that holds one outlives
    the exec, and the new build adopts it and starts one of its own for
    new windows. A new build that cannot speak to one refuses the
    upgrade; `-W` closes that compositor's windows instead.
    Lillecarl/pyterm#1004, Lillecarl/pyterm#1012.
    """
    from pymux.core.upgrade import upgrade

    command = shlex.split(args.command)
    if not command:
        raise CommandException("no command to start the new build")
    return upgrade(pymux, command, close_windows=args.close_windows)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, upgrade_server)
    parser.add_argument(
        "-W",
        "--close-windows",
        action="store_true",
        help="close the Wayland windows of a compositor the new build cannot adopt, rather than refuse",
    )
    parser.add_argument("command", nargs="?", default="pymux", metavar="COMMAND")
