"""
Tests for what a client draws when its terminal does not speak the
kitty graphics protocol, and for the detection that picks the way.
"""
import re

import pytest
from ptterm.sixel import decode_sixel

from pymux import graphics as graphics_module
from pymux.blocks import LOWER_HALF, UPPER_HALF
from pymux.graphics import ClientGraphics

from test_graphics_output import IMAGE_DATA, make_state, placement, view


def make_client(kitty=False, sixel=False, repaint=None):
    "Return (client graphics, list that collects what it writes)."
    written = []
    client = ClientGraphics(written.append, lambda: None, repaint)
    client.kitty_supported = kitty
    client.sixel_supported = sixel
    return client, written


def sixels(written):
    "The sixel sequences that were written, with their cursor position."
    return re.findall(
        r"\x1b\[(\d+);(\d+)H(\x1bP[^\x1b]*\x1b\\)", "".join(written)
    )


# ----------------------------------------------------------------------
# Detection.


def test_the_graphics_query_reply_turns_on_kitty():
    client, _written = make_client()
    client.handle_reply("\x1b_Gi=31;OK\x1b\\")
    assert client.kitty_supported
    assert not client.sixel_supported


def test_device_attributes_with_four_turn_on_sixel():
    client, _written = make_client()
    client.handle_reply("\x1b[?62;1;4;6c")
    assert client.sixel_supported
    assert client.supported


def test_device_attributes_without_four_leave_sixel_off():
    client, _written = make_client()
    client.handle_reply("\x1b[?62;1;6c")
    assert not client.sixel_supported
    # It still draws: half blocks need nothing but colour.
    assert client.supported
    assert client.blocks_wanted


def test_a_forty_in_the_attributes_is_not_a_four():
    client, _written = make_client()
    client.handle_reply("\x1b[?62;40;46c")
    assert not client.sixel_supported


def test_the_cell_size_report_is_read():
    client, _written = make_client()
    assert (client.cell_width, client.cell_height) == (10, 20)
    client.handle_reply("\x1b[6;17;8t")  # Height first, then width.
    assert (client.cell_width, client.cell_height) == (8, 17)


def test_an_impossible_cell_size_is_ignored():
    client, _written = make_client()
    client.handle_reply("\x1b[6;0;0t")
    client.handle_reply("\x1b[6;9999;9999t")
    assert (client.cell_width, client.cell_height) == (10, 20)


def test_a_terminal_that_answers_nothing_draws_half_blocks():
    "It used to draw nothing at all. Every client shows something now."
    client, written = make_client()
    client.handle_reply("\x1b[?62;1;6c")
    client.render([view(make_state(placement()))])
    joined = "".join(written)
    assert UPPER_HALF in joined or LOWER_HALF in joined
    assert "\x1bP" not in joined  # No sixel.
    assert "\x1b_G" not in joined  # No kitty.


def test_nothing_is_drawn_before_the_detection_answers():
    """
    The device attributes reply closes the detection. Drawing half
    blocks before it would put text where an image is about to go.
    """
    client, written = make_client()
    assert not client.supported
    client.render([view(make_state(placement()))])
    assert written == []


# ----------------------------------------------------------------------
# Choosing the way to draw.


def test_kitty_wins_over_sixel():
    client, written = make_client(kitty=True, sixel=True)
    client.render([view(make_state(placement()))])
    joined = "".join(written)
    assert "\x1b_Ga=t," in joined  # The kitty transmission.
    assert "\x1bP" not in joined  # No sixel.


def test_sixel_is_used_when_kitty_is_missing():
    client, written = make_client(sixel=True)
    client.render([view(make_state(placement()), x=4, y=2)])
    found = sixels(written)
    assert len(found) == 1
    row, column, _sequence = found[0]
    assert (int(row), int(column)) == (3, 5)  # One based.


def test_the_sixel_batch_saves_and_restores_the_cursor():
    client, written = make_client(sixel=True)
    client.render([view(make_state(placement()))])
    assert written[-1].startswith("\x1b7")
    assert written[-1].endswith("\x1b8")


