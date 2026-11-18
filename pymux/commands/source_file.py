from __future__ import annotations

import argparse
import os
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux import grammar
from pymux.commands import CommandException, CommandParser, _run_in_order, add_command, read_commands

#: One command of a file, with where it is: the file and its line.
Located = tuple[str, int, list[str]]


def source_file(pymux: Pymux, args: argparse.Namespace):
    """
    Read configuration files, in order.

    Each file is parsed whole before any of it runs, as tmux does: a
    `{ }` block or a `\\` may span lines, and a `%if` is judged while
    the file is read. A file that does not parse runs none of its
    commands. With `-q`, a file that is not there is no error, as in
    tmux.
    """
    commands: list[Located] = []
    for filename in args.filenames:
        filename = os.path.expanduser(filename)
        try:
            with open(filename) as f:
                text = f.read()
        except FileNotFoundError as e:
            if not args.quiet:
                raise CommandException("IOError: %s" % (e,))
            continue
        except OSError as e:
            raise CommandException("IOError: %s" % (e,))

        try:
            parsed = read_commands(pymux, text)
        except grammar.ParseError as e:
            pymux.sourcing = "%s line %i" % (filename, e.line)
            try:
                pymux.add_command_error("pymux: %s" % (e.message,))
            finally:
                pymux.sourcing = None
            continue
        commands.extend((filename, command.line, command.strings()) for command in parsed)

    return _run_located(pymux, commands)


def _run_located(pymux: Pymux, commands: list[Located]):
    """
    Run the commands of the files, top to bottom.

    A command that waits takes the commands under it with it, so a
    configuration file means the same thing whether or not one of its
    commands has to wait. Lillecarl/pyterm#89.
    """
    # A command that fails names the file and the line it is on. Without
    # that a person reads "Invalid option: -g" and has to find which of
    # forty lines said it.
    for index, (filename, number, words) in enumerate(commands):
        where = "%s line %i" % (filename, number)
        pymux.sourcing = where
        try:
            answer = _run_in_order(pymux, [words])
        finally:
            pymux.sourcing = None

        if answer is not None:
            return _then_the_rest(pymux, where, answer, commands[index + 1 :])

    return None


async def _then_the_rest(pymux: Pymux, where: str, answer: Awaitable[Any], rest: list[Located]) -> None:
    pymux.sourcing = where
    try:
        await answer
    finally:
        pymux.sourcing = None

    more = _run_located(pymux, rest)
    if more is not None:
        await more


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, source_file)
    parser.add_argument("-q", dest="quiet", action="store_true", help="No error for a file that is not there.")
    parser.add_argument("filenames", metavar="<filename>", nargs="+", help="The configuration files to read.")
