import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.arrangement import LayoutTypes
from pymux.commands import CommandException
from pymux.commands import add_command


def select_layout(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Arrange the panes of the window in a named layout.
    """
    layout_type = args.layout_type

    try:
        layout_type_obj: LayoutTypes = LayoutTypes(layout_type)
    except ValueError:
        raise CommandException("Invalid layout type.")
    else:
        pymux.arrangement.get_active_window().select_layout(layout_type_obj)


def register(subparsers):
    parser = add_command(subparsers, select_layout)
    parser.add_argument("layout_type", metavar="<layout-type>", help="The layout to arrange the panes in.")
