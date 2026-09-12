import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def move_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this window to another index.
    """
    dst_window = args.dst_window
    try:
        new_index = int(dst_window)
    except ValueError:
        raise CommandException("Invalid window index: %r" % (dst_window,))

    # Check first whether the index was not yet taken.
    if pymux.arrangement.get_window_by_index(new_index):
        raise CommandException("Can't move window: index in use.")

    # Save index.
    w = pymux.arrangement.get_active_window()
    pymux.arrangement.move_window(w, new_index)


def register(subparsers):
    parser = add_command(subparsers, move_window)
    parser.add_argument("-t", dest="dst_window", metavar="<dst-window>", required=True, help="The index to move to.")
