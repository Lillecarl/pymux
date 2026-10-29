"""
`pymux imgcat` writes an image as the kitty graphics protocol.

The bytes it writes are read back by a real screen, so what is judged
is what pymux makes of them: an image held, and a placement over the
cells it was sized to.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pyte.screen import Screen
from pyte.streams import Stream

from pymux.entry_points.run_pymux import Mode, parse_arguments
from pymux.graphics import DEFAULT_CELL_WIDTH
from pymux.imgcat import CHUNK, cells_for, imgcat, sequences_of


def _png(width: int, height: int) -> bytes:
    written = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(written, "PNG")
    return written.getvalue()


@pytest.mark.parametrize(
    ("pixels", "cell", "room", "width", "expected"),
    [
        # As many cells as the pixels fill.
        ((100, 40), (10, 20), None, None, (10, 2)),
        # Held to the terminal's width, with the rows following the shape.
        ((400, 200), (10, 20), 20, None, (20, 5)),
        # As many columns as asked for.
        ((100, 100), (10, 20), 80, 4, (4, 2)),
        # Never less than a cell.
        ((1, 1), (10, 20), 80, None, (1, 1)),
    ],
)
def test_an_image_covers_the_cells_its_pixels_fill(pixels, cell, room, width, expected):
    assert cells_for(pixels, cell, room, width) == expected


def test_a_large_image_goes_in_chunks_and_the_last_says_so():
    sequences = sequences_of(b"x" * (CHUNK * 2), 3, 2)
    assert len(sequences) == 3
    assert sequences[0].startswith(b"\x1b_Ga=T,f=100,q=2,c=3,r=2,m=1;")
    assert sequences[1].startswith(b"\x1b_Gm=1;")
    assert sequences[2].startswith(b"\x1b_Gm=0;")
    assert all(len(sequence.partition(b";")[2]) <= CHUNK + 2 for sequence in sequences)


def test_a_screen_holds_the_image_over_the_cells_it_was_sized_to(tmp_path):
    path = tmp_path / "red.png"
    path.write_bytes(_png(40, 40))
    out = io.BytesIO()
    assert imgcat(["--width", "4", str(path)], io.BytesIO(), out, io.StringIO()) == 0

    screen = Screen(10, 20, write_process_input=lambda _data: None)
    Stream(screen).feed(out.getvalue().decode("ascii"))
    (placement,) = screen.graphics.placements
    image = screen.graphics.images_by_id[placement.image_id]
    assert (placement.columns, placement.rows) == (4, 2)
    assert (image.width, image.height) == (40, 40)


def test_an_svg_is_drawn_at_the_pixels_of_the_cells_it_covers(tmp_path):
    path = tmp_path / "red.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="red"/></svg>'
    )
    out = io.BytesIO()
    assert imgcat(["--width", "4", str(path)], io.BytesIO(), out, io.StringIO()) == 0

    screen = Screen(10, 20, write_process_input=lambda _data: None)
    Stream(screen).feed(out.getvalue().decode("ascii"))
    (placement,) = screen.graphics.placements
    image = screen.graphics.images_by_id[placement.image_id]
    assert (placement.columns, placement.rows) == (4, 2)
    assert (image.width, image.height) == (4 * DEFAULT_CELL_WIDTH, 4 * DEFAULT_CELL_WIDTH)


def test_standard_input_is_read_for_a_dash():
    out = io.BytesIO()
    assert imgcat(["-"], io.BytesIO(_png(10, 10)), out, io.StringIO()) == 0
    assert out.getvalue().startswith(b"\x1b_Ga=T,f=100")


def test_a_file_that_is_no_image_says_so_and_the_rest_still_show(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not an image")
    good = tmp_path / "red.png"
    good.write_bytes(_png(10, 10))
    out, err = io.BytesIO(), io.StringIO()
    assert imgcat([str(bad), str(good)], io.BytesIO(), out, err) == 1
    assert "notes.txt" in err.getvalue()
    assert out.getvalue().count(b"a=T") == 1


def test_the_command_line_hands_its_words_to_imgcat():
    a, mode, command = parse_arguments(["imgcat", "-w", "10", "a.png", "b.jpg"])
    assert mode is Mode.IMGCAT
    assert command is None
    assert a.imgcat_words == ["-w", "10", "a.png", "b.jpg"]
