import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing


def show_hooks(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per hook: the event, and the commands it runs.

    The commands run in the order they were given.
    Lillecarl/pymux#297.
    """
    lines = [
        "%s: %s" % (name, "; ".join(commands))
        for name, commands in sorted(pymux.hooks.items())
    ]
    show_listing(pymux, "show-hooks", "\n".join(lines))


def register(subparsers):
    add_command(subparsers, show_hooks)
