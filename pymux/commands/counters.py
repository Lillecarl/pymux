import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import show_listing
from pymux import introspect


def counters(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Say what this server has done, and how often.

    The half a stack cannot give: a stack says where the server is in
    one instant, and this says what it has been doing for an hour.
    """
    show_listing(pymux, "counters", introspect.counters(pymux))


def register(subparsers):
    add_command(subparsers, counters)
