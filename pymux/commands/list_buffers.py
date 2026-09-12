import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import answer


def list_buffers(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per named buffer: the name, and how much it holds.
    Lillecarl/pymux#303.
    """
    lines = [
        "%s %i" % (name, len(text))
        for name, text in sorted(pymux.named_buffers.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    add_command(subparsers, list_buffers)
