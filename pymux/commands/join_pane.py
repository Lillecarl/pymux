from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.move_pane import add_arguments, move_pane


def join_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Join a pane to another window.

    move-pane under tmux's other name for it. The pane goes in beside
    or above the pane that is focused in the window it joins, the
    inverse of what break-pane does. Lillecarl/pymux#297.
    """
    move_pane(pymux, args)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_arguments(add_command(subparsers, join_pane))
