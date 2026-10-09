"""
The example plugin: a command that counts the panes of each window.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer


def count_panes(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Print how many panes each window of the session holds.
    """
    windows = sorted(pymux.arrangement.windows, key=lambda window: window.index)
    answer(pymux, "\n".join("%s: %s %d" % (window.index, window.name, len(window.panes)) for window in windows))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, count_panes, read_only=True)
