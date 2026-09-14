"""
An image a pane holds reaches the screen with its own pixels.

A pane reserves `ceil(pixels / cell)` cells for an image against the
cell size it was told, and the client draws those cells in the cell its
own terminal has. When the two are the same cell the box is the image
rounded up, so the image fits inside the cells with less than one cell
of slack -- and then neither path has anything to resample:

* the kitty path leaves "c" and "r" out, so the terminal draws the
  image at its own size and counts the cells itself;
* the sixel path encodes the pixels without `scale_rgba`.

That is Lillecarl/pymux#369. Before it, the box was always sent and
always filled, so every client whose cell was not the ten by twenty
that `pyte` assumed drew a resampled picture.

**Three placements must still be fitted to their box**, and each has
its own reason:

* one the program asked for, with "c" and "r" of its own. That is a
  request, not an artefact.
* one whose cells are too small for the pixels. There is nothing to do
  but fit it.
* a virtual one, drawn through unicode placeholders. The run of
  placeholder cells is the box, and `test_graphics_placeholders.py`
  judges that one.

`tests/picture-differences.txt` measures the same thing with real
terminals in front of it; this measures what pymux asks for.
"""

import re

from pyte.images import GraphicsImage, GraphicsPlacement, GraphicsState
from pyte.sixel import decode_sixel

from pymux.graphics import ClientGraphics, PaneView

#: An image that is four cells wide and two tall at a ten by twenty
#: cell, and the same at a ten by nineteen one: `ceil(38/19)` is 2.
IMAGE_WIDTH, IMAGE_HEIGHT = 40, 38

#: A cell that a real terminal has. kitty and foot draw DejaVu Sans
#: Mono at size 12 in ten by nineteen.
REAL_CELL = (10, 19)


def image_bytes(width=IMAGE_WIDTH, height=IMAGE_HEIGHT):
    return bytes((index * 7) % 256 for index in range(width * height * 3))


def client(kitty=False, sixel=False, cell=REAL_CELL):
    written = []
    graphics = ClientGraphics(written.append, lambda: None)
    graphics.kitty_supported = kitty
    graphics.sixel_supported = sixel
    graphics.cell_width, graphics.cell_height = cell
    return graphics, written


def state(columns, rows, asked_for_the_box=False, virtual=False):
    "A pane holding one image, placed over `columns` by `rows` cells."
    graphics = GraphicsState()
    graphics.images_by_id[1] = GraphicsImage(
        24, IMAGE_WIDTH, IMAGE_HEIGHT, image_bytes()
    )
    graphics.placements = [
        GraphicsPlacement(
            1,
            0,
            0,
            0,
            columns,
            rows,
            virtual=virtual,
            asked_for_the_box=asked_for_the_box,
        )
    ]
    return graphics


def view(graphics, width=80, height=24):
    return PaneView(
        pane_id=1,
        x=0,
        y=0,
        width=width,
        height=height,
        vertical_scroll=0,
        horizontal_scroll=0,
        graphics=graphics,
    )


def put_keys(written):
    "The keys of the one put command that was written."
    found = re.findall(r"\x1b\[\d+;\d+H\x1b_G(a=p,[^\x1b]*)\x1b\\", "".join(written))
    assert len(found) == 1, "expected one placement, got %d" % len(found)
    return dict(part.split("=", 1) for part in found[0].split(",") if "=" in part)


def sixel_size(written):
    "The pixel size of the one sixel that was written."
    found = re.findall(r"\x1bP[^\x1b]*\x1b\\", "".join(written))
    assert len(found) == 1, "expected one sixel, got %d" % len(found)
    width, height, _pixels = decode_sixel(found[0][2:-2])
    return (width, height)


# ----------------------------------------------------------------------
# The image keeps its pixels.


def test_the_kitty_path_sends_no_box_when_the_image_fits():
    graphics, written = client(kitty=True)
    graphics.render([view(state(columns=4, rows=2))])

    keys = put_keys(written)
    assert "c" not in keys and "r" not in keys


def test_the_sixel_path_encodes_the_pixels_when_the_image_fits():
    graphics, written = client(sixel=True)
    graphics.render([view(state(columns=4, rows=2))])

    assert sixel_size(written) == (IMAGE_WIDTH, IMAGE_HEIGHT)


def test_a_cell_taller_than_the_image_needs_leaves_the_slack_alone():
    """
    Four by two cells of a ten by twenty cell is 40 by 40 pixels, and
    the image is 40 by 38. The two rows over are not the image's to
    fill.
    """
    graphics, written = client(sixel=True, cell=(10, 20))
    graphics.render([view(state(columns=4, rows=2))])

    assert sixel_size(written) == (IMAGE_WIDTH, IMAGE_HEIGHT)


# ----------------------------------------------------------------------
# The three that are still fitted.


def test_a_box_the_program_asked_for_is_a_request():
    '"c=8,r=4" with this image means "draw it twice as big".'
    graphics, written = client(kitty=True)
    graphics.render([view(state(columns=8, rows=4, asked_for_the_box=True))])

    keys = put_keys(written)
    assert (keys["c"], keys["r"]) == ("8", "4")


def test_the_sixel_path_fills_a_box_the_program_asked_for():
    graphics, written = client(sixel=True)
    graphics.render([view(state(columns=8, rows=4, asked_for_the_box=True))])

    assert sixel_size(written) == (8 * 10, 4 * 19)


def test_cells_too_small_for_the_pixels_are_still_a_box():
    """
    A client whose cell is smaller than the one the pane counted
    against. Two rows of a six pixel cell is twelve, and the image is
    38: there is nothing to do but fit it.
    """
    graphics, written = client(kitty=True, cell=(6, 6))
    graphics.render([view(state(columns=4, rows=2))])

    keys = put_keys(written)
    assert (keys["c"], keys["r"]) == ("4", "2")
