from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Window
    from pymux.main import Pymux
    from pymux.tags import Tags


from pymux.commands import CommandParser, add_command
from pymux.commands.common import (
    add_format_arguments,
    add_tag_arguments,
    find_window,
    print_object_format,
    the_window,
)
from pymux.tags import parse_tags


def split_window(pymux: Pymux, args: argparse.Namespace):
    """
    Split this window into two panes, side by side or stacked.

    `--tag` tags the new pane, as `set-tag` would.
    """
    executable = args.executable
    start_directory = args.start_directory
    tags = parse_tags(args.tags)

    # Split in the target window. (libtmux targets the pane of the window.)
    target_window = find_window(pymux, args.target_window)

    return _split(pymux, executable, start_directory, target_window, tags, args)


async def _split(
    pymux: Pymux,
    executable: str | None,
    start_directory: str | None,
    target_window: Window | None,
    tags: Tags,
    args: argparse.Namespace,
) -> None:
    # The tmux definition of horizontal is the opposite of prompt_toolkit.
    pane = await pymux.add_process(
        executable,
        vsplit=args.h,
        start_directory=start_directory,
        window=target_window,
        select=not args.d,
    )
    pane.tags.update(tags)

    if args.P:
        # The new pane, which `-d` leaves inactive: not the window's active one.
        window = target_window if target_window is not None else the_window(pymux, None)
        print_object_format(pymux, args, window=window, pane=pane)


def add_arguments(parser: argparse.ArgumentParser):
    "What a command that opens a pane in a window takes."
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", dest="v", action="store_true", help="Split top over bottom.")
    group.add_argument("-h", dest="h", action="store_true", help="Split side by side.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to split.")
    parser.add_argument("-c", dest="start_directory", metavar="<start-directory>", help="Where the program starts.")
    parser.add_argument("-d", dest="d", action="store_true", help="Leave the new pane unfocused.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the new pane.")
    add_format_arguments(parser, "The format to print with -P.")
    parser.add_argument("executable", nargs="?", metavar="<executable>")


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, split_window)
    add_arguments(parser)
    add_tag_arguments(parser, "pane")
