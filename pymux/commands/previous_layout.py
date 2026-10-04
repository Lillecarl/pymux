import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import the_window


def previous_layout(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Select previous layout."
    the_window(pymux, None).select_previous_layout()


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, previous_layout)
