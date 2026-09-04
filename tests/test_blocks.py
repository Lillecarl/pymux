"""
Half blocks: an image drawn as coloured text.

One cell carries two pixels. The foreground paints the top half and the
background paints the bottom, so a grid of cells holds an image at twice
the vertical resolution.
"""
import pytest
from prompt_toolkit.output import ColorDepth

from pymux.blocks import (
    LOWER_HALF,
    OPAQUE,
    UPPER_HALF,
    average_rgba,
    blocks_for,
    rows_for_cells,
)

RED = (255, 0, 0, 255)
GREEN = (0, 255, 0, 255)
BLUE = (0, 0, 255, 255)
CLEAR = (0, 0, 0, 0)


def rgba(*pixels):
    "RGBA bytes, from pixels given in reading order."
    out = bytearray()
    for pixel in pixels:
        out.extend(pixel)
    return bytes(out)


# ----------------------------------------------------------------------
# One cell holds two pixels.


def test_a_cell_takes_the_pixel_above_and_the_one_below():
    lines = blocks_for(rgba(RED, GREEN), columns=1, rows=1)
    assert lines == ["\x1b[38;2;255;0;0;48;2;0;255;0m" + UPPER_HALF + "\x1b[0m"]


def test_a_row_of_cells_reads_two_rows_of_pixels():
    # Two columns, two pixel rows: the top row first, then the bottom.
    lines = blocks_for(rgba(RED, GREEN, BLUE, RED), columns=2, rows=1)
    assert lines[0].count(UPPER_HALF) == 2
    assert "38;2;255;0;0;48;2;0;0;255" in lines[0]  # Red over blue.
    assert "38;2;0;255;0;48;2;255;0;0" in lines[0]  # Green over red.


def test_one_row_of_cells_for_every_two_rows_of_pixels():
    lines = blocks_for(rgba(RED, GREEN, BLUE, RED), columns=1, rows=2)
    assert len(lines) == 2


def test_how_many_pixel_rows_a_cell_row_needs():
    assert rows_for_cells(1) == 2
    assert rows_for_cells(5) == 10
    assert rows_for_cells(0) == 2  # Never nothing.


# ----------------------------------------------------------------------
# Where a pixel is clear.


def test_a_clear_bottom_half_leaves_the_background_alone():
    lines = blocks_for(rgba(RED, CLEAR), columns=1, rows=1)
    assert lines == ["\x1b[38;2;255;0;0;49m" + UPPER_HALF + "\x1b[0m"]


def test_a_clear_top_half_draws_the_lower_block():
    lines = blocks_for(rgba(CLEAR, BLUE), columns=1, rows=1)
    assert lines == ["\x1b[38;2;0;0;255;49m" + LOWER_HALF + "\x1b[0m"]


def test_a_cell_that_is_clear_through_is_stepped_over():
    "The text under a transparent edge has to stay."
    lines = blocks_for(rgba(CLEAR, RED, CLEAR, GREEN), columns=2, rows=1)
    assert lines[0].startswith("\x1b[1C")
    assert lines[0].count(UPPER_HALF) + lines[0].count(LOWER_HALF) == 1


def test_a_row_that_is_clear_through_draws_nothing():
    lines = blocks_for(rgba(CLEAR, CLEAR), columns=1, rows=1)
    assert lines == [""]


def test_a_pixel_that_is_half_clear_counts_as_clear():
    almost = (255, 0, 0, OPAQUE - 1)
    assert blocks_for(rgba(almost, almost), columns=1, rows=1) == [""]
    enough = (255, 0, 0, OPAQUE)
    assert blocks_for(rgba(enough, enough), columns=1, rows=1) != [""]


# ----------------------------------------------------------------------
# What goes on the wire.


