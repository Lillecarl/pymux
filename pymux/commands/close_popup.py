import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def close_popup(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Close the overlay pane, and kill what runs in it.
    """
    pymux.close_overlay()


def register(subparsers):
    add_command(subparsers, close_popup)