def test_the_half_blocks_land_on_the_cells_of_the_placement():
    "One cursor move for each row, at the left edge of the placement."
    client, written = make_client()
    client.handle_reply("\x1b[?62;1;6c")
    client.render(
        [view(make_state(placement(columns=3, rows=2)), x=4, y=2)]
    )
    joined = "".join(written)
    moves = re.findall(r"\x1b\[(\d+);(\d+)H", joined)
    assert moves == [("3", "5"), ("4", "5")]


def test_the_half_blocks_save_and_restore_the_cursor():
    client, written = make_client()
    client.handle_reply("\x1b[?62;1;6c")
    client.render([view(make_state(placement()))])
    assert written[-1].startswith("\x1b7")
    assert written[-1].endswith("\x1b8")


def test_a_second_frame_that_did_not_change_writes_nothing():
    client, written = make_client()
    client.handle_reply("\x1b[?62;1;6c")
    views = [view(make_state(placement()))]
    client.render(views)
    assert written
    written.clear()
    client.render(views)
    assert written == []


# ----------------------------------------------------------------------
# The image itself.


def test_the_image_is_scaled_to_the_cells_of_the_terminal():
    client, written = make_client(sixel=True)
    client.cell_width, client.cell_height = 8, 17
    # The placement covers three columns and two rows.
    client.render([view(make_state(placement(columns=3, rows=2)))])

    _row, _column, sequence = sixels(written)[0]
    width, height, _pixels = decode_sixel(sequence[2:-2])
    assert (width, height) == (3 * 8, 2 * 17)


def test_the_image_keeps_its_pixels_without_a_cell_size_report():
    client, written = make_client(sixel=True)
    # The default cell matches the one that the pane assumes, so a
    # 2x2 pixel image over one cell grows to that cell.
    client.render([view(make_state(placement(columns=1, rows=1)))])
    _row, _column, sequence = sixels(written)[0]
    width, height, _pixels = decode_sixel(sequence[2:-2])
    assert (width, height) == (10, 20)


