from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import terminal_of


def clear_history(pymux: Pymux, args: argparse.Namespace) -> None:
    "Clear the scrollback of the pane."
    pane = pymux.arrangement.get_active_pane()
    if pane is None:
        raise CommandException("no current pane")

    # A copy mode open on the pane reads its own copy of the history,
    # which this leaves alone.
    terminal_of(pane).screen.clear_history()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, clear_history)
