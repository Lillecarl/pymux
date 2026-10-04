import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def previous_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Focus the previous window."
    pymux.arrangement.focus_previous_window()


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, previous_window)
