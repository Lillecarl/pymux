"""
What `capture-pane --png`, `capture-client --png` and `capture-window
--png` share: the flags, the cell, the drawing in a thread, and `-o`.
"""

from __future__ import annotations

import argparse
import base64
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import anyio
from platformdirs import user_pictures_dir

from pymux import screenshot
from pymux.commands import CommandException

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = [
    "IN_PICTURES",
    "add_output_argument",
    "add_picture_arguments",
    "cell_from",
    "drawn",
    "drawn_png",
    "fitted_cell",
    "output_path",
    "refuse_picture_flags",
    "save_png",
]


def add_picture_arguments(parser: argparse.ArgumentParser, what: str) -> None:
    "The flags a picture takes; `what` is the thing it draws, for the help."
    parser.add_argument(
        "--png",
        action="store_true",
        help="With -p: a picture of the %s, as base64 PNG." % (what,),
    )
    parser.add_argument(
        "--font",
        dest="fonts",
        action="append",
        default=[],
        metavar="<file>",
        help="A font file --png draws with, once per face. fontconfig's monospace without it.",
    )
    parser.add_argument(
        "--fallback",
        dest="fallbacks",
        action="append",
        default=[],
        metavar="<file>",
        help="A font file --png draws a glyph from when the font lacks it, tried in order.",
    )
    parser.add_argument(
        "--cell",
        metavar="<width>x<height>",
        help="The pixels of one cell for --png. The client terminal's cell without it.",
    )
    parser.add_argument(
        "--fit",
        type=int,
        metavar="<pixels>",
        help="Shrink the cell of --png until the picture's longest edge is at most this.",
    )


#: What `-o` with no path writes: a new name in the pictures directory.
IN_PICTURES = ""

#: How long a capture waits for what it shows to draw again, in seconds.
FRAME_WAIT = 1.0


def add_output_argument(parser: argparse.ArgumentParser) -> None:
    "The `-o` a picture takes, to write it to a file instead of printing it."
    parser.add_argument(
        "-o",
        dest="output",
        nargs="?",
        const=IN_PICTURES,
        metavar="<file>",
        help="Write a PNG to <file>, relative to the pictures directory; a new file there without <file>.",
    )


def output_path(given: str) -> str:
    """
    Where `-o` writes. A relative path, and no path at all, are in the
    pictures directory of the server's user: the server has no working
    directory of the person who typed the command.
    """
    if given == IN_PICTURES:
        given = time.strftime("pymux-%Y%m%d-%H%M%S.png")
    return os.path.join(user_pictures_dir(), os.path.expanduser(given))


async def save_png(
    pymux: Pymux, grid: screenshot.Grid, args: argparse.Namespace, cell: tuple[int, int], command: str
) -> None:
    "Draw the grid into the file `-o` names, and say where."
    picture = await drawn_png(grid, args, cell, command)
    path = anyio.Path(output_path(args.output))
    try:
        await path.parent.mkdir(parents=True, exist_ok=True)
        await path.write_bytes(picture)
    except OSError as error:
        raise CommandException("%s: %s" % (command, error))
    pymux.print_command_line(str(path))
    pymux.show_message("Saved %s" % (path,))


def refuse_picture_flags(args: argparse.Namespace, command: str) -> None:
    "A picture's flags without --png are a mistake, and saying so beats ignoring them."
    if args.fonts or args.fallbacks or args.cell or args.fit is not None:
        raise CommandException("%s: --font, --fallback, --cell and --fit draw the picture of --png" % (command,))


def cell_from(given: str | None, otherwise: tuple[int, int], command: str) -> tuple[int, int]:
    "The cell a picture draws, in pixels: as `--cell` gave it, or `otherwise`."
    if given is None:
        return otherwise
    width, x, height = given.partition("x")
    if not (x and width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0):
        raise CommandException("%s: --cell takes <width>x<height> in pixels, as 9x18: %s" % (command, given))
    return int(width), int(height)


def fitted_cell(cell: tuple[int, int], columns: int, rows: int, fit: int | None) -> tuple[int, int]:
    """
    The cell, made smaller until the longest edge of the picture is at
    most `fit` pixels. The font is sized to the cell, so the glyphs stay
    sharp where scaling the picture down would blur them.
    """
    width, height = cell
    longest = max(columns * width, rows * height)
    if fit is None or longest <= fit:
        return width, height
    scale = fit / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


async def drawn(grid: screenshot.Grid, args: argparse.Namespace, cell: tuple[int, int], command: str) -> str:
    "The grid as a PNG in base64."
    return base64.b64encode(await drawn_png(grid, args, cell, command)).decode("ascii")


async def drawn_png(grid: screenshot.Grid, args: argparse.Namespace, cell: tuple[int, int], command: str) -> bytes:
    """
    The grid as a PNG, drawn in a thread with the fonts the flags name or
    the system's. The system's font takes the system's fallbacks too, when
    no `--fallback` names any; a caller's font takes only the caller's.
    """
    paths = args.fonts or await screenshot.system_fonts()
    if not paths:
        raise CommandException("%s: no font to draw with: pass --font, or put fontconfig's fc-match on PATH" % command)
    try:
        faces = await anyio.to_thread.run_sync(screenshot.faces_of, paths, args.fallbacks)
        if not args.fonts and not args.fallbacks:
            faces = replace(faces, fallbacks=tuple(await screenshot.system_fallbacks(grid, faces.regular)))
        return await anyio.to_thread.run_sync(screenshot.draw, grid, faces, *cell)
    except ValueError as error:
        raise CommandException("%s: %s" % (command, error))
