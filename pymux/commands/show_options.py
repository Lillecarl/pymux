from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, option_as_written
from pymux.commands.set_option import find_option
from pymux.options import Scope


def show_options(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Read a session or server option, or list the ones there are.

    A name says what that one holds, as a person writes it: the same
    wording `set-option` without a value answers with. A server
    option's name finds it without `-s`, as on the write. Without a
    name, one `name value` line per option, sorted: the session's, or
    with `-s` the server's, which is what tmux lists. `-g` says
    nothing here, on the read as on the write -- there is one
    session per server. Lillecarl/pymux#298, Lillecarl/pymux#757.
    """
    name = args.option
    if name:
        option = find_option(pymux, name, Scope.SESSION)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, args))
        return

    table = pymux.option_tables[Scope.SERVER if args.s else Scope.SESSION]
    lines = ["%s %s" % (key, option_as_written(pymux, option, args)) for key, option in sorted(table.items())]
    answer(pymux, "\n".join(lines))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_options)
    parser.add_argument(
        "-g",
        dest="g",
        action="store_true",
        help="Accepted for tmux and changes nothing: there is one session per server.",
    )
    parser.add_argument("-s", dest="s", action="store_true", help="List the server's options.")
    parser.add_argument("option", metavar="<option>", nargs="?")
