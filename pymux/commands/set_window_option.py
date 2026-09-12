import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.set_option import set_option


def set_window_option(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Set a window option.

    -g: say what every new window starts with, rather than changing
        this one.
    """
    set_option(pymux, args, window=True)


def register(subparsers):
    parser = add_command(subparsers, set_window_option)
    parser.add_argument("-g", dest="g", action="store_true", help="What every new window starts with.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