def test_the_colours_of_the_image_survive():
    client, written = make_client(sixel=True)
    client.cell_width, client.cell_height = 1, 1
    state = make_state(placement(columns=2, rows=2), data=IMAGE_DATA)
    client.render([view(state)])

    _row, _column, sequence = sixels(written)[0]
    width, height, pixels = decode_sixel(sequence[2:-2])
    assert (width, height) == (2, 2)
    # The source is a 2x2 RGB image of the bytes 0 to 11. Sixel holds a
    # colour as three percentages, so every channel is approximated.
    for index, expected in enumerate([(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]):
        got = tuple(pixels[index * 4 : index * 4 + 3])
        assert max(abs(a - b) for a, b in zip(got, expected)) <= 3
        assert pixels[index * 4 + 3] == 255


def test_a_cropped_placement_is_encoded_smaller():
    client, written = make_client(sixel=True)
    client.cell_width, client.cell_height = 1, 1
    # Two of the four columns of the placement fit in the pane.
    client.render(
        [view(make_state(placement(x=2, columns=4, rows=2)), width=4)]
    )
    _row, _column, sequence = sixels(written)[0]
    width, _height, _pixels = decode_sixel(sequence[2:-2])
    assert width == 2


def test_an_image_outside_the_pane_draws_nothing():
    client, written = make_client(sixel=True)
    client.render([view(make_state(placement(y=50)), height=24)])
    assert sixels(written) == []


def test_a_png_image_is_drawn():
    from test_sixel_encoder import _png

    client, written = make_client(sixel=True)
    client.cell_width, client.cell_height = 1, 1
    png = _png(2, 1, [(10, 20, 30), (200, 100, 50)])
    state = make_state(
        placement(columns=2, rows=1), data=png, format=100, width=2, height=1
    )
    client.render([view(state)])

    _row, _column, sequence = sixels(written)[0]
    width, height, pixels = decode_sixel(sequence[2:-2])
    assert (width, height) == (2, 1)
    assert max(abs(pixels[i] - (10, 20, 30)[i]) for i in range(3)) <= 3


def test_an_image_that_cannot_be_read_is_skipped():
    client, written = make_client(sixel=True)
    state = make_state(placement(), data=b"nonsense", format=100)
    client.render([view(state)])
    assert sixels(written) == []


# ----------------------------------------------------------------------
# Frames and repainting.


def test_an_unchanged_frame_draws_nothing_more():
    calls = []
    client, written = make_client(sixel=True, repaint=lambda: calls.append(1))
    state = make_state(placement())

    client.render([view(state)])
    assert len(calls) == 1  # The image appeared.
    written.clear()

    # The repaint of the renderer wipes the pixels, so they go out
    # again.
    client.render([view(state)])
    assert sixels(written) != []
    written.clear()

    # After that, nothing changes any more.
    client.render([view(state)])
    assert written == []
    assert len(calls) == 1


def test_a_moved_image_asks_for_a_repaint():
    calls = []
    client, written = make_client(sixel=True, repaint=lambda: calls.append(1))
    state = make_state(placement())
    client.render([view(state)])
    client.render([view(state)])  # Settle.
    written.clear()
    calls.clear()

    state.placements[0].y = 4
    client.render([view(state)])
    assert len(calls) == 1
    row, _column, _sequence = sixels(written)[0]
    assert int(row) == 5


def test_a_removed_image_asks_for_a_repaint_and_draws_nothing():
    calls = []
    client, written = make_client(sixel=True, repaint=lambda: calls.append(1))
    state = make_state(placement())
    client.render([view(state)])
    client.render([view(state)])
    written.clear()
    calls.clear()

    state.placements = []
    client.render([view(state)])
    assert len(calls) == 1
    assert sixels(written) == []


def test_no_views_removes_the_images():
    client, written = make_client(sixel=True)
    state = make_state(placement())
    client.render([view(state)])
    client.render([view(state)])
    written.clear()

    client.render([])
    assert sixels(written) == []
    client.render([])
    assert written == []


def test_reset_asks_for_a_repaint():
    calls = []
    client, _written = make_client(sixel=True, repaint=lambda: calls.append(1))
    client.render([view(make_state(placement()))])
    calls.clear()

    client.reset()
    assert len(calls) == 1

    # And it forgot everything: the image goes out again.
    client, written = make_client(sixel=True)
    client.render([view(make_state(placement()))])
    client.reset()
    written.clear()
    client.render([view(make_state(placement()))])
    assert sixels(written) != []


# ----------------------------------------------------------------------
# The encoder cache.


@pytest.fixture
def counted_encoder(monkeypatch):
    "Count the calls of the sixel encoder."
    calls = []
    original = graphics_module.encode_sixel

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(graphics_module, "encode_sixel", counting)
    return calls


def test_an_unchanged_image_is_encoded_once(counted_encoder):
    client, _written = make_client(sixel=True)
    state = make_state(placement())
    for _ in range(4):
        client.render([view(state)])
    assert len(counted_encoder) == 1


def test_moving_an_image_does_not_encode_it_again(counted_encoder):
    client, _written = make_client(sixel=True)
    state = make_state(placement())
    client.render([view(state)])
    state.placements[0].y = 3
    client.render([view(state)])
    # Same pixels, same size: only the cursor position differs.
    assert len(counted_encoder) == 1


def test_a_new_crop_is_encoded_again(counted_encoder):
    client, _written = make_client(sixel=True)
    state = make_state(placement(columns=4, rows=2))
    client.render([view(state, width=80)])
    client.render([view(state, width=3)])  # Now cropped.
    assert len(counted_encoder) == 2


def test_the_cache_forgets_an_image_that_is_gone(counted_encoder):
    client, _written = make_client(sixel=True)
    state = make_state(placement())
    client.render([view(state)])
    client.render([])  # The image is gone: the cache drops it.
    client.render([view(state)])
    assert len(counted_encoder) == 2
