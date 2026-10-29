"""
`pymux imgcat`: show image files in the terminal it runs in, SVG too.

**It writes to its own terminal**, which in a pane is the pane, as the
kitty graphics protocol: the image as PNG, in chunks, put at the cursor
over the cells it covers. pymux reads that from any program and draws
it for each client as that client can -- kitty graphics, sixel or half
blocks -- so this writes and nothing on the server is new. A kitty or a
Ghostty outside pymux shows it as well. Lillecarl/pymux#764.

The size in cells comes from the image's pixels over the terminal's
cell, which a pane's pty reports with its size, and is held to the
width of the terminal. `--width` sets the columns, and the rows follow
the image's shape.
"""

from __future__ import annotations

import argparse
import array
import base64
import fcntl
import math
import termios
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

import resvg_py

from pymux.graphics import DEFAULT_CELL_HEIGHT, DEFAULT_CELL_WIDTH
from pymux.inline_images import as_png

if TYPE_CHECKING:
    from typing import TextIO

__all__ = ["cells_for", "imgcat", "sequences_of"]

#: The base64 a kitty graphics chunk may carry, by the protocol.
CHUNK: Final = 4096

#: What kitty's `f=` says the payload is.
_PNG_FORMAT: Final = 100


def _terminal_of(out: BinaryIO) -> tuple[int, int, int] | None:
    "The columns of the terminal `out` writes to, and its cell in pixels when it says, else the default cell."
    try:
        size = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(out.fileno(), termios.TIOCGWINSZ, size, True)
    except OSError, ValueError:
        return None
    rows, columns, width, height = size
    if not columns:
        return None
    if not (width and height and rows):
        return columns, DEFAULT_CELL_WIDTH, DEFAULT_CELL_HEIGHT
    return columns, width // columns, height // rows


def cells_for(
    pixels: tuple[int, int], cell: tuple[int, int], room: int | None, width: int | None = None
) -> tuple[int, int]:
    """
    The columns and rows an image of `pixels` covers: as many as its
    pixels fill at `cell`, or `width` columns, held to `room` columns,
    with the rows following the image's shape.
    """
    image_width, image_height = pixels
    cell_width, cell_height = cell
    columns = width or math.ceil(image_width / cell_width)
    if room is not None:
        columns = min(columns, room)
    columns = max(1, columns)
    rows = math.ceil(columns * cell_width * image_height / image_width / cell_height)
    return columns, max(1, rows)


def sequences_of(png: bytes, columns: int, rows: int) -> list[bytes]:
    "The kitty graphics sequences that put a PNG over `columns` by `rows` cells at the cursor."
    payload = base64.b64encode(png)
    chunks = [payload[start : start + CHUNK] for start in range(0, len(payload), CHUNK)] or [b""]
    sequences = []
    for index, chunk in enumerate(chunks):
        more = int(index < len(chunks) - 1)
        # Only the first chunk says what the image is and where it goes.
        first = b"a=T,f=%d,q=2,c=%d,r=%d," % (_PNG_FORMAT, columns, rows) if index == 0 else b""
        head = first + b"m=%d" % (more,)
        sequences.append(b"\x1b_G" + head + b";" + chunk + b"\x1b\\")
    return sequences


def _svg_pixels(svg: str) -> tuple[int, int] | None:
    "The size an SVG gives itself, in pixels; None when it is no SVG resvg reads."
    try:
        found = as_png(resvg_py.svg_to_bytes(svg_string=svg))
    except ValueError:
        return None
    return found[1] if found else None


def _shown(source: bytes, cell: tuple[int, int], room: int | None, width: int | None) -> tuple[bytes, int, int] | None:
    """
    An image file as PNG over the cells it covers; None for no image.

    An SVG is drawn at the pixels of those cells, so it is as sharp as
    the terminal's cell allows. Lillecarl/pymux#805.
    """
    found = as_png(source)
    if found is not None:
        png, pixels = found
        return png, *cells_for(pixels, cell, room, width)
    try:
        svg = source.decode()
    except UnicodeDecodeError:
        return None
    pixels = _svg_pixels(svg)
    if pixels is None:
        return None
    columns, rows = cells_for(pixels, cell, room, width)
    return resvg_py.svg_to_bytes(svg_string=svg, width=columns * cell[0]), columns, rows


def imgcat(words: list[str], stdin: BinaryIO, out: BinaryIO, err: TextIO) -> int:
    "`pymux imgcat`: each image, one after the other. Returns the exit status."
    parser = argparse.ArgumentParser(prog="pymux imgcat", description="Show images in this terminal.")
    parser.add_argument("files", nargs="+", metavar="FILE", help="An image file; `-` reads standard input.")
    parser.add_argument("-w", "--width", type=int, metavar="COLUMNS", help="The columns an image covers.")
    arguments = parser.parse_args(words)
    if arguments.width is not None and arguments.width < 1:
        parser.error("--width takes a number of columns above zero")

    terminal = _terminal_of(out)
    room, cell = (terminal[0], terminal[1:]) if terminal else (None, (DEFAULT_CELL_WIDTH, DEFAULT_CELL_HEIGHT))
    status = 0
    for path in arguments.files:
        try:
            found = _shown(stdin.read() if path == "-" else Path(path).read_bytes(), cell, room, arguments.width)
        except OSError as error:
            err.write("pymux imgcat: %s: %s\n" % (path, error))
            status = 1
            continue
        if found is None:
            err.write("pymux imgcat: %s: not an image\n" % (path,))
            status = 1
            continue
        png, columns, rows = found
        out.writelines(sequences_of(png, columns, rows))
        # The cursor stands after the image's last cell; the next one
        # starts on a line of its own.
        out.write(b"\r\n")
        out.flush()
    return status
