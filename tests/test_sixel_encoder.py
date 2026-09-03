"""
Tests for the sixel encoder (`pymux.sixel`).

Most of them run the encoded image back through the decoder of ptterm,
which is an independent implementation: what the encoder writes has to
mean what it was given.
"""
import re
import struct
import zlib

from ptterm.sixel import decode_sixel

from pymux.sixel import encode_sixel, scale_rgba, to_rgba


def rgba(pixels):
    "RGBA bytes from a list of (r, g, b, a) tuples."
    return b"".join(bytes(pixel) for pixel in pixels)


def checkerboard(width, height, first=(255, 0, 0, 255), second=(0, 0, 255, 255)):
    return rgba(
        [
            first if (x + y) % 2 == 0 else second
            for y in range(height)
            for x in range(width)
        ]
    )


def round_trip(width, height, data):
    "Encode, then decode. Returns (width, height, RGBA bytes)."
    sequence = encode_sixel(width, height, data)
    assert sequence is not None
    assert sequence.startswith("\x1bP")
    assert sequence.endswith("\x1b\\")
    result = decode_sixel(sequence[2:-2])
    assert result is not None
    return result


def test_the_sequence_is_a_dcs_with_transparent_background():
    sequence = encode_sixel(2, 6, checkerboard(2, 6))
    # "P2 = 1" keeps the pixels that are not drawn clear.
    assert sequence.startswith('\x1bP0;1;0q"1;1;2;6')


def test_the_raster_attributes_carry_the_size():
    sequence = encode_sixel(7, 13, checkerboard(7, 13))
    assert '"1;1;7;13' in sequence


def test_a_two_colour_image_round_trips_exactly():
    width, height = 4, 8
    original = checkerboard(width, height)
    assert round_trip(width, height, original) == (width, height, original)


def test_a_tall_image_round_trips_over_several_bands():
    width, height = 3, 20
    original = checkerboard(width, height)
    assert round_trip(width, height, original) == (width, height, original)


def test_a_single_pixel_round_trips():
    original = rgba([(1, 2, 3, 255)])
    got_width, got_height, data = round_trip(1, 1, original)
    assert (got_width, got_height) == (1, 1)
    # The palette is stored in percent, so the value is approximated.
    assert data[3] == 255
    assert max(abs(data[i] - original[i]) for i in range(3)) <= 3


def test_transparent_pixels_are_not_drawn():
    pixels = [(255, 0, 0, 255), (0, 255, 0, 0), (255, 0, 0, 255)]
    _width, _height, data = round_trip(3, 1, rgba(pixels))
    assert tuple(data[0:4]) == (255, 0, 0, 255)
    assert tuple(data[4:8]) == (0, 0, 0, 0)  # Left clear.
    assert tuple(data[8:12]) == (255, 0, 0, 255)


def test_a_fully_transparent_image_has_nothing_to_draw():
    assert encode_sixel(2, 2, rgba([(1, 2, 3, 0)] * 4)) is None


def test_a_long_run_uses_the_repeat_introducer():
    sequence = encode_sixel(40, 6, rgba([(255, 0, 0, 255)] * 240))
    assert "!40" in sequence


def test_a_short_run_is_written_out():
    sequence = encode_sixel(2, 6, rgba([(255, 0, 0, 255)] * 12))
    assert "!" not in sequence


