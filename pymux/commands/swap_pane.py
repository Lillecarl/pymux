import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def swap_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Swap the active pane with the one above or below.
    """
    pymux.arrangement.get_active_window().rotate(with_pane_after_only=args.U)


def register(subparsers):
    parser = add_command(subparsers, swap_pane)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-D", dest="D", action="store_true")
    group.add_argument("-U", dest="U", action="store_true", help="Swap with the pane below.")
