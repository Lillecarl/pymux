import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def rotate_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Rotate the panes of the window.
    """
    if args.D:
        pymux.arrangement.rotate_window(count=-1)
    else:
        pymux.arrangement.rotate_window()


def register(subparsers):
    parser = add_command(subparsers, rotate_window)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-D", dest="D", action="store_true", help="Rotate the other way.")
    group.add_argument("-U", dest="U", action="store_true")
