import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.commands.common import clients_named
from pymux.commands.common import option_as_written
from pymux.options import ALL_CLIENT_OPTIONS


def show_client_options(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read a client option, or list the ones there are.

    A name says what that client holds; without a name, one
    `name value` line per option, sorted. `-t` names the client, and
    without it this is the client the command came from. Nobody
    attached reads as not set, the way a window option with no window
    does. Lillecarl/pymux#223, Lillecarl/pymux#298.
    """
    target = None
    if args.target_client is not None:
        target = clients_named(pymux, args.target_client)[0]

    name = args.option
    if name:
        option = ALL_CLIENT_OPTIONS.get(name)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, args, target))
        return

    lines = [
        "%s %s" % (key, option_as_written(pymux, option, args, target))
        for key, option in sorted(ALL_CLIENT_OPTIONS.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, show_client_options)
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client of this name, as list-clients prints it.")
    parser.add_argument("option", metavar="<option>", nargs="?")
