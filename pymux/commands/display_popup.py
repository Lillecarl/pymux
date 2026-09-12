import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def display_popup(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Open an overlay pane in the middle of the screen.

    It runs the given program, or the default shell, and closes itself
    when that finishes. `-w` and `-h` take a number of cells or a share
    of the screen, like "80" or "60%". `-T` names the title bar.

    A session has one overlay at a time, so a second call replaces the
    first. `-E` is accepted for the tmux command line and changes
    nothing: an overlay of pymux always closes when its program ends.
    """
    pymux.display_overlay(
        command=args.executable,
        width=args.width,
        height=args.height,
        title=args.title,
    )


def register(subparsers):
    parser = add_command(subparsers, display_popup)
    parser.add_argument("-E", dest="E", action="store_true", help="Accepted for tmux. The overlay always closes when its program ends.")
    parser.add_argument("-w", dest="width", metavar="<width>", help="How many cells wide, or a share like '60%%'.")
    parser.add_argument("-h", dest="height", metavar="<height>", help="How many cells high, or a share like '60%%'.")
    parser.add_argument("-T", dest="title", metavar="<title>", help="The name on the title bar.")
    parser.add_argument("executable", nargs="?", metavar="<executable>", help="The program to run.")
