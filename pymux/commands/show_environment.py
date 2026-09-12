import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import answer


def show_environment(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read the environment a new pane runs under.

    Without a name, one `NAME=value` line per variable, the form
    `eval $(pymux show-environment -s)` wants; `-s` escapes the
    values for the shell. A name that no scope set and the server
    does not hold reads as an error. With `-g`, only what the global
    scope holds: an unset prints as `-NAME`, and nothing that the
    server holds on its own shows. Lillecarl/pymux#270.
    """
    name = args.name
    escaped = args.s

    if args.g:
        scope = dict(pymux.global_environment)
        if name:
            value = scope.get(name)
            if value is None and name not in scope:
                raise CommandException("Can't find variable: %s" % (name,))
            answer(
                pymux,
                "-%s" % (name,)
                if value is None
                else "%s=%s" % (name, shlex.quote(value) if escaped else value),
            )
            return
        lines = [
            "-%s" % (key,)
            if value is None
            else "%s=%s" % (key, shlex.quote(value) if escaped else value)
            for key, value in sorted(scope.items())
        ]
        answer(pymux, "\n".join(lines))
        return

    merged = pymux.pane_environment()
    if name:
        if name not in merged:
            raise CommandException("Can't find variable: %s" % (name,))
        answer(
            pymux,
            "%s=%s" % (name, shlex.quote(merged[name]) if escaped else merged[name]),
        )
        return

    lines = [
        "%s=%s" % (key, shlex.quote(value) if escaped else value)
        for key, value in sorted(merged.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, show_environment)
    parser.add_argument("-g", dest="g", action="store_true", help="Read the global scope rather than what a new pane runs under.")
    parser.add_argument("-s", dest="s", action="store_true", help="Escape the values for the shell.")
    parser.add_argument("name", metavar="<name>", nargs="?")
