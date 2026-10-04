from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_pane
from pymux.enums import Woke


def respawn_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Kill the program of a pane and run a new one in its place.

    -k: kill even when the program still runs. Without it a pane
    whose program is alive refuses -- the same rule tmux's has. -t
    targets; without it the active pane. The pane keeps its place
    and its size; its id and its screen are new, because they belong
    to the program. A pane whose program ended is already out of the
    tree -- pymux does not keep dead panes on screen -- and there is
    nothing to respawn. Lillecarl/pymux#306.
    """
    pane = the_pane(pymux, args.target_pane)

    replace_pane_program(pymux, pane, args)


def replace_pane_program(pymux: Pymux, pane: Pane, args: argparse.Namespace) -> None:
    """
    The tail `respawn-window` shares: the pane is chosen, and the
    program that runs in it is replaced.
    """
    if pymux._window_holding(pane) is None:
        raise CommandException(
            "Can't respawn a pane whose window is gone: a pane that ends leaves the tree, unless remain-on-exit holds it."
        )

    if not pane.process.is_terminated and not args.k:
        raise CommandException("Pane is busy: -k kills a program that runs.")

    pane.process.kill()
    # The pane stays where it was: a respawn restarts the program in
    # the directory it reported, and a pane that never reported starts
    # the way a new one does.
    new_pane = pymux._create_pane(command=args.command or None, start_directory=pane.current_directory)
    pymux.arrangement.replace_pane(pane, new_pane)
    pymux.invalidate(Woke.PANE_WAS_RESPAWNED)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, respawn_pane)
    parser.add_argument("-k", dest="k", action="store_true", help="Kill a program that still runs.")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to respawn.")
    parser.add_argument(
        "command", nargs="?", metavar="<command>", help="The program to run, instead of the default shell."
    )
