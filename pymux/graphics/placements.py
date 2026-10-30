"""
Where each image of a pane lands on a client's screen, and which of its
pixels show there: the plain placements, the unicode placeholders, and
the cuts around what covers a pane. Pure arithmetic over a `PaneView`;
nothing here writes to a terminal.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from pyte.images import GraphicsImage, GraphicsPlacement

from ..layout.plane import Rect

# Through the modules, so the kernels `pymux.graphics.kernels.install()` puts there
# are the ones that run.
from . import sixel

if TYPE_CHECKING:
    from pymux.graphics.graphics import PaneView

__all__ = [
    "Placed",
    "Scroll",
    "Source",
    "moved_by",
    "pane_placements",
    "placeholder_placements",
    "source_rgba",
    "visible_placements",
]


#: The pixels of an image that a placement shows: x, y, width, height.
type Source = tuple[int, int, int, int]


#: One image a pane shows, as `pane_placements` yields it: the
#: placement, the image, its size in cells, its top left cell on the
#: screen, and the pixels of the image it shows, or None for all of it.
type Placed = tuple[GraphicsPlacement, GraphicsImage, int, int, int, int, Source | None]


#: A scroll the renderer wrote: first row, last row, distance.
type Scroll = tuple[int, int, int]


def moved_by(rows: range, scrolls: Sequence[Scroll]) -> bool:
    """
    Whether a scroll moved what lies on these rows: kitty moves an image
    that lies wholly inside a scrolled region, and only such an image
    (`scroll_filter_margins_func`, `kitty/graphics.c`). Lillecarl/pymux#821.
    """
    return bool(rows) and any(top <= rows[0] and rows[-1] <= bottom for top, bottom, _distance in scrolls)


def source_rgba(image: GraphicsImage, source: Source | None) -> tuple[bytes, int, int] | None:
    "The RGBA pixels a placement shows, and their size."
    pixels = sixel.to_rgba(image.format, image.width, image.height, image.data)
    if pixels is None:
        return None
    if source is None:
        return pixels, image.width, image.height
    pixels = _crop_rgba(pixels, image.width, image.height, source)
    if pixels is None:
        return None
    return pixels, source[2], source[3]


def drawn_size(image: GraphicsImage, source: Source | None) -> tuple[int, int]:
    "The pixels a placement shows: the image, or the crop out of it."
    if source is not None:
        return (source[2], source[3])
    return (image.width, image.height)


def _crop_rgba(pixels: bytes, width: int, height: int, source: tuple[int, int, int, int]) -> bytes | None:
    "Cut the source rectangle out of RGBA pixels."
    left, top, crop_width, crop_height = source
    if left < 0 or top < 0 or crop_width <= 0 or crop_height <= 0:
        return None
    if left + crop_width > width or top + crop_height > height:
        return None

    out = bytearray()
    for row in range(top, top + crop_height):
        start = (row * width + left) * 4
        out += pixels[start : start + crop_width * 4]
    return bytes(out)


def pane_placements(view: PaneView, cell_width: int, cell_height: int) -> Iterator[Placed]:
    """
    Everything of one pane that the client should draw: the plain
    placements, and the images that the unicode placeholders in the
    text of the pane point at.
    """
    yield from visible_placements(view)
    yield from placeholder_placements(view, cell_width, cell_height)


def _uncovered(placed: Placed, covered: Sequence[Rect]) -> Iterator[Placed]:
    """
    The parts of one placement that nothing in `covered` hides, each
    with the pixels of the image it shows: the placement itself when
    nothing does, nothing when they hide it whole. Lillecarl/pymux#816.

    **Cut, not lowered.** A window pane's picture goes under every cell
    with a background of its own (`WINDOW_Z`), so a float covers it with
    no cut. A program's image cannot: it belongs over its pane's own
    coloured cells.

    The cells map onto the pixels evenly, the way a crop at the edge of
    a pane does.
    """
    placement, image, columns, rows, x, y, source = placed
    whole = Rect(x, y, columns, rows)
    pieces = whole.minus(covered)
    if pieces == [whole]:
        yield placed
        return
    left, top, width, height = source if source is not None else (0, 0, image.width, image.height)
    for piece in pieces:
        start_x = left + (piece.x - x) * width // columns
        start_y = top + (piece.y - y) * height // rows
        end_x = left + (piece.right - x) * width // columns
        end_y = top + (piece.bottom - y) * height // rows
        cut = (start_x, start_y, max(1, end_x - start_x), max(1, end_y - start_y))
        yield placement, image, piece.width, piece.height, piece.x, piece.y, cut


def placeholder_placements(view: PaneView, cell_width: int, cell_height: int) -> Iterator[Placed]:
    """
    Yield the images that the unicode placeholders of one pane
    point at.

    A placeholder cell says which cell of which image it stands
    for. `Screen.placeholder_runs` gathers the neighbouring
    cells into rectangles; each rectangle becomes one placement
    with the matching piece of the image.
    """
    screen = view.screen
    if screen is None:
        return

    first_row = view.vertical_scroll
    last_row = first_row + view.height - 1
    for run in screen.placeholder_runs(first_row, last_row):
        placement = view.graphics.virtual_placement(run.image_id, run.placement_id)
        if placement is None or not placement.columns or not placement.rows:
            continue
        image = view.graphics.images_by_id.get(run.image_id)
        if image is None or not image.width or not image.height:
            continue

        x = run.column - view.horizontal_scroll
        y = run.row - first_row

        crop_left = max(0, -x)
        columns = min(run.columns - crop_left, view.width - x - crop_left)
        rows = min(run.rows, view.height - y)
        if columns <= 0 or rows <= 0:
            continue  # Outside the pane.

        source = _placeholder_source(
            image,
            placement,
            run.image_column + crop_left,
            run.image_row,
            columns,
            rows,
            cell_width,
            cell_height,
        )
        if source is None:
            continue  # The empty border around a fitted image.

        placed = (placement, image, columns, rows, view.x + x + crop_left, view.y + y, source)
        if view.covered:
            yield from _uncovered(placed, view.covered)
        else:
            yield placed


def visible_placements(view: PaneView) -> Iterator[Placed]:
    """
    Yield the placements of one pane that the client can see, with
    their position on the screen and their cropping.

    The pane stores rows of its scroll buffer, so the scroll of the
    pane turns them into screen rows. A placement that hangs over
    the edge of the pane is cropped instead of dropped: kitty takes
    a source rectangle in pixels on the put command.
    """
    width = view.width
    height = view.height

    for placement in view.graphics.placements:
        if placement.virtual:
            continue  # Unicode placeholders: the text carries them.
        image = view.graphics.images_by_id.get(placement.image_id)
        if image is None or not placement.columns or not placement.rows:
            continue

        # Position in the window, before cropping.
        x = placement.x - view.horizontal_scroll
        y = placement.y - view.vertical_scroll

        crop_left = max(0, -x)
        crop_top = max(0, -y)
        columns = min(placement.columns - crop_left, width - x - crop_left)
        rows = min(placement.rows - crop_top, height - y - crop_top)
        if columns <= 0 or rows <= 0:
            continue  # Fully outside the pane.

        piece = placement.crop
        if piece is not None or crop_left or crop_top or columns != placement.columns or rows != placement.rows:
            # Crop in pixels: the cell box of the placement maps
            # onto the pixels of the image. A piece that text cut
            # out of a sixel image maps through the whole box.
            piece_column, piece_row, box_columns, box_rows = piece or (0, 0, placement.columns, placement.rows)
            px_per_column = image.width / box_columns
            px_per_row = image.height / box_rows
            source = (
                int((piece_column + crop_left) * px_per_column),
                int((piece_row + crop_top) * px_per_row),
                max(1, int(columns * px_per_column)),
                max(1, int(rows * px_per_row)),
            )
        else:
            source = None

        placed = (placement, image, columns, rows, view.x + x + crop_left, view.y + y + crop_top, source)
        # Not through `_uncovered` when nothing covers the pane: a frame
        # pays for a generator per placement, which `checks.pymux-images`
        # counts.
        if view.covered:
            yield from _uncovered(placed, view.covered)
        else:
            yield placed


def _placeholder_source(
    image: GraphicsImage,
    placement: GraphicsPlacement,
    image_column: int,
    image_row: int,
    columns: int,
    rows: int,
    cell_width: int,
    cell_height: int,
) -> Source | None:
    """
    The rectangle of `image`, in pixels, that a run of placeholder
    cells shows. None when the run falls outside the image.

    kitty fits the image into the box that the placement covers,
    keeping the proportions and centring what is left over. A cell of
    the box therefore does not map onto a fixed piece of the image, and
    a cell along the border may show none of it.

    **The cell is the client's own.** The box is pixels on the screen
    of this client, so which part of the image a cell of it shows
    depends on the shape of that cell. A ten by twenty cell and a ten
    by nineteen one fit the same image differently.
    """
    box_width = placement.columns * cell_width
    box_height = placement.rows * cell_height

    if image.width * box_height > image.height * box_width:
        # The image fills the box sideways. What is left over is a
        # border above and below it.
        scale = box_width / image.width
        x_offset = 0.0
        y_offset = (box_height - image.height * scale) / 2
    else:
        scale = box_height / image.height
        y_offset = 0.0
        x_offset = (box_width - image.width * scale) / 2

    left = (image_column * cell_width - x_offset) / scale
    top = (image_row * cell_height - y_offset) / scale
    right = left + columns * cell_width / scale
    bottom = top + rows * cell_height / scale

    # Cut away what falls outside the image. A run along the border
    # keeps all of its cells, so the piece that is left stretches over
    # them. That is at most one cell of error, on the first row and the
    # last one.
    left, top = max(0.0, left), max(0.0, top)
    right = min(float(image.width), right)
    bottom = min(float(image.height), bottom)
    if right - left < 1 or bottom - top < 1:
        return None

    return (
        int(left),
        int(top),
        max(1, int(right - left)),
        max(1, int(bottom - top)),
    )


def put_command(
    outer_id: int,
    slot: int,
    x: int,
    y: int,
    columns: int,
    rows: int,
    z: int,
    source: tuple[int, int, int, int] | None,
    natural: bool = False,
) -> str:
    """
    The escape sequences that put one image at (`x`, `y`) on the outer
    terminal. The cursor moves there first; `C=1` keeps the image from
    moving it again.

    **`natural` leaves `c` and `r` out, and that is what stops the
    terminal resampling.** `c` and `r` are a box to fit the image into,
    so sending them always scales unless the box is the image to the
    pixel. With neither, kitty draws the image at its own size and
    works the covered cells out itself -- `ceil(width / cell)`, which
    is at most the `columns` and `rows` the pane reserved whenever the
    image fits the box. So nothing below the image is overdrawn.
    `draws_at_its_own_size` is where that is decided, and it also
    refuses a box the program asked for. Lillecarl/pymux#369.
    """
    parts = [
        "a=p",
        "i=%i" % outer_id,
        "p=%i" % slot,
    ]
    if not natural:
        parts.append("c=%i" % columns)
        parts.append("r=%i" % rows)
    parts += [
        "C=1",
        "q=2",
    ]
    if z:
        parts.append("z=%i" % z)
    if source is not None:
        parts.append("x=%i,y=%i,w=%i,h=%i" % source)

    return "\x1b[%i;%iH\x1b_G%s\x1b\\" % (y + 1, x + 1, ",".join(parts))
