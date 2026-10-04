import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def choose_notifications(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Show the notifications the hub keeps, to choose from.

    The hub lists what every source recorded -- a pane's OSC 99, a
    `notify` call -- newest first, with when each arrived and which
    pane it came from. Enter takes this client to that pane, q and
    Escape close. It runs the keys of the other choosers: `/`
    searches, j and k with the arrows move.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing -- the same shape as the pop-ups.
    Lillecarl/pymux#272.
    """
    if pymux.command_output is not None:
        return
    pymux.get_client_state().layout_manager.display_notifications_chooser()


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, choose_notifications)
