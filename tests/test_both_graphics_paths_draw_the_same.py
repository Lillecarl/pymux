"""
The kitty path and the sixel path show the same picture.

pymux re-encodes a pane's image for whatever the client terminal
speaks. The two paths do the same job in opposite ways:

- **sixel**: pymux crops and scales the pixels itself and writes the
  result. What the terminal draws is what pymux encoded.
- **kitty**: pymux sends the image untouched and writes a placement
  that *tells* the terminal the crop rectangle and the cell box. The
  terminal does the cropping and the scaling.

So the two cannot be compared byte for byte, and nothing compared them
at all: the kitty path was checked for the shape of its sequence
(`a=t,` present, no sixel) and never for what it would draw. If its
geometry drifts from the sixel path -- a crop off by a row, a cell
count that rounds the other way -- no test went red.

**What this file does.** It renders one pane state twice, once for each
path, and reduces both to pixels: the sixel by decoding it, the kitty
transmission by doing to it exactly what its own placement asks the
terminal to do. Then it compares them.

**What that can and cannot catch.** It catches a disagreement about
*what pymux asked for* -- the crop rectangle, the cell box, the
position, and which images are drawn at all. It cannot catch a terminal
that scales differently from `scale_rgba`, because the terminal is not
here; that is what the picture fixture in Lillecarl/pymux#262 is for.

Sixel holds a colour as three percentages, so every channel is
approximated and the comparison allows the same 3 that
`test_graphics_fallback.py` allows. Lillecarl/pymux#262.
"""

import base64
import re
import zlib

import pytest
from pyte.sixel import decode_sixel

from pymux.graphics import ClientGraphics, _crop_rgba
from pymux.sixel import scale_rgba, to_rgba
from pyte.images import PixelFormat

from test_graphics_output import IMAGE_DATA, make_state, placement, view

#: What sixel's three percentages cost a channel. The same number
#: `test_graphics_fallback.test_colours_of_image_survive` allows.
CHANNEL_SLACK = 3


def _client(kitty=False, sixel=False, cell=(1, 1)):
    written = []
    client = ClientGraphics(written.append, lambda: None, None)
    client.kitty_supported = kitty
    client.sixel_supported = sixel
    client.cell_width, client.cell_height = cell
    return client, written


def _drawn_by_sixel(written):
    "Where the sixel went, and the pixels it holds."
    found = re.findall(r"\x1b\[(\d+);(\d+)H(\x1bP[^\x1b]*\x1b\\)", "".join(written))
    assert len(found) == 1, "expected one sixel, got %d" % len(found)
    row, column, sequence = found[0]
    width, height, pixels = decode_sixel(sequence[2:-2])
    return (int(row), int(column)), width, height, pixels


def _keys(control):
    "The `k=v` pairs of one kitty control string, as a dict of ints."
    pairs = {}
    for part in control.split(","):
        name, _, value = part.partition("=")
        if value.lstrip("-").isdigit():
            pairs[name] = int(value)
    return pairs


def _drawn_by_kitty(written, cell_width, cell_height):
    """
    Where the kitty placement went, and the pixels it asks the terminal
    to end up with.

    The transmission carries the image as the pane wrote it. The
    placement carries the crop (`x,y,w,h`) and the cell box (`c,r`), so
    doing both here is doing what the terminal is being told to do.
    """
    joined = "".join(written)

    sent = re.findall(r"\x1b_G(a=t,[^;]*);([^\x1b]*)\x1b\\", joined)
    more = re.findall(r"\x1b_G(m=\d+);([^\x1b]*)\x1b\\", joined)
    assert len(sent) == 1, "expected one transmission, got %d" % len(sent)
    control, first = sent[0]
    payload = first + "".join(chunk for _control, chunk in more)

    data = base64.b64decode(payload)
    keys = _keys(control)
    if "o" in control and "o=z" in control:
        data = zlib.decompress(data)

    put = re.findall(r"\x1b\[(\d+);(\d+)H\x1b_G(a=p,[^\x1b]*)\x1b\\", joined)
    assert len(put) == 1, "expected one placement, got %d" % len(put)
    row, column, place = put[0]
    asked = _keys(place)

    fmt = keys.get("f", PixelFormat.RGB)
    if fmt == PixelFormat.PNG:
        pixels = to_rgba(fmt, 0, 0, data)
        # A PNG carries its own size, and `to_rgba` reads it, so the
        # crop below needs the size the placement was built against.
        width = height = None
    else:
        width, height = keys["s"], keys["v"]
        pixels = to_rgba(fmt, width, height, data)

    assert pixels is not None, "the transmitted image could not be read"

    if "w" in asked:
        assert width is not None, "a cropped PNG is not covered here"
        pixels = _crop_rgba(pixels, width, height, (asked["x"], asked["y"], asked["w"], asked["h"]))
        width, height = asked["w"], asked["h"]

    target_width = max(1, asked["c"] * cell_width)
    target_height = max(1, asked["r"] * cell_height)
    pixels = scale_rgba(pixels, width, height, target_width, target_height)

    return (int(row), int(column)), target_width, target_height, pixels


