import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def noop(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Do nothing.

    It is what a table binds a key to when the key must be swallowed
    and nothing more: `bind-key -T copy-mode Any noop` makes that mode
    strict, so the keys it does not name go nowhere.
    Lillecarl/pymux#394.
    """


def register(subparsers):
    add_command(subparsers, noop)
