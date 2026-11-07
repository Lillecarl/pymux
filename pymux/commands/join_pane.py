from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, Target, add_command
from pymux.commands.move_pane import add_arguments, move_pane


def join_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Join a pane to another window.

    move-pane under tmux's other name for it. The pane goes in beside
    or above the pane that is focused in the window it joins, the
    inverse of what break-pane does. Lillecarl/pyterm#299.
    """
    move_pane(pymux, args)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_arguments(add_command(subparsers, join_pane, acts_on={"s": Target.PANE, "t": Target.WINDOW}))
