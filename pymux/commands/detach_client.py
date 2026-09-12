import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import get_app
from pymux.commands import add_command


def detach_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Detach this client from the session.

    The session and its panes stay with the server.
    """
    pymux.detach_client(get_app())


def register(subparsers):
    add_command(subparsers, detach_client)
