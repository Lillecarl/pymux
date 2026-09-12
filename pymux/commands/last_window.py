import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def last_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Go to previous active window."
    w = pymux.arrangement.get_previous_active_window()

    if w:
        pymux.arrangement.set_active_window(w)


def register(subparsers):
    add_command(subparsers, last_window, name="last-window")
