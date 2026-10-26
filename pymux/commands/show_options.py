from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, option_as_written
from pymux.commands.set_option import find_option, session_holder
from pymux.options import Option, Scope
from pymux.session import OwnOptions


def show_options(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Read a session or server option, or list the ones there are.

    A name says what that one holds, as a person writes it: the same
    wording `set-option` without a value answers with. A server
    option's name finds it without `-s`, as on the write. Without a
    name, one `name value` line per option, sorted: the session's, or
    with `-s` the server's, which is what tmux lists.
    Lillecarl/pymux#298, Lillecarl/pymux#757.

    A session reads what `-t` names, or the caller's; `-g` reads the
    defaults. `-A` marks a value the session did not set itself with
    `*` after its name, which is how tmux spells an inherited one.
    Lillecarl/pymux#756.
    """
    holder = session_holder(pymux, args)

    def target_of(option: Option):
        return holder if option.scope is Scope.SESSION else None

    name = args.option
    if name:
        option = find_option(pymux, name, Scope.SESSION)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, args, target_of(option)))
        return

    def inherited(option: Option) -> bool:
        return (
            option.scope is Scope.SESSION
            and option.attribute_name is not None
            and isinstance(holder, OwnOptions)
            and not holder.holds(option.attribute_name)
        )

    table = pymux.option_tables[Scope.SERVER if getattr(args, "s", False) else Scope.SESSION]
    lines = [
        "%s%s %s"
        % (
            key,
            "*" if getattr(args, "A", False) and inherited(option) else "",
            option_as_written(pymux, option, args, target_of(option)),
        )
        for key, option in sorted(table.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_options)
    parser.add_argument("-g", dest="g", action="count", default=0, help="The defaults every session reads.")
    parser.add_argument("-s", dest="s", action="store_true", help="List the server's options.")
    parser.add_argument(
        "-A", dest="A", action="store_true", help="Mark with * a value the session reads from the default."
    )
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to read.")
    parser.add_argument("option", metavar="<option>", nargs="?")
