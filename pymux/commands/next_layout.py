import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import the_window


def next_layout(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Select next layout."
    the_window(pymux, None).select_next_layout()


def register(subparsers):
    add_command(subparsers, next_layout)
