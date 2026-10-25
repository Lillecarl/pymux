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

**A glyph the font lacks comes from a fallback**, as a terminal does
it: `--fallback` files are tried in order, each by its own character
map. Without one a missing glyph is the font's box. The person's
terminal draws its own fallbacks on its own machine, and none of its
fonts reach the server.

**Images are placed as a client would place them**: `graphics.
pane_placements` answers each one's cells and crop, kitty's and
sixel's alike, and a negative z-index goes under the text.
Lillecarl/pymux#763.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import anyio
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from pyte.cells import PLAIN_APPEARANCE
from pyte.html import visible_char
from pyte.painted import Paint

from .graphics import PaneView, pane_placements, source_rgba

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
    """
    The font files a picture draws with; a face nobody gave takes the
    regular one. `fallbacks` are tried in order for a character the
    regular face lacks, as a terminal falls back per glyph.
    """

    regular: str
    bold: str
    italic: str
    bolditalic: str
    fallbacks: tuple[str, ...] = ()


@cache
def _characters(path: str) -> frozenset[int]:
    "Every code point a font file has a glyph for."
    with TTFont(path, lazy=True) as font:
        return frozenset(font.getBestCmap() or ())


def _face_of(path: str) -> str:
    try:
        style = ImageFont.truetype(path, 10).getname()[1] or "regular"
    except OSError as error:
        raise ValueError("can't read the font %s: %s" % (path, error)) from error
    face = "".join(style.lower().split()).replace("oblique", "italic")
    return "regular" if face in _REGULAR_NAMES else face


