from __future__ import annotations

import argparse
import os
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer


def restart_holder(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    End the holder, and start a new one that adopts every program.

    No pane notices: this server holds its own copy of every terminal,
    so the programs keep theirs while the holder changes, and only a
    program's exit code from before is lost. It is how a fix to the
    holder reaches a running server, which `upgrade-server` cannot.
    Lillecarl/pymux#730.
    """
    holding = pymux.holding
    if holding is None or holding.pid is None:
        raise CommandException("this server has no holder")
    # The old holder need not know `adopt`: it only has to end, and the
    # new one adopts. So today's holder can be replaced too.
    os.kill(holding.pid, signal.SIGTERM)
    answer(pymux, "The holder (pid %d) ends; a new one adopts every program." % (holding.pid,))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, restart_holder)
