import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def paste_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Paste the buffer of the session into the pane.

    The buffer holds what copy mode copied and what a pane wrote to the
    clipboard of the user. It belongs to the session, so the command
    reads it there and not from the application of one client.
    """
    pane = pymux.arrangement.get_active_pane()
    pane.process.write_input(pane.screen.wrap_paste(pymux.clipboard.get_data().text))


def register(subparsers):
    add_command(subparsers, paste_buffer)
