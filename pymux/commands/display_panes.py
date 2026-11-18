from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command


def display_panes(pymux: Pymux, args: argparse.Namespace) -> None:
    "Show the number of each pane until a key is pressed or `display-panes-time` runs out, as tmux does."
    window = pymux.arrangement.get_active_window()
    if window is None:
        return
    milliseconds = args.d if args.d is not None else window.options.display_panes_time
    shown = pymux.display_pane_numbers_by = object()
    pymux.display_pane_numbers = True
    if milliseconds > 0 and pymux.tasks is not None:
        pymux.tasks.start_soon(_take_down, pymux, shown, milliseconds / 1000)


async def _take_down(pymux: Pymux, shown: object, seconds: float) -> None:
    await anyio.sleep(seconds)
    if pymux.display_pane_numbers_by is shown:
        pymux.display_pane_numbers = False
        pymux.invalidate()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, display_panes)
    parser.add_argument(
        "-d",
        dest="d",
        type=int,
        metavar="MILLISECONDS",
        help="How long, in place of `display-panes-time`; 0 until a key.",
    )
