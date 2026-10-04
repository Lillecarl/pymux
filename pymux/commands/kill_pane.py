from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import the_pane


def kill_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Kill a pane, or the active one.
    """
    pane = the_pane(pymux, args.target_pane)
    pymux.kill_pane(pane)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, kill_pane)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to kill.")
