from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_window
from pymux.commands.respawn_pane import replace_pane_program


def respawn_window(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Kill the program a window's active pane runs, and start a new one in its place.

    tmux's respawn-window restarts the command of the window; a
    window of pymux has no command of its own, only the panes in it,
    so this respawns the active pane of the window the target names,
    or of the active one. The same `-k` rule as respawn-pane has.
    Lillecarl/pymux#306.
    """
    window = the_window(pymux, args.target_window)

    pane = window.active_pane
    if pane is None:
        raise CommandException("no current pane")

    replace_pane_program(pymux, pane, args)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, respawn_window)
    parser.add_argument("-k", dest="k", action="store_true", help="Kill a program that still runs.")
    parser.add_argument(
        "-t", dest="target_window", metavar="<target-window>", help="The window whose active pane respawns."
    )
    parser.add_argument(
        "command", nargs="?", metavar="<command>", help="The program to run, instead of the default shell."
    )
