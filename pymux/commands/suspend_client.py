import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def suspend_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Suspend this client, the way ctrl+z suspends a program in a shell.
    """
    connection = pymux.get_connection()

    if connection:
        connection.suspend_client_to_background()


def register(subparsers):
    add_command(subparsers, suspend_client)
