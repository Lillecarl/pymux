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
from fontTools.ttLib import TTCollection, TTFont
from PIL import Image
from pyte.painted import Paint

from pymux import screenshot
from pymux.answer import Answer
from pymux.commands import call_command_handler
from pymux.graphics import DEFAULT_CELL_HEIGHT, DEFAULT_CELL_WIDTH
from pymux.main import Pymux
from pymux.window_frames import WINDOW_Z

COLUMNS = 20
LINES = 5

FONT_DIR = Path(os.environ.get("PYMUX_TEST_FONT_DIR", "/nonexistent"))
FONTS = [str(FONT_DIR / ("JetBrainsMonoNerdFontMono-%s.ttf" % face)) for face in ("Regular", "Bold", "Italic")]
FALLBACK = os.environ.get("PYMUX_TEST_FALLBACK", "/nonexistent")

pytestmark = pytest.mark.skipif(not FONT_DIR.is_dir(), reason="PYMUX_TEST_FONT_DIR names no fonts")


@pytest.fixture
async def pymux():
    "A server with one window, whose program waits, so the pane outlives a picture or two."
    mux = Pymux()
    async with mux.running():
        await mux.create_window("%s -c 'import time; time.sleep(60)'" % (sys.executable,))
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
    mux.answer = Answer()
    try:
        fonts = [word for path in FONTS for word in ("--font", path)]
        answer = call_command_handler("capture-pane", mux, ["-p", "--png", *fonts, *arguments])
        assert answer is not None, "a picture is drawn later, so the handler answers an awaitable"
        await answer
        (encoded,) = mux.answer.output
    finally:
        mux.answer = None
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


async def test_fit_shrinks_the_cell_until_the_longest_edge_fits(pymux):
    create_pane(pymux, "hello")
    image = await picture(pymux, "--cell", "20x40", "--fit", "200")
    assert max(image.size) <= 200
    assert image.size == (COLUMNS * 10, LINES * 20)


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


def _colors(image: Image.Image, box: tuple[int, int, int, int]) -> set[tuple[int, int, int]]:
    found = image.crop(box)
    return {color for _count, color in found.getcolors(found.width * found.height)}


