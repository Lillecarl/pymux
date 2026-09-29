import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import the_window


def rename_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Rename the active window.
    """
    the_window(pymux, None).chosen_name = args.name


def register(subparsers):
    parser = add_command(subparsers, rename_window)
    parser.add_argument("name", metavar="<name>", help="The new name of the window.")
