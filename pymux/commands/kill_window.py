import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import the_window


def kill_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    "Kill all panes in the current window."
    w = the_window(pymux, args.target_window)

    for pane in w.panes:
        pymux.kill_pane(pane)


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    parser = add_command(subparsers, kill_window)
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to kill.")
