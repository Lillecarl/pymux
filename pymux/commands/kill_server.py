from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def kill_server(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Kill the server, and every session in it.

    `kill-session` takes one session; this takes them all, and the
    clients go with it.
    """
    pymux.stop()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, kill_server)