async def test_a_block_element_fills_its_cell_to_the_edge(pymux):
    "Drawn as foot draws it, so block art shows no gap between cells. Lillecarl/pymux#777."
    pane = create_pane(pymux, "\x1b[?25l██▀▐▛▒")
    paint = Paint.of(pane.screen.colors)
    image = await picture(pymux, "--cell", "9x18")
    ink, paper = _rgb(paint.foreground), _rgb(paint.background)
    assert _colors(image, (0, 0, 18, 18)) == {ink}
    assert (_colors(image, (18, 0, 27, 9)), _colors(image, (18, 9, 27, 18))) == ({ink}, {paper})
    # foot rounds half a cell of nine up, to five.
    assert (_colors(image, (27, 0, 32, 18)), _colors(image, (32, 0, 36, 18))) == ({paper}, {ink})
    # Three quarters, each rounded out: the lower left reaches column five.
    assert (_colors(image, (36, 0, 45, 9)), _colors(image, (36, 9, 41, 18))) == ({ink}, {ink})
    assert _colors(image, (41, 9, 45, 18)) == {paper}
    halfway = tuple((a + b) // 2 for a, b in zip(ink, paper, strict=True))
    assert _colors(image, (45, 0, 54, 18)) == {halfway}


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


async def test_a_glyph_the_font_lacks_comes_from_the_fallback(pymux):
    "U+23BF, the corner Claude Code draws under a tool call: no Nerd Font has it, and Unifont does."
    create_pane(pymux, "\x1b[?25l⎿ \U0010fffd")
    alone = await picture(pymux, "--cell", "9x18")
    helped = await picture(pymux, "--cell", "9x18", "--fallback", FALLBACK)
    corner, missing = (helped.crop((column * 9, 0, (column + 1) * 9, 18)).tobytes() for column in (0, 2))
    assert corner != missing
    assert alone.crop((0, 0, 9, 18)).tobytes() == alone.crop((18, 0, 27, 18)).tobytes(), "the font has U+23BF after all"


def test_a_cell_of_several_characters_takes_a_font_that_has_them_all():
    "The face has the digit of a keycap and not its U+20E3. Lillecarl/pymux#802."
    keycap = "1\ufe0f\u20e3"
    assert 0x20E3 not in screenshot._characters(FONTS[0])
    assert 0x20E3 in screenshot._characters(FALLBACK)
    pens = screenshot._Pens(screenshot.faces_of(FONTS, [FALLBACK]), 9, 18)
    font, _ = pens.font_for(keycap, "regular")
    assert font.path == FALLBACK
    font, _ = pens.font_for("1", "regular")
    assert font.path == FONTS[0]


async def test_a_glyph_wider_than_its_cells_stays_in_them(pymux):
    """
    Unifont draws the conjunct ksha with its vowel sign about three cells
    wide, and the screen gives it two: the third stays blank. Lillecarl/pymux#803.
    """
    pane = create_pane(pymux, "\x1b[?25l\u0915\u094d\u0937\u093f ")
    assert pane.screen.page.data_buffer[0][2].char == " "
    paint = Paint.of(pane.screen.colors)
    image = await picture(pymux, "--cell", "9x18", "--fallback", FALLBACK)
    assert len(_cell(image, 0, 0, 9, 18) | _cell(image, 1, 0, 9, 18)) > 1
    assert _cell(image, 2, 0, 9, 18) == {_rgb(paint.background)}


def test_a_fallback_is_known_by_what_it_holds():
    assert 0x23BF in screenshot._characters(FALLBACK)
    assert 0x23BF not in screenshot._characters(FONTS[0])


async def test_a_fallback_may_be_a_collection(pymux, tmp_path):
    "A .ttc holds several fonts, and WenQuanYi ships as one; its first one draws."
    collection = TTCollection()
    collection.fonts.append(TTFont(FALLBACK))
    path = tmp_path / "fallback.ttc"
    collection.save(str(path))
    assert 0x23BF in screenshot._characters(str(path))
    create_pane(pymux, "\x1b[?25l⎿ \U0010fffd")
    helped = await picture(pymux, "--cell", "9x18", "--fallback", str(path))
    corner, missing = (helped.crop((column * 9, 0, (column + 1) * 9, 18)).tobytes() for column in (0, 2))
    assert corner != missing


RED = (255, 0, 0)

#: A two by two red image, as RGB, put over four columns and two rows.
KITTY_RED = "\x1b_Ga=T,f=24,s=2,v=2,c=4,r=2%s;" + base64.b64encode(bytes(RED) * 4).decode() + "\x1b\\"


async def test_a_kitty_image_covers_the_cells_it_was_put_on(pymux):
    create_pane(pymux, "\x1b[?25l" + KITTY_RED % "")
    image = await picture(pymux, "--cell", "9x18")
    for column, row in ((0, 0), (3, 1)):
        assert _cell(image, column, row, 9, 18) == {RED}, (column, row)
    assert RED not in _cell(image, 4, 0, 9, 18)


async def test_a_sixel_image_is_drawn_where_it_was_written(pymux):
    "Red in colour register 0, one band of six rows, twelve columns wide."
    create_pane(pymux, "\x1b[?25l\x1bPq#0;2;100;0;0#0" + "~" * 12 + "\x1b\\")
    image = await picture(pymux, "--cell", "9x18")
    assert RED in _cell(image, 0, 0, 9, 18)


async def test_an_image_under_the_text_lets_the_text_show(pymux):
    "z below zero is under the text, as kitty draws it; the glyph's ink lands on the red."
    create_pane(pymux, "\x1b[?25l" + KITTY_RED % ",z=-1,C=1" + "X")
    image = await picture(pymux, "--cell", "9x18")
    assert RED in _cell(image, 0, 0, 9, 18)
    assert len(_cell(image, 0, 0, 9, 18)) > 1, "the X is not drawn over the image"
    assert _cell(image, 1, 0, 9, 18) == {RED}


async def test_an_image_under_the_backgrounds_shows_only_where_the_background_is_the_default(pymux):
    "As kitty draws a z under -2**30: a cell with a colour of its own covers it. Lillecarl/pymux#814."
    create_pane(pymux, "\x1b[?25l" + KITTY_RED % (",z=%d,C=1" % WINDOW_Z) + "\x1b[44m \x1b[m")
    image = await picture(pymux, "--cell", "9x18")
    assert RED not in _cell(image, 0, 0, 9, 18)
    assert _cell(image, 1, 0, 9, 18) == {RED}


def test_a_window_frame_is_drawn_over_its_cells_and_under_their_colours():
    "Lillecarl/pymux#814."
    plain, blue = screenshot.Look("#ffffff", "#000000"), screenshot.Look("#ffffff", "#0000ff")
    frame = screenshot.Pixels(2, 1, bytes((0, 255, 0, 255)) * 2)
    grid = screenshot.Grid(
        [[(" ", blue), (" ", plain), (" ", plain)]],
        3,
        "#ffffff",
        "#000000",
        None,
        [screenshot.window_shown(0, 0, 2, 1, frame)],
    )
    image = Image.open(io.BytesIO(screenshot.draw(grid, screenshot.faces_of(FONTS), 9, 18))).convert("RGB")
    assert _cell(image, 0, 0, 9, 18) == {(0, 0, 255)}
    assert _cell(image, 1, 0, 9, 18) == {(0, 255, 0)}
    assert _cell(image, 2, 0, 9, 18) == {(0, 0, 0)}


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


@pytest.mark.skipif(shutil.which("fc-match") is None, reason="no fontconfig on PATH")
async def test_the_system_font_falls_back_where_fontconfig_says():
    "The check's fontconfig knows Unifont, which has U+23BF. Lillecarl/pymux#815."
    look = screenshot.Look("#ffffff", "#000000")
    grid = screenshot.Grid([[("⎿", look), ("a", look)]], 2, "#ffffff", "#000000", None)
    (found,) = await screenshot.system_fallbacks(grid, FONTS[0])
    assert 0x23BF in screenshot._characters(found)
