import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def kill_server(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill the server, and the session that runs in it.

    Pymux has one session per server, so this is the same as
    `kill-session`.
    """
    pymux.stop()


def register(subparsers):
    add_command(subparsers, kill_server)
