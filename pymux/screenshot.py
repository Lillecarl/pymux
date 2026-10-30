"""
A pane as a PNG, drawn from its cells: `capture-pane --png`; and the
whole of what a client is shown, from its renderer's last frame:
`capture-client --png`.

The picture is for a reader with eyes and no terminal, an agent above
all, so it is drawn straight from what the pane holds and no browser
lays anything out. One glyph per cell, on the grid, in the colours
`pyte.painted` resolves; bold and italic take the font's own faces,
underline and strike are lines, dim mixes towards the background, and
the cursor is an outline where it stands. Box drawing and block
elements come from the cell, not the font: `box_drawing`.

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
map. Without one, the system's font takes fontconfig's answer for each
character it lacks, and a caller's font draws the font's box. The
person's terminal draws its own fallbacks on its own machine, and none
of its fonts reach the server. Lillecarl/pymux#815.

**Images are placed as a client would place them**: `graphics.
pane_placements` answers each one's cells and crop, kitty's and
sixel's alike, and a negative z-index goes under the text.
Lillecarl/pymux#763.
"""

from __future__ import annotations

import io
import math
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import anyio
from fontTools.ttLib import TTFont, TTLibError
from PIL import Image, ImageDraw, ImageFont
from prompt_toolkit.output.vt100_colors import FG_ANSI_COLORS
from pyte.cells import PLAIN_APPEARANCE
from pyte.html import visible_char
from pyte.painted import Paint

from .graphics.graphics import PaneView
from .graphics.placements import pane_placements, source_rgba
from .graphics.window_frames import UNDER_BACKGROUNDS, WINDOW_Z
from .layout import box_drawing

if TYPE_CHECKING:
    from prompt_toolkit.styles import Attrs
    from pyte.cells import Appearance
    from pyte.osc import ColorOverrides
    from pyte.screen import Screen

    from .connection.client_state import ClientState
    from .layout.plane import Line, Rect

__all__ = [
    "Faces",
    "Grid",
    "Look",
    "Pixels",
    "client_grid_of",
    "draw",
    "faces_of",
    "grid_of",
    "system_fallbacks",
    "system_fonts",
    "window_grid_of",
]

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
    """
    Every code point a font file has a glyph for. Of a collection, its
    first font: the one Pillow draws with.
    """
    with TTFont(path, lazy=True, fontNumber=0) as font:
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


