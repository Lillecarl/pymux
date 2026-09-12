import argparse
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands import handle_command


def source_file(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read a configuration file.
    """
    filename = os.path.expanduser(args.filename)
    try:
        with open(filename, "r") as f:
            lines = list(f)
    except IOError as e:
        raise CommandException("IOError: %s" % (e,))

    # A line that fails names the file and the line it is on. Without
    # that a person reads "Invalid option: -g" and has to find which of
    # forty lines said it.
    for number, line in enumerate(lines, start=1):
        pymux.sourcing = "%s line %i" % (filename, number)
        try:
            handle_command(pymux, line)
        finally:
            pymux.sourcing = None


def register(subparsers):
    parser = add_command(subparsers, source_file)
    parser.add_argument("filename", metavar="<filename>", help="The configuration file to read.")
