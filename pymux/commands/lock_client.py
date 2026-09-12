import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.lock import lock


def lock_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Lock the calling client, and take the keyboard until it is done.

    The lock covers the screen with the program `lock-command` names.

    The overlay belongs to the session, so the other clients see it
    too -- which is what lock-session says; there is no per-client
    screen to cover alone. Lillecarl/pymux#297.
    """
    lock(pymux, args)


def register(subparsers):
    add_command(subparsers, lock_client)
