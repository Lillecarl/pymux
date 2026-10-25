"""
`pymux imgcat`: show image files in the terminal it runs in.

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
import io
import math
import termios
from typing import TYPE_CHECKING, BinaryIO, Final

from PIL import Image, UnidentifiedImageError

from pymux.graphics import DEFAULT_CELL_HEIGHT, DEFAULT_CELL_WIDTH

if TYPE_CHECKING:
    from typing import TextIO

__all__ = ["cells_for", "imgcat", "sequences_of"]

#: The base64 a kitty graphics chunk may carry, by the protocol.
CHUNK: Final = 4096

#: What kitty's `f=` says the payload is.
_PNG_FORMAT: Final = 100

#: The modes Pillow writes as PNG; a CMYK JPEG, say, is converted first.
_PNG_MODES: Final = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"})


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
            with Image.open(stdin if path == "-" else path) as image:
                image.load()
                pixels = image.size
                encoded = io.BytesIO()
                (image if image.mode in _PNG_MODES else image.convert("RGBA")).save(encoded, "PNG")
        except (OSError, UnidentifiedImageError) as error:
            err.write("pymux imgcat: %s: %s\n" % (path, error))
            status = 1
            continue
        columns, rows = cells_for(pixels, cell, room, arguments.width)
        out.writelines(sequences_of(encoded.getvalue(), columns, rows))
        # The cursor stands after the image's last cell; the next one
        # starts on a line of its own.
        out.write(b"\r\n")
        out.flush()
    return status
