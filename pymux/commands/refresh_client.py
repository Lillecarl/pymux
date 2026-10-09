from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def refresh_client(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Draw this client's terminal again, every cell of it.

    This is what tmux's refresh-client does. A frame writes what
    changed, so a cell the terminal lost stays lost until something
    writes over it; this writes every cell. The other clients keep the
    frames they have. Lillecarl/pymux#301.
    """
    if pymux.command_output is not None:
        return  # The command line drew nothing and has nothing to draw.
    pymux.redraw(pymux.get_client_state())


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    # A client that only watches may redraw its own terminal: it
    # touches nobody else's.
    add_command(subparsers, refresh_client, aliases=("redraw",), read_only=True)
