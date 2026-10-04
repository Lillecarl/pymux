import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def display_panes(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Display the pane numbers."
    pymux.display_pane_numbers = True


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, display_panes)
