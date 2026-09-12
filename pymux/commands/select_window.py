import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_window


def select_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Focus a window by index, by id, or by the pane that holds it.
    E.g:  select-window -t :3  or  select-window -t @1001
    """
    window_id = args.target_window

    w = find_window(pymux, window_id)
    if w is None:
        raise CommandException("Can't find window: %s" % (window_id,))

    pymux.arrangement.set_active_window(w)


def register(subparsers):
    parser = add_command(subparsers, select_window)
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", required=True, help="The window to focus.")