def faces_of(paths: Sequence[str], fallbacks: Sequence[str] = ()) -> Faces:
    """
    The faces of the font files given, each known by its own style name,
    and the fallbacks after them.

    Raises `ValueError` for a file nothing reads, and for a set with no
    regular face.
    """
    for path in fallbacks:
        _face_of(path)
    found: dict[str, str] = {}
    for path in paths:
        found.setdefault(_face_of(path), path)
    regular = found.get("regular")
    if regular is None:
        raise ValueError("no regular face among %s" % (", ".join(paths),))
    bold = found.get("bold", regular)
    italic = found.get("italic", regular)
    return Faces(regular, bold, italic, found.get("bolditalic", bold), tuple(fallbacks))


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
class Shown:
    """
    One image the rows show, as a client would place it: the cells it
    covers, the crop of the image in pixels (None for all of it), and
    its z-index, which puts it below the text when negative.
    """

    column: int
    row: int
    columns: int
    rows: int
    z: int
    image: Any
    source: tuple[int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class Grid:
    "What a picture draws, copied out of a screen: each row as (character, appearance) per column."

    rows: list[list[tuple[str, Appearance]]]
    columns: int
    paint: Paint
    reverse_video: bool
    #: The cursor's column and row in `rows`, when it shows inside them.
    cursor: tuple[int, int] | None
    images: list[Shown] = field(default_factory=list)


def grid_of(screen: Screen, first: int, last: int, colors: ColorOverrides | None, cell: tuple[int, int]) -> Grid:
    """
    The rows from `first` to `last`, numbered as the buffer numbers them,
    with the images on them. `cell` is the picture's cell in pixels,
    which a placeholder image's crop is counted in.
    """
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
    view = PaneView(0, 0, 0, columns, last - first + 1, first, 0, screen.graphics, screen)
    images = [
        Shown(x, y, width, height, placement.z, image, source)
        for placement, image, width, height, x, y, source in pane_placements(view, *cell)
    ]
    return Grid(rows, columns, Paint.of(colors), screen.has_reverse_video, cursor, images)


def _mixed(color: str, toward: str) -> str:
    "Halfway from one colour to the other: how a terminal draws dim."
    ours = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    theirs = [int(toward[index : index + 2], 16) for index in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple((a + b) // 2 for a, b in zip(ours, theirs, strict=True))


def _size_of(path: str, width: int, height: int) -> int:
    "The largest size at which the font's glyph fits one cell."
    probe = ImageFont.truetype(path, 100)
    ascent, descent = probe.getmetrics()
    advance = probe.getlength("M")
    return max(1, int(min(height * 100 / (ascent + descent), width * 100 / advance)))


def _fitted(path: str, size: int, height: int) -> tuple[ImageFont.FreeTypeFont, int]:
    "A font at `size`, and how far down the cell its glyph starts."
    font = ImageFont.truetype(path, size)
    ascent, descent = font.getmetrics()
    return font, (height - ascent - descent) // 2


class _Pens:
    "The fonts of one picture, and which one draws each glyph."

    def __init__(self, faces: Faces, width: int, height: int) -> None:
        size = _size_of(faces.regular, width, height)
        self.faces = {face: _fitted(getattr(faces, face), size, height) for face in _FACES}
        self.known = _characters(faces.regular)
        self.fallbacks = [
            (_characters(path), _fitted(path, _size_of(path, width, height), height)) for path in faces.fallbacks
        ]

    def font_for(self, glyph: str, face: str) -> tuple[ImageFont.FreeTypeFont, int]:
        """
        The face asked for when it has the glyph, or the first fallback
        that does. The bold and italic faces are taken to hold what the
        regular one holds. A glyph nobody has gets the face's own box.
        """
        code = ord(glyph[0])
        if code not in self.known:
            for known, fitted in self.fallbacks:
                if code in known:
                    return fitted
        return self.faces[face]


def _paste(picture: Image.Image, images: list[Shown], width: int, height: int) -> None:
    "Each image scaled to the cells it covers, over what is drawn, through its own alpha."
    for shown in images:
        found = source_rgba(shown.image, shown.source)
        if found is None:
            continue  # A format nothing here reads.
        pixels, image_width, image_height = found
        drawn = Image.frombytes("RGBA", (image_width, image_height), pixels)
        drawn = drawn.resize((shown.columns * width, shown.rows * height), Image.Resampling.LANCZOS)
        picture.paste(drawn, (shown.column * width, shown.row * height), drawn)


def draw(grid: Grid, faces: Faces, width: int, height: int) -> bytes:
    "The grid as a PNG, `width` by `height` pixels to a cell."
    paint = grid.paint
    screen_foreground, screen_background = paint.shown(PLAIN_APPEARANCE, grid.reverse_video)
    image = Image.new("RGB", (grid.columns * width, max(1, len(grid.rows)) * height), screen_background)
    pen = ImageDraw.Draw(image)
    pens = _Pens(faces, width, height)
    # As kitty layers them: backgrounds, the images under the text, the
    # text, and the images over it.
    for y, row in enumerate(grid.rows):
        for x, (char, appearance) in enumerate(row):
            _, background = paint.shown(appearance, grid.reverse_video)
            if char and background != screen_background:
                span = 2 if x + 1 < len(row) and not row[x + 1][0] else 1
                left, upper = x * width, y * height
                pen.rectangle((left, upper, left + span * width - 1, upper + height - 1), fill=background)
    _paste(image, [shown for shown in grid.images if shown.z < 0], width, height)
    for y, row in enumerate(grid.rows):
        for x, (char, appearance) in enumerate(row):
            if not char:
                continue
            span = 2 if x + 1 < len(row) and not row[x + 1][0] else 1
            left, upper = x * width, y * height
            right, lower = left + span * width - 1, upper + height - 1
            foreground, background = paint.shown(appearance, grid.reverse_video)
            rendition = appearance.rendition
            if rendition.dim:
                foreground = _mixed(foreground, background)
            glyph = visible_char(char)
            if glyph.strip() and not rendition.hidden:
                face = ("bold" if rendition.bold else "") + ("italic" if rendition.italic else "")
                font, top = pens.font_for(glyph, face or "regular")
                pen.text((left, upper + top), glyph, font=font, fill=foreground)
            if rendition.underline:
                pen.line((left, lower - 1, right, lower - 1), fill=foreground)
            if rendition.strike:
                pen.line((left, upper + height // 2, right, upper + height // 2), fill=foreground)
    _paste(image, [shown for shown in grid.images if shown.z >= 0], width, height)
    if grid.cursor is not None:
        x, y = grid.cursor
        pen.rectangle((x * width, y * height, (x + 1) * width - 1, (y + 1) * height - 1), outline=screen_foreground)
    written = io.BytesIO()
    image.save(written, "PNG", optimize=True)
    return written.getvalue()
