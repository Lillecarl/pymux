import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.lock import lock


def lock_server(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Lock the server, and take the keyboard until it is done.

    The lock covers the screen with the program `lock-command` names.

    The screen a server has is the session's overlay, and every
    client watches the one session, so this is the same screen
    lock-session covers. Lillecarl/pymux#297.
    """
    lock(pymux, args)


def register(subparsers):
    add_command(subparsers, lock_server)
