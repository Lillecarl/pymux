from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import add_pane_command_argument, the_window
from pymux.commands.respawn_pane import replace_pane_program


def respawn_window(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None]:
    """
    Kill the program a window's active pane runs, and start a new one in its place.

    tmux's respawn-window restarts the command of the window; a
    window of pymux has no command of its own, only the panes in it,
    so this respawns the active pane of the window the target names,
    or of the active one. The same `-k` rule as respawn-pane has.
    Lillecarl/pyterm#308.
    """
    window = the_window(pymux, args.target_window)

    pane = window.active_pane
    if pane is None:
        raise CommandException("no current pane")

    return replace_pane_program(pymux, pane, args)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, respawn_window, forwards=True)
    parser.add_argument("-k", dest="k", action="store_true", help="Kill a program that still runs.")
    add_target(parser, "-t", kind=Target.WINDOW, dest="target_window", help="The window whose active pane respawns.")
    add_pane_command_argument(
        parser, "What to run instead of the default shell: one string through it, or a program and its arguments."
    )
