from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import find_pane, the_window
from pymux.layout.layout import move_focus
from pymux.layout.rects import Side


def select_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Focus a pane beside this one, or rotate the panes of the window.
    """
    if args.pane_id:
        pane_id = args.pane_id

        if pane_id == ":.+":
            the_window(pymux, None).focus_next()
        elif pane_id == ":.-":
            the_window(pymux, None).focus_previous()
        else:
            pane = find_pane(pymux, pane_id)
            window = pymux._window_holding(pane) if pane is not None else None
            if pane is None or window is None:
                raise CommandException("can't find pane: %s" % (pane_id,))
            # Its own window's: the window a person used last may not
            # hold it. Lillecarl/pyterm#508.
            window.active_pane = pane

    elif args.l:
        the_window(pymux, None).rotate(with_pane_after_only=True)

    else:
        if args.L:
            side = Side.LEFT
        elif args.U:
            side = Side.ABOVE
        elif args.D:
            side = Side.BELOW
        else:
            side = Side.RIGHT

        # From the pane the command came from: a forwarded command
        # names the person's pane, which the owner may not hold active.
        # Lillecarl/pyterm#874.
        move_focus(pymux, side, find_pane(pymux, None))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, select_pane, forwards=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", dest="L", action="store_true", help="Focus the pane to the left.")
    group.add_argument("-R", dest="R", action="store_true", help="Focus the pane to the right.")
    group.add_argument("-U", dest="U", action="store_true", help="Focus the pane above.")
    group.add_argument("-D", dest="D", action="store_true", help="Focus the pane below.")
    group.add_argument("-l", dest="l", action="store_true", help="Rotate the panes of the window once.")
    add_target(group, "-t", kind=Target.PANE, dest="pane_id", help="The pane to focus.")
