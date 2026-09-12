import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command


def switch_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this client to another session.

    tmux's switch-client walks a client between the sessions of a
    server. Every client of pymux watches the one session the server
    holds, so there is nowhere to walk to, and the command says so
    rather than doing nothing in silence. Lillecarl/pymux#297.
    """
    raise CommandException("Nowhere to switch: every client of this server watches its one session.")


def register(subparsers):
    add_command(subparsers, switch_client)
