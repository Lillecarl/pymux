from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def next_window(pymux: Pymux, args: argparse.Namespace) -> None:
    "Focus the next window."
    pymux.arrangement.focus_next_window()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, next_window)
