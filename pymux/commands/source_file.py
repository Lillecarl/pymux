from __future__ import annotations

import argparse
import os
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command, handle_command


def source_file(pymux: Pymux, args: argparse.Namespace):
    """
    Read configuration files, in order.

    With `-q`, a file that is not there is no error, as in tmux.
    """
    lines: list[tuple[str, int, str]] = []
    for filename in args.filenames:
        filename = os.path.expanduser(filename)
        try:
            with open(filename) as f:
                lines.extend((filename, number, line) for number, line in enumerate(f, start=1))
        except FileNotFoundError as e:
            if not args.quiet:
                raise CommandException("IOError: %s" % (e,))
        except OSError as e:
            raise CommandException("IOError: %s" % (e,))

    return _read_lines(pymux, lines)


def _read_lines(pymux: Pymux, lines: list[tuple[str, int, str]]):
    """
    Run the lines of the files, top to bottom.

    A line that waits takes the lines under it with it, so a
    configuration file means the same thing whether or not one of its
    commands has to wait. Lillecarl/pyterm#89.
    """
    # A line that fails names the file and the line it is on. Without
    # that a person reads "Invalid option: -g" and has to find which of
    # forty lines said it.
    for index, (filename, number, line) in enumerate(lines):
        pymux.sourcing = "%s line %i" % (filename, number)
        try:
            answer = handle_command(pymux, line)
        finally:
            pymux.sourcing = None

        if answer is not None:
            return _then_the_rest(pymux, "%s line %i" % (filename, number), answer, lines[index + 1 :])

    return None


async def _then_the_rest(pymux: Pymux, where: str, answer: Awaitable[Any], rest: list[tuple[str, int, str]]) -> None:
    pymux.sourcing = where
    try:
        await answer
    finally:
        pymux.sourcing = None

    more = _read_lines(pymux, rest)
    if more is not None:
        await more


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, source_file)
    parser.add_argument("-q", dest="quiet", action="store_true", help="No error for a file that is not there.")
    parser.add_argument("filenames", metavar="<filename>", nargs="+", help="The configuration files to read.")
