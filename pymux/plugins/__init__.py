"""
Commands written as plugins: every module in this directory.

A plugin has the shape of a command module: a function that takes the
server and the parsed arguments, and a `register(subparsers)` that
calls `add_command` for it. The tree mounts each one after the
commands of pymux, so a plugin command runs from a key binding, the
command bar, a configuration file and the socket, and the shell
completes it. A name a command already has is refused by argparse
when the tree is built. Lillecarl/pymux#529.
"""

from __future__ import annotations

import argparse
import pkgutil
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.commands import CommandParser


def register(subparsers: argparse._SubParsersAction[CommandParser]) -> None:
    for plugin in pkgutil.iter_modules(__path__):
        import_module("." + plugin.name, __name__).register(subparsers)
