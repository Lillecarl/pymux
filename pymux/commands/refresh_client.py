import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def refresh_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Ask this client for a frame.

    A client draws when something changed; this says something did,
    for the client that ran the command. The other clients keep the
    frames they have. Lillecarl/pymux#301.
    """
    if pymux.command_output is not None:
        return  # The command line drew nothing and has nothing to draw.
    pymux.get_client_state().app.invalidate()


def register(subparsers):
    add_command(subparsers, refresh_client)
