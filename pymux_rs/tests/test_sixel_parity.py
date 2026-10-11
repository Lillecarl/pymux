"""
`pymux_rs` encodes and converts what `pymux.sixel` does, byte for byte.

The encoder takes random images: few colours and many, under every
palette size, with pixels transparent enough to stay clear, so the
median cut splits, ties and runs out of buckets. The conversions take
every size. `pymux.kernels.install()` is held to the Python too, edges
and all. Lillecarl/pymux#574.
"""

from __future__ import annotations

import pymux_rs
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pyte.images import PixelFormat

from pymux import kernels, sixel

#: The references, taken before any test could have installed a kernel.
PURE_ENCODE = sixel.encode_sixel
PURE_SCALE = sixel.scale_rgba
PURE_TO_RGBA = sixel.to_rgba


@st.composite
def images(draw):
    width = draw(st.integers(1, 14))
    height = draw(st.integers(1, 14))
    # A few colours, so runs and ties happen, or any colour at all.
    colours = draw(
        st.one_of(
            st.lists(st.binary(min_size=3, max_size=3), min_size=1, max_size=6),
            st.just(None),
        )
    )
    pixels = bytearray()
    for _ in range(width * height):
        rgb = draw(st.sampled_from(colours)) if colours else draw(st.binary(min_size=3, max_size=3))
        alpha = draw(st.sampled_from([0, 127, 128, 255]))
        pixels += rgb + bytes([alpha])
    return width, height, bytes(pixels)


@given(images(), st.integers(-2, 300))
@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_sixel_is_encoded(image, max_colors):
    width, height, pixels = image
    assert pymux_rs.encode_sixel(width, height, pixels, max(1, min(max_colors, 255))) == PURE_ENCODE(
        width, height, pixels, max_colors
    )


@given(images(), st.integers(1, 6))
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_small_palette_cuts_the_same_buckets(image, max_colors):
    "The median cut runs whenever an image has more colours than this."
    width, height, pixels = image
    assert pymux_rs.encode_sixel(width, height, pixels, max_colors) == PURE_ENCODE(width, height, pixels, max_colors)


@given(images(), st.integers(1, 30), st.integers(1, 30))
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_pixels_are_scaled(image, new_width, new_height):
    width, height, pixels = image
    if (new_width, new_height) == (width, height):
        new_width += 1
    assert pymux_rs.scale_rgba(pixels, width, height, new_width, new_height) == PURE_SCALE(
        pixels, width, height, new_width, new_height
    )


@given(st.integers(1, 20), st.integers(1, 20), st.data())
@settings(max_examples=300, deadline=None)
def test_rgb_gains_the_same_alpha(width, height, data):
    rgb = data.draw(st.binary(min_size=width * height * 3, max_size=width * height * 3 + 5))
    assert pymux_rs.rgb_to_rgba(rgb, width * height) == PURE_TO_RGBA(PixelFormat.RGB, width, height, rgb)


@pytest.fixture
def installed(monkeypatch):
    "`kernels.install()`, undone once the test is over."
    monkeypatch.delenv(kernels.PURE, raising=False)
    for name in ("encode_sixel", "scale_rgba", "to_rgba"):
        monkeypatch.setattr(sixel, name, getattr(sixel, name))
    monkeypatch.setattr(kernels, "_replaced", {})
    kernels._install_images(pymux_rs)
    assert sixel.encode_sixel is not PURE_ENCODE


@given(images())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_installed_functions_answer_as_the_python(installed, image):
    width, height, pixels = image
    assert sixel.encode_sixel(width, height, pixels) == PURE_ENCODE(width, height, pixels)
    assert sixel.scale_rgba(pixels, width, height, 3, 2) == PURE_SCALE(pixels, width, height, 3, 2)
    assert sixel.to_rgba(PixelFormat.RGBA, width, height, pixels) == PURE_TO_RGBA(
        PixelFormat.RGBA, width, height, pixels
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda f: f.encode_sixel(0, 4, b""),
        lambda f: f.encode_sixel(5000, 5000, b""),
        lambda f: f.scale_rgba(b"\x01\x02\x03\x04", 1, 1, 1, 1),
        lambda f: f.scale_rgba(b"\x01\x02", 1, 1, 2, 2),
        lambda f: f.to_rgba(PixelFormat.RGB, 2, 2, b"\x00" * 5),
        lambda f: f.to_rgba(99, 1, 1, b"\x00" * 4),
    ],
)
def test_an_edge_goes_to_the_python(installed, call):
    class Pure:
        encode_sixel = staticmethod(PURE_ENCODE)
        scale_rgba = staticmethod(PURE_SCALE)
        to_rgba = staticmethod(PURE_TO_RGBA)

    assert call(sixel) == call(Pure)
