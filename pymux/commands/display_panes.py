import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def display_panes(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Display the pane numbers."
    pymux.display_pane_numbers = True


def register(subparsers):
    add_command(subparsers, display_panes)
