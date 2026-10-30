from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_pane, terminal_of

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def unscroll_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Bring the history back above the cursor, until the cursor is on the bottom row.

    The empty rows under the cursor go, and as many rows of the history
    come back above it, as kitty's `scroll_prompt_to_bottom` does. After
    a clear, or a program that left the cursor high, the pane shows what
    came before. The program is told nothing. Only on the main screen.
    """
    pane = find_pane(pymux, args.target_pane)
    if pane is None:
        raise CommandException("unscroll-pane: no such pane")
    if terminal_of(pane).screen.scroll_cursor_to_bottom():
        pymux.invalidate()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, unscroll_pane)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to unscroll.")
