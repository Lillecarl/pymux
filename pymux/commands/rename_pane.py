from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command


def rename_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Rename the active pane.
    """
    pane = pymux.arrangement.get_active_pane()
    if pane is None:
        raise CommandException("no current pane")
    pane.chosen_name = args.name


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, rename_pane)
    parser.add_argument("name", metavar="<name>", help="The new name of the pane.")
