from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, Target, add_command, add_target
from pymux.commands.set_option import add_append_argument, add_unset_argument, set_option
from pymux.options.options import Scope


def set_window_option(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Set a window option.

    -g: say what every new window starts with, rather than changing
        this one.
    """
    set_option(pymux, args, scope=Scope.WINDOW)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, set_window_option)
    parser.add_argument("-g", dest="g", action="store_true", help="What every new window starts with.")
    add_unset_argument(parser)
    add_append_argument(parser)
    add_target(parser, "-t", kind=Target.WINDOW, dest="target_window", help="The window whose value this is.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
