from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import the_pane
from pymux.layout.geometry import change_pane_size


def resize_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Resize a pane, the active one by default, or zoom it.
    """
    try:
        left = int(args.left or 0)
        right = int(args.right or 0)
        up = int(args.up or 0)
        down = int(args.down or 0)
    except ValueError:
        raise CommandException("Expecting an integer.")

    pane = the_pane(pymux, args.target_pane)
    w = pymux._window_holding(pane)
    if w is None:
        raise CommandException("can't find pane: %s" % (args.target_pane,))

    change_pane_size(pymux, w, pane, up=up, right=right, down=down, left=left)

    # Zoom in/out.
    if args.Z:
        w.zoom = not w.zoom


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, resize_pane, forwards=True)
    add_target(parser, "-t", kind=Target.PANE, dest="target_pane", help="The pane to resize.")
    parser.add_argument("-L", dest="left", metavar="<left>", help="That many columns narrower.")
    parser.add_argument("-U", dest="up", metavar="<up>", help="That many rows shorter.")
    parser.add_argument("-D", dest="down", metavar="<down>", help="That many rows taller.")
    parser.add_argument("-R", dest="right", metavar="<right>", help="That many columns wider.")
    parser.add_argument("-Z", dest="Z", action="store_true", help="Zoom the pane in or out.")
