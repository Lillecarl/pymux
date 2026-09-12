import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def next_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Focus the next window."
    pymux.arrangement.focus_next_window()


def register(subparsers):
    add_command(subparsers, next_window)
