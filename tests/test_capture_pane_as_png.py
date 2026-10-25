"""
`capture-pane --png` draws a pane for a reader with eyes and no terminal.

The picture is read back with Pillow and judged by its pixels: the size
of the grid, the colour a cell is painted, and whether a glyph left ink.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image
from pyte.painted import Paint

from pymux import screenshot
from pymux.commands import call_command_handler
from pymux.graphics import DEFAULT_CELL_HEIGHT, DEFAULT_CELL_WIDTH
from pymux.main import Pymux

COLUMNS = 20
LINES = 5

FONT_DIR = Path(os.environ.get("PYMUX_TEST_FONT_DIR", "/nonexistent"))
FONTS = [str(FONT_DIR / ("JetBrainsMonoNerdFontMono-%s.ttf" % face)) for face in ("Regular", "Bold", "Italic")]

pytestmark = pytest.mark.skipif(not FONT_DIR.is_dir(), reason="PYMUX_TEST_FONT_DIR names no fonts")


@pytest.fixture
async def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    async with mux.running():
        await mux.create_window("%s -c pass" % (sys.executable,))
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def create_pane(mux, data: str):
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    pane.terminal.terminal_control.stream.feed(data)
    return pane


async def picture(mux, *arguments) -> Image.Image:
    mux.command_output = []
    try:
        fonts = [word for path in FONTS for word in ("--font", path)]
        answer = call_command_handler("capture-pane", mux, ["-p", "--png", *fonts, *arguments])
        assert answer is not None, "a picture is drawn later, so the handler answers an awaitable"
        await answer
        (encoded,) = mux.command_output
    finally:
        mux.command_output = None
    return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")


def _rgb(color: str) -> tuple[int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def _cell(image: Image.Image, column: int, row: int, width: int, height: int) -> set[tuple[int, int, int]]:
    "Every colour inside one cell."
    box = image.crop((column * width, row * height, (column + 1) * width, (row + 1) * height))
    return {color for _count, color in box.getcolors(width * height)}


async def test_the_picture_is_the_grid_at_the_cell_it_was_given(pymux):
    create_pane(pymux, "hello")
    assert (await picture(pymux, "--cell", "9x18")).size == (COLUMNS * 9, LINES * 18)


async def test_without_a_cell_it_takes_the_attached_terminals(pymux):
    "Nobody attached here, so the cell pyte assumes."
    create_pane(pymux, "hello")
    assert (await picture(pymux)).size == (COLUMNS * DEFAULT_CELL_WIDTH, LINES * DEFAULT_CELL_HEIGHT)


async def test_a_cell_is_painted_in_the_panes_own_colours(pymux):
    pane = create_pane(pymux, "\x1b[41m  \x1b[0m")
    paint = Paint.of(pane.screen.colors)
    image = await picture(pymux, "--cell", "9x18")
    assert _cell(image, 0, 0, 9, 18) == {_rgb(paint.themed[1])}
    assert _cell(image, 5, 0, 9, 18) == {_rgb(paint.background)}


async def test_a_glyph_leaves_ink_and_a_blank_does_not(pymux):
    pane = create_pane(pymux, "\x1b[?25lX ")
    paint = Paint.of(pane.screen.colors)
    image = await picture(pymux, "--cell", "9x18")
    assert len(_cell(image, 0, 0, 9, 18)) > 1
    assert _cell(image, 1, 0, 9, 18) == {_rgb(paint.background)}


async def test_a_nerd_font_icon_is_drawn_and_not_the_missing_glyph(pymux):
    """
    U+F07B, the folder a file manager draws: a Nerd Font has it, and
    DejaVu does not. A font draws its missing glyph as ink too, so the
    icon is judged against a code point no font has.
    """
    create_pane(pymux, "\x1b[?25l \U0010fffd")
    image = await picture(pymux, "--cell", "9x18")
    icon, missing = (image.crop((column * 9, 0, (column + 1) * 9, 18)).tobytes() for column in (0, 2))
    assert len(set(icon)) > 1 and icon != missing


async def test_the_cursor_is_an_outline_where_it_stands(pymux):
    pane = create_pane(pymux, "ab")
    paint = Paint.of(pane.screen.colors)
    image = await picture(pymux, "--cell", "9x18")
    assert _rgb(paint.foreground) in _cell(image, 2, 0, 9, 18)


def test_each_file_is_the_face_its_own_style_name_says():
    faces = screenshot.faces_of(list(reversed(FONTS)))
    assert (faces.regular, faces.bold, faces.italic) == tuple(FONTS)
    # No bold italic was given, so it takes the bold.
    assert faces.bolditalic == FONTS[1]


def test_a_set_with_no_regular_face_is_refused():
    with pytest.raises(ValueError, match="no regular face"):
        screenshot.faces_of(FONTS[1:])


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--png"], "needs -p"),
        (["-p", "--png", "-H"], "is a picture"),
        (["-p", "--font", "x.ttf"], "draw the picture of --png"),
    ],
)
async def test_what_cannot_be_drawn_is_refused(pymux, arguments, message):
    create_pane(pymux, "x")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda _message: None
    call_command_handler("capture-pane", pymux, arguments)
    assert len(errors) == 1 and message in errors[0], errors


async def test_a_cell_that_is_not_a_size_is_refused(pymux):
    create_pane(pymux, "x")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda _message: None
    answer = call_command_handler("capture-pane", pymux, ["-p", "--png", "--font", FONTS[0], "--cell", "nine"])
    assert answer is not None
    await answer
    assert len(errors) == 1 and "--cell" in errors[0], errors


@pytest.mark.skipif(shutil.which("fc-match") is None, reason="no fontconfig on PATH")
async def test_without_a_font_fontconfig_answers():
    assert await screenshot.system_fonts()
