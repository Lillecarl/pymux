from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import the_window


def last_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Focus the pane that was active before this one.
    """
    w = the_window(pymux, None)
    prev_active_pane = w.previous_active_pane

    if prev_active_pane:
        w.active_pane = prev_active_pane


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, last_pane)
