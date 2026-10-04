import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def clock_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Show a clock in the active pane, or put the program back.
    """
    pane = pymux.arrangement.get_active_pane()
    if pane:
        pane.clock_mode = not pane.clock_mode


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, clock_mode)
