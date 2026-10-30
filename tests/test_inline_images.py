"""
iTerm2's inline images: `OSC 1337 ; File=inline=1`, drawn in the pane
as any image of it. Lillecarl/pymux#789.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.inline_images import as_png, cells_of

CELL = (10, 20)
PANE = (80, 24)


def _image(kind: str, size=(40, 20)) -> bytes:
    encoded = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(encoded, kind)
    return encoded.getvalue()


@pytest.mark.parametrize(
    ("arguments", "wanted"),
    [
        # As many cells as its pixels fill.
        ({}, (4, 1)),
        ({"width": "auto", "height": "auto"}, (4, 1)),
        # Cells, and the other side follows the shape.
        ({"width": "8"}, (8, 2)),
        ({"height": "3"}, (12, 3)),
        # Pixels, and a share of the pane.
        ({"width": "100px"}, (10, 3)),
        ({"width": "50%"}, (40, 10)),
        # Both, fitted inside the box, or stretched to it.
        ({"width": "8", "height": "8"}, (8, 2)),
        ({"width": "8", "height": "8", "preserveAspectRatio": "0"}, (8, 8)),
        # Never wider than the pane.
        ({"width": "200"}, (80, 20)),
    ],
)
def test_the_box_follows_the_arguments(arguments, wanted):
    assert cells_of(arguments, (40, 20), CELL, PANE) == wanted


def test_a_png_is_kept_and_another_kind_turns_into_one():
    png = _image("PNG")
    assert as_png(png) == (png, (40, 20))
    found = as_png(_image("JPEG"))
    assert found is not None
    converted, size = found
    assert converted.startswith(b"\x89PNG") and size == (40, 20)
    assert as_png(b"no image at all") is None


async def test_an_inline_image_is_placed_at_the_cursor():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
            screen = pane.screen
            screen.set_cell_size(*CELL)
            cursor = screen.pt_cursor_position
            start = (cursor.x, cursor.y)
            payload = base64.b64encode(_image("GIF")).decode("ascii")
            pymux.outward.forward_osc(pane, "1337", "File=inline=1;width=6:" + payload)

            (placement,) = screen.graphics.placements
            assert (placement.x, placement.y, placement.columns, placement.rows) == (*start, 6, 2)
            # Right after the image, on its last row, as kitty's.
            assert (cursor.x, cursor.y) == (start[0] + 6, start[1] + 1)


async def test_what_is_no_image_draws_nothing():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
            pymux.outward.forward_osc(pane, "1337", "File=inline=1:" + base64.b64encode(b"plain text").decode("ascii"))
            assert pane.screen.graphics.placements == []
