import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def previous_layout(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Select previous layout."
    pane = pymux.arrangement.get_active_window()
    if pane:
        pane.select_previous_layout()


def register(subparsers):
    add_command(subparsers, previous_layout)
