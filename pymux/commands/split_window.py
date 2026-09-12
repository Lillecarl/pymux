import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import find_window
from pymux.commands.common import print_object_format


def split_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Split this window into two panes, side by side or stacked.
    """
    executable = args.executable
    start_directory = args.start_directory

    # Split in the target window. (libtmux targets the pane of the window.)
    target_window = find_window(pymux, args.target_window)

    # The tmux definition of horizontal is the opposite of prompt_toolkit.
    pymux.add_process(
        executable,
        vsplit=args.h,
        start_directory=start_directory,
        window=target_window,
    )

    if args.P:
        window = pymux.arrangement.get_active_window()
        if target_window is not None:
            window = target_window
        print_object_format(
            pymux, args.format, window=window, pane=window.active_pane
        )


def add_arguments(parser):
    "What a command that opens a pane in a window takes."
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", dest="v", action="store_true", help="Split top over bottom.")
    group.add_argument("-h", dest="h", action="store_true", help="Split side by side.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to split.")
    parser.add_argument("-c", dest="start_directory", metavar="<start-directory>", help="Where the program starts.")
    parser.add_argument("-d", dest="d", action="store_true", help="Leave the new pane unfocused.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the new pane.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="The format to print with -P.")
    parser.add_argument("executable", nargs="?", metavar="<executable>")


def register(subparsers):
    add_arguments(add_command(subparsers, split_window))
