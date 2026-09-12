import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def delete_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Remove a named buffer. Lillecarl/pymux#303.
    """
    name = args.buffer_name
    if name not in pymux.named_buffers:
        raise CommandException("Can't find buffer: %s" % (name,))
    del pymux.named_buffers[name]


def register(subparsers):
    parser = add_command(subparsers, delete_buffer)
    parser.add_argument("-b", dest="buffer_name", metavar="<buffer-name>", required=True, help="The named buffer to remove.")
