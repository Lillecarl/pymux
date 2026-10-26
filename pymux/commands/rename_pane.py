from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_pane


def rename_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Rename a pane.

    The one `-t` names, else the pane a socket command came from, else
    the active pane. Lillecarl/pymux#506.
    """
    pane = find_pane(pymux, args.target_pane)
    if pane is None:
        raise CommandException("can't find pane: %s" % (args.target_pane,) if args.target_pane else "no current pane")
    pane.chosen_name = args.name


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, rename_pane)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to rename.")
    parser.add_argument("name", metavar="<name>", help="The new name of the pane.")
