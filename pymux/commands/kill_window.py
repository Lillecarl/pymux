import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_window


def kill_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Kill all panes in the current window."
    if args.target_window:
        w = find_window(pymux, args.target_window)
        if w is None:
            raise CommandException(
                "Can't find window: %s" % (args.target_window,)
            )
    else:
        w = pymux.arrangement.get_active_window()

    for pane in w.panes:
        pymux.kill_pane(pane)


def register(subparsers):
    parser = add_command(subparsers, kill_window)
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to kill.")
