import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.lock import lock


def lock_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Lock the session, and take the keyboard until it is done.

    The lock covers the screen with the program `lock-command` names.

    The overlay that covers the screen belongs to the session, so
    this locks every client of this session and no other.
    Lillecarl/pymux#324.
    """
    lock(pymux, args)


def register(subparsers):
    add_command(subparsers, lock_session)
