"""
A pane as a PNG, drawn from its cells: `capture-pane --png`.

The picture is for a reader with eyes and no terminal, an agent above
all, so it is drawn straight from what the pane holds and no browser
lays anything out. One glyph per cell, on the grid, in the colours
`pyte.painted` resolves; bold and italic take the font's own faces,
underline and strike are lines, dim mixes towards the background, and
the cursor is an outline where it stands.

**The cells are copied on the event loop and drawn in a thread.** A
program writes into the screen between two awaits, so the drawing reads
a copy, and the drawing itself -- 8000 glyphs for a 160x50 pane -- stays
off the loop every client shares.

**The font is the caller's, or the system's.** `--font` names files,
and each one's own style name says which face it is. Without one,
fontconfig's `monospace` answers, when `fc-match` is on PATH. pymux
ships no font: pymux-mcp bundles one and passes it.

Not drawn yet: images in the pane (kitty, sixel). Lillecarl/pymux#763.
"""

from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio
from PIL import Image, ImageDraw, ImageFont
from pyte.cells import PLAIN_APPEARANCE
from pyte.html import visible_char
from pyte.painted import Paint

if TYPE_CHECKING:
    from pyte.cells import Appearance
    from pyte.osc import ColorOverrides
    from pyte.screen import Screen

__all__ = ["Faces", "Grid", "draw", "faces_of", "grid_of", "system_fonts"]

#: The faces a picture draws with, by the words of a font's own style
#: name, with the spaces gone and in lower case.
_FACES: Final = ("regular", "bold", "italic", "bolditalic")

#: How a style name says regular when it says something else.
_REGULAR_NAMES: Final = {"book", "normal", "roman", "medium"}


@dataclass(frozen=True, slots=True)
class Faces:
    "The font files a picture draws with; a face nobody gave takes the regular one."

    regular: str
    bold: str
    italic: str
    bolditalic: str


def _face_of(path: str) -> str:
    try:
        style = ImageFont.truetype(path, 10).getname()[1] or "regular"
    except OSError as error:
        raise ValueError("can't read the font %s: %s" % (path, error)) from error
    face = "".join(style.lower().split()).replace("oblique", "italic")
    return "regular" if face in _REGULAR_NAMES else face


def faces_of(paths: list[str]) -> Faces:
    """
    The faces of the font files given, each known by its own style name.

    Raises `ValueError` for a file nothing reads, and for a set with no
    regular face.
    """
    found: dict[str, str] = {}
    for path in paths:
        found.setdefault(_face_of(path), path)
    regular = found.get("regular")
    if regular is None:
        raise ValueError("no regular face among %s" % (", ".join(paths),))
    bold = found.get("bold", regular)
    italic = found.get("italic", regular)
    return Faces(regular, bold, italic, found.get("bolditalic", bold))


async def system_fonts() -> list[str]:
    "fontconfig's `monospace`, a file per face, or nothing without `fc-match`."
    fc_match = shutil.which("fc-match")
    if fc_match is None:
        return []
    paths = []
    for style in ("Regular", "Bold", "Italic", "Bold Italic"):
        found = await anyio.run_process([fc_match, "--format", "%{file}", "monospace:style=" + style], check=False)
        path = found.stdout.decode(errors="replace").strip()
        if found.returncode == 0 and path and path not in paths:
            paths.append(path)
    return paths


@dataclass(frozen=True, slots=True)
class Grid:
    "What a picture draws, copied out of a screen: each row as (character, appearance) per column."

    rows: list[list[tuple[str, Appearance]]]
    columns: int
    paint: Paint
    reverse_video: bool
    #: The cursor's column and row in `rows`, when it shows inside them.
    cursor: tuple[int, int] | None


def grid_of(screen: Screen, first: int, last: int, colors: ColorOverrides | None) -> Grid:
    "The rows from `first` to `last`, numbered as the buffer numbers them."
    page = screen.page
    assert page is not None
    columns = screen.columns
    rows = []
    for number in range(first, last + 1):
        row = page.data_buffer.get(number)
        if row is None:
            rows.append([])
        else:
            rows.append([(row[column].char, row[column].appearance) for column in range(columns)])
    position = screen.pt_cursor_position
    cursor = None
    if page.show_cursor and first <= position.y <= last and position.x < columns:
        cursor = (position.x, position.y - first)
    return Grid(rows, columns, Paint.of(colors), screen.has_reverse_video, cursor)


def _mixed(color: str, toward: str) -> str:
    "Halfway from one colour to the other: how a terminal draws dim."
    ours = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    theirs = [int(toward[index : index + 2], 16) for index in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple((a + b) // 2 for a, b in zip(ours, theirs, strict=True))


def _sized(faces: Faces, width: int, height: int) -> tuple[dict[str, ImageFont.FreeTypeFont], int]:
    """
    Each face at the largest size whose glyph fits one cell, and how far
    down the cell a glyph starts.
    """
    probe = ImageFont.truetype(faces.regular, 100)
    ascent, descent = probe.getmetrics()
    advance = probe.getlength("M")
    size = max(1, int(min(height * 100 / (ascent + descent), width * 100 / advance)))
    fonts = {face: ImageFont.truetype(getattr(faces, face), size) for face in _FACES}
    ascent, descent = fonts["regular"].getmetrics()
    return fonts, (height - ascent - descent) // 2


def draw(grid: Grid, faces: Faces, width: int, height: int) -> bytes:
    "The grid as a PNG, `width` by `height` pixels to a cell."
    paint = grid.paint
    screen_foreground, screen_background = paint.shown(PLAIN_APPEARANCE, grid.reverse_video)
    image = Image.new("RGB", (grid.columns * width, max(1, len(grid.rows)) * height), screen_background)
    pen = ImageDraw.Draw(image)
    fonts, top = _sized(faces, width, height)
    for y, row in enumerate(grid.rows):
        for x, (char, appearance) in enumerate(row):
            if not char:
                continue
            span = 2 if x + 1 < len(row) and not row[x + 1][0] else 1
            left, upper = x * width, y * height
            right, lower = left + span * width - 1, upper + height - 1
            foreground, background = paint.shown(appearance, grid.reverse_video)
            if background != screen_background:
                pen.rectangle((left, upper, right, lower), fill=background)
            rendition = appearance.rendition
            if rendition.dim:
                foreground = _mixed(foreground, background)
            glyph = visible_char(char)
            if glyph.strip() and not rendition.hidden:
                face = ("bold" if rendition.bold else "") + ("italic" if rendition.italic else "")
                pen.text((left, upper + top), glyph, font=fonts[face or "regular"], fill=foreground)
            if rendition.underline:
                pen.line((left, lower - 1, right, lower - 1), fill=foreground)
            if rendition.strike:
                pen.line((left, upper + height // 2, right, upper + height // 2), fill=foreground)
    if grid.cursor is not None:
        x, y = grid.cursor
        pen.rectangle((x * width, y * height, (x + 1) * width - 1, (y + 1) * height - 1), outline=screen_foreground)
    written = io.BytesIO()
    image.save(written, "PNG", optimize=True)
    return written.getvalue()
