import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.commands.common import option_as_written
from pymux.options import ALL_OPTIONS


def show_options(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read a session option, or list the ones there are.

    A name says what that one holds, as a person writes it: the same
    wording `set-option` without a value answers with. Without a
    name, one `name value` line per option, sorted. `-g` says
    nothing here, on the read as on the write -- there is one
    session per server. Lillecarl/pymux#298.
    """
    name = args.option
    if name:
        option = ALL_OPTIONS.get(name)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, args, window=False))
        return

    lines = [
        "%s %s" % (key, option_as_written(pymux, option, args, window=False))
        for key, option in sorted(ALL_OPTIONS.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, show_options)
    parser.add_argument("-g", dest="g", action="store_true", help="Accepted for tmux and changes nothing: there is one session per server.")
    parser.add_argument("option", metavar="<option>", nargs="?")
