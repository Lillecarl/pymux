"""
`pymux_rs.Vp9Decoder` gives back the pixels a VP9 stream holds, as RGB.

The streams are lossless 4:4:4, made by ffmpeg from planes this file
writes, so the decoder has exactly those planes and only the colour
conversion stands between them and the RGB: full range BT.601, held to
the formula in floating point. Lillecarl/pymux#639.
"""

from __future__ import annotations

import random
import shutil
import struct
import subprocess

import pymux_rs
import pytest

WIDTH, HEIGHT = 40, 24


def planes(seed: int) -> bytes:
    "Random Y, U and V planes, one byte a pixel each."
    chance = random.Random(seed)
    return bytes(chance.randrange(256) for _ in range(3 * WIDTH * HEIGHT))


def vp9_frames(yuv: list[bytes]) -> list[bytes]:
    "Each frame of `yuv` as lossless VP9, one packet a frame."
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "the check puts ffmpeg on PATH"
    ivf = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv444p",
         "-color_range", "pc", "-s", "%dx%d" % (WIDTH, HEIGHT), "-i", "-",
         "-c:v", "libvpx-vp9", "-lossless", "1", "-f", "ivf", "-"],
        input=b"".join(yuv), capture_output=True, check=True,
    ).stdout
    frames, at = [], 32
    while at < len(ivf):
        (size,) = struct.unpack_from("<I", ivf, at)
        frames.append(ivf[at + 12 : at + 12 + size])
        at += 12 + size
    return frames


def rgb_of(yuv: bytes, x: int, y: int, w: int, h: int) -> bytes:
    "The formula, pixel by pixel, for a rectangle of the frame."
    area = WIDTH * HEIGHT
    out = bytearray()
    for row in range(y, y + h):
        for column in range(x, x + w):
            at = row * WIDTH + column
            luma, u, v = yuv[at], yuv[area + at] - 128, yuv[2 * area + at] - 128
            for value in (luma + 1.402 * v, luma - 0.344136 * u - 0.714136 * v, luma + 1.772 * u):
                out.append(min(255, max(0, round(value))))
    return bytes(out)


def assert_close(got: bytes, want: bytes) -> None:
    "Equal up to the last bit the fixed point rounds differently."
    assert len(got) == len(want)
    assert max(abs(a - b) for a, b in zip(got, want, strict=True)) <= 1


def test_a_frame_comes_back_whole():
    yuv = planes(1)
    (frame,) = vp9_frames([yuv])
    width, height, ((x, y, w, h, rgb),) = pymux_rs.Vp9Decoder().decode(frame)
    assert (width, height, x, y, w, h) == (WIDTH, HEIGHT, 0, 0, WIDTH, HEIGHT)
    assert_close(rgb, rgb_of(yuv, 0, 0, WIDTH, HEIGHT))


def test_the_next_frame_reads_against_the_one_before():
    first, second = planes(2), planes(3)
    frames = vp9_frames([first, second])
    decoder = pymux_rs.Vp9Decoder()
    decoder.decode(frames[0])
    _width, _height, ((*_rect, rgb),) = decoder.decode(frames[1])
    assert_close(rgb, rgb_of(second, 0, 0, WIDTH, HEIGHT))


def test_rectangles_are_cut_to_the_frame():
    yuv = planes(4)
    (frame,) = vp9_frames([yuv])
    _width, _height, rects = pymux_rs.Vp9Decoder().decode(frame, [(3, 5, 7, 2), (35, 20, 10, 10), (50, 0, 4, 4)])
    assert [rect[:4] for rect in rects] == [(3, 5, 7, 2), (35, 20, 5, 4)]
    assert_close(rects[0][4], rgb_of(yuv, 3, 5, 7, 2))
    assert_close(rects[1][4], rgb_of(yuv, 35, 20, 5, 4))


def test_data_that_is_no_frame_is_an_error():
    with pytest.raises(ValueError):
        pymux_rs.Vp9Decoder().decode(b"\x00" * 16)
