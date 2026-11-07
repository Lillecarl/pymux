from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, Target, add_command, add_target
from pymux.commands.common import the_window


def rename_window(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Rename a window: the active one when -t names none.
    """
    the_window(pymux, args.target_window).chosen_name = args.name


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, rename_window, forwards=True)
    add_target(parser, "-t", kind=Target.WINDOW, dest="target_window", help="The window to rename.")
    parser.add_argument("name", metavar="<name>", help="The new name of the window.")
