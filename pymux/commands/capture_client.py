from __future__ import annotations

import argparse
import os
import time
from typing import TYPE_CHECKING

import anyio
from platformdirs import user_pictures_dir
from pyte.osc import COLOR_BASE, ColorOverrides

from pymux import screenshot
from pymux.colors import theme_color_base
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_client
from pymux.commands.pictures import (
    add_picture_arguments,
    cell_from,
    drawn,
    drawn_png,
    fitted_cell,
    refuse_picture_flags,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pymux.main import ClientState, Pymux

#: What `-o` with no path writes: a new name in the pictures directory.
IN_PICTURES = ""


def _colors_of(pymux: Pymux, client_state: ClientState) -> ColorOverrides:
    """
    The colours this client's terminal shows with: its theme's when the
    theme paints the screen, else what the terminal said it draws with,
    else the table pyte reports. `Terminals.tell_pane_about_colours` reads
    them the same way.
    """
    if client_state.session.options.paint_screen:
        return ColorOverrides(theme_color_base(client_state.theme_in_use))
    try:
        base = client_state.default_colors.color_base()
    except AttributeError:
        base = None
    return ColorOverrides(base or COLOR_BASE)


def _cell_of(pymux: Pymux, client_state: ClientState) -> tuple[int, int]:
    "The client's own cell in pixels, when its terminal said; the server's otherwise."
    graphics = client_state.connection.graphics
    if graphics.cell_size_known:
        return graphics.cell_width, graphics.cell_height
    return pymux.terminals.cell_size()


def _grid_of(pymux: Pymux, client_state: ClientState, cell: tuple[int, int]) -> screenshot.Grid:
    try:
        return screenshot.client_grid_of(client_state, _colors_of(pymux, client_state), cell)
    except LookupError as error:
        raise CommandException("capture-client: %s" % (error,))


def output_path(given: str) -> str:
    """
    Where `-o` writes. A relative path, and no path at all, are in the
    pictures directory of the server's user: the server has no working
    directory of the person who typed the command.
    """
    pictures = user_pictures_dir()
    if given == IN_PICTURES:
        given = time.strftime("pymux-%Y%m%d-%H%M%S.png")
    return os.path.join(pictures, os.path.expanduser(given))


def capture_client(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None] | None:
    """
    Capture the whole of what a client is shown, as text or a picture.

    That is all of pymux on its terminal: the status line, the borders,
    every pane, and the popups, menus and modes drawn over them. Text
    by default, the characters of each row. `--png` draws it, in
    the client's own colours and with the panes' images where the
    client shows them, at the client terminal's cell. -t names the
    client, as `list-clients` prints it; without it the client that ran
    this, else the one a person used last.

    `-o` writes the picture to a file on the server's machine, by default
    a new one in the pictures directory, and says where.

    It reads the frame the client was last sent, so it is what the
    person sees, and a change a command just made shows once pymux has
    drawn it.
    """
    if args.output is not None:
        client_state = the_client(pymux, args.target_client, "capture-client")
        return _save_png(pymux, client_state, args)
    if not args.p:
        raise CommandException("capture-client: it prints, so it needs -p or -o")
    client_state = the_client(pymux, args.target_client, "capture-client")
    if args.png:
        return _print_png(pymux, client_state, args)
    refuse_picture_flags(args, "capture-client")
    size = client_state.app.output.get_size()
    grid = _grid_of(pymux, client_state, _cell_of(pymux, client_state))
    pymux.print_command_line("\n".join("".join(char for char, _look in row).rstrip() for row in grid.rows[: size.rows]))
    return None


def _picture_grid(
    pymux: Pymux, client_state: ClientState, args: argparse.Namespace
) -> tuple[screenshot.Grid, tuple[int, int]]:
    size = client_state.app.output.get_size()
    cell = cell_from(args.cell, _cell_of(pymux, client_state), "capture-client")
    cell = fitted_cell(cell, size.columns, size.rows, args.fit)
    return _grid_of(pymux, client_state, cell), cell


async def _print_png(pymux: Pymux, client_state: ClientState, args: argparse.Namespace) -> None:
    grid, cell = _picture_grid(pymux, client_state, args)
    pymux.print_command_line(await drawn(grid, args, cell, "capture-client"))


async def _save_png(pymux: Pymux, client_state: ClientState, args: argparse.Namespace) -> None:
    grid, cell = _picture_grid(pymux, client_state, args)
    picture = await drawn_png(grid, args, cell, "capture-client")
    path = anyio.Path(output_path(args.output))
    try:
        await path.parent.mkdir(parents=True, exist_ok=True)
        await path.write_bytes(picture)
    except OSError as error:
        raise CommandException("capture-client: %s" % (error,))
    pymux.print_command_line(str(path))
    pymux.show_message("Saved %s" % (path,))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, capture_client)
    parser.add_argument("-p", dest="p", action="store_true", help="Print to the output of the command line.")
    parser.add_argument(
        "-o",
        dest="output",
        nargs="?",
        const=IN_PICTURES,
        metavar="<file>",
        help="Write a PNG to <file>, relative to the pictures directory; a new file there without <file>.",
    )
    add_picture_arguments(parser, "whole client screen")
    parser.add_argument("-t", dest="target_client", metavar="<target-client>", help="The client to capture.")
