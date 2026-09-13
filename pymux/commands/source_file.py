import argparse
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands import handle_command


def source_file(pymux: "Pymux", args: argparse.Namespace):
    """
    Read a configuration file.
    """
    filename = os.path.expanduser(args.filename)
    try:
        with open(filename, "r") as f:
            lines = list(f)
    except IOError as e:
        raise CommandException("IOError: %s" % (e,))

    return _read_lines(pymux, filename, list(enumerate(lines, start=1)))


def _read_lines(pymux: "Pymux", filename: str, lines: list):
    """
    Run the lines of the file, top to bottom.

    A line that waits takes the lines under it with it, so a
    configuration file means the same thing whether or not one of its
    commands has to wait. Lillecarl/pymux#87.
    """
    # A line that fails names the file and the line it is on. Without
    # that a person reads "Invalid option: -g" and has to find which of
    # forty lines said it.
    for index, (number, line) in enumerate(lines):
        pymux.sourcing = "%s line %i" % (filename, number)
        try:
            answer = handle_command(pymux, line)
        finally:
            pymux.sourcing = None

        if answer is not None:
            return _then_the_rest(
                pymux, filename, number, answer, lines[index + 1 :]
            )

    return None


async def _then_the_rest(
    pymux: "Pymux", filename: str, number: int, answer, rest: list
) -> None:
    pymux.sourcing = "%s line %i" % (filename, number)
    try:
        await answer
    finally:
        pymux.sourcing = None

    more = _read_lines(pymux, filename, rest)
    if more is not None:
        await more


def register(subparsers):
    parser = add_command(subparsers, source_file)
    parser.add_argument("filename", metavar="<filename>", help="The configuration file to read.")
