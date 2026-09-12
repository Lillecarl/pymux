import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_window
from pymux.commands.respawn_pane import replace_pane_program


def respawn_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill the program a window's active pane runs, and start a new one in its place.

    tmux's respawn-window restarts the command of the window; a
    window of pymux has no command of its own, only the panes in it,
    so this respawns the active pane of the window the target names,
    or of the active one. The same `-k` rule as respawn-pane has.
    Lillecarl/pymux#306.
    """
    if args.target_window:
        window = find_window(pymux, args.target_window)
        if window is None:
            raise CommandException(
                "Can't find window: %s" % (args.target_window,)
            )
    else:
        window = pymux.arrangement.get_active_window()

    replace_pane_program(pymux, window.active_pane, args)


def register(subparsers):
    parser = add_command(subparsers, respawn_window)
    parser.add_argument("-k", dest="k", action="store_true", help="Kill a program that still runs.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window whose active pane respawns.")
    parser.add_argument("command", nargs="?", metavar="<command>", help="The program to run, instead of the default shell.")
