import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def last_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Focus the pane that was active before this one.
    """
    w = pymux.arrangement.get_active_window()
    prev_active_pane = w.previous_active_pane

    if prev_active_pane:
        w.active_pane = prev_active_pane


def register(subparsers):
    add_command(subparsers, last_pane)
