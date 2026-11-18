from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.common import answer, option_as_written
from pymux.commands.set_option import find_option, session_holder
from pymux.core.session import OwnOptions
from pymux.options import user_options
from pymux.options.options import Option, Scope


def show_options(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Read a session or server option, or list the ones there are.

    A name says what that one holds, as a person writes it: the same
    wording `set-option` without a value answers with. A server
    option's name finds it without `-s`, as on the write. Without a
    name, one `name value` line per option, sorted: the session's, or
    with `-s` the server's, which is what tmux lists.
    Lillecarl/pyterm#300, Lillecarl/pyterm#759.

    A session reads what `-t` names, or the caller's; `-g` reads the
    defaults. `-q` answers nothing for an option there is not, and `-v`
    is accepted for tmux: a name already answers with the value alone.
    A user option, `@name`, reads the one `set -g @name` wrote, or the
    server's with `-s`. Lillecarl/pyterm#1032. `-A` marks a value the session did not set itself with
    `*` after its name, which is how tmux spells an inherited one.
    Lillecarl/pyterm#758.
    """
    holder = session_holder(pymux, args)

    def target_of(option: Option):
        return holder if option.scope is Scope.SESSION else None

    name = args.option
    if name and user_options.is_user_option(name):
        value = pymux.user_options[user_options.read_scope(args, Scope.SESSION)].get(name)
        if value is None and not getattr(args, "q", False):
            raise CommandException("Invalid option: %s" % (name,))
        answer(pymux, value or "")
        return
    if name:
        option = find_option(pymux, name, Scope.SESSION)
        if option is None:
            if getattr(args, "q", False):
                answer(pymux, "")
                return
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
    parser.add_argument("-q", dest="q", action="store_true", help="Answer nothing for an option there is not.")
    parser.add_argument("-v", dest="v", action="store_true", help="Accepted for tmux: a name answers with the value.")
    add_target(parser, "-t", kind=Target.SESSION, dest="target_session", help="The session to read.")
    parser.add_argument("option", metavar="<option>", nargs="?")
