import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def clear_history(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Clear the scrollback of the pane."
    pane = pymux.arrangement.get_active_pane()

    if pane.is_copying:
        raise CommandException("Not available in copy mode")
    else:
        pane.screen.clear_history()


def register(subparsers):
    add_command(subparsers, clear_history)
