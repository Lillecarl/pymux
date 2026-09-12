import argparse
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.clipboard import ClipboardData
from pymux.commands import CommandException
from pymux.commands import add_command


def load_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read a file into a buffer.

    `-b` names the buffer; without it the session's one buffer fills.
    Lillecarl/pymux#303.
    """
    filename = os.path.expanduser(args.filename)
    try:
        with open(filename, "r") as f:
            text = f.read()
    except OSError as e:
        raise CommandException("IOError: %s" % (e,))

    name = args.buffer_name
    if name:
        pymux.named_buffers[name] = text
    else:
        pymux.clipboard.set_data(ClipboardData(text))


def register(subparsers):
    parser = add_command(subparsers, load_buffer)
    parser.add_argument("-b", dest="buffer_name", metavar="<buffer-name>", help="The named buffer to fill.")
    parser.add_argument("filename", metavar="<filename>")
