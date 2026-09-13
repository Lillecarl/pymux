import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def kill_server(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill the server, and every session in it.

    `kill-session` takes one session; this takes them all, and the
    clients go with it.
    """
    pymux.stop()


def register(subparsers):
    add_command(subparsers, kill_server)
