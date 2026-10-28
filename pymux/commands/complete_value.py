from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer
from pymux.commands.completer import values_for

if TYPE_CHECKING:
    from pymux.main import Pymux


def complete_value(pymux: Pymux, args: argparse.Namespace):
    """
    List what the command bar offers for one argument of a command.

    One value a line: the word, a tab, and what it is. The shell's Tab
    asks this, so a value completes the same from a shell as from the
    command bar.
    """
    given = json.loads(args.given) if args.given else {}
    for word, says in values_for(pymux, args.command, args.dest, args.prefix, given).items():
        answer(pymux, "%s\t%s" % (word, says))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, complete_value, read_only=True)
    parser.add_argument("command", metavar="<command>", help="The command, as the command line spells it.")
    parser.add_argument("dest", metavar="<dest>", help="The argument of that command, by its dest.")
    parser.add_argument("prefix", nargs="?", default="", metavar="<prefix>", help="What is typed of the value so far.")
    parser.add_argument(
        "given", nargs="?", default=None, metavar="<json>", help="The arguments parsed so far, as a JSON object."
    )
