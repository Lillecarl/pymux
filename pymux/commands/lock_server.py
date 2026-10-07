from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def lock_server(pymux: Pymux, args: argparse.Namespace):
    """
    Lock the server: every session of it, and every client on them.

    The lock covers the screen with the program `lock-command` names.

    An overlay belongs to one session, so this opens one on each --
    which is what "the server" means with more than one session, and
    what separates this from lock-session. Lillecarl/pymux#324.
    """
    return _lock_each(pymux)


async def _lock_each(pymux: Pymux) -> None:
    for session in list(pymux.sessions):
        await pymux.display_overlay(
            command=pymux.lock_command,
            width="100%",
            height="100%",
            session=session,
        )


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, lock_server)
