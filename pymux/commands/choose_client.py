from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.enums import Chooser


def choose_client(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show the attached clients, to choose one to detach.

    Enter or `d` detaches the pointed client, as tmux's choose-client
    does with its default command. `/` searches what each row says.
    Prefix D opens it, as in tmux. Lillecarl/pyterm#1050.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing -- the same shape as the pop-ups.
    """
    if pymux.answer is not None:
        return
    pymux.get_client_state().layout_manager.chooser.display_box_chooser(Chooser.CLIENT)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, choose_client, read_only=True)
