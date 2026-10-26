from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux
    from pymux.options import Holder
    from pymux.session import OwnOptions


from pymux.actions import Origin, started_by_now
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, option_as_written, session_of_target
from pymux.options import Option, Scope, SetOptionError

#: Which command sets an option of each scope.
_COMMAND_FOR = {
    Scope.SERVER: "set-option",
    Scope.SESSION: "set-option",
    Scope.WINDOW: "set-window-option",
    Scope.CLIENT: "set-client-option",
}


def _scopes_read_by(scope: Scope) -> tuple[Scope, ...]:
    """
    The tables one command reads a name from.

    `set-option` and `show-options` find a server option by its name,
    with or without `-s`, the way tmux's `options_scope_from_name`
    does, so a tmux configuration's `set -g set-clipboard on` holds.
    """
    if scope is Scope.SESSION:
        return (Scope.SESSION, Scope.SERVER)
    return (scope,)


def find_option(pymux: Pymux, name: str, scope: Scope) -> Option | None:
    "The option `name` of the tables a command of `scope` reads."
    for each in _scopes_read_by(scope):
        option = pymux.option_tables[each].get(name)
        if option is not None:
            return option
    return None


def session_holder(pymux: Pymux, args: argparse.Namespace) -> OwnOptions | Pymux:
    """
    What a session option is written on and read from: the defaults
    on `Pymux` or one session's own values.

    **A configuration file writes the defaults**, `-g` or not. pymux
    makes its first session before the file is read, and a line meant
    for every session must not become that one session's own value.
    Lillecarl/pymux#756.
    """
    if getattr(args, "g", 0):
        return pymux
    wanted = getattr(args, "target_session", None)
    if wanted is None and (pymux.sourcing is not None or started_by_now() is Origin.CONFIG):
        return pymux
    return session_of_target(pymux, wanted).options


def _not_here(pymux: Pymux, name: str, scope: Scope) -> str:
    """
    What to say about a name this command does not hold.

    **A name that lives in another scope names the command that takes
    it.** A bare "invalid option" is what a person sees the first time
    they run a configuration file after an option moves -- `full-screen`
    became one terminal's (Lillecarl/pymux#471) -- and it reads as a
    typo rather than as a line to change.
    """
    for other, table in pymux.option_tables.items():
        if other not in _scopes_read_by(scope) and name in table:
            return "%s is a %s option: use %s" % (name, other, _COMMAND_FOR[other])

    return "Invalid option: %s" % (name,)


def set_option(
    pymux: Pymux,
    args: argparse.Namespace,
    scope: Scope = Scope.SESSION,
    target: Holder | None = None,
) -> None:
    """
    Set an option of the server, a session, a window or a client.

    -g: for a session option, the default every session reads unless
        it set its own. For a window option, what every new window
        starts with. `set -g` is the most common line in a tmux
        configuration, and `pymux -V` says pymux speaks tmux 3.4.

    -gg: the default, and every session's own value dropped, so the
        default reaches every session. tmux counts no `-g`, so it reads
        this as `-g` and a tmux configuration keeps working.
        Lillecarl/pymux#756.

    -t: the session whose own value this is. Without `-t` or `-g`, the
        caller's session.

    -s: the option is the server's. A server option's name finds it
        without the flag too, as in tmux; Tab with it offers only
        those.

    With no value, tmux prints what the option holds, and pymux does
    the same. Without it the command line was write-only: a person
    could change `mouse` and had no way to see what it was.
    Lillecarl/pymux#292.
    """
    name = args.option
    value = args.value

    option = find_option(pymux, name, scope)

    if option is None:
        raise CommandException(_not_here(pymux, name, scope))

    if option.scope is Scope.SESSION:
        target = session_holder(pymux, args)

    if value is None:
        answer(pymux, "%s %s" % (name, option_as_written(pymux, option, args, target)))
        return

    try:
        # `-g` says what every new window starts with, and changes
        # no window that is open, which is what it means in tmux.
        # It is the only way a configuration file can set a window
        # option, because that file is read before there is a
        # window. Lillecarl/pymux#199.
        if scope is Scope.WINDOW and args.g:
            option.set_default(pymux, value)
        else:
            option.set_value(pymux, value, target)
            if option.scope is Scope.SESSION and args.g > 1 and option.attribute_name is not None:
                for session in pymux.sessions:
                    session.options.forget(option.attribute_name)
    except SetOptionError as e:
        raise CommandException(e.message)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, set_option)
    parser.add_argument(
        "-g",
        dest="g",
        action="count",
        default=0,
        help="The default every session reads; -gg also drops each session's own value.",
    )
    parser.add_argument("-s", dest="s", action="store_true", help="A server option.")
    parser.add_argument(
        "-t", dest="target_session", metavar="<target-session>", help="The session whose own value this is."
    )
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
