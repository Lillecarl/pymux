import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command


def attach_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Attach the calling client to the session.

    A client that reaches this command is attached already: the words
    arrive over the socket of the session they name, and pymux holds
    one session per server. tmux's attach-session moves a client
    between sessions, and there is nothing to move to here, so the
    command says so rather than pretending to attach.
    Lillecarl/pymux#297.
    """
    raise CommandException("Already attached: pymux holds one session per server.")


def register(subparsers):
    add_command(subparsers, attach_session)