def test_every_colour_gets_a_palette_entry():
    colors = [(0, 0, 0, 255), (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
    sequence = encode_sixel(4, 1, rgba(colors))
    definitions = re.findall(r"#(\d+);2;(\d+);(\d+);(\d+)", sequence)
    assert len(definitions) == 4
    # The palette is in percent, not in bytes.
    assert ("100", "0", "0") in [tuple(d[1:]) for d in definitions]


def test_many_colours_are_reduced_to_the_palette_size():
    # A gradient with far more colours than sixel can hold.
    pixels = [
        (x * 4 % 256, y * 4 % 256, (x + y) * 3 % 256, 255)
        for y in range(24)
        for x in range(64)
    ]
    sequence = encode_sixel(64, 24, rgba(pixels))
    definitions = re.findall(r"#(\d+);2;", sequence)
    assert len(definitions) <= 255

    _width, _height, data = round_trip(64, 24, rgba(pixels))
    # Every pixel is close to the colour it started with.
    worst = 0
    for index, pixel in enumerate(pixels):
        for channel in range(3):
            worst = max(worst, abs(data[index * 4 + channel] - pixel[channel]))
    assert worst <= 64


def test_a_small_palette_can_be_asked_for():
    pixels = [(x * 8 % 256, 0, 0, 255) for x in range(32)]
    sequence = encode_sixel(32, 1, rgba(pixels), max_colors=4)
    assert len(re.findall(r"#(\d+);2;", sequence)) <= 4


def test_bad_sizes_are_refused():
    assert encode_sixel(0, 4, b"") is None
    assert encode_sixel(4, 0, b"") is None
    assert encode_sixel(-1, 4, b"") is None
    assert encode_sixel(4000, 4000, b"") is None  # Past the pixel bound.


def test_short_data_is_refused():
    assert encode_sixel(4, 4, b"\x00" * 10) is None


# ----------------------------------------------------------------------
# Pixel conversion.


def test_rgba_data_passes_through():
    data = rgba([(1, 2, 3, 4)] * 4)
    assert to_rgba(32, 2, 2, data) == data


def test_rgb_data_gets_an_alpha_channel():
    data = bytes([1, 2, 3, 4, 5, 6])
    assert to_rgba(24, 2, 1, data) == bytes([1, 2, 3, 255, 4, 5, 6, 255])


def test_short_data_has_no_pixels():
    assert to_rgba(24, 2, 2, b"\x01\x02") is None
    assert to_rgba(32, 2, 2, b"\x01\x02") is None


def test_an_unknown_format_has_no_pixels():
    assert to_rgba(7, 1, 1, b"\x00" * 4) is None


def test_png_data_is_decoded():
    png = _png(2, 1, [(10, 20, 30), (40, 50, 60)])
    assert to_rgba(100, 2, 1, png) == bytes(
        [10, 20, 30, 255, 40, 50, 60, 255]
    )


def test_broken_png_data_has_no_pixels():
    assert to_rgba(100, 1, 1, b"\x89PNG\r\n\x1a\nrubbish") is None


def _png(width, height, pixels):
    "A minimal RGB PNG. (Only the tests use it.)"
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # Filter: none.
        for x in range(width):
            raw += bytes(pixels[y * width + x])

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


# ----------------------------------------------------------------------
# Scaling.


def test_the_same_size_is_not_copied():
    data = checkerboard(2, 2)
    assert scale_rgba(data, 2, 2, 2, 2) is data


def test_growing_repeats_the_pixels():
    data = rgba([(1, 0, 0, 255), (2, 0, 0, 255)])
    grown = scale_rgba(data, 2, 1, 4, 1)
    assert [grown[i * 4] for i in range(4)] == [1, 1, 2, 2]


def test_shrinking_drops_pixels():
    data = rgba([(1, 0, 0, 255), (2, 0, 0, 255), (3, 0, 0, 255), (4, 0, 0, 255)])
    small = scale_rgba(data, 4, 1, 2, 1)
    assert [small[i * 4] for i in range(2)] == [1, 3]


def test_scaling_works_in_both_directions():
    data = checkerboard(2, 2)
    grown = scale_rgba(data, 2, 2, 6, 4)
    assert len(grown) == 6 * 4 * 4
    # The top left pixel keeps its colour.
    assert tuple(grown[0:4]) == (255, 0, 0, 255)


def test_a_scaled_image_still_encodes():
    data = checkerboard(4, 4)
    grown = scale_rgba(data, 4, 4, 8, 12)
    width, height, _pixels = round_trip(8, 12, grown)
    assert (width, height) == (8, 12)
