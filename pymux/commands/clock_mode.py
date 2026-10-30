from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import find_pane


def clock_mode(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show a clock in the active pane, or put the program back.

    A socket command from a pane shows it in that pane instead.
    """
    pane = find_pane(pymux, None)
    if pane:
        pane.clock_mode = not pane.clock_mode


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, clock_mode)
