import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def rename_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Rename the active pane.
    """
    pymux.arrangement.get_active_pane().chosen_name = args.name


def register(subparsers):
    parser = add_command(subparsers, rename_pane)
    parser.add_argument("name", metavar="<name>", help="The new name of the pane.")
