import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def kill_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill this session, and the server that runs it.

    This is the way the last `tmux kill-session` ends its server.
    """
    pymux.stop()


def register(subparsers):
    add_command(subparsers, kill_session)