def _both(state_args, cell=(1, 1), **view_args):
    "One pane state rendered down each path, reduced to pixels."
    kitty_client, kitty_written = _client(kitty=True, cell=cell)
    kitty_client.render([view(make_state(*state_args[0], **state_args[1]), **view_args)])

    sixel_client, sixel_written = _client(sixel=True, cell=cell)
    sixel_client.render([view(make_state(*state_args[0], **state_args[1]), **view_args)])

    return (
        _drawn_by_kitty(kitty_written, *cell),
        _drawn_by_sixel(sixel_written),
    )


def _same_pixels(kitty, sixel):
    "Every channel within sixel's slack, alpha exactly."
    assert len(kitty) == len(sixel), "%d bytes against %d" % (len(kitty), len(sixel))
    for index in range(0, len(kitty), 4):
        for channel in range(3):
            a = kitty[index + channel]
            b = sixel[index + channel]
            assert abs(a - b) <= CHANNEL_SLACK, (
                "pixel %d channel %d: kitty %d, sixel %d"
                % (index // 4, channel, a, b)
            )
        assert kitty[index + 3] == sixel[index + 3]


@pytest.mark.parametrize(
    "name,placements,state,cell,viewed",
    [
        # The plain case: one placement, one cell each way.
        ("one cell", [placement(columns=2, rows=2)], {}, (1, 1), {}),
        # A cell that is not square, which is what a real terminal has.
        # The two paths have to agree about which way round it goes.
        ("a real cell", [placement(columns=3, rows=2)], {}, (8, 17), {}),
        # A placement the pane is too narrow for. The sixel path crops
        # the pixels; the kitty path sends `x,y,w,h` and a smaller `c`.
        # This is the one where a drift is most likely.
        ("cropped by the pane", [placement(x=2, columns=4, rows=2)], {}, (1, 1), {"width": 4}),
        # Two rows of cells, so a crop that is off by a row shows.
        ("cropped from the top", [placement(y=0, columns=2, rows=4)], {}, (1, 1), {"height": 2}),
    ],
)
def test_the_two_paths_draw_the_same_picture(name, placements, state, cell, viewed):
    kitty, sixel = _both((placements, state), cell=cell, **viewed)
    kitty_at, kitty_width, kitty_height, kitty_pixels = kitty
    sixel_at, sixel_width, sixel_height, sixel_pixels = sixel

    assert kitty_at == sixel_at, "%s: the two paths draw at different cells" % name
    assert (kitty_width, kitty_height) == (sixel_width, sixel_height), (
        "%s: kitty asks for %dx%d and sixel drew %dx%d"
        % (name, kitty_width, kitty_height, sixel_width, sixel_height)
    )
    _same_pixels(kitty_pixels, sixel_pixels)


def test_a_placement_outside_the_pane_draws_on_neither_path():
    "Both paths agree that there is nothing to draw, not just one."
    kitty_client, kitty_written = _client(kitty=True)
    sixel_client, sixel_written = _client(sixel=True)
    for client in (kitty_client, sixel_client):
        client.render([view(make_state(placement(y=50)), height=24)])

    assert "a=p," not in "".join(kitty_written)
    assert "\x1bP" not in "".join(sixel_written)


def test_the_image_is_read_the_same_way_by_both(caplog):
    """
    The colours, end to end, and not only the geometry.

    `IMAGE_DATA` is a 2x2 RGB image of the bytes 0 to 11, which
    `test_graphics_fallback.py` checks the sixel path against directly.
    This says the kitty path carries the same four pixels.
    """
    kitty, sixel = _both(([placement(columns=2, rows=2)], {}), cell=(1, 1))
    _kitty_at, _w, _h, kitty_pixels = kitty
    _sixel_at, _sw, _sh, sixel_pixels = sixel

    for index, expected in enumerate([(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]):
        got = tuple(kitty_pixels[index * 4 : index * 4 + 3])
        assert got == expected, "kitty pixel %d is %r" % (index, got)
        drew = tuple(sixel_pixels[index * 4 : index * 4 + 3])
        assert max(abs(a - b) for a, b in zip(drew, expected)) <= CHANNEL_SLACK
