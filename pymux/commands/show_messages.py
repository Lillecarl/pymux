import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing


def show_messages(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per message the server said, oldest last.

    The warnings, the answers and the errors of this session: what
    show_message put on the message line, and what a command that
    failed said. tmux pages through them per client; the server here
    holds one log, which is one answer for every client that reads
    it. Lillecarl/pymux#297.
    """
    show_listing(pymux, "show-messages", "\n".join(pymux.message_log))


def register(subparsers):
    add_command(subparsers, show_messages)
