import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.enums import Woke


def break_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Take the active pane out of its window, into one of its own.
    """
    dont_focus_window = args.d

    pymux.arrangement.break_pane(set_active=not dont_focus_window)
    pymux.invalidate(Woke.PANE_BROKE_OUT)


def register(subparsers):
    parser = add_command(subparsers, break_pane)
    parser.add_argument("-d", dest="d", action="store_true", help="Leave the new window unfocused.")
