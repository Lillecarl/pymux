from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, cast

from pyte.screen import Screen

from pymux import screenshot
from pymux.arrangement import Pane
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_window
from pymux.commands.pictures import (
    FRAME_WAIT,
    add_output_argument,
    add_picture_arguments,
    cell_from,
    drawn,
    fitted_cell,
    refuse_picture_flags,
    save_png,
)
from pymux.geometry import plan_of
from pymux.window_pane import WindowWidget

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pymux.arrangement import Window
    from pymux.main import Pymux
    from pymux.plane import Rect


async def _drawn_again(pymux: Pymux, window: Window) -> None:
    "Let the window panes draw what they wait to draw: a window no client shows is not let do it otherwise."
    toplevels = [pane.terminal.toplevel for pane in window.panes if isinstance(pane.terminal, WindowWidget)]
    await pymux.wayland.drawn_again(toplevels, FRAME_WAIT)


def _grid_of(pymux: Pymux, window: Window, cell: tuple[int, int]) -> screenshot.Grid:
    "The window's panes and border lines, where its plan puts them."
    plan = plan_of(pymux, window)
    panes: list[tuple[Rect, Screen | screenshot.Pixels | None]] = []
    for slot, rect in plan.rects.items():
        terminal = cast(Pane, slot.shown).terminal
        if isinstance(terminal, WindowWidget):
            frame = pymux.wayland.frame(terminal.toplevel)
            panes.append((rect, None if frame is None else screenshot.Pixels(*frame)))
            continue
        screen = getattr(terminal, "screen", None)
        panes.append((rect, screen if isinstance(screen, Screen) else None))
    focused = getattr(window.active_pane.terminal, "screen", None) if window.active_pane is not None else None
    focused = focused if isinstance(focused, Screen) else None
    colors = focused.colors if focused is not None else None
    return screenshot.window_grid_of(panes, plan.lines, focused, colors, cell)


def capture_window(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None] | None:
    """
    Capture a window as its layout draws it, whether a client shows it or not.

    Every pane at its place, and the border lines between them. Text by
    default, `--png` a picture, `-o` a picture in a file on the server's
    machine. -t names the window; without it the active one.

    The status line is a client's and not the window's, so it is not
    here: `capture-client` draws what a client shows. A Wayland window
    pane draws what it waits to draw first, for a second at most.
    Lillecarl/pymux#818, Lillecarl/pymux#822.
    """
    if args.output is None and not args.p:
        raise CommandException("capture-window: it prints, so it needs -p or -o")
    window = the_window(pymux, args.target_window)
    if args.output is not None or args.png:
        return _png(pymux, window, args)
    refuse_picture_flags(args, "capture-window")
    return _text(pymux, window)


async def _text(pymux: Pymux, window: Window) -> None:
    await _drawn_again(pymux, window)
    grid = _grid_of(pymux, window, pymux.terminals.cell_size())
    pymux.print_command_line("\n".join("".join(char for char, _look in row).rstrip() for row in grid.rows))


async def _png(pymux: Pymux, window: Window, args: argparse.Namespace) -> None:
    await _drawn_again(pymux, window)
    cell = cell_from(args.cell, pymux.terminals.cell_size(), "capture-window")
    if args.fit is not None:
        # The cell sizes the images, so the window is measured first.
        measured = _grid_of(pymux, window, cell)
        cell = fitted_cell(cell, measured.columns, len(measured.rows), args.fit)
    grid = _grid_of(pymux, window, cell)
    if args.output is not None:
        await save_png(pymux, grid, args, cell, "capture-window")
    else:
        pymux.print_command_line(await drawn(grid, args, cell, "capture-window"))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, capture_window)
    parser.add_argument("-p", dest="p", action="store_true", help="Print to the output of the command line.")
    add_output_argument(parser)
    add_picture_arguments(parser, "window")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to capture.")
