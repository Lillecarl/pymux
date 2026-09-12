import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.layout import change_pane_size


def resize_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Resize the active pane, or zoom it.
    """
    try:
        left = int(args.left or 0)
        right = int(args.right or 0)
        up = int(args.up or 0)
        down = int(args.down or 0)
    except ValueError:
        raise CommandException("Expecting an integer.")

    w = pymux.arrangement.get_active_window()

    if w and w.active_pane is not None:
        change_pane_size(
            pymux, w, w.active_pane, up=up, right=right, down=down, left=left
        )

        # Zoom in/out.
        if args.Z:
            w.zoom = not w.zoom


def register(subparsers):
    parser = add_command(subparsers, resize_pane)
    parser.add_argument("-L", dest="left", metavar="<left>", help="That many columns narrower.")
    parser.add_argument("-U", dest="up", metavar="<up>", help="That many rows shorter.")
    parser.add_argument("-D", dest="down", metavar="<down>", help="That many rows taller.")
    parser.add_argument("-R", dest="right", metavar="<right>", help="That many columns wider.")
    parser.add_argument("-Z", dest="Z", action="store_true", help="Zoom the pane in or out.")
