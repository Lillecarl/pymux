import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.commands.common import option_as_written
from pymux.options import ALL_WINDOW_OPTIONS


def show_window_options(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read a window option, or list the ones there are.

    A name says what the active window holds; `-g` reads what every
    new window starts with, where a default nobody set reads as not
    set and shows as such. Without a name, one `name value` line per
    option, sorted. Lillecarl/pymux#298.
    """
    name = args.option
    if name:
        option = ALL_WINDOW_OPTIONS.get(name)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, args, window=True))
        return

    lines = [
        "%s %s" % (key, option_as_written(pymux, option, args, window=True))
        for key, option in sorted(ALL_WINDOW_OPTIONS.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, show_window_options)
    parser.add_argument("-g", dest="g", action="store_true", help="Read what every new window starts with.")
    parser.add_argument("option", metavar="<option>", nargs="?")