async def system_fallbacks(grid: Grid, regular: str) -> list[str]:
    """
    fontconfig's font for each character the grid shows and `regular`
    lacks, as a terminal asks it, or nothing without `fc-match`.

    Not a colour font: Pillow draws a bitmap emoji font at one size only.
    fontconfig answers its best font even when that font lacks the
    character too, so each answer's own map has the last word.
    """
    fc_match = shutil.which("fc-match")
    if fc_match is None:
        return []
    known = await anyio.to_thread.run_sync(_characters, regular)
    shown = {ord(char) for row in grid.rows for glyph, _look in row for char in glyph if char not in _SELECTORS}
    paths: list[str] = []
    covered: set[int] = set()
    for code in sorted(shown - known):
        if code < 0x20 or code in covered:
            continue
        found = await anyio.run_process(
            [fc_match, "--format", "%{file}", "monospace:color=false:charset=%x" % code], check=False
        )
        path = found.stdout.decode(errors="replace").strip()
        if found.returncode != 0 or not path or path in paths:
            continue
        try:
            has = await anyio.to_thread.run_sync(_characters, path)
        except OSError, TTLibError:
            # A bitmap font, which fontconfig knows and fontTools cannot read.
            continue
        if code in has:
            paths.append(path)
            covered |= has
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
    #: A `GraphicsImage`, or the `Pixels` of a Wayland window's frame.
    image: Any
    source: tuple[int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class Pixels:
    "A Wayland window's last frame, as `WaylandServer.frame` copies it out."

    width: int
    height: int
    rgba: bytes


def window_shown(column: int, row: int, columns: int, rows: int, frame: Pixels) -> Shown:
    """
    A window pane's frame over the pane's cells, where a client places
    it: under every cell with a background of its own. Lillecarl/pymux#814.
    """
    return Shown(column, row, columns, rows, WINDOW_Z, frame, None)


class Look(NamedTuple):
    "How one cell is drawn, resolved: its two colours as `#rrggbb`, and what else it shows."

    foreground: str
    background: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    dim: bool = False
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class Grid:
    """
    What a picture draws, copied out of a screen: each row as (character,
    look) per column. The second column of a wide character holds an
    empty character. `foreground` and `background` are the screen's own.
    """

    rows: list[list[tuple[str, Look]]]
    columns: int
    foreground: str
    background: str
    #: The cursor's column and row in `rows`, when it shows inside them.
    cursor: tuple[int, int] | None
    images: list[Shown] = field(default_factory=list)


def _look_of(paint: Paint, appearance: Appearance, reverse_video: bool) -> Look:
    foreground, background = paint.shown(appearance, reverse_video)
    rendition = appearance.rendition
    return Look(
        foreground,
        background,
        bool(rendition.bold),
        bool(rendition.italic),
        bool(rendition.underline),
        bool(rendition.strike),
        bool(rendition.dim),
        bool(rendition.hidden),
    )


def grid_of(screen: Screen, first: int, last: int, colors: ColorOverrides | None, cell: tuple[int, int]) -> Grid:
    """
    The rows from `first` to `last`, numbered as the buffer numbers them,
    with the images on them. `cell` is the picture's cell in pixels,
    which a placeholder image's crop is counted in.
    """
    page = screen.page
    assert page is not None
    columns = screen.columns
    paint = Paint.of(colors)
    reverse_video = screen.has_reverse_video
    looks: dict[Appearance, Look] = {}
    rows: list[list[tuple[str, Look]]] = []
    for number in range(first, last + 1):
        row = page.data_buffer.get(number)
        if row is None:
            rows.append([])
            continue
        cells = []
        for column in range(columns):
            held = row[column]
            look = looks.get(held.appearance)
            if look is None:
                look = looks[held.appearance] = _look_of(paint, held.appearance, reverse_video)
            cells.append((held.char, look))
        rows.append(cells)
    position = screen.pt_cursor_position
    cursor = None
    if page.show_cursor and first <= position.y <= last and position.x < columns:
        cursor = (position.x, position.y - first)
    view = PaneView(0, 0, 0, columns, last - first + 1, first, 0, screen.graphics, screen)
    images = [
        Shown(x, y, width, height, placement.z, image, source)
        for placement, image, width, height, x, y, source in pane_placements(view, *cell)
    ]
    foreground, background = paint.shown(PLAIN_APPEARANCE, reverse_video)
    return Grid(rows, columns, foreground, background, cursor, images)


def window_grid_of(
    panes: Sequence[tuple[Rect, Screen | Pixels | None]],
    lines: Sequence[Line],
    focused: Screen | None,
    colors: ColorOverrides | None,
    cell: tuple[int, int],
) -> Grid:
    """
    A window as its plan lays it out, with no client: each pane's visible
    rows at its rectangle, and the layout's border lines in the gaps.
    `capture-window`. Lillecarl/pymux#818.

    `panes` pairs each shown pane's rectangle with its screen, or with a
    Wayland window pane's last frame, `None` while it has drawn none.
    `colors` paint what no pane covers. The cursor is `focused`'s.
    """
    boxes = [rect for rect, _screen in panes] + [line.rect for line in lines]
    left = min((box.x for box in boxes), default=0)
    top = min((box.y for box in boxes), default=0)
    columns = max((box.right for box in boxes), default=0) - left
    height = max((box.bottom for box in boxes), default=0) - top
    paint = Paint.of(colors)
    plain = Look(paint.foreground, paint.background)
    rows = [[(" ", plain)] * columns for _row in range(height)]
    for line in lines:
        for y in range(line.rect.y, line.rect.bottom):
            for x in range(line.rect.x, line.rect.right):
                rows[y - top][x - left] = (line.char, plain)

    cursor = None
    images: list[Shown] = []
    for rect, screen in panes:
        if isinstance(screen, Pixels):
            images.append(window_shown(rect.x - left, rect.y - top, rect.width, rect.height, screen))
            continue
        if screen is None:
            continue
        first = screen.line_offset
        pane = grid_of(screen, first, first + screen.lines - 1, screen.colors, cell)
        at_x, at_y = rect.x - left, rect.y - top
        for y, row in enumerate(pane.rows[: rect.height]):
            rows[at_y + y][at_x : at_x + min(len(row), rect.width)] = row[: rect.width]
        images.extend(replace(shown, column=shown.column + at_x, row=shown.row + at_y) for shown in pane.images)
        if screen is focused and pane.cursor is not None:
            x, y = pane.cursor
            if x < rect.width and y < rect.height:
                cursor = (at_x + x, at_y + y)
    return Grid(rows, columns, paint.foreground, paint.background, cursor, images)


def _ansi_index(name: str) -> int | None:
    "The palette index of a prompt_toolkit ANSI colour name: SGR 30-37 are 0-7, 90-97 are 8-15."
    code = FG_ANSI_COLORS.get(name)
    if code is None or code == FG_ANSI_COLORS["ansidefault"]:
        return None
    return code - 30 if code < 90 else code - 82


def _client_color(paint: Paint, value: str | None, default: str) -> str:
    "A colour of prompt_toolkit's `Attrs`: empty or default, an ANSI name, or hex without the `#`."
    if not value or value in ("default", "ansidefault"):
        return default
    index = _ansi_index(value)
    if index is not None:
        return paint.themed[index]
    if len(value) == 6:
        return "#" + value.lower()
    return default


def _look_of_style(paint: Paint, attrs: Attrs) -> Look:
    foreground = _client_color(paint, attrs.color, paint.foreground)
    background = _client_color(paint, attrs.bgcolor, paint.background)
    if attrs.reverse:
        foreground, background = background, foreground
    return Look(
        foreground,
        background,
        bool(attrs.bold),
        bool(attrs.italic),
        bool(attrs.underline),
        bool(attrs.strike),
        bool(attrs.dim),
        bool(attrs.hidden),
    )


def client_grid_of(client_state: ClientState, colors: ColorOverrides, cell: tuple[int, int]) -> Grid:
    """
    What one client is shown, as its renderer last drew it: the status
    line, the borders, every pane and whatever is drawn over them, in
    the client's own style. `colors` are the client's terminal's or its
    theme's. `cell` is the picture's cell, which a placeholder image's
    crop is counted in.

    The images are the client's own: its `graphics_views`, and the last
    frame of each Wayland window pane it shows. None under a popup.

    Raises `LookupError` before the client's first frame.
    """
    app = client_state.app
    renderer = app.renderer
    screen = renderer.last_rendered_screen
    if screen is None:
        raise LookupError("the client has drawn no frame yet")
    size = app.output.get_size()
    paint = Paint.of(colors)
    looks: dict[str, Look] = {}
    blank = (" ", Look(paint.foreground, paint.background))
    rows = []
    for y in range(size.rows):
        row = screen.data_buffer.get(y)
        cells = [blank] * size.columns
        if row is not None:
            x = 0
            while x < size.columns:
                char = row.get(x)
                if char is None:
                    x += 1
                    continue
                look = looks.get(char.style)
                if look is None:
                    look = looks[char.style] = _look_of_style(paint, renderer.style.get_attrs_for_style_str(char.style))
                cells[x] = (char.char, look)
                # The renderer steps over a wide character's second
                # column, and so does the picture.
                for following in range(x + 1, min(x + (char.width or 1), size.columns)):
                    cells[following] = ("", look)
                x += char.width or 1
        rows.append(cells)
    cursor = None
    window = app.layout.current_window
    if screen.show_cursor and window in screen.cursor_positions:
        point = screen.get_cursor_position(window)
        if 0 <= point.x < size.columns and 0 <= point.y < size.rows:
            cursor = (point.x, point.y)
    images = [
        Shown(x, y, width, height, placement.z, image, source)
        for view in client_state.graphics_views()
        for placement, image, width, height, x, y, source in pane_placements(view, *cell)
    ]
    for _pane, widget, place in client_state.window_panes():
        frame = client_state.pymux.wayland.frame(widget.toplevel)
        if frame is not None:
            images.append(window_shown(place.xpos, place.ypos, place.width, place.height, Pixels(*frame)))
    return Grid(rows, size.columns, paint.foreground, paint.background, cursor, images)


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


#: What steers a glyph and draws nothing of its own: the text and emoji
#: variation selectors and the zero width joiner. A font need not map them.
_SELECTORS = frozenset("\ufe0e\ufe0f\u200d")


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

        A cell of several characters goes to a font that has all of
        them, as a keycap does: the face has its digit and not U+20E3,
        and drew a missing-glyph box beside it. Lillecarl/pymux#802.
        """
        codes = {ord(char) for char in glyph if char not in _SELECTORS} or {ord(glyph[0])}
        if not codes <= self.known:
            for known, fitted in self.fallbacks:
                if codes <= known:
                    return fitted
            code = ord(glyph[0])
            if code not in self.known:
                for known, fitted in self.fallbacks:
                    if code in known:
                        return fitted
        return self.faces[face]


def _paste(picture: Image.Image, images: list[Shown], width: int, height: int) -> None:
    "Each image scaled to the cells it covers, over what is drawn, through its own alpha."
    for shown in images:
        image = shown.image
        found = (
            (image.rgba, image.width, image.height) if isinstance(image, Pixels) else source_rgba(image, shown.source)
        )
        if found is None:
            continue  # A format nothing here reads.
        pixels, image_width, image_height = found
        drawn = Image.frombytes("RGBA", (image_width, image_height), pixels)
        drawn = drawn.resize((shown.columns * width, shown.rows * height), Image.Resampling.LANCZOS)
        picture.paste(drawn, (shown.column * width, shown.row * height), drawn)


def _squeeze(
    image: Image.Image,
    glyph: str,
    font: ImageFont.FreeTypeFont,
    top: int,
    at: tuple[int, int],
    sizes: tuple[int, int, int],
    foreground: str,
) -> None:
    """
    A glyph wider than its cells, scaled down to their width and centred
    in their height, as kitty fits one. Drawn at its own width it covered
    the next cell: an emoji in a cell of one column, a conjunct from a
    fallback font. Lillecarl/pymux#803.
    """
    wide, room, height = sizes
    mask = Image.new("L", (wide, height))
    ImageDraw.Draw(mask).text((0, top), glyph, font=font, fill=255)
    tall = max(1, height * room // wide)
    mask = mask.resize((room, tall), Image.Resampling.LANCZOS)
    left, upper = at
    image.paste(foreground, (left, upper + (height - tall) // 2), mask)


def draw(grid: Grid, faces: Faces, width: int, height: int) -> bytes:
    "The grid as a PNG, `width` by `height` pixels to a cell."
    image = Image.new("RGB", (grid.columns * width, max(1, len(grid.rows)) * height), grid.background)
    pen = ImageDraw.Draw(image)
    pens = _Pens(faces, width, height)
    # As kitty layers them: the images under the backgrounds, the
    # backgrounds, the images under the text, the text, and the images
    # over it.
    _paste(image, [shown for shown in grid.images if shown.z < UNDER_BACKGROUNDS], width, height)
    for y, row in enumerate(grid.rows):
        for x, (char, look) in enumerate(row):
            if char and look.background != grid.background:
                span = 2 if x + 1 < len(row) and not row[x + 1][0] else 1
                left, upper = x * width, y * height
                pen.rectangle((left, upper, left + span * width - 1, upper + height - 1), fill=look.background)
    _paste(image, [shown for shown in grid.images if UNDER_BACKGROUNDS <= shown.z < 0], width, height)
    for y, row in enumerate(grid.rows):
        for x, (char, look) in enumerate(row):
            if not char:
                continue
            span = 2 if x + 1 < len(row) and not row[x + 1][0] else 1
            left, upper = x * width, y * height
            right, lower = left + span * width - 1, upper + height - 1
            foreground = _mixed(look.foreground, look.background) if look.dim else look.foreground
            glyph = visible_char(char)
            drawn = box_drawing.mask(ord(glyph), span * width, height) if len(glyph) == 1 else None
            if drawn is not None and not look.hidden:
                image.paste(foreground, (left, upper), drawn)
            elif glyph.strip() and not look.hidden:
                face = ("bold" if look.bold else "") + ("italic" if look.italic else "")
                font, top = pens.font_for(glyph, face or "regular")
                room = span * width
                wide = 0 if glyph.isascii() else math.ceil(font.getlength(glyph))
                if wide > room:
                    _squeeze(image, glyph, font, top, (left, upper), (wide, room, height), foreground)
                else:
                    pen.text((left, upper + top), glyph, font=font, fill=foreground)
            if look.underline:
                pen.line((left, lower - 1, right, lower - 1), fill=foreground)
            if look.strike:
                pen.line((left, upper + height // 2, right, upper + height // 2), fill=foreground)
    _paste(image, [shown for shown in grid.images if shown.z >= 0], width, height)
    if grid.cursor is not None:
        x, y = grid.cursor
        pen.rectangle((x * width, y * height, (x + 1) * width - 1, (y + 1) * height - 1), outline=grid.foreground)
    written = io.BytesIO()
    image.save(written, "PNG", optimize=True)
    return written.getvalue()
