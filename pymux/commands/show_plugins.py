from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import show_listing


def show_plugins(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    One line per event plugin: its name and whether it runs.

    The line also says how many events it lost by falling behind, and
    why it failed. Lillecarl/pyterm#982.
    """
    show_listing(pymux, "show-plugins", "\n".join(pymux.plugins.listing()))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, show_plugins, read_only=True)
