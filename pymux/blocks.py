"""
Draw an image as text, for a terminal that draws no pixels.

The upper half block, U+2580, splits a cell in two: the foreground
paints the top half and the background paints the bottom. So one cell
carries two pixels, and a grid of cells carries an image at twice the
vertical resolution.

This is the way chafa calls `--symbols vhalf`, and chafa says it gives
fair results. chafa does better by default, because it picks from a
whole set of block and border characters and takes the one that fits
each cell. That is a larger job and it can come later; one symbol is
enough to stop denying a capability.

**The colours.** Twenty four bits write the pixel itself, so there is
nothing to choose. A terminal with 256 or 16 colours needs the closest
one, and the tables of prompt_toolkit answer that here. They compare in
plain RGB, which is what chafa does by default as well: chafa offers
DIN99d for a more faithful answer and keeps RGB as the default because
the conversion costs more than it returns.

Using the tables of prompt_toolkit buys something else, and it is the
reason to prefer them over a table of our own. The renderer that draws
every other cell of the screen picks its colours through exactly these,
so a half block lands on the same colour as the text beside it. Two
tables would drift.

**The scaling.** An image shrinks a long way to reach a grid of cells,
and the nearest neighbour that the sixel path uses throws most of the
picture away at that size. This averages the pixels that fall into each
cell instead, weighted by how opaque they are.
"""
from typing import List, Optional, Tuple

from prompt_toolkit.output import ColorDepth

# The colour tables of the renderer. A half block is a cell like any
# other, so it has to land on the same colour that the renderer would
# choose, and these are where that choice lives.
from prompt_toolkit.output.vt100 import (
    BG_ANSI_COLORS,
    FG_ANSI_COLORS,
    _256_colors,
    _get_closest_ansi_color,
)

__all__ = ["LOWER_HALF", "UPPER_HALF", "blocks_for", "rows_for_cells"]

#: The top half of a cell.
UPPER_HALF = "▀"

#: The bottom half, for a cell whose top half has nothing to show.
LOWER_HALF = "▄"

#: A pixel more transparent than this draws nothing.
OPAQUE = 128

#: How many pixels of the image one cell holds, up and down.
PIXELS_PER_CELL = 2


def rows_for_cells(rows: int) -> int:
    "How many pixel rows an image needs to fill this many cell rows."
    return max(1, rows) * PIXELS_PER_CELL


def _true_color(code: int, red: int, green: int, blue: int) -> str:
    return "%i;2;%i;%i;%i" % (code, red, green, blue)


def _256_color(code: int, red: int, green: int, blue: int) -> str:
    return "%i;5;%i" % (code, _256_colors[(red, green, blue)])


def _16_color(code: int, red: int, green: int, blue: int) -> str:
    name = _get_closest_ansi_color(red, green, blue)
    table = BG_ANSI_COLORS if code == 48 else FG_ANSI_COLORS
    return str(table[name])


#: How to write one colour, for each depth a terminal can have.
_WRITER = {
    ColorDepth.DEPTH_24_BIT: _true_color,
    ColorDepth.DEPTH_8_BIT: _256_color,
    ColorDepth.DEPTH_4_BIT: _16_color,
    ColorDepth.DEPTH_1_BIT: _16_color,
}


def _pixel(rgba: bytes, columns: int, x: int, y: int) -> Tuple[int, int, int, int]:
    "One pixel of an RGBA buffer that is `columns` pixels wide."
    at = (y * columns + x) * 4
    return (rgba[at], rgba[at + 1], rgba[at + 2], rgba[at + 3])


def average_rgba(
    rgba: bytes, width: int, height: int, new_width: int, new_height: int
) -> bytes:
    """
    Resize RGBA pixels by averaging the ones that fall into each new
    pixel.

    An image that reaches a grid of cells has shrunk a long way, and a
    nearest neighbour keeps one pixel out of hundreds. The average
    keeps the colour of the area, which is what the eye reads at that
    size.

    A pixel that is more transparent counts for less, so the colour of
    an edge does not bleed out of the shape.
    """
    if (new_width, new_height) == (width, height):
        return rgba
    if width <= 0 or height <= 0 or new_width <= 0 or new_height <= 0:
        return b""

    out = bytearray(new_width * new_height * 4)
    for y in range(new_height):
        top = y * height // new_height
        bottom = max(top + 1, (y + 1) * height // new_height)
        for x in range(new_width):
            left = x * width // new_width
            right = max(left + 1, (x + 1) * width // new_width)

            red = green = blue = alpha = 0
            weight = 0
            count = 0
            for source_y in range(top, bottom):
                row = source_y * width
                for source_x in range(left, right):
                    at = (row + source_x) * 4
                    a = rgba[at + 3]
                    red += rgba[at] * a
                    green += rgba[at + 1] * a
                    blue += rgba[at + 2] * a
                    alpha += a
                    weight += a
                    count += 1

            at = (y * new_width + x) * 4
            if weight:
                out[at] = red // weight
                out[at + 1] = green // weight
                out[at + 2] = blue // weight
                out[at + 3] = alpha // count
            # A block of pixels that are all clear stays clear, and the
            # zeros are already there.

    return bytes(out)


def blocks_for(
    rgba: bytes,
    columns: int,
    rows: int,
    depth: ColorDepth = ColorDepth.DEPTH_24_BIT,
) -> List[str]:
    """
    One string for each row of cells, drawing `rgba` as half blocks.

    `rgba` holds `columns` by `rows * 2` pixels. Each cell takes the
    pixel above and the pixel below it.

    A cell whose two pixels are both clear draws nothing: it steps over
    with a cursor move, so the text under a transparent edge stays. A
    cell with one clear half paints the half that is there and leaves
    the other on the background of the terminal.

    Every row ends with a reset, so no row carries a colour into the
    text that follows it.
    """
    if columns <= 0 or rows <= 0:
        return []

    needed = columns * rows_for_cells(rows) * 4
    if len(rgba) < needed:
        return []

    write = _WRITER.get(depth, _true_color)
    lines = []

    for row in range(rows):
        parts: List[str] = []
        #: What the cell before set, so a run of one colour writes the
        #: sequence once.
        current: Optional[Tuple[str, str]] = None
        #: Cells passed over since the last one that drew.
        skipped = 0

        for column in range(columns):
            top = _pixel(rgba, columns, column, row * PIXELS_PER_CELL)
            bottom = _pixel(rgba, columns, column, row * PIXELS_PER_CELL + 1)
            top_shows = top[3] >= OPAQUE
            bottom_shows = bottom[3] >= OPAQUE

            if not top_shows and not bottom_shows:
                skipped += 1
                # The colours do not reach across a cell that was
                # stepped over, because nothing was written there.
                current = None
                continue

            if skipped:
                parts.append("\x1b[%iC" % (skipped,))
                skipped = 0

            if top_shows and bottom_shows:
                character = UPPER_HALF
                wanted = (write(38, *top[:3]), write(48, *bottom[:3]))
            elif top_shows:
                character = UPPER_HALF
                wanted = (write(38, *top[:3]), "49")
            else:
                character = LOWER_HALF
                wanted = (write(38, *bottom[:3]), "49")

            if wanted != current:
                parts.append("\x1b[%sm" % (";".join(wanted),))
                current = wanted
            parts.append(character)

        lines.append(("".join(parts) + "\x1b[0m") if parts else "")

    return lines
