import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.lock import lock


def lock_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Lock the session: cover the screen with the program
    `lock-command` names, and take the keyboard until it is done.

    Every client of the server watches this one session, so this is
    the whole of what locking means here; lock-server says the same
    thing for the same screen. Lillecarl/pymux#297.
    """
    lock(pymux, args)


def register(subparsers):
    add_command(subparsers, lock_session)