def test_a_run_of_one_colour_writes_the_sequence_once():
    # Three cells wide: the top row of pixels, then the bottom row.
    lines = blocks_for(rgba(RED, RED, RED, GREEN, GREEN, GREEN), columns=3, rows=1)
    assert lines[0].count("\x1b[38") == 1
    assert lines[0].count(UPPER_HALF) == 3


def test_a_cell_that_was_stepped_over_breaks_the_run():
    "Nothing was written on that cell, so no colour reaches across it."
    lines = blocks_for(
        rgba(RED, CLEAR, RED, GREEN, CLEAR, GREEN), columns=3, rows=1
    )
    assert "\x1b[1C" in lines[0]
    assert lines[0].count("\x1b[38") == 2


def test_every_row_ends_with_a_reset():
    "Nothing of an image may reach the text that follows it."
    for line in blocks_for(rgba(RED, GREEN, BLUE, RED), columns=1, rows=2):
        assert line.endswith("\x1b[0m")


def test_an_image_that_is_too_small_draws_nothing():
    assert blocks_for(rgba(RED), columns=1, rows=1) == []
    assert blocks_for(b"", columns=0, rows=0) == []


# ----------------------------------------------------------------------
# The colours that the terminal has.


def test_twenty_four_bits_write_the_pixel():
    lines = blocks_for(
        rgba(RED, GREEN), columns=1, rows=1, depth=ColorDepth.DEPTH_24_BIT
    )
    assert "38;2;255;0;0" in lines[0]


def test_two_hundred_and_fifty_six_colours_take_an_index():
    lines = blocks_for(
        rgba(RED, GREEN), columns=1, rows=1, depth=ColorDepth.DEPTH_8_BIT
    )
    assert "38;5;" in lines[0]
    assert "48;5;" in lines[0]
    assert ";2;" not in lines[0]


def test_sixteen_colours_take_the_plain_codes():
    # The same colour above and below, so the two codes name one
    # colour and the background is the foreground plus ten.
    lines = blocks_for(
        rgba(RED, RED), columns=1, rows=1, depth=ColorDepth.DEPTH_4_BIT
    )
    assert ";5;" not in lines[0]
    assert ";2;" not in lines[0]
    codes = lines[0].split("m")[0].lstrip("\x1b[").split(";")
    assert len(codes) == 2
    assert int(codes[1]) - int(codes[0]) == 10


# ----------------------------------------------------------------------
# Making an image fit the cells.


def test_the_average_of_a_block_of_pixels():
    "Four pixels into one: the colour of the area, not one corner."
    pixels = rgba(
        (0, 0, 0, 255), (100, 0, 0, 255),
        (0, 0, 0, 255), (100, 0, 0, 255),
    )
    out = average_rgba(pixels, 2, 2, 1, 1)
    assert out[0] == 50
    assert out[3] == 255


def test_a_clear_pixel_does_not_colour_its_neighbour():
    "A transparent pixel carries no colour into the average."
    pixels = rgba((255, 0, 0, 255), (0, 255, 0, 0))
    out = average_rgba(pixels, 2, 1, 1, 1)
    assert (out[0], out[1], out[2]) == (255, 0, 0)
    # Half of the area was clear, so the cell is half clear.
    assert out[3] == 127


def test_a_block_of_clear_pixels_stays_clear():
    out = average_rgba(rgba(CLEAR, CLEAR), 2, 1, 1, 1)
    assert out == bytes(4)


def test_the_same_size_is_given_back_untouched():
    pixels = rgba(RED, GREEN)
    assert average_rgba(pixels, 1, 2, 1, 2) is pixels


def test_growing_an_image_repeats_the_pixels():
    out = average_rgba(rgba(RED, BLUE), 2, 1, 4, 1)
    assert out[0:4] == bytes(RED)
    assert out[12:16] == bytes(BLUE)


@pytest.mark.parametrize("size", [(0, 1), (1, 0)])
def test_a_size_of_nothing_is_no_crash(size):
    assert average_rgba(rgba(RED), 1, 1, *size) == b""
