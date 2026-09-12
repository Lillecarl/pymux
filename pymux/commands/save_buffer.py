import argparse
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def save_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Write a buffer to a file.

    `-b` names the buffer; without it the session's one buffer is
    written. Lillecarl/pymux#303.
    """
    name = args.buffer_name
    if name:
        if name not in pymux.named_buffers:
            raise CommandException("Can't find buffer: %s" % (name,))
        text = pymux.named_buffers[name]
    else:
        text = pymux.clipboard.get_data().text

    filename = os.path.expanduser(args.filename)
    try:
        with open(filename, "w") as f:
            f.write(text)
    except OSError as e:
        raise CommandException("IOError: %s" % (e,))


def register(subparsers):
    parser = add_command(subparsers, save_buffer)
    parser.add_argument("-b", dest="buffer_name", metavar="<buffer-name>", help="The named buffer to write.")
    parser.add_argument("filename", metavar="<filename>")
