from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_pane, terminal_of


def paste_buffer(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Paste the buffer of the session into the pane.

    The buffer holds what copy mode copied and what a pane wrote to the
    clipboard of the user. It belongs to the session, so the command
    reads it there and not from the application of one client.
    """
    pane = find_pane(pymux, None)
    if pane is None:
        raise CommandException("no current pane")
    terminal = terminal_of(pane)
    terminal.process.write_input(terminal.screen.wrap_paste(pymux.clipboard.get_data().text))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, paste_buffer)
