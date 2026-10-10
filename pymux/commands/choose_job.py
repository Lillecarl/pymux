from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.enums import Chooser


def choose_job(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show the jobs of the server, newest first, to choose from.

    Enter shows the pointed job in this session's overlay,
    read-only; `o` opens it as a pane in a new window, and `t`
    runs its command again, interactively, in a new window. `/`
    searches the commands and the tags. Escape closes.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing -- the same shape as the pop-ups.
    Lillecarl/pymux#272. Lillecarl/pymux#528.
    """
    if pymux.command_output is not None:
        return
    pymux.get_client_state().layout_manager.display_box_chooser(Chooser.JOB)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, choose_job)
