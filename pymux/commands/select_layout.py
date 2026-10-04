from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.arrangement import LayoutTypes
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_window


def select_layout(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Arrange the panes of the window in a named layout.
    """
    layout_type = args.layout_type

    try:
        layout_type_obj: LayoutTypes = LayoutTypes(layout_type)
    except ValueError:
        raise CommandException("Invalid layout type.")
    else:
        the_window(pymux, None).select_layout(layout_type_obj)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, select_layout)
    parser.add_argument("layout_type", metavar="<layout-type>", help="The layout to arrange the panes in.")
