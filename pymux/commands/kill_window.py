from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, Target, add_command, add_target
from pymux.commands.common import the_window


def kill_window(pymux: Pymux, args: argparse.Namespace) -> None:
    "Kill all panes in the current window."
    w = the_window(pymux, args.target_window)

    for pane in w.panes:
        pymux.panes.kill_pane(pane)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, kill_window, forwards=True)
    add_target(parser, "-t", kind=Target.WINDOW, dest="target_window", help="The window to kill.")
