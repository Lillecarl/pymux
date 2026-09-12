import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def set_environment(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Put a variable in the environment a new pane runs under.

    -g fills the global scope, which is the fallback a session unset
    of a name falls through to. A name with no value, or with `-u`,
    is an unset: the name leaves the environment of a new pane, and
    with `-g` it leaves the global scope itself. A program that
    already runs never sees any of this -- the environment is read
    once, at exec, so only panes spawned after the set carry it.
    Lillecarl/pymux#270.
    """
    name, value = args.name, args.value
    if not name or "=" in name:
        raise CommandException("Invalid variable name: %r" % (name,))

    scope = pymux.global_environment if args.g else pymux.session_environment
    if args.u or value is None:
        scope[name] = None
    else:
        scope[name] = value


def register(subparsers):
    parser = add_command(subparsers, set_environment)
    parser.add_argument("-g", dest="g", action="store_true", help="Fill the global scope, which new sessions start from.")
    parser.add_argument("-u", dest="u", action="store_true", help="Remove the variable from the scope.")
    parser.add_argument("name", metavar="<name>")
    parser.add_argument("value", metavar="<value>", nargs="?")
